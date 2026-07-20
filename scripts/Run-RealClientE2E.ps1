[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{40}$')]
    [string]$CandidateSha,

    [Parameter(Mandatory = $true)]
    [string]$DebugBuild,

    [Parameter(Mandatory = $true)]
    [ValidateScript({ $_ -ceq 'codexhub-openai/gpt-5.6-luna' })]
    [string]$LunaModel,

    [Parameter(Mandatory = $true)]
    [ValidateScript({ $_ -ceq 'codexhub-volc/glm-5.2' })]
    [string]$VolcModel,

    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory,

    [string]$CodexDesktopPath = 'Codex.exe',
    [string]$CodexCliPath = 'codex.exe',
    [string]$ZCodePath = 'zcode.exe',
    [string]$OpenCodePath = 'opencode.exe',
    [string]$PiPath = 'pi.exe',
    [string]$OmpPath = 'omp.exe',

    [ValidateRange(1, 900)]
    [int]$TimeoutSeconds = 180
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:PinnedVersions = [ordered]@{
    desktop = '26.715.4045.0'
    codex_cli = '0.144.5'
    zcode = '3.3.6'
    opencode = '1.18.3'
    pi = '0.80.6'
    omp = '17.0.3'
}
$script:MaximumCapturedCharacters = 65536
$script:Utf8NoBom = [System.Text.UTF8Encoding]::new($false)

function ConvertTo-ProcessArgument {
    param([AllowNull()][string]$Argument)
    if ($null -eq $Argument -or $Argument.Length -eq 0) {
        return '""'
    }
    if ($Argument -notmatch '[\s"]') {
        return $Argument
    }
    $escaped = $Argument -replace '(\\*)"', '$1$1\"'
    $escaped = $escaped -replace '(\\+)$', '$1$1'
    return '"' + $escaped + '"'
}

function Resolve-CommandPath {
    param([string]$Path, [string]$Name)
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        return (Resolve-Path -LiteralPath $Path).Path
    }
    $command = Get-Command $Path -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $command) {
        throw "preflight_${Name}_executable_missing"
    }
    return [string]$command.Source
}

function Get-Sha256 {
    param([string]$Path)
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $bytes = [System.Security.Cryptography.SHA256]::Create().ComputeHash($stream)
        return 'sha256:' + ([System.BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant())
    }
    finally {
        $stream.Dispose()
    }
}

function Get-TextSha256 {
    param([string]$Text)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
    $digest = [System.Security.Cryptography.SHA256]::Create().ComputeHash($bytes)
    return 'sha256:' + ([System.BitConverter]::ToString($digest).Replace('-', '').ToLowerInvariant())
}

function Write-JsonFile {
    param([string]$Path, [object]$Value)
    $json = $Value | ConvertTo-Json -Depth 20
    [System.IO.File]::WriteAllText($Path, $json + "`n", $script:Utf8NoBom)
}

function Get-JsonProperty {
    param([object]$Value, [string]$Name, [object]$Default = $null)
    if ($null -eq $Value) {
        return $Default
    }
    $property = $Value.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $Default
    }
    return $property.Value
}

function Stop-ProcessTree {
    param([int]$ProcessId)
    $children = @()
    try {
        $searcher = [System.Management.ManagementObjectSearcher]::new(
            "SELECT ProcessId FROM Win32_Process WHERE ParentProcessId = $ProcessId"
        )
        $children = @($searcher.Get())
        $searcher.Dispose()
    }
    catch {
        $children = @()
    }
    foreach ($child in $children) {
        Stop-ProcessTree -ProcessId ([int]$child.ProcessId)
    }
    try {
        $process = [System.Diagnostics.Process]::GetProcessById($ProcessId)
        $process.Kill()
        [void]$process.WaitForExit(5000)
    }
    catch {
        # The process may have exited between discovery and cleanup.
    }
}

function New-IsolatedStartInfo {
    param(
        [string]$Executable,
        [string[]]$Arguments,
        [string]$CaseRoot,
        [hashtable]$ExtraEnvironment
    )
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $processArguments = [System.Collections.Generic.List[string]]::new()
    $extension = [System.IO.Path]::GetExtension($Executable)
    if ($extension -iin @('.cmd', '.bat')) {
        $startInfo.FileName = if ($env:ComSpec) { $env:ComSpec } else { 'cmd.exe' }
        $commandLine = @(
            (ConvertTo-ProcessArgument $Executable)
            ($Arguments | ForEach-Object { ConvertTo-ProcessArgument $_ })
        ) -join ' '
        $startInfo.Arguments = "/d /s /c $(ConvertTo-ProcessArgument $commandLine)"
    }
    elseif ($extension -ieq '.ps1') {
        $startInfo.FileName = (Get-Command powershell.exe -ErrorAction Stop).Source
        foreach ($argument in @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $Executable)) {
            [void]$processArguments.Add($argument)
        }
    }
    else {
        $startInfo.FileName = $Executable
    }
    if ($extension -notin @('.cmd', '.bat')) {
        foreach ($argument in $Arguments) {
            [void]$processArguments.Add([string]$argument)
        }
        $startInfo.Arguments = ($processArguments | ForEach-Object { ConvertTo-ProcessArgument $_ }) -join ' '
    }
    $startInfo.WorkingDirectory = $CaseRoot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.EnvironmentVariables.Clear()
    foreach ($name in @('SystemRoot', 'WINDIR', 'ComSpec', 'PATH', 'PATHEXT')) {
        $value = [System.Environment]::GetEnvironmentVariable($name)
        if ($value) {
            $startInfo.EnvironmentVariables[$name] = $value
        }
    }
    $startInfo.EnvironmentVariables['HOME'] = $CaseRoot
    $startInfo.EnvironmentVariables['USERPROFILE'] = $CaseRoot
    $startInfo.EnvironmentVariables['APPDATA'] = (Join-Path $CaseRoot 'appdata\roaming')
    $startInfo.EnvironmentVariables['LOCALAPPDATA'] = (Join-Path $CaseRoot 'appdata\local')
    $startInfo.EnvironmentVariables['CODEX_HOME'] = (Join-Path $CaseRoot '.codex')
    $startInfo.EnvironmentVariables['XDG_CONFIG_HOME'] = (Join-Path $CaseRoot '.config')
    $startInfo.EnvironmentVariables['TEMP'] = (Join-Path $CaseRoot 'temp')
    $startInfo.EnvironmentVariables['TMP'] = (Join-Path $CaseRoot 'temp')
    foreach ($entry in $ExtraEnvironment.GetEnumerator()) {
        $startInfo.EnvironmentVariables[[string]$entry.Key] = [string]$entry.Value
    }
    return $startInfo
}

function Invoke-IsolatedProcess {
    param(
        [string]$Executable,
        [string[]]$Arguments,
        [string]$CaseRoot,
        [hashtable]$Environment,
        [string]$StandardInput,
        [int]$ProcessTimeoutSeconds
    )
    foreach ($relative in @('.codex', '.config', 'appdata\roaming', 'appdata\local', 'temp')) {
        [void](New-Item -ItemType Directory -Force -Path (Join-Path $CaseRoot $relative))
    }
    $startInfo = New-IsolatedStartInfo -Executable $Executable -Arguments $Arguments -CaseRoot $CaseRoot -ExtraEnvironment $Environment
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    $startedAt = [System.Diagnostics.Stopwatch]::StartNew()
    [void]$process.Start()
    if ($StandardInput) {
        $process.StandardInput.Write($StandardInput)
    }
    $process.StandardInput.Close()
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $completed = $process.WaitForExit($ProcessTimeoutSeconds * 1000)
    if (-not $completed) {
        Stop-ProcessTree -ProcessId $process.Id
        [void]$process.WaitForExit(5000)
    }
    $stdout = $stdoutTask.GetAwaiter().GetResult()
    $stderr = $stderrTask.GetAwaiter().GetResult()
    $startedAt.Stop()
    $exitCode = if ($completed) { $process.ExitCode } else { -1 }
    if ($stdout.Length -gt $script:MaximumCapturedCharacters) {
        $stdout = $stdout.Substring(0, $script:MaximumCapturedCharacters)
    }
    if ($stderr.Length -gt $script:MaximumCapturedCharacters) {
        $stderr = $stderr.Substring(0, $script:MaximumCapturedCharacters)
    }
    return [pscustomobject]@{
        timed_out = -not $completed
        exit_code = $exitCode
        duration_ms = [Math]::Min([int]$startedAt.ElapsedMilliseconds, $ProcessTimeoutSeconds * 1000)
        stdout = $stdout
        stderr = $stderr
    }
}

function Start-IsolatedProcess {
    param(
        [string]$Executable,
        [string[]]$Arguments,
        [string]$CaseRoot,
        [hashtable]$Environment
    )
    foreach ($relative in @('.codex', '.config', 'appdata\roaming', 'appdata\local', 'temp')) {
        [void](New-Item -ItemType Directory -Force -Path (Join-Path $CaseRoot $relative))
    }
    $startInfo = New-IsolatedStartInfo -Executable $Executable -Arguments $Arguments -CaseRoot $CaseRoot -ExtraEnvironment $Environment
    $startInfo.RedirectStandardInput = $false
    $startInfo.RedirectStandardOutput = $false
    $startInfo.RedirectStandardError = $false
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    [void]$process.Start()
    return $process
}

function Get-ClientArguments {
    param([string]$Client, [string]$Model, [string]$WorkRoot, [string]$Prompt)
    switch ($Client) {
        'codex-cli' { return @('exec', '--ephemeral', '--json', '-C', $WorkRoot, '-m', $Model, '-s', 'read-only', '-a', 'never', $Prompt) }
        'opencode' { return @('run', '--format', 'json', '--model', $Model, $Prompt) }
        'pi' { return @('--print', '--mode', 'json', '--model', $Model, '--no-session', $Prompt) }
        'omp' { return @('run', '--format', 'json', '--model', $Model, $Prompt) }
        default { return @() }
    }
}

function ConvertFrom-NormalizedEvents {
    param([string]$Text)
    $events = [System.Collections.Generic.List[object]]::new()
    $malformedCount = 0
    foreach ($line in ($Text -split "`r?`n")) {
        if (-not $line.Trim()) {
            continue
        }
        try {
            $event = $line | ConvertFrom-Json -ErrorAction Stop
            if ($null -ne (Get-JsonProperty -Value $event -Name 'event')) {
                [void]$events.Add($event)
            }
            else {
                $malformedCount++
            }
        }
        catch {
            $malformedCount++
        }
    }
    return [pscustomobject]@{ events = @($events); malformed_count = $malformedCount }
}

function Invoke-ClientAttempt {
    param([pscustomobject]$Case, [string]$Executable, [string]$CaseRoot, [int]$Attempt)
    $sentinel = "SENTINEL:codexhub-real-client-e2e:$($Case.case_id)"
    $sentinelPath = Join-Path $CaseRoot 'sentinel.txt'
    [System.IO.File]::WriteAllText($sentinelPath, $sentinel, $script:Utf8NoBom)
    $prompt = "Read the sentinel file once with one read-only tool call, then stream exactly $sentinel and stop."
    $arguments = @(Get-ClientArguments -Client $Case.client -Model $Case.canonical_model -WorkRoot $CaseRoot -Prompt $prompt)
    $environment = @{
        CODEXHUB_E2E_CASE = $Case.case_id
        CODEXHUB_E2E_MODEL = $Case.canonical_model
        CODEXHUB_E2E_SENTINEL = $sentinel
        CODEXHUB_E2E_SENTINEL_PATH = $sentinelPath
        CODEXHUB_E2E_ATTEMPT = [string]$Attempt
    }
    $processResult = Invoke-IsolatedProcess -Executable $Executable -Arguments $arguments -CaseRoot $CaseRoot -Environment $environment -StandardInput $prompt -ProcessTimeoutSeconds $TimeoutSeconds
    Remove-Item -LiteralPath $sentinelPath -Force -ErrorAction SilentlyContinue
    $parsed = ConvertFrom-NormalizedEvents -Text $processResult.stdout
    return [pscustomobject]@{
        process = $processResult
        events = @($parsed.events)
        malformed_count = $parsed.malformed_count
    }
}

function Measure-AutomatedAttempt {
    param([pscustomobject]$Attempt, [pscustomobject]$Case)
    $events = @($Attempt.events)
    $modelEvents = @($events | Where-Object { (Get-JsonProperty $_ 'event') -eq 'model_selected' })
    $allToolEvents = @($events | Where-Object { (Get-JsonProperty $_ 'event') -eq 'tool_call' })
    $toolEvents = @($allToolEvents | Where-Object { (Get-JsonProperty $_ 'read_only' $false) -eq $true -and (Get-JsonProperty $_ 'tool') -ceq 'read_file' })
    $sentinel = "SENTINEL:codexhub-real-client-e2e:$($Case.case_id)"
    $streamEvents = @($events | Where-Object { (Get-JsonProperty $_ 'event') -eq 'stream_delta' })
    $sentinelEvents = @($streamEvents | Where-Object { (Get-JsonProperty $_ 'text') -ceq $sentinel })
    $requestEvents = @($events | Where-Object { (Get-JsonProperty $_ 'event') -eq 'request_complete' })
    $terminalEvents = @($events | Where-Object { (Get-JsonProperty $_ 'event') -eq 'terminal' })
    $fallbackEvents = @($events | Where-Object { (Get-JsonProperty $_ 'event') -eq 'fallback' })
    $reconnectEvents = @($events | Where-Object { (Get-JsonProperty $_ 'event') -eq 'reconnect' })
    $capacityEvents = @($events | Where-Object { (Get-JsonProperty $_ 'event') -eq 'provider_capacity' })
    $errorEvents = @($events | Where-Object { (Get-JsonProperty $_ 'event') -eq 'error' })
    $httpStatus = if ($requestEvents.Count -eq 1) { [int](Get-JsonProperty $requestEvents[0] 'status' 0) } else { 0 }
    $terminalClassification = if ($Attempt.process.timed_out) {
        'timeout'
    }
    elseif ($Attempt.process.exit_code -ne 0) {
        'nonzero_exit'
    }
    elseif ($terminalEvents.Count -eq 1) {
        [string](Get-JsonProperty $terminalEvents[0] 'classification' 'unclassified')
    }
    else {
        'unclassified'
    }
    $reconnectClassification = if ($reconnectEvents.Count -eq 0) {
        'none'
    }
    elseif ($reconnectEvents.Count -eq 1) {
        [string](Get-JsonProperty $reconnectEvents[0] 'classification' 'unclassified')
    }
    else {
        'unclassified'
    }
    $capacityStatus = if ($capacityEvents.Count -eq 1) { [int](Get-JsonProperty $capacityEvents[0] 'status' 0) } else { 0 }
    $capacityOutputSeen = if ($capacityEvents.Count -eq 1) { [bool](Get-JsonProperty $capacityEvents[0] 'output_seen' $true) } else { $true }
    $retryableCapacity = $Attempt.process.exit_code -ne 0 -and -not $Attempt.process.timed_out -and $capacityEvents.Count -eq 1 -and $capacityStatus -in @(429, 503) -and -not $capacityOutputSeen -and $streamEvents.Count -eq 0
    $passed = -not $Attempt.process.timed_out -and
        $Attempt.process.exit_code -eq 0 -and
        $Attempt.malformed_count -eq 0 -and
        $modelEvents.Count -eq 1 -and
        (Get-JsonProperty $modelEvents[0] 'model') -ceq $Case.canonical_model -and
        $allToolEvents.Count -eq 1 -and $toolEvents.Count -eq 1 -and
        $streamEvents.Count -eq 1 -and $sentinelEvents.Count -eq 1 -and
        $requestEvents.Count -eq 1 -and
        $httpStatus -eq 200 -and
        $terminalEvents.Count -eq 1 -and
        $terminalClassification -ceq 'completed' -and
        $errorEvents.Count -eq 0 -and
        $fallbackEvents.Count -eq 0 -and
        $reconnectClassification -cne 'unclassified'
    return [pscustomobject]@{
        passed = $passed
        retryable_capacity = $retryableCapacity
        capacity_status = $capacityStatus
        terminal_classification = $terminalClassification
        reconnect_classification = $reconnectClassification
        request_complete_count = $requestEvents.Count
        http_status = $httpStatus
        read_only_tool_call_count = $toolEvents.Count
        sentinel_chunk_count = $sentinelEvents.Count
        fallback_count = $fallbackEvents.Count
        error_event_count = $errorEvents.Count
        duplicate_terminal_count = [Math]::Max(0, $terminalEvents.Count - 1)
    }
}

function Invoke-AutomatedCase {
    param([pscustomobject]$Case, [string]$Executable, [string]$ArtifactRoot, [string]$WorkRoot)
    $caseWorkRoot = Join-Path $WorkRoot $Case.case_id
    [void](New-Item -ItemType Directory -Force -Path $caseWorkRoot)
    $attempt = Invoke-ClientAttempt -Case $Case -Executable $Executable -CaseRoot $caseWorkRoot -Attempt 1
    $measurement = Measure-AutomatedAttempt -Attempt $attempt -Case $Case
    $retryClassification = 'not_needed'
    $duration = $attempt.process.duration_ms
    if (-not $measurement.passed -and $measurement.retryable_capacity) {
        $retryClassification = "capacity_$($measurement.capacity_status)_pre_output_retried"
        $attempt = Invoke-ClientAttempt -Case $Case -Executable $Executable -CaseRoot $caseWorkRoot -Attempt 2
        $measurement = Measure-AutomatedAttempt -Attempt $attempt -Case $Case
        $duration = [Math]::Min($TimeoutSeconds * 2000, $duration + $attempt.process.duration_ms)
    }
    elseif (-not $measurement.passed) {
        $retryClassification = 'not_eligible'
    }
    $artifactRelative = "cases/$($Case.case_id).json"
    $artifactPath = Join-Path $ArtifactRoot ($artifactRelative -replace '/', '\')
    $artifact = [ordered]@{
        case_id = $Case.case_id
        candidate_sha = $CandidateSha
        canonical_model = $Case.canonical_model
        outcome = if ($measurement.passed) { 'passed' } else { 'failed' }
        stdout_sha256 = Get-TextSha256 -Text $attempt.process.stdout
        stderr_sha256 = Get-TextSha256 -Text $attempt.process.stderr
    }
    Write-JsonFile -Path $artifactPath -Value $artifact
    return [ordered]@{
        case_id = $Case.case_id
        canonical_model = $Case.canonical_model
        outcome = $artifact.outcome
        duration_ms = $duration
        request_complete_count = $measurement.request_complete_count
        http_status = $measurement.http_status
        read_only_tool_call_count = $measurement.read_only_tool_call_count
        sentinel_chunk_count = $measurement.sentinel_chunk_count
        fallback_count = $measurement.fallback_count
        error_event_count = $measurement.error_event_count
        duplicate_terminal_count = $measurement.duplicate_terminal_count
        terminal_classification = $measurement.terminal_classification
        reconnect_classification = $measurement.reconnect_classification
        retry_classification = $retryClassification
        artifact = "artifacts/$artifactRelative"
    }
}

function Get-ManualResults {
    param([string]$EvidencePath, [object[]]$ManualCases, [string]$ArtifactRoot)
    try {
        $evidence = Get-Content -LiteralPath $EvidencePath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw 'manual_evidence_malformed'
    }
    $topLevelNames = @($evidence.PSObject.Properties.Name | Sort-Object)
    if (($topLevelNames -join ',') -cne 'candidate_sha,cases,schema') {
        throw 'manual_evidence_schema_invalid'
    }
    if ((Get-JsonProperty $evidence 'schema') -cne 'codexhub.real-client-manual-evidence.v1') {
        throw 'manual_evidence_schema_invalid'
    }
    if ((Get-JsonProperty $evidence 'candidate_sha') -cne $CandidateSha) {
        throw 'manual_evidence_candidate_sha_stale'
    }
    $providedCases = @(Get-JsonProperty $evidence 'cases' @())
    if ($providedCases.Count -ne $ManualCases.Count) {
        throw 'manual_evidence_case_count_invalid'
    }
    $results = [System.Collections.Generic.List[object]]::new()
    foreach ($expected in $ManualCases) {
        $matches = @($providedCases | Where-Object { (Get-JsonProperty $_ 'case_id') -ceq $expected.case_id })
        if ($matches.Count -ne 1) {
            throw 'manual_evidence_case_missing_or_duplicate'
        }
        $item = $matches[0]
        $expectedNames = @(
            'canonical_model', 'case_id', 'client', 'duplicate_terminal_count',
            'fallback_count', 'http_status', 'human_finalized', 'outcome',
            'read_only_tool_call_count', 'reconnect_classification',
            'request_complete_count', 'sentinel_chunk_count',
            'terminal_classification'
        ) | Sort-Object
        $itemNames = @($item.PSObject.Properties.Name | Sort-Object)
        if (($expectedNames -join ',') -cne ($itemNames -join ',')) {
            throw 'manual_evidence_schema_invalid'
        }
        $valid = (Get-JsonProperty $item 'client') -ceq $expected.client -and
            (Get-JsonProperty $item 'canonical_model') -ceq $expected.canonical_model -and
            (Get-JsonProperty $item 'human_finalized' $false) -eq $true -and
            (Get-JsonProperty $item 'outcome') -ceq 'passed' -and
            (Get-JsonProperty $item 'terminal_classification') -ceq 'completed' -and
            (Get-JsonProperty $item 'reconnect_classification') -ceq 'none' -and
            [int](Get-JsonProperty $item 'request_complete_count' 0) -eq 1 -and
            [int](Get-JsonProperty $item 'http_status' 0) -eq 200 -and
            [int](Get-JsonProperty $item 'read_only_tool_call_count' 0) -eq 1 -and
            [int](Get-JsonProperty $item 'sentinel_chunk_count' 0) -eq 1 -and
            [int](Get-JsonProperty $item 'fallback_count' 1) -eq 0 -and
            [int](Get-JsonProperty $item 'duplicate_terminal_count' 1) -eq 0
        if (-not $valid) {
            throw 'manual_evidence_contradictory'
        }
        $artifactRelative = "cases/$($expected.case_id).json"
        $artifactPath = Join-Path $ArtifactRoot ($artifactRelative -replace '/', '\')
        Write-JsonFile -Path $artifactPath -Value ([ordered]@{
            case_id = $expected.case_id
            candidate_sha = $CandidateSha
            canonical_model = $expected.canonical_model
            outcome = 'passed'
            evidence_sha256 = Get-Sha256 -Path $EvidencePath
        })
        [void]$results.Add([ordered]@{
            case_id = $expected.case_id
            canonical_model = $expected.canonical_model
            outcome = 'passed'
            duration_ms = 0
            request_complete_count = 1
            http_status = 200
            read_only_tool_call_count = 1
            sentinel_chunk_count = 1
            fallback_count = 0
            error_event_count = 0
            duplicate_terminal_count = 0
            terminal_classification = 'completed'
            reconnect_classification = 'none'
            retry_classification = 'manual_not_applicable'
            artifact = "artifacts/$artifactRelative"
        })
    }
    return @($results)
}

$CandidateSha = $CandidateSha.ToLowerInvariant()
if (-not (Test-Path -LiteralPath $OutputDirectory -PathType Container)) {
    throw 'preflight_output_directory_missing'
}
$OutputDirectory = (Resolve-Path -LiteralPath $OutputDirectory).Path
$isolationRoot = Join-Path $OutputDirectory 'isolated'
$accountPath = Join-Path $isolationRoot 'account\profile.json'
$credentialPath = Join-Path $isolationRoot 'credentials\volc.json'
$configRoot = Join-Path $isolationRoot 'config'
$workRoot = Join-Path $isolationRoot 'work'
$versionManifestPath = Join-Path $configRoot 'client-versions.json'
$manualEvidencePath = Join-Path $OutputDirectory 'manual-evidence.json'
$summaryPath = Join-Path $OutputDirectory 'summary.json'
$artifactRoot = Join-Path $OutputDirectory 'artifacts'

foreach ($directory in @($isolationRoot, $configRoot, $workRoot)) {
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        throw 'preflight_isolated_directory_missing'
    }
}
foreach ($file in @($DebugBuild, $accountPath, $credentialPath, $versionManifestPath, $manualEvidencePath)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        throw 'preflight_required_file_missing'
    }
}
$DebugBuild = (Resolve-Path -LiteralPath $DebugBuild).Path
$shaSidecar = "$DebugBuild.candidate-sha"
if (-not (Test-Path -LiteralPath $shaSidecar -PathType Leaf)) {
    throw 'preflight_debug_build_sha_sidecar_missing'
}
if ((Get-Content -LiteralPath $shaSidecar -Raw).Trim() -cne $CandidateSha) {
    throw 'preflight_debug_build_sha_mismatch'
}
try {
    $installedVersions = Get-Content -LiteralPath $versionManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
}
catch {
    throw 'preflight_client_versions_malformed'
}
$expectedVersionNames = @($script:PinnedVersions.Keys | Sort-Object)
$installedVersionNames = @($installedVersions.PSObject.Properties.Name | Sort-Object)
if (($expectedVersionNames -join ',') -cne ($installedVersionNames -join ',')) {
    throw 'preflight_client_versions_invalid'
}
foreach ($name in $expectedVersionNames) {
    if ([string]$installedVersions.$name -cne [string]$script:PinnedVersions[$name]) {
        throw 'preflight_client_versions_invalid'
    }
}
if (Test-Path -LiteralPath $summaryPath) {
    throw 'preflight_output_already_contains_summary'
}

$executables = @{
    desktop = Resolve-CommandPath -Path $CodexDesktopPath -Name 'desktop'
    'codex-cli' = Resolve-CommandPath -Path $CodexCliPath -Name 'codex_cli'
    zcode = Resolve-CommandPath -Path $ZCodePath -Name 'zcode'
    opencode = Resolve-CommandPath -Path $OpenCodePath -Name 'opencode'
    pi = Resolve-CommandPath -Path $PiPath -Name 'pi'
    omp = Resolve-CommandPath -Path $OmpPath -Name 'omp'
}
[void](New-Item -ItemType Directory -Path $artifactRoot)
[void](New-Item -ItemType Directory -Path (Join-Path $artifactRoot 'cases'))

$manualCases = @(
    [pscustomobject]@{ case_id = 'desktop-luna'; client = 'desktop'; canonical_model = 'gpt-5.6-luna' },
    [pscustomobject]@{ case_id = 'desktop-volc'; client = 'desktop'; canonical_model = 'volc/glm-5.2' },
    [pscustomobject]@{ case_id = 'zcode-luna'; client = 'zcode'; canonical_model = $LunaModel },
    [pscustomobject]@{ case_id = 'zcode-volc'; client = 'zcode'; canonical_model = $VolcModel }
)
$automatedCases = @(
    [pscustomobject]@{ case_id = 'codex-cli-luna'; client = 'codex-cli'; canonical_model = 'gpt-5.6-luna' },
    [pscustomobject]@{ case_id = 'codex-cli-volc'; client = 'codex-cli'; canonical_model = 'volc/glm-5.2' },
    [pscustomobject]@{ case_id = 'opencode-luna'; client = 'opencode'; canonical_model = $LunaModel },
    [pscustomobject]@{ case_id = 'opencode-volc'; client = 'opencode'; canonical_model = $VolcModel },
    [pscustomobject]@{ case_id = 'pi-luna'; client = 'pi'; canonical_model = $LunaModel },
    [pscustomobject]@{ case_id = 'pi-volc'; client = 'pi'; canonical_model = $VolcModel },
    [pscustomobject]@{ case_id = 'omp-luna'; client = 'omp'; canonical_model = $LunaModel },
    [pscustomobject]@{ case_id = 'omp-volc'; client = 'omp'; canonical_model = $VolcModel }
)

$manualResults = @(Get-ManualResults -EvidencePath $manualEvidencePath -ManualCases $manualCases -ArtifactRoot $artifactRoot)
$trackedProcesses = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
try {
    $candidateRoot = Join-Path $workRoot 'candidate'
    [void](New-Item -ItemType Directory -Force -Path $candidateRoot)
    $candidateProcess = Start-IsolatedProcess -Executable $DebugBuild -Arguments @() -CaseRoot $candidateRoot -Environment @{
        CODEXHUB_E2E_CANDIDATE_SHA = $CandidateSha
        CODEXHUB_E2E_CONFIG_ROOT = $configRoot
        CODEXHUB_E2E_ACCOUNT_PATH = $accountPath
        CODEXHUB_E2E_VOLC_CREDENTIAL_PATH = $credentialPath
    }
    [void]$trackedProcesses.Add($candidateProcess)
    if ([System.IO.Path]::GetExtension($DebugBuild) -notin @('.ps1', '.cmd', '.bat')) {
        Start-Sleep -Seconds 5
        if ($candidateProcess.HasExited) {
            throw 'candidate_debug_build_exited_during_startup'
        }
    }

    foreach ($guiClient in @('desktop', 'zcode')) {
        $guiCases = @($manualCases | Where-Object { $_.client -ceq $guiClient })
        $guiRoot = Join-Path $workRoot ("gui-" + $guiClient)
        [void](New-Item -ItemType Directory -Force -Path $guiRoot)
        $guiProcess = Start-IsolatedProcess -Executable $executables[$guiClient] -Arguments @() -CaseRoot $guiRoot -Environment @{
            CODEXHUB_E2E_CASES = ($guiCases.case_id -join ',')
            CODEXHUB_E2E_MODELS = ($guiCases.canonical_model -join ',')
        }
        [void]$trackedProcesses.Add($guiProcess)
    }

    $automatedById = @{}
    foreach ($case in $automatedCases) {
        $automatedById[$case.case_id] = Invoke-AutomatedCase -Case $case -Executable $executables[$case.client] -ArtifactRoot $artifactRoot -WorkRoot $workRoot
    }
    $manualById = @{}
    foreach ($result in $manualResults) {
        $manualById[$result.case_id] = $result
    }
    $caseOrder = @(
        'desktop-luna', 'desktop-volc',
        'codex-cli-luna', 'codex-cli-volc',
        'opencode-luna', 'opencode-volc',
        'zcode-luna', 'zcode-volc',
        'pi-luna', 'pi-volc',
        'omp-luna', 'omp-volc'
    )
    $caseResults = @($caseOrder | ForEach-Object {
        if ($manualById.ContainsKey($_)) { $manualById[$_] } else { $automatedById[$_] }
    })
    $passedCount = @($caseResults | Where-Object { $_.outcome -ceq 'passed' }).Count
    $summary = [ordered]@{
        schema = 'codexhub.real-client-e2e-summary.v1'
        candidate_sha = $CandidateSha
        outcome = if ($passedCount -eq 12) { 'passed' } else { 'failed' }
        hashes = [ordered]@{
            debug_build = Get-Sha256 -Path $DebugBuild
            account_profile = Get-Sha256 -Path $accountPath
            volc_credentials = Get-Sha256 -Path $credentialPath
            manual_evidence = Get-Sha256 -Path $manualEvidencePath
            client_versions = Get-Sha256 -Path $versionManifestPath
        }
        pinned_versions = $script:PinnedVersions
        canonical_models = @('gpt-5.6-luna', 'volc/glm-5.2', $LunaModel, $VolcModel)
        counts = [ordered]@{
            case_count = 12
            passed_count = $passedCount
            failed_count = 12 - $passedCount
            manual_case_count = 4
            automated_case_count = 8
        }
        cases = $caseResults
        artifacts = @($caseResults | ForEach-Object { $_.artifact })
    }
    Write-JsonFile -Path $summaryPath -Value $summary
    if ($passedCount -ne 12) {
        exit 1
    }
}
finally {
    foreach ($trackedProcess in $trackedProcesses) {
        if (-not $trackedProcess.HasExited) {
            Stop-ProcessTree -ProcessId $trackedProcess.Id
        }
    }
}
