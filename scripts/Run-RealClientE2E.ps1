[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$CandidateSha,

    [Parameter(Mandatory = $true)]
    [string]$DebugBuild,

    [Parameter(Mandatory = $true)]
    [string]$LunaModel,

    [Parameter(Mandatory = $true)]
    [string]$VolcModel,

    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory,

    [Parameter(Mandatory = $true)]
    [string]$SnapshotManifest,

    [string]$CodexDesktopPath = 'Codex.exe',
    [string]$CodexCliPath = 'codex.exe',
    [string]$ZCodePath = 'zcode.exe',
    [string]$OpenCodePath = 'opencode.exe',
    [string]$PiPath = 'pi.exe',
    [string]$OmpPath = 'omp.exe',

    [int]$TimeoutSeconds = 180,

    [int]$ManualEvidenceTimeoutSeconds = 900
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

function Read-JsonObject {
    param([string]$Path, [string]$Failure)
    try {
        $value = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw $Failure
    }
    if ($null -eq $value -or $value -is [System.Array]) {
        throw $Failure
    }
    return $value
}

function Assert-ExactJsonProperties {
    param([object]$Value, [string[]]$Names, [string]$Failure)
    $actual = @($Value.PSObject.Properties.Name | Sort-Object)
    $expected = @($Names | Sort-Object)
    if (($actual -join ',') -cne ($expected -join ',')) {
        throw $Failure
    }
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
        'omp' { return @('--print', '--mode', 'json', '--model', $Model, $Prompt) }
        default { return @() }
    }
}

function ConvertFrom-ClientEvents {
    param([string]$Client, [string]$Text)
    $events = [System.Collections.Generic.List[object]]::new()
    $malformedCount = 0
    foreach ($line in ($Text -split "`r?`n")) {
        if (-not $line.Trim()) {
            continue
        }
        try {
            $native = $line | ConvertFrom-Json -ErrorAction Stop
            $type = [string](Get-JsonProperty $native 'type' '')
            switch ($Client) {
                'codex-cli' {
                    if ($type -eq 'item.completed') {
                        $item = Get-JsonProperty $native 'item'
                        $itemType = [string](Get-JsonProperty $item 'type' '')
                        if ($itemType -eq 'command_execution') {
                            $command = [string](Get-JsonProperty $item 'command' '')
                            if ($command -match '(?i)(read|type|get-content|cat).+sentinel\.txt' -and
                                [string](Get-JsonProperty $item 'status' 'completed') -eq 'completed') {
                                [void]$events.Add([pscustomobject]@{ event = 'tool_call'; tool = 'read_file'; read_only = $true })
                            }
                        }
                        elseif ($itemType -eq 'agent_message') {
                            [void]$events.Add([pscustomobject]@{ event = 'stream_delta'; text = [string](Get-JsonProperty $item 'text' '') })
                        }
                    }
                    elseif ($type -eq 'turn.completed') {
                        [void]$events.Add([pscustomobject]@{ event = 'terminal'; classification = 'completed' })
                    }
                    elseif ($type -in @('error', 'turn.failed')) {
                        [void]$events.Add([pscustomobject]@{ event = 'error' })
                    }
                }
                'opencode' {
                    $part = Get-JsonProperty $native 'part'
                    if ($type -eq 'tool_use' -and [string](Get-JsonProperty $part 'tool' '') -eq 'read' -and
                        [string](Get-JsonProperty (Get-JsonProperty $part 'state') 'status' '') -eq 'completed') {
                        [void]$events.Add([pscustomobject]@{ event = 'tool_call'; tool = 'read_file'; read_only = $true })
                    }
                    elseif ($type -eq 'text') {
                        [void]$events.Add([pscustomobject]@{ event = 'stream_delta'; text = [string](Get-JsonProperty $part 'text' '') })
                    }
                    elseif ($type -eq 'step_finish' -and [string](Get-JsonProperty $part 'reason' '') -ne 'unknown') {
                        [void]$events.Add([pscustomobject]@{ event = 'terminal'; classification = 'completed' })
                    }
                    elseif ($type -eq 'error') {
                        [void]$events.Add([pscustomobject]@{ event = 'error' })
                    }
                }
                { $_ -in @('pi', 'omp') } {
                    if ($type -eq 'tool_execution_end' -and [string](Get-JsonProperty $native 'toolName' '') -eq 'read' -and
                        -not [bool](Get-JsonProperty $native 'isError' $false)) {
                        [void]$events.Add([pscustomobject]@{ event = 'tool_call'; tool = 'read_file'; read_only = $true })
                    }
                    elseif ($type -eq 'message_end') {
                        $message = Get-JsonProperty $native 'message'
                        if ([string](Get-JsonProperty $message 'role' '') -eq 'assistant') {
                            foreach ($content in @(Get-JsonProperty $message 'content' @())) {
                                if ([string](Get-JsonProperty $content 'type' '') -eq 'text') {
                                    [void]$events.Add([pscustomobject]@{ event = 'stream_delta'; text = [string](Get-JsonProperty $content 'text' '') })
                                }
                            }
                        }
                    }
                    elseif ($type -eq 'agent_end') {
                        [void]$events.Add([pscustomobject]@{ event = 'terminal'; classification = 'completed' })
                    }
                }
            }
        }
        catch {
            $malformedCount++
        }
    }
    return [pscustomobject]@{ events = @($events); malformed_count = $malformedCount }
}

function Test-CanonicalModelMatch {
    param([string]$Actual, [string]$Expected)
    if ($Actual -ceq $Expected) {
        return $true
    }
    if ($Expected -ceq 'gpt-5.6-luna' -and $Actual -ceq 'openai/gpt-5.6-luna') {
        return $true
    }
    return $false
}

function Get-DiagnosticLineCount {
    if (-not $script:DiagnosticsPath -or -not (Test-Path -LiteralPath $script:DiagnosticsPath -PathType Leaf)) {
        return 0
    }
    return @(Get-Content -LiteralPath $script:DiagnosticsPath -Encoding UTF8).Count
}

function Read-CorrelatedGatewayEvents {
    param([pscustomobject]$Case, [int]$StartLine, [int]$ExpectedRequestCount, [bool]$WaitForCompletion)
    $nativeEvents = @()
    $malformedDiagnosticCount = 0
    $probeLimit = if ($WaitForCompletion) { 20 } else { 1 }
    for ($probe = 0; $probe -lt $probeLimit; $probe++) {
        $lines = if (Test-Path -LiteralPath $script:DiagnosticsPath -PathType Leaf) {
            @(Get-Content -LiteralPath $script:DiagnosticsPath -Encoding UTF8 | Select-Object -Skip $StartLine)
        }
        else { @() }
        $parsedDiagnostics = [System.Collections.Generic.List[object]]::new()
        $malformedDiagnosticCount = 0
        foreach ($line in $lines) {
            try { [void]$parsedDiagnostics.Add(($line | ConvertFrom-Json -ErrorAction Stop)) }
            catch { $malformedDiagnosticCount++ }
        }
        $caseStarts = @($parsedDiagnostics | Where-Object {
            if ([string](Get-JsonProperty $_ 'event' '') -cne 'request_start') {
                return $false
            }
            $diagnosticClient = [string](Get-JsonProperty $_ 'client_id' '')
            return $diagnosticClient -ceq $Case.client -or
                ($Case.client -ceq 'codex-cli' -and $diagnosticClient -ceq 'unknown')
        })
        $requestIds = @($caseStarts | ForEach-Object {
            [string](Get-JsonProperty $_ 'request_id' '')
        } | Where-Object { $_ } | Select-Object -Unique)
        $nativeEvents = @($parsedDiagnostics | Where-Object {
            $diagnosticClient = [string](Get-JsonProperty $_ 'client_id' '')
            $clientMatches = $diagnosticClient -ceq $Case.client -or
                ($Case.client -ceq 'codex-cli' -and $diagnosticClient -ceq 'unknown')
            $requestId = [string](Get-JsonProperty $_ 'request_id' '')
            return $clientMatches -or ($requestId -and $requestIds -contains $requestId)
        })
        $completeCount = @($nativeEvents | Where-Object { [string](Get-JsonProperty $_ 'event' '') -eq 'request_complete' }).Count
        $errorCount = @($nativeEvents | Where-Object { [string](Get-JsonProperty $_ 'event' '') -eq 'request_error' }).Count
        if ($completeCount -ge $ExpectedRequestCount -or $errorCount -gt 0) {
            break
        }
        Start-Sleep -Milliseconds 100
    }
    $events = [System.Collections.Generic.List[object]]::new()
    if ($malformedDiagnosticCount -gt 0) {
        [void]$events.Add([pscustomobject]@{ event = 'error' })
    }
    $modelContradictionCount = @($nativeEvents | Where-Object {
        $eventName = [string](Get-JsonProperty $_ 'event' '')
        if ($eventName -notin @('request_start', 'request_complete', 'request_error', 'upstream_protocol_fallback')) {
            return $false
        }
        $actualModel = [string](Get-JsonProperty $_ 'model_canonical' (Get-JsonProperty $_ 'model' ''))
        return -not (Test-CanonicalModelMatch -Actual $actualModel -Expected $Case.canonical_model)
    }).Count
    if ($modelContradictionCount -gt 0) {
        [void]$events.Add([pscustomobject]@{ event = 'error' })
    }
    $starts = @($nativeEvents | Where-Object { [string](Get-JsonProperty $_ 'event' '') -eq 'request_start' })
    foreach ($start in $starts) {
        [void]$events.Add([pscustomobject]@{ event = 'gateway_request' })
    }
    if ($starts.Count -gt $ExpectedRequestCount) {
        [void]$events.Add([pscustomobject]@{ event = 'reconnect'; classification = 'unclassified' })
    }
    $completes = @($nativeEvents | Where-Object { [string](Get-JsonProperty $_ 'event' '') -eq 'request_complete' })
    foreach ($complete in $completes) {
        [void]$events.Add([pscustomobject]@{ event = 'gateway_complete' })
    }
    if ($completes.Count -gt 0) {
        $native = $completes[-1]
        $actualModel = [string](Get-JsonProperty $native 'model_canonical' (Get-JsonProperty $native 'model' ''))
        [void]$events.Add([pscustomobject]@{ event = 'model_selected'; model = $actualModel })
        [void]$events.Add([pscustomobject]@{ event = 'request_complete'; status = [int](Get-JsonProperty $native 'status' 0) })
        $terminalCount = [int](Get-JsonProperty $native 'terminal_count' 0)
        if ($terminalCount -eq 0 -and [bool](Get-JsonProperty $native 'sse_terminal_event_seen' $false)) {
            $terminalCount = 1
        }
        for ($index = 0; $index -lt $terminalCount; $index++) {
            [void]$events.Add([pscustomobject]@{ event = 'gateway_terminal' })
        }
    }
    foreach ($native in $nativeEvents) {
        $event = [string](Get-JsonProperty $native 'event' '')
        if ($event -eq 'request_error') {
            $status = [int](Get-JsonProperty $native 'status' 0)
            [void]$events.Add([pscustomobject]@{ event = 'error' })
            if ($status -in @(429, 503)) {
                [void]$events.Add([pscustomobject]@{ event = 'provider_capacity'; status = $status; output_seen = $false })
            }
        }
        elseif ($event -eq 'upstream_protocol_fallback') {
            [void]$events.Add([pscustomobject]@{ event = 'fallback' })
        }
    }
    return @($events)
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
        CODEXHUB_E2E_CLIENT = $Case.client
        CODEXHUB_E2E_MODEL = $Case.canonical_model
        CODEXHUB_E2E_SENTINEL = $sentinel
        CODEXHUB_E2E_SENTINEL_PATH = $sentinelPath
        CODEXHUB_E2E_ATTEMPT = [string]$Attempt
        CODEXHUB_E2E_DIAGNOSTICS_PATH = $script:DiagnosticsPath
    }
    $diagnosticStartLine = Get-DiagnosticLineCount
    $processResult = Invoke-IsolatedProcess -Executable $Executable -Arguments $arguments -CaseRoot $CaseRoot -Environment $environment -StandardInput $prompt -ProcessTimeoutSeconds $TimeoutSeconds
    Remove-Item -LiteralPath $sentinelPath -Force -ErrorAction SilentlyContinue
    $parsed = ConvertFrom-ClientEvents -Client $Case.client -Text $processResult.stdout
    $expectedRequestCount = @($parsed.events | Where-Object { (Get-JsonProperty $_ 'event') -eq 'tool_call' }).Count + 1
    $gatewayEvents = @(Read-CorrelatedGatewayEvents -Case $Case -StartLine $diagnosticStartLine -ExpectedRequestCount $expectedRequestCount -WaitForCompletion (-not $processResult.timed_out -and $processResult.exit_code -eq 0))
    return [pscustomobject]@{
        process = $processResult
        events = @($parsed.events) + $gatewayEvents
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
    $gatewayTerminalEvents = @($events | Where-Object { (Get-JsonProperty $_ 'event') -eq 'gateway_terminal' })
    $gatewayRequestEvents = @($events | Where-Object { (Get-JsonProperty $_ 'event') -eq 'gateway_request' })
    $gatewayCompleteEvents = @($events | Where-Object { (Get-JsonProperty $_ 'event') -eq 'gateway_complete' })
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
        $gatewayRequestEvents.Count -eq ($allToolEvents.Count + 1) -and
        $gatewayCompleteEvents.Count -eq ($allToolEvents.Count + 1) -and
        $streamEvents.Count -eq 1 -and $sentinelEvents.Count -eq 1 -and
        $requestEvents.Count -eq 1 -and
        $httpStatus -eq 200 -and
        $terminalEvents.Count -eq 1 -and
        $gatewayTerminalEvents.Count -eq 1 -and
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
        duplicate_terminal_count = [Math]::Max([Math]::Max(0, $terminalEvents.Count - 1), [Math]::Max(0, $gatewayTerminalEvents.Count - 1))
        gateway_terminal_count = $gatewayTerminalEvents.Count
        gateway_request_count = $gatewayRequestEvents.Count
        gateway_complete_count = $gatewayCompleteEvents.Count
    }
}

function Invoke-AutomatedCase {
    param([pscustomobject]$Case, [string]$Executable, [string]$ArtifactRoot, [string]$WorkRoot)
    $caseWorkRoot = Join-Path $WorkRoot $Case.case_id
    [void](New-Item -ItemType Directory -Force -Path $caseWorkRoot)
    Initialize-ClientConfiguration -Client $Case.client -CaseRoot $caseWorkRoot -Model $Case.canonical_model
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
        gateway_terminal_count = $measurement.gateway_terminal_count
        gateway_request_count = $measurement.gateway_request_count
        gateway_complete_count = $measurement.gateway_complete_count
        terminal_classification = $measurement.terminal_classification
        reconnect_classification = $measurement.reconnect_classification
        retry_classification = $retryClassification
        artifact = "artifacts/$artifactRelative"
    }
}

function Get-ManualResults {
    param([string]$EvidencePath, [object[]]$ManualCases, [string]$ArtifactRoot, [string]$RunBinding)
    try {
        $evidence = Get-Content -LiteralPath $EvidencePath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw 'manual_evidence_malformed'
    }
    $topLevelNames = @($evidence.PSObject.Properties.Name | Sort-Object)
    if (($topLevelNames -join ',') -cne 'candidate_sha,cases,gui_confirmed,login_confirmed,run_binding_sha256,schema') {
        throw 'manual_evidence_schema_invalid'
    }
    if ((Get-JsonProperty $evidence 'schema') -cne 'codexhub.real-client-manual-evidence.v2') {
        throw 'manual_evidence_schema_invalid'
    }
    if ((Get-JsonProperty $evidence 'candidate_sha') -cne $CandidateSha) {
        throw 'manual_evidence_candidate_sha_stale'
    }
    if ((Get-JsonProperty $evidence 'run_binding_sha256') -cne $RunBinding) {
        throw 'manual_evidence_run_binding_stale'
    }
    if ((Get-JsonProperty $evidence 'login_confirmed' $false) -ne $true) {
        throw 'manual_evidence_login_missing'
    }
    if ((Get-JsonProperty $evidence 'gui_confirmed' $false) -ne $true) {
        throw 'manual_evidence_gui_missing'
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
            'request_complete_count', 'sentinel_chunk_count', 'sentinel_relative_path',
            'terminal_classification'
        ) | Sort-Object
        $itemNames = @($item.PSObject.Properties.Name | Sort-Object)
        if (($expectedNames -join ',') -cne ($itemNames -join ',')) {
            throw 'manual_evidence_schema_invalid'
        }
        $valid = (Get-JsonProperty $item 'client') -ceq $expected.client -and
            (Get-JsonProperty $item 'canonical_model') -ceq $expected.canonical_model -and
            (Get-JsonProperty $item 'sentinel_relative_path') -ceq "isolated/work/gui-$($expected.client)/$($expected.case_id)/sentinel.txt" -and
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

function New-RunBinding {
    $bytes = New-Object byte[] 32
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
    return 'sha256:' + ([System.BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant())
}

function Write-ManualEvidenceTemplate {
    param([string]$Path, [object[]]$ManualCases, [string]$RunBinding)
    $cases = @($ManualCases | ForEach-Object {
        [ordered]@{
            case_id = $_.case_id
            client = $_.client
            canonical_model = $_.canonical_model
            sentinel_relative_path = "isolated/work/gui-$($_.client)/$($_.case_id)/sentinel.txt"
            human_finalized = $false
            outcome = 'pending'
            terminal_classification = 'unclassified'
            reconnect_classification = 'unclassified'
            request_complete_count = 0
            http_status = 0
            read_only_tool_call_count = 0
            sentinel_chunk_count = 0
            fallback_count = 0
            duplicate_terminal_count = 0
        }
    })
    Write-JsonFile -Path $Path -Value ([ordered]@{
        schema = 'codexhub.real-client-manual-evidence.v2'
        candidate_sha = $CandidateSha
        run_binding_sha256 = $RunBinding
        login_confirmed = $false
        gui_confirmed = $false
        cases = $cases
    })
}

function Get-NativeClientVersion {
    param([string]$Client, [string]$Executable, [string]$Expected, [string]$ProbeRoot)
    $failureClient = $Client.Replace('-', '_')
    $extension = [System.IO.Path]::GetExtension($Executable)
    if ($extension -ieq '.exe' -and $Client -in @('desktop', 'zcode')) {
        $version = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($Executable).ProductVersion
        if ($version -and $version.Trim() -ceq $Expected) {
            return $Expected
        }
        throw "preflight_${failureClient}_version_mismatch"
    }
    [void](New-Item -ItemType Directory -Force -Path $ProbeRoot)
    $result = Invoke-IsolatedProcess -Executable $Executable -Arguments @('--version') -CaseRoot $ProbeRoot -Environment @{
        CODEXHUB_E2E_VERSION_PROBE = '1'
        CODEXHUB_E2E_EXPECTED_VERSION = $Expected
        CODEXHUB_E2E_CLIENT = $Client
    } -StandardInput '' -ProcessTimeoutSeconds 15
    $text = ($result.stdout + "`n" + $result.stderr).Trim()
    if ($result.timed_out -or $result.exit_code -ne 0 -or $text -notmatch ('(?<![0-9.])' + [regex]::Escape($Expected) + '(?![0-9.])')) {
        throw "preflight_${failureClient}_version_mismatch"
    }
    return $Expected
}

function Get-GatewayBaseUrl {
    return "http://127.0.0.1:$([int]$script:GatewayConfig.listen_port)/v1"
}

function Get-CodexProviderConfigText {
    param([string]$Model)
    $baseUrl = Get-GatewayBaseUrl
    $key = [string]$script:GatewayConfig.gateway_client_key
    return @"
model = "$Model"
model_provider = "codex_proxy"

[model_providers.codex_proxy]
name = "CodexHub Gateway"
base_url = "$baseUrl"
wire_api = "responses"
requires_openai_auth = true
experimental_bearer_token = "$key"
supports_websockets = false
"@
}

function Get-ClientProviderMap {
    param([string]$Client)
    $baseUrl = Get-GatewayBaseUrl
    $key = [string]$script:GatewayConfig.gateway_client_key
    $header = @{ 'x-codex-client-id' = $Client }
    return [ordered]@{
        'codexhub-openai' = [ordered]@{
            name = 'CodexHub OpenAI'
            npm = '@ai-sdk/openai'
            options = [ordered]@{ baseURL = "$baseUrl/providers/openai"; apiKey = $key; headers = $header }
            models = [ordered]@{ 'gpt-5.6-luna' = [ordered]@{ name = 'GPT-5.6 Luna' } }
        }
        'codexhub-volc' = [ordered]@{
            name = 'CodexHub Volcengine'
            npm = '@ai-sdk/openai'
            options = [ordered]@{ baseURL = "$baseUrl/providers/volc"; apiKey = $key; headers = $header }
            models = [ordered]@{ 'glm-5.2' = [ordered]@{ name = 'Volc GLM-5.2' } }
        }
    }
}

function Initialize-ClientConfiguration {
    param([string]$Client, [string]$CaseRoot, [string]$Model)
    foreach ($relative in @('.codex', '.config\opencode', '.pi\agent', '.omp\agent', '.zcode\v2', 'appdata\roaming\ZCode\model-providers')) {
        [void](New-Item -ItemType Directory -Force -Path (Join-Path $CaseRoot $relative))
    }
    Copy-Item -LiteralPath $script:AccountAuthPath -Destination (Join-Path $CaseRoot '.codex\auth.json') -Force
    [System.IO.File]::WriteAllText((Join-Path $CaseRoot '.codex\config.toml'), (Get-CodexProviderConfigText -Model $Model), $script:Utf8NoBom)
    $providerMap = Get-ClientProviderMap -Client $Client
    $selector = if ($Model -eq 'gpt-5.6-luna') { 'codexhub-openai/gpt-5.6-luna' } elseif ($Model -eq 'volc/glm-5.2') { 'codexhub-volc/glm-5.2' } else { $Model }
    Write-JsonFile -Path (Join-Path $CaseRoot '.config\opencode\opencode.json') -Value ([ordered]@{
        '$schema' = 'https://opencode.ai/config.json'
        model = $selector
        small_model = $selector
        provider = $providerMap
    })
    $providerId, $modelId = $selector -split '/', 2
    Write-JsonFile -Path (Join-Path $CaseRoot '.pi\agent\settings.json') -Value ([ordered]@{
        defaultProvider = $providerId
        defaultModel = $modelId
    })
    $piProviders = [ordered]@{}
    foreach ($entry in $providerMap.GetEnumerator()) {
        $piProviders[$entry.Key] = [ordered]@{
            baseUrl = $entry.Value.options.baseURL
            api = 'openai-responses'
            apiKey = $entry.Value.options.apiKey
            authHeader = $true
            headers = @{ 'x-codex-client-id' = 'pi' }
            models = @($entry.Value.models.GetEnumerator() | ForEach-Object {
                [ordered]@{ id = $_.Key; name = $_.Value.name; headers = @{ 'x-codex-client-id' = 'pi' } }
            })
        }
    }
    Write-JsonFile -Path (Join-Path $CaseRoot '.pi\agent\models.json') -Value ([ordered]@{ providers = $piProviders })
    $key = [string]$script:GatewayConfig.gateway_client_key
    $baseUrl = Get-GatewayBaseUrl
    $ompText = @"
providers:
  codexhub-openai:
    baseUrl: $baseUrl/providers/openai
    api: openai-responses
    apiKey: $key
    authHeader: true
    models:
      - id: gpt-5.6-luna
        name: GPT-5.6 Luna
        headers:
          x-codex-client-id: omp
  codexhub-volc:
    baseUrl: $baseUrl/providers/volc
    api: openai-responses
    apiKey: $key
    authHeader: true
    models:
      - id: glm-5.2
        name: Volc GLM-5.2
        headers:
          x-codex-client-id: omp
"@
    [System.IO.File]::WriteAllText((Join-Path $CaseRoot '.omp\agent\config.yml'), "modelRoles:`n  default: $selector`n", $script:Utf8NoBom)
    [System.IO.File]::WriteAllText((Join-Path $CaseRoot '.omp\agent\models.yml'), $ompText, $script:Utf8NoBom)
    $zcodeProviders = @($providerMap.GetEnumerator() | ForEach-Object {
        [ordered]@{
            id = $_.Key
            name = $_.Value.name
            kind = 'openai'
            apiFormat = 'openai-responses'
            endpoints = [ordered]@{ baseURL = $_.Value.options.baseURL; paths = [ordered]@{ openai = '/responses' } }
            apiKey = $_.Value.options.apiKey
            headers = @{ 'x-codex-client-id' = 'zcode' }
            models = $_.Value.models
        }
    })
    $zcodeCatalog = [ordered]@{ schemaVersion = 'zcode.model-providers.v2'; providers = $zcodeProviders }
    Write-JsonFile -Path (Join-Path $CaseRoot 'appdata\roaming\ZCode\model-providers\codexhub.json') -Value $zcodeCatalog
    Write-JsonFile -Path (Join-Path $CaseRoot '.zcode\v2\bots-model-cache.v2.json') -Value $zcodeCatalog
    $zcodeProviderObject = [ordered]@{}
    foreach ($provider in $zcodeProviders) { $zcodeProviderObject[$provider.id] = $provider }
    Write-JsonFile -Path (Join-Path $CaseRoot '.zcode\v2\config.json') -Value ([ordered]@{ provider = $zcodeProviderObject; model = $selector })
}

function Initialize-CandidateRuntime {
    param([string]$CandidateRoot)
    $script:CandidateRuntimeRoot = Join-Path $CandidateRoot 'runtime'
    $script:CandidateCodexRoot = Join-Path $CandidateRoot 'codex'
    $proxyRoot = Join-Path $script:CandidateRuntimeRoot 'proxy'
    [void](New-Item -ItemType Directory -Force -Path (Join-Path $proxyRoot 'config'))
    [void](New-Item -ItemType Directory -Force -Path $script:CandidateCodexRoot)
    Copy-Item -LiteralPath $script:AccountAuthPath -Destination (Join-Path $script:CandidateCodexRoot 'auth.json') -Force
    [System.IO.File]::WriteAllText((Join-Path $script:CandidateCodexRoot 'config.toml'), (Get-CodexProviderConfigText -Model 'gpt-5.6-luna'), $script:Utf8NoBom)
    Write-JsonFile -Path (Join-Path $proxyRoot 'settings.json') -Value ([ordered]@{
        auto_start_gateway = $true
        gateway_bind_address = '127.0.0.1'
        gateway_client_key = [string]$script:GatewayConfig.gateway_client_key
        gateway_enable_models = $true
        gateway_enable_responses = $true
        gateway_enable_chat_completions = $true
        proxy_port = [int]$script:GatewayConfig.listen_port
    })
    $providerText = @"
[[providers]]
id = "volc"
name = "Volcengine"
base_url = "https://ark.cn-beijing.volces.com/api/coding/v3"
api_key = "{env:VOLCENGINE_API_KEY}"
enabled = true

  [[providers.models]]
  id = "glm-5.2"
  display_name = "Volc GLM-5.2"
  context_window = 1024000
  max_output_tokens = 8192
  enabled = true
"@
    [System.IO.File]::WriteAllText((Join-Path $proxyRoot 'config\providers.toml'), $providerText, $script:Utf8NoBom)
    $script:DiagnosticsPath = Join-Path $proxyRoot 'codex-proxy-events.jsonl'
    [System.IO.File]::WriteAllText($script:DiagnosticsPath, '', $script:Utf8NoBom)
}

function Get-SafeFailureClassification {
    param([System.Management.Automation.ErrorRecord]$ErrorRecord)
    $message = [string]$ErrorRecord.Exception.Message
    if ($message -match '^(preflight|manual|candidate|client|automated)_[a-z0-9_]+$') {
        return $message
    }
    return 'internal_error'
}

$CandidateSha = $CandidateSha.ToLowerInvariant()
$failureOutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
[void](New-Item -ItemType Directory -Force -Path $failureOutputDirectory)
$failureSummaryPath = Join-Path $failureOutputDirectory 'summary.json'
$failureArtifactRoot = Join-Path $failureOutputDirectory 'artifacts'
try {
if ($CandidateSha -notmatch '^[0-9a-f]{40}$') {
    throw 'preflight_candidate_sha_invalid'
}
if ($LunaModel -cne 'codexhub-openai/gpt-5.6-luna') {
    throw 'preflight_luna_model_invalid'
}
if ($VolcModel -cne 'codexhub-volc/glm-5.2') {
    throw 'preflight_volc_model_invalid'
}
if ($TimeoutSeconds -lt 1 -or $TimeoutSeconds -gt 900 -or
    $ManualEvidenceTimeoutSeconds -lt 1 -or $ManualEvidenceTimeoutSeconds -gt 3600) {
    throw 'preflight_timeout_invalid'
}
if (-not (Test-Path -LiteralPath $OutputDirectory -PathType Container)) {
    throw 'preflight_output_directory_missing'
}
$OutputDirectory = (Resolve-Path -LiteralPath $OutputDirectory).Path
$isolationRoot = Join-Path $OutputDirectory 'isolated'
$accountPath = Join-Path $isolationRoot 'account\profile.json'
$accountAuthPath = Join-Path $isolationRoot 'account\auth.json'
$credentialPath = Join-Path $isolationRoot 'credentials\volc.json'
$configRoot = Join-Path $isolationRoot 'config'
$workRoot = Join-Path $isolationRoot 'work'
$gatewayConfigPath = Join-Path $configRoot 'gateway.json'
$manualEvidencePath = Join-Path $OutputDirectory 'manual-evidence.json'
$manualTemplatePath = Join-Path $OutputDirectory 'manual-evidence.template.json'
$summaryPath = Join-Path $OutputDirectory 'summary.json'
$artifactRoot = Join-Path $OutputDirectory 'artifacts'

foreach ($directory in @($isolationRoot, $configRoot, $workRoot)) {
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        throw 'preflight_isolated_directory_missing'
    }
}
foreach ($file in @($DebugBuild, $SnapshotManifest, $accountPath, $accountAuthPath, $credentialPath, $gatewayConfigPath)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        throw 'preflight_required_file_missing'
    }
}
$DebugBuild = (Resolve-Path -LiteralPath $DebugBuild).Path
$SnapshotManifest = (Resolve-Path -LiteralPath $SnapshotManifest).Path
$shaSidecar = "$DebugBuild.candidate-sha"
if (-not (Test-Path -LiteralPath $shaSidecar -PathType Leaf)) {
    throw 'preflight_debug_build_sha_sidecar_missing'
}
if ((Get-Content -LiteralPath $shaSidecar -Raw).Trim() -cne $CandidateSha) {
    throw 'preflight_debug_build_sha_mismatch'
}
$snapshot = Read-JsonObject -Path $SnapshotManifest -Failure 'preflight_snapshot_manifest_invalid'
Assert-ExactJsonProperties -Value $snapshot -Names @('schema', 'snapshot', 'machine_name_sha256') -Failure 'preflight_snapshot_manifest_invalid'
if ([string]$snapshot.schema -cne 'codexhub.real-client-vm-snapshot.v1' -or
    [string]$snapshot.snapshot -cne 'codexhub-real-client-e2e-v1' -or
    [string]$snapshot.machine_name_sha256 -cne (Get-TextSha256 -Text ([string]$env:COMPUTERNAME))) {
    throw 'preflight_snapshot_identity_mismatch'
}
$profile = Read-JsonObject -Path $accountPath -Failure 'preflight_account_profile_invalid'
Assert-ExactJsonProperties -Value $profile -Names @('schema', 'dedicated_account', 'codex_login_ready', 'gui_ready') -Failure 'preflight_account_profile_invalid'
if ([string]$profile.schema -cne 'codexhub.real-client-account.v1' -or
    [bool]$profile.dedicated_account -ne $true -or [bool]$profile.codex_login_ready -ne $true -or [bool]$profile.gui_ready -ne $true) {
    throw 'preflight_account_not_ready'
}
$accountAuth = Read-JsonObject -Path $accountAuthPath -Failure 'preflight_codex_auth_invalid'
$authTokens = Get-JsonProperty $accountAuth 'tokens'
if ([string](Get-JsonProperty $accountAuth 'auth_mode' '') -cne 'chatgpt' -or
    [string](Get-JsonProperty $authTokens 'access_token' '') -eq '' -or
    [string](Get-JsonProperty $authTokens 'refresh_token' '') -eq '') {
    throw 'preflight_codex_login_missing'
}
$credential = Read-JsonObject -Path $credentialPath -Failure 'preflight_volc_credential_invalid'
Assert-ExactJsonProperties -Value $credential -Names @('schema', 'api_key') -Failure 'preflight_volc_credential_invalid'
if ([string]$credential.schema -cne 'codexhub.real-client-volc.v1' -or [string]$credential.api_key -notmatch '^\S{16,}$') {
    throw 'preflight_volc_credential_invalid'
}
$script:GatewayConfig = Read-JsonObject -Path $gatewayConfigPath -Failure 'preflight_gateway_config_invalid'
Assert-ExactJsonProperties -Value $script:GatewayConfig -Names @('schema', 'listen_port', 'gateway_client_key') -Failure 'preflight_gateway_config_invalid'
if ([string]$script:GatewayConfig.schema -cne 'codexhub.real-client-gateway.v1' -or
    [int]$script:GatewayConfig.listen_port -lt 1024 -or [int]$script:GatewayConfig.listen_port -gt 65535 -or
    [string]$script:GatewayConfig.gateway_client_key -notmatch '^\S{16,}$') {
    throw 'preflight_gateway_config_invalid'
}
$script:AccountAuthPath = $accountAuthPath
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
$actualVersions = [ordered]@{}
foreach ($versionTarget in @(
    [pscustomobject]@{ client = 'desktop'; key = 'desktop' },
    [pscustomobject]@{ client = 'codex-cli'; key = 'codex_cli' },
    [pscustomobject]@{ client = 'zcode'; key = 'zcode' },
    [pscustomobject]@{ client = 'opencode'; key = 'opencode' },
    [pscustomobject]@{ client = 'pi'; key = 'pi' },
    [pscustomobject]@{ client = 'omp'; key = 'omp' }
)) {
    $actualVersions[$versionTarget.key] = Get-NativeClientVersion -Client $versionTarget.client -Executable $executables[$versionTarget.client] -Expected $script:PinnedVersions[$versionTarget.key] -ProbeRoot (Join-Path $workRoot "version-$($versionTarget.client)")
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

$runBinding = New-RunBinding
Write-ManualEvidenceTemplate -Path $manualTemplatePath -ManualCases $manualCases -RunBinding $runBinding
if (Test-Path -LiteralPath $manualEvidencePath) {
    throw 'manual_evidence_preexisting'
}
$trackedProcesses = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
$nativeGuiProcesses = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
$manualSentinelPaths = [System.Collections.Generic.List[string]]::new()
try {
    $candidateRoot = Join-Path $workRoot 'candidate'
    [void](New-Item -ItemType Directory -Force -Path $candidateRoot)
    Initialize-CandidateRuntime -CandidateRoot $candidateRoot
    $candidateProcess = Start-IsolatedProcess -Executable $DebugBuild -Arguments @() -CaseRoot $candidateRoot -Environment @{
        CODEXHUB_E2E_CANDIDATE_SHA = $CandidateSha
        CODEXHUB_RUNTIME_HOME = $script:CandidateRuntimeRoot
        CODEXHUB_CODEX_TARGET_HOME = $script:CandidateCodexRoot
        CODEX_HOME = $script:CandidateCodexRoot
        CODEX_PROXY_GATEWAY_CLIENT_KEY = [string]$script:GatewayConfig.gateway_client_key
        VOLCENGINE_API_KEY = [string]$credential.api_key
    }
    [void]$trackedProcesses.Add($candidateProcess)
    Start-Sleep -Seconds 1
    if ($candidateProcess.HasExited) {
        throw 'candidate_debug_build_exited_during_startup'
    }

    foreach ($guiClient in @('desktop', 'zcode')) {
        $guiCases = @($manualCases | Where-Object { $_.client -ceq $guiClient })
        $guiRoot = Join-Path $workRoot ("gui-" + $guiClient)
        [void](New-Item -ItemType Directory -Force -Path $guiRoot)
        Initialize-ClientConfiguration -Client $guiClient -CaseRoot $guiRoot -Model $guiCases[0].canonical_model
        foreach ($guiCase in $guiCases) {
            $guiCaseRoot = Join-Path $guiRoot $guiCase.case_id
            [void](New-Item -ItemType Directory -Force -Path $guiCaseRoot)
            $guiSentinelPath = Join-Path $guiCaseRoot 'sentinel.txt'
            [System.IO.File]::WriteAllText($guiSentinelPath, "SENTINEL:codexhub-real-client-e2e:$($guiCase.case_id)", $script:Utf8NoBom)
            [void]$manualSentinelPaths.Add($guiSentinelPath)
        }
        $guiProcess = Start-IsolatedProcess -Executable $executables[$guiClient] -Arguments @() -CaseRoot $guiRoot -Environment @{
            CODEXHUB_E2E_GUI_CLIENT = $guiClient
            CODEXHUB_E2E_CASES = ($guiCases.case_id -join ',')
            CODEXHUB_E2E_MODELS = ($guiCases.canonical_model -join ',')
            CODEXHUB_E2E_MANUAL_TEMPLATE = $manualTemplatePath
            CODEXHUB_E2E_MANUAL_EVIDENCE = $manualEvidencePath
            CODEXHUB_E2E_GUI_LAUNCH_MARKER = (Join-Path $workRoot "gui-$guiClient.launched")
        }
        [void]$trackedProcesses.Add($guiProcess)
        [void]$nativeGuiProcesses.Add($guiProcess)
    }

    $manualDeadline = [DateTime]::UtcNow.AddSeconds($ManualEvidenceTimeoutSeconds)
    while (-not (Test-Path -LiteralPath $manualEvidencePath -PathType Leaf)) {
        foreach ($guiProcess in $nativeGuiProcesses) {
            if ($guiProcess.HasExited) {
                throw 'manual_gui_exited_before_finalization'
            }
        }
        if ([DateTime]::UtcNow -ge $manualDeadline) {
            throw 'manual_evidence_timeout'
        }
        Start-Sleep -Milliseconds 100
    }
    $manualResults = @(Get-ManualResults -EvidencePath $manualEvidencePath -ManualCases $manualCases -ArtifactRoot $artifactRoot -RunBinding $runBinding)

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
        run_binding_sha256 = $runBinding
        outcome = if ($passedCount -eq 12) { 'passed' } else { 'failed' }
        failure_classification = if ($passedCount -eq 12) { 'none' } else { 'case_failure' }
        hashes = [ordered]@{
            debug_build = Get-Sha256 -Path $DebugBuild
        }
        pinned_versions = $actualVersions
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
    foreach ($manualSentinelPath in $manualSentinelPaths) {
        Remove-Item -LiteralPath $manualSentinelPath -Force -ErrorAction SilentlyContinue
    }
}
}
catch {
    $failureClassification = Get-SafeFailureClassification -ErrorRecord $_
    if (Test-Path -LiteralPath $failureArtifactRoot) {
        $resolvedOutput = [System.IO.Path]::GetFullPath($failureOutputDirectory).TrimEnd('\') + '\'
        $resolvedArtifacts = [System.IO.Path]::GetFullPath($failureArtifactRoot)
        if ($resolvedArtifacts.StartsWith($resolvedOutput, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $resolvedArtifacts -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    $failureSummary = [ordered]@{
        schema = 'codexhub.real-client-e2e-summary.v1'
        candidate_sha = if ($CandidateSha -match '^[0-9a-f]{40}$') { $CandidateSha } else { $null }
        outcome = 'failed'
        failure_classification = $failureClassification
        pinned_versions = $script:PinnedVersions
        canonical_models = @('gpt-5.6-luna', 'volc/glm-5.2', 'codexhub-openai/gpt-5.6-luna', 'codexhub-volc/glm-5.2')
        counts = [ordered]@{
            case_count = 0
            passed_count = 0
            failed_count = 0
            manual_case_count = 0
            automated_case_count = 0
        }
        cases = @()
        artifacts = @()
    }
    Write-JsonFile -Path $failureSummaryPath -Value $failureSummary
    exit 1
}
