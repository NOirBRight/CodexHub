[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$CandidateSha,

    [Parameter(Mandatory = $true)]
    [string]$DebugBuild,

    [Parameter(Mandatory = $true)]
    [string]$ManagedClientConfigBuild,

    [Parameter(Mandatory = $true)]
    [string]$ManagedClientConfigSha,

    [Parameter(Mandatory = $true)]
    [string]$LunaModel,

    [Parameter(Mandatory = $true)]
    [string]$VolcModel,

    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory,

    [Parameter(Mandatory = $true)]
    [string]$HostEnvironmentManifest,

    [string]$TestWindowsInstallMetadataFixture,

    [string]$CodexDesktopPath = 'Codex.exe',
    [string]$CodexCliPath = 'codex.exe',
    [string]$ZCodePath = 'zcode.exe',
    [string]$OpenCodePath = 'opencode.exe',
    [string]$PiPath = 'pi.exe',
    [string]$OmpPath = 'omp.exe',

    [int]$TimeoutSeconds = 180,

    [int]$ManualEvidenceTimeoutSeconds = 900,

    [int]$OverallTimeoutSeconds = 5400,

    [string]$InternalSupervisorToken
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

try {
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class CodexHubE2EJob
{
    [StructLayout(LayoutKind.Sequential)]
    private struct BasicLimitInformation
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ExtendedLimitInformation
    {
        public BasicLimitInformation BasicLimitInformation;
        public IoCounters IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr CreateJobObject(IntPtr attributes, string name);

    [DllImport("kernel32.dll")]
    private static extern bool SetInformationJobObject(
        IntPtr job,
        int informationClass,
        IntPtr information,
        uint informationLength);

    [DllImport("kernel32.dll")]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll")]
    private static extern bool QueryInformationJobObject(
        IntPtr job,
        int informationClass,
        IntPtr information,
        uint informationLength,
        IntPtr returnLength);

    [DllImport("kernel32.dll")]
    private static extern bool CloseHandle(IntPtr handle);

    public static IntPtr CreateKillOnClose()
    {
        IntPtr job = CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero)
            return IntPtr.Zero;
        ExtendedLimitInformation information = new ExtendedLimitInformation();
        information.BasicLimitInformation.LimitFlags = 0x00002000;
        int length = Marshal.SizeOf(information);
        IntPtr pointer = Marshal.AllocHGlobal(length);
        try
        {
            Marshal.StructureToPtr(information, pointer, false);
            if (!SetInformationJobObject(job, 9, pointer, (uint)length))
            {
                CloseHandle(job);
                return IntPtr.Zero;
            }
            return job;
        }
        finally
        {
            Marshal.FreeHGlobal(pointer);
        }
    }

    public static bool Assign(IntPtr job, IntPtr process)
    {
        return job != IntPtr.Zero && AssignProcessToJobObject(job, process);
    }

    public static uint[] GetProcessCounts(IntPtr job)
    {
        if (job == IntPtr.Zero)
            return new uint[] { 0, 0 };
        int length = Marshal.SizeOf(typeof(BasicAccountingInformation));
        IntPtr pointer = Marshal.AllocHGlobal(length);
        try
        {
            if (!QueryInformationJobObject(job, 1, pointer, (uint)length, IntPtr.Zero))
                return new uint[] { 0, 0 };
            BasicAccountingInformation information =
                (BasicAccountingInformation)Marshal.PtrToStructure(
                    pointer, typeof(BasicAccountingInformation));
            return new uint[] { information.TotalProcesses, information.ActiveProcesses };
        }
        finally
        {
            Marshal.FreeHGlobal(pointer);
        }
    }

    public static void Close(IntPtr job)
    {
        if (job != IntPtr.Zero)
            CloseHandle(job);
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct BasicAccountingInformation
    {
        public long TotalUserTime;
        public long TotalKernelTime;
        public long ThisPeriodTotalUserTime;
        public long ThisPeriodTotalKernelTime;
        public uint TotalPageFaultCount;
        public uint TotalProcesses;
        public uint ActiveProcesses;
        public uint TotalTerminatedProcesses;
    }
}
'@ -ErrorAction Stop
}
catch {
    # Bounded taskkill/descendant cleanup remains available as fallback.
}

$script:MinimumVersions = [ordered]@{
    desktop = '26.715.8383.0'
    codex_cli = '0.144.5'
    zcode = '3.3.6'
    opencode = '1.18.4'
    pi = '0.80.6'
    omp = '17.0.3'
}
$script:MaximumCapturedCharacters = 65536
$script:Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$script:ProcessJobHandle = [IntPtr]::Zero
$script:ProcessJobAvailable = $null -ne ('CodexHubE2EJob' -as [type])
$script:UnassignedProcessIds = [System.Collections.Generic.HashSet[int]]::new()

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

function Get-HostMachineBindingSha256 {
    try {
        $machineGuid = [string](Get-ItemPropertyValue -LiteralPath 'Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Cryptography' -Name 'MachineGuid' -ErrorAction Stop)
    }
    catch {
        throw 'preflight_host_environment_identity_unavailable'
    }
    if ($machineGuid -notmatch '^[0-9a-fA-F-]{32,36}$') {
        throw 'preflight_host_environment_identity_unavailable'
    }
    return Get-TextSha256 -Text ("windows-machine-guid-v1:" + $machineGuid.ToLowerInvariant())
}

function Assert-IsolatedRegularFile {
    param([string]$Path, [string]$IsolationRoot)
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $rootPrefix = [System.IO.Path]::GetFullPath($IsolationRoot).TrimEnd('\') + '\'
    if (-not $fullPath.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'preflight_host_session_reuse_detected'
    }
    $item = Get-Item -LiteralPath $fullPath -Force
    $linkType = if ($item.PSObject.Properties['LinkType']) { [string]$item.LinkType } else { '' }
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or $linkType) {
        throw 'preflight_host_session_reuse_detected'
    }
    $directory = $item.Directory
    while ($directory -and $directory.FullName.StartsWith($rootPrefix.TrimEnd('\'), [System.StringComparison]::OrdinalIgnoreCase)) {
        $directoryLinkType = if ($directory.PSObject.Properties['LinkType']) { [string]$directory.LinkType } else { '' }
        if (($directory.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or $directoryLinkType) {
            throw 'preflight_host_session_reuse_detected'
        }
        if ($directory.FullName.TrimEnd('\') -ieq $rootPrefix.TrimEnd('\')) {
            break
        }
        $directory = $directory.Parent
    }
}

function Assert-CanonicalNonReparseDirectory {
    param([string]$Path, [string]$Failure)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw $Failure
    }
    $fullPath = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    $resolvedPath = (Resolve-Path -LiteralPath $Path).Path.TrimEnd('\')
    if (-not $fullPath.Equals($resolvedPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw $Failure
    }
    $directory = Get-Item -LiteralPath $resolvedPath -Force
    while ($directory) {
        $linkType = if ($directory.PSObject.Properties['LinkType']) { [string]$directory.LinkType } else { '' }
        if (($directory.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or $linkType) {
            throw $Failure
        }
        $directory = $directory.Parent
    }
}

function Write-JsonFile {
    param([string]$Path, [object]$Value)
    $json = $Value | ConvertTo-Json -Depth 20
    [System.IO.File]::WriteAllText($Path, $json + "`n", $script:Utf8NoBom)
}

function Get-FailureSummaryValue {
    param([string]$FailureClassification, [string[]]$Artifacts = @())
    return [ordered]@{
        schema = 'codexhub.real-client-e2e-summary.v1'
        candidate_sha = if ($CandidateSha -match '^[0-9a-fA-F]{40}$') { $CandidateSha.ToLowerInvariant() } else { $null }
        managed_client_config_sha = if ($ManagedClientConfigSha -match '^[0-9a-fA-F]{40}$') { $ManagedClientConfigSha.ToLowerInvariant() } else { $null }
        outcome = 'failed'
        failure_classification = $FailureClassification
        pinned_versions = $script:MinimumVersions
        canonical_models = @('gpt-5.6-luna', 'volc/glm-5.2', 'codexhub-openai/gpt-5.6-luna', 'codexhub-volc/glm-5.2')
        counts = [ordered]@{
            case_count = 0
            passed_count = 0
            failed_count = 0
            manual_case_count = 0
            automated_case_count = 0
        }
        cases = @()
        artifacts = @($Artifacts)
    }
}

function Set-RunnerPhase {
    param([string]$Phase)
    if (-not $script:WatchdogStatePath) {
        return
    }
    if ($Phase -notin @('preflight', 'client_materialization', 'candidate_startup', 'candidate_gateway_ready', 'manual_evidence', 'automated_cases', 'summary')) {
        throw 'internal_runner_phase_invalid'
    }
    [System.IO.File]::WriteAllText($script:WatchdogStatePath, $Phase, $script:Utf8NoBom)
}

function Invoke-RunnerSupervisor {
    $supervisorOutput = [System.IO.Path]::GetFullPath($OutputDirectory)
    [void](New-Item -ItemType Directory -Force -Path $supervisorOutput)
    $summaryPath = Join-Path $supervisorOutput 'summary.json'
    $statePath = Join-Path $supervisorOutput 'runner-watchdog-state'
    $stdoutPath = Join-Path $supervisorOutput 'runner-watchdog.stdout'
    $stderrPath = Join-Path $supervisorOutput 'runner-watchdog.stderr'
    [System.IO.File]::WriteAllText($statePath, 'preflight', $script:Utf8NoBom)
    foreach ($path in @($stdoutPath, $stderrPath)) {
        [System.IO.File]::WriteAllText($path, '', $script:Utf8NoBom)
    }

    if (-not $script:ProcessJobAvailable) {
        Write-JsonFile -Path $summaryPath -Value (Get-FailureSummaryValue -FailureClassification 'preflight_process_supervision_unavailable')
        return 1
    }

    $token = [Guid]::NewGuid().ToString('N')
    $forwardedArguments = [ordered]@{
        CandidateSha = $CandidateSha
        DebugBuild = $DebugBuild
        ManagedClientConfigBuild = $ManagedClientConfigBuild
        ManagedClientConfigSha = $ManagedClientConfigSha
        LunaModel = $LunaModel
        VolcModel = $VolcModel
        OutputDirectory = $OutputDirectory
        HostEnvironmentManifest = $HostEnvironmentManifest
        TestWindowsInstallMetadataFixture = $TestWindowsInstallMetadataFixture
        CodexDesktopPath = $CodexDesktopPath
        CodexCliPath = $CodexCliPath
        ZCodePath = $ZCodePath
        OpenCodePath = $OpenCodePath
        PiPath = $PiPath
        OmpPath = $OmpPath
        TimeoutSeconds = $TimeoutSeconds
        ManualEvidenceTimeoutSeconds = $ManualEvidenceTimeoutSeconds
        OverallTimeoutSeconds = $OverallTimeoutSeconds
    }
    $forwardedJson = $forwardedArguments | ConvertTo-Json -Compress
    $forwardedPayload = [Convert]::ToBase64String(
        [System.Text.Encoding]::UTF8.GetBytes($forwardedJson)
    )
    $workerBootstrap = @'
& $env:CODEXHUB_E2E_SUPERVISOR_SCRIPT `
  -CandidateSha '0000000000000000000000000000000000000000' `
  -DebugBuild '.' `
  -ManagedClientConfigBuild '.' `
  -ManagedClientConfigSha '0000000000000000000000000000000000000000' `
  -LunaModel 'internal' `
  -VolcModel 'internal' `
  -OutputDirectory '.' `
  -HostEnvironmentManifest '.' `
  -TimeoutSeconds 1 `
  -ManualEvidenceTimeoutSeconds 1 `
  -OverallTimeoutSeconds 1 `
  -InternalSupervisorToken $env:CODEXHUB_E2E_SUPERVISOR_TOKEN
exit $LASTEXITCODE
'@
    $encodedBootstrap = [Convert]::ToBase64String(
        [System.Text.Encoding]::Unicode.GetBytes($workerBootstrap)
    )
    $workerArguments = [System.Collections.Generic.List[string]]::new()
    foreach ($argument in @(
        '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-EncodedCommand', $encodedBootstrap
    )) {
        [void]$workerArguments.Add([string]$argument)
    }
    $powershellPath = (Get-Command powershell.exe -ErrorAction Stop).Source
    $supervisorEnvironment = [ordered]@{
        CODEXHUB_E2E_SUPERVISOR_TOKEN = $token
        CODEXHUB_E2E_SUPERVISOR_SCRIPT = $PSCommandPath
        CODEXHUB_E2E_SUPERVISOR_ARGUMENTS = $forwardedPayload
    }
    $previousEnvironment = @{}
    foreach ($entry in $supervisorEnvironment.GetEnumerator()) {
        $previousEnvironment[$entry.Key] = [System.Environment]::GetEnvironmentVariable(
            $entry.Key,
            [System.EnvironmentVariableTarget]::Process
        )
        [System.Environment]::SetEnvironmentVariable(
            $entry.Key,
            [string]$entry.Value,
            [System.EnvironmentVariableTarget]::Process
        )
    }
    $process = $null
    $job = [CodexHubE2EJob]::CreateKillOnClose()
    $started = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        try {
            $process = Start-Process `
                -FilePath $powershellPath `
                -ArgumentList @($workerArguments) `
                -WorkingDirectory (Get-Location).Path `
                -WindowStyle Hidden `
                -RedirectStandardOutput $stdoutPath `
                -RedirectStandardError $stderrPath `
                -PassThru
        }
        finally {
            foreach ($entry in $previousEnvironment.GetEnumerator()) {
                [System.Environment]::SetEnvironmentVariable(
                    $entry.Key,
                    $entry.Value,
                    [System.EnvironmentVariableTarget]::Process
                )
            }
        }
        if ($job -eq [IntPtr]::Zero -or -not [CodexHubE2EJob]::Assign($job, $process.Handle)) {
            Stop-ProcessTree -ProcessId $process.Id -TimeoutMilliseconds 2000
            Write-JsonFile -Path $summaryPath -Value (Get-FailureSummaryValue -FailureClassification 'preflight_process_supervision_unavailable')
            return 1
        }
        $completed = $process.WaitForExit($OverallTimeoutSeconds * 1000)
        if (-not $completed) {
            $counts = [CodexHubE2EJob]::GetProcessCounts($job)
            [CodexHubE2EJob]::Close($job)
            $job = [IntPtr]::Zero
            [void]$process.WaitForExit(2000)
            $phase = 'preflight'
            try {
                $candidatePhase = [System.IO.File]::ReadAllText($statePath).Trim()
                if ($candidatePhase -in @('preflight', 'client_materialization', 'candidate_startup', 'manual_evidence', 'automated_cases', 'summary')) {
                    $phase = $candidatePhase
                }
            }
            catch {
                $phase = 'preflight'
            }
            $timeoutArtifact = 'runner-timeout.json'
            Write-JsonFile -Path (Join-Path $supervisorOutput $timeoutArtifact) -Value ([ordered]@{
                schema = 'codexhub.real-client-e2e-timeout.v1'
                outcome = 'failed'
                failure_classification = 'automated_outer_timeout'
                phase = $phase
                duration_ms = [Math]::Min([int]$started.ElapsedMilliseconds, $OverallTimeoutSeconds * 1000)
                total_process_count = [Math]::Min([int]$counts[0], 1000)
                active_process_count = [Math]::Min([int]$counts[1], 1000)
            })
            Write-JsonFile -Path $summaryPath -Value (Get-FailureSummaryValue -FailureClassification 'automated_outer_timeout' -Artifacts @($timeoutArtifact))
            return 1
        }
        $exitCode = $process.ExitCode
        [CodexHubE2EJob]::Close($job)
        $job = [IntPtr]::Zero
        return $exitCode
    }
    finally {
        $started.Stop()
        if ($job -ne [IntPtr]::Zero) {
            [CodexHubE2EJob]::Close($job)
        }
        if ($null -ne $process) {
            $process.Dispose()
        }
        foreach ($stream in @(
            [pscustomobject]@{ path = $stdoutPath; target = [Console]::Out },
            [pscustomobject]@{ path = $stderrPath; target = [Console]::Error }
        )) {
            try {
                $text = [System.IO.File]::ReadAllText($stream.path)
                if ($text.Length -gt $script:MaximumCapturedCharacters) {
                    $text = $text.Substring(0, $script:MaximumCapturedCharacters)
                }
                if ($text) { $stream.target.Write($text) }
            }
            catch {
                # Watchdog output is optional and never enters published evidence.
            }
            Remove-Item -LiteralPath $stream.path -Force -ErrorAction SilentlyContinue
        }
        Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
    }
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

function Get-DescendantProcessIds {
    param([int]$ProcessId)
    $searcher = $null
    $processes = @()
    try {
        $scope = [System.Management.ManagementScope]::new('\\.\root\cimv2')
        $query = [System.Management.ObjectQuery]::new('SELECT ProcessId, ParentProcessId FROM Win32_Process')
        $options = [System.Management.EnumerationOptions]::new()
        $options.Timeout = [TimeSpan]::FromMilliseconds(750)
        $searcher = [System.Management.ManagementObjectSearcher]::new($scope, $query, $options)
        $processes = @($searcher.Get())
        $descendants = [System.Collections.Generic.HashSet[int]]::new()
        [void]$descendants.Add($ProcessId)
        $changed = $true
        while ($changed) {
            $changed = $false
            foreach ($process in $processes) {
                $parentId = [int]$process.Properties['ParentProcessId'].Value
                $childId = [int]$process.Properties['ProcessId'].Value
                if ($descendants.Contains($parentId) -and $descendants.Add($childId)) {
                    $changed = $true
                }
            }
        }
        return @($descendants | Where-Object { $_ -ne $ProcessId })
    }
    catch {
        return @()
    }
    finally {
        foreach ($process in $processes) {
            $process.Dispose()
        }
        if ($null -ne $searcher) {
            $searcher.Dispose()
        }
    }
}

function Stop-ProcessTree {
    param([int]$ProcessId, [int]$TimeoutMilliseconds = 1500)
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    $killer = $null
    $descendantIds = @(Get-DescendantProcessIds -ProcessId $ProcessId)
    try {
        $taskkill = Join-Path ([System.Environment]::GetFolderPath('System')) 'taskkill.exe'
        if (Test-Path -LiteralPath $taskkill -PathType Leaf) {
            $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
            $startInfo.FileName = $taskkill
            $startInfo.Arguments = "/PID $ProcessId /T /F"
            $startInfo.UseShellExecute = $false
            $startInfo.CreateNoWindow = $true
            $startInfo.RedirectStandardOutput = $true
            $startInfo.RedirectStandardError = $true
            $killer = [System.Diagnostics.Process]::new()
            $killer.StartInfo = $startInfo
            [void]$killer.Start()
            $stdoutTask = $killer.StandardOutput.ReadToEndAsync()
            $stderrTask = $killer.StandardError.ReadToEndAsync()
            $remaining = [Math]::Max(1, [Math]::Min(500, $TimeoutMilliseconds - [int]$stopwatch.ElapsedMilliseconds))
            if (-not $killer.WaitForExit($remaining)) {
                $killer.Kill()
                [void]$killer.WaitForExit(250)
            }
            if ($stdoutTask.IsCompleted) { [void]$stdoutTask.GetAwaiter().GetResult() }
            if ($stderrTask.IsCompleted) { [void]$stderrTask.GetAwaiter().GetResult() }
        }
    }
    catch {
        # Fall through to the direct root-process kill.
    }
    finally {
        if ($null -ne $killer) {
            $killer.Dispose()
        }
    }
    foreach ($descendantId in $descendantIds) {
        if ($stopwatch.ElapsedMilliseconds -ge $TimeoutMilliseconds) {
            break
        }
        try {
            $descendant = [System.Diagnostics.Process]::GetProcessById([int]$descendantId)
            $descendant.Kill()
            $descendant.Dispose()
        }
        catch {
            # The descendant may already have exited through taskkill.
        }
    }
    try {
        $process = [System.Diagnostics.Process]::GetProcessById($ProcessId)
        $process.Kill()
        $remaining = [Math]::Max(1, $TimeoutMilliseconds - [int]$stopwatch.ElapsedMilliseconds)
        [void]$process.WaitForExit($remaining)
        $process.Dispose()
    }
    catch {
        # The process may have exited between discovery and cleanup.
    }
    finally {
        $stopwatch.Stop()
    }
}

function Stop-TrackedProcesses {
    param([object[]]$Processes, [int]$TimeoutMilliseconds = 5000)
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    foreach ($process in $Processes) {
        if ($stopwatch.ElapsedMilliseconds -ge $TimeoutMilliseconds) {
            break
        }
        try {
            $remaining = $TimeoutMilliseconds - [int]$stopwatch.ElapsedMilliseconds
            Stop-ProcessTree -ProcessId $process.Id -TimeoutMilliseconds ([Math]::Min(1500, $remaining))
        }
        catch {
            # Cleanup is best effort inside one shared bounded budget.
        }
    }
    $stopwatch.Stop()
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
            'call'
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
    $processJob = [IntPtr]::Zero
    $assignedToJob = $false
    if ($script:ProcessJobAvailable) {
        $processJob = [CodexHubE2EJob]::CreateKillOnClose()
        if ($processJob -ne [IntPtr]::Zero) {
            $assignedToJob = [CodexHubE2EJob]::Assign($processJob, $process.Handle)
        }
    }
    if ($StandardInput) {
        $process.StandardInput.Write($StandardInput)
    }
    $process.StandardInput.Close()
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $completed = $process.WaitForExit($ProcessTimeoutSeconds * 1000)
    if ($assignedToJob) {
        [CodexHubE2EJob]::Close($processJob)
        $processJob = [IntPtr]::Zero
    }
    elseif (-not $completed) {
        Stop-ProcessTree -ProcessId $process.Id
    }
    elseif ($processJob -ne [IntPtr]::Zero) {
        [CodexHubE2EJob]::Close($processJob)
        $processJob = [IntPtr]::Zero
    }
    [void]$process.WaitForExit(500)
    [void]$stdoutTask.Wait(1000)
    [void]$stderrTask.Wait(1000)
    if ((-not $stdoutTask.IsCompleted -or -not $stderrTask.IsCompleted) -and -not $assignedToJob) {
        Stop-ProcessTree -ProcessId $process.Id
        [void]$stdoutTask.Wait(500)
        [void]$stderrTask.Wait(500)
    }
    $stdout = if ($stdoutTask.Status -eq [System.Threading.Tasks.TaskStatus]::RanToCompletion) { $stdoutTask.GetAwaiter().GetResult() } else { '' }
    $stderr = if ($stderrTask.Status -eq [System.Threading.Tasks.TaskStatus]::RanToCompletion) { $stderrTask.GetAwaiter().GetResult() } else { '' }
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
    $assigned = $false
    if ($script:ProcessJobAvailable -and $script:ProcessJobHandle -ne [IntPtr]::Zero) {
        $assigned = [CodexHubE2EJob]::Assign($script:ProcessJobHandle, $process.Handle)
    }
    if (-not $assigned) {
        [void]$script:UnassignedProcessIds.Add($process.Id)
    }
    return $process
}

function Test-DebugPortableBuildResources {
    param([string]$Executable)
    $root = Split-Path -Parent $Executable
    foreach ($relative in @(
        'config\providers.toml',
        'src-python\codex_proxy.py',
        'src-python\diagnostic_recorder.py',
        'python\python.exe',
        'python\codexhub-python-runtime.json'
    )) {
        if (-not (Test-Path -LiteralPath (Join-Path $root $relative) -PathType Leaf)) {
            return $false
        }
    }
    return $true
}

function Test-LoopbackListener {
    param([int]$Port)
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connect = $client.ConnectAsync('127.0.0.1', $Port)
        return $connect.Wait(250) -and $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Test-GatewayHealth {
    param([int]$Port)
    $response = $null
    try {
        $request = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/health")
        $request.Method = 'GET'
        $request.Timeout = 500
        $request.ReadWriteTimeout = 500
        $request.KeepAlive = $false
        $response = [System.Net.HttpWebResponse]$request.GetResponse()
        if ([int]$response.StatusCode -ne 200 -or $response.ContentLength -gt 4096) {
            return $false
        }
        $reader = [System.IO.StreamReader]::new($response.GetResponseStream(), [System.Text.Encoding]::UTF8)
        try {
            $payload = $reader.ReadToEnd()
        }
        finally {
            $reader.Dispose()
        }
        if ($payload.Length -gt 4096) {
            return $false
        }
        $health = $payload | ConvertFrom-Json -ErrorAction Stop
        return [bool](Get-JsonProperty -Value $health -Name 'ok' -Default $false)
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $response) {
            $response.Dispose()
        }
    }
}

function Test-GatewayPythonProcess {
    param([int]$Port)
    $netstat = $null
    try {
        $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
        $startInfo.FileName = Join-Path ([System.Environment]::GetFolderPath('System')) 'netstat.exe'
        $startInfo.Arguments = '-ano -p tcp'
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $netstat = [System.Diagnostics.Process]::new()
        $netstat.StartInfo = $startInfo
        [void]$netstat.Start()
        $stdoutTask = $netstat.StandardOutput.ReadToEndAsync()
        $stderrTask = $netstat.StandardError.ReadToEndAsync()
        if (-not $netstat.WaitForExit(750)) {
            $netstat.Kill()
            [void]$netstat.WaitForExit(250)
            return $false
        }
        [void]$stderrTask.GetAwaiter().GetResult()
        $text = $stdoutTask.GetAwaiter().GetResult()
        $pattern = '^\s*TCP\s+127\.0\.0\.1:' + [regex]::Escape([string]$Port) + '\s+\S+\s+LISTENING\s+(\d+)\s*$'
        foreach ($line in ($text -split "`r?`n")) {
            $match = [regex]::Match($line, $pattern, [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
            if (-not $match.Success) {
                continue
            }
            $owner = [System.Diagnostics.Process]::GetProcessById([int]$match.Groups[1].Value)
            try {
                if ($owner.ProcessName -match '^pythonw?$') {
                    return $true
                }
            }
            finally {
                $owner.Dispose()
            }
        }
        return $false
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $netstat) {
            if (-not $netstat.HasExited) {
                try {
                    $netstat.Kill()
                }
                catch {
                    # The bounded ownership probe may already have exited.
                }
            }
            $netstat.Dispose()
        }
    }
}

function Write-CandidateStartupDiagnostic {
    param(
        [string]$FailureClassification,
        [int]$DurationMilliseconds,
        [bool]$PortableResourcesReady,
        [bool]$CandidateRunning,
        [bool]$PythonChildSeen,
        [bool]$ListenerSeen,
        [bool]$HealthReady,
        [bool]$DiagnosticsReady
    )
    $relative = 'candidate-startup.json'
    Write-JsonFile -Path (Join-Path $failureOutputDirectory $relative) -Value ([ordered]@{
        schema = 'codexhub.real-client-candidate-startup.v1'
        outcome = 'failed'
        failure_classification = $FailureClassification
        duration_ms = [Math]::Max(0, [Math]::Min($DurationMilliseconds, 30000))
        portable_resources_ready = $PortableResourcesReady
        candidate_running = $CandidateRunning
        python_child_seen = $PythonChildSeen
        listener_seen = $ListenerSeen
        health_ready = $HealthReady
        diagnostics_ready = $DiagnosticsReady
    })
    if (-not $script:FailureArtifacts.Contains($relative)) {
        [void]$script:FailureArtifacts.Add($relative)
    }
}

function Wait-CandidateGatewayReady {
    param(
        [System.Diagnostics.Process]$CandidateProcess,
        [int]$TimeoutMilliseconds,
        [int]$ElapsedBeforeWaitMilliseconds
    )
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    $listenerSeen = $false
    $healthReady = $false
    $pythonChildSeen = $false
    $diagnosticsReady = Test-Path -LiteralPath $script:DiagnosticsPath -PathType Leaf
    $deadlineMilliseconds = [Math]::Max(0, [Math]::Min($TimeoutMilliseconds, 30000))
    $pythonProbeReserveMilliseconds = if ($deadlineMilliseconds -ge 800) { 800 } else { 0 }
    $readinessDeadlineMilliseconds = $deadlineMilliseconds - $pythonProbeReserveMilliseconds
    while ($stopwatch.ElapsedMilliseconds -lt $readinessDeadlineMilliseconds) {
        if ($CandidateProcess.HasExited) {
            $stopwatch.Stop()
            $reportedDuration = [Math]::Min($ElapsedBeforeWaitMilliseconds + $stopwatch.ElapsedMilliseconds, $ElapsedBeforeWaitMilliseconds + $deadlineMilliseconds)
            Write-CandidateStartupDiagnostic -FailureClassification 'candidate_debug_build_exited_during_startup' -DurationMilliseconds $reportedDuration -PortableResourcesReady $true -CandidateRunning $false -PythonChildSeen $pythonChildSeen -ListenerSeen $listenerSeen -HealthReady $healthReady -DiagnosticsReady $diagnosticsReady
            throw 'candidate_debug_build_exited_during_startup'
        }
        $listenerSeen = $listenerSeen -or (Test-LoopbackListener -Port ([int]$script:GatewayConfig.listen_port))
        if ($listenerSeen) {
            if (-not $pythonChildSeen) {
                $pythonChildSeen = Test-GatewayPythonProcess -Port ([int]$script:GatewayConfig.listen_port)
            }
            $healthReady = Test-GatewayHealth -Port ([int]$script:GatewayConfig.listen_port)
        }
        if ($healthReady) {
            $diagnosticsReady = Test-Path -LiteralPath $script:DiagnosticsPath -PathType Leaf
            if ($diagnosticsReady) {
                $stopwatch.Stop()
                return
            }
        }
        Start-Sleep -Milliseconds 100
    }
    $stopwatch.Stop()
    if (-not $pythonChildSeen -and $pythonProbeReserveMilliseconds -gt 0 -and $stopwatch.ElapsedMilliseconds -lt $deadlineMilliseconds) {
        $pythonChildSeen = Test-GatewayPythonProcess -Port ([int]$script:GatewayConfig.listen_port)
    }
    $failure = if ($listenerSeen -and -not $healthReady) {
        'candidate_gateway_startup_failed_lifecycle'
    }
    elseif (-not $pythonChildSeen) {
        'candidate_gateway_startup_failed_python'
    }
    elseif (-not $listenerSeen) {
        'candidate_gateway_startup_failed_listener'
    }
    else {
        'candidate_gateway_startup_failed_diagnostics'
    }
    $reportedDuration = [Math]::Min($ElapsedBeforeWaitMilliseconds + $stopwatch.ElapsedMilliseconds, $ElapsedBeforeWaitMilliseconds + $deadlineMilliseconds)
    Write-CandidateStartupDiagnostic -FailureClassification $failure -DurationMilliseconds $reportedDuration -PortableResourcesReady $true -CandidateRunning (-not $CandidateProcess.HasExited) -PythonChildSeen $pythonChildSeen -ListenerSeen $listenerSeen -HealthReady $healthReady -DiagnosticsReady $diagnosticsReady
    throw $failure
}

function Invoke-CandidateOfficialBootstrap {
    param(
        [string]$Executable,
        [string]$CandidateRoot,
        [hashtable]$Environment,
        [int]$TimeoutSeconds
    )
    $result = Invoke-IsolatedProcess -Executable $Executable -Arguments @('refresh-models') -CaseRoot $CandidateRoot -Environment $Environment -StandardInput '' -ProcessTimeoutSeconds ([Math]::Min($TimeoutSeconds, 30))
    if ($result.timed_out) {
        Write-CandidateStartupDiagnostic -FailureClassification 'candidate_gateway_bootstrap_timeout' -DurationMilliseconds $result.duration_ms -PortableResourcesReady $true -CandidateRunning $false -PythonChildSeen $false -ListenerSeen $false -HealthReady $false -DiagnosticsReady (Test-Path -LiteralPath $script:DiagnosticsPath -PathType Leaf)
        throw 'candidate_gateway_bootstrap_timeout'
    }
    if ($result.exit_code -ne 0) {
        $boundedOutput = [string]$result.stdout + "`n" + [string]$result.stderr
        $failure = if ($boundedOutput -match '(?i)published Official catalog contains no safe resolved context budget|current Official context snapshot is unavailable') {
            'candidate_gateway_bootstrap_failed_context_budget'
        }
        else {
            'candidate_gateway_bootstrap_failed'
        }
        Write-CandidateStartupDiagnostic -FailureClassification $failure -DurationMilliseconds $result.duration_ms -PortableResourcesReady $true -CandidateRunning $false -PythonChildSeen $false -ListenerSeen $false -HealthReady $false -DiagnosticsReady (Test-Path -LiteralPath $script:DiagnosticsPath -PathType Leaf)
        throw $failure
    }
    return $result
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
    $lineIndex = 0
    $assistantMessageCount = 0
    $lastAssistantLine = -1
    $lastAssistantStopReason = ''
    $assistantErrorSeen = $false
    $agentEndCount = 0
    $lastAgentEndLine = -1
    foreach ($line in ($Text -split "`r?`n")) {
        if (-not $line.Trim()) {
            continue
        }
        $lineIndex++
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
                            $exitCode = Get-JsonProperty $item 'exit_code' $null
                            if ($command -match '(?i)(read|type|get-content|cat).+sentinel\.txt' -and
                                [string](Get-JsonProperty $item 'status' '') -ceq 'completed' -and
                                ($exitCode -is [int] -or $exitCode -is [long]) -and [int64]$exitCode -eq 0) {
                                [void]$events.Add([pscustomobject]@{ event = 'tool_call'; tool = 'read_file'; read_only = $true })
                            }
                        }
                        elseif ($itemType -eq 'agent_message') {
                            [void]$events.Add([pscustomobject]@{ event = 'assistant_output'; text = [string](Get-JsonProperty $item 'text' '') })
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
                        [void]$events.Add([pscustomobject]@{ event = 'assistant_output'; text = [string](Get-JsonProperty $part 'text' '') })
                    }
                    elseif ($type -eq 'step_finish' -and [string](Get-JsonProperty $part 'reason' '') -ceq 'stop') {
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
                            $assistantMessageCount++
                            $lastAssistantLine = $lineIndex
                            $lastAssistantStopReason = [string](Get-JsonProperty $message 'stopReason' (Get-JsonProperty $native 'stopReason' ''))
                            $errorMessage = [string](Get-JsonProperty $message 'errorMessage' (Get-JsonProperty $native 'errorMessage' ''))
                            if ($errorMessage) {
                                $assistantErrorSeen = $true
                            }
                            foreach ($content in @(Get-JsonProperty $message 'content' @())) {
                                if ([string](Get-JsonProperty $content 'type' '') -eq 'text') {
                                    [void]$events.Add([pscustomobject]@{ event = 'assistant_output'; text = [string](Get-JsonProperty $content 'text' '') })
                                }
                            }
                        }
                    }
                    elseif ($type -eq 'agent_end') {
                        $agentEndCount++
                        $lastAgentEndLine = $lineIndex
                    }
                }
            }
        }
        catch {
            $malformedCount++
        }
    }
    if ($Client -in @('pi', 'omp')) {
        $terminalClassification = if ($assistantErrorSeen) {
            'error'
        }
        elseif ($lastAssistantStopReason -ceq 'stop') {
            'completed'
        }
        elseif ($lastAssistantStopReason -in @('error', 'aborted', 'length')) {
            $lastAssistantStopReason
        }
        else {
            'unclassified'
        }
        for ($index = 0; $index -lt $agentEndCount; $index++) {
            [void]$events.Add([pscustomobject]@{ event = 'terminal'; classification = $terminalClassification })
        }
        $validTerminal = $assistantMessageCount -gt 0 -and
            $agentEndCount -eq 1 -and
            $lastAgentEndLine -gt $lastAssistantLine -and
            $lastAssistantStopReason -ceq 'stop' -and
            -not $assistantErrorSeen
        if (-not $validTerminal) {
            [void]$events.Add([pscustomobject]@{ event = 'error' })
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
        return -not (Test-CanonicalModelMatch -Actual $actualModel -Expected $Case.gateway_model)
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
        $isStream = Get-JsonProperty $complete 'is_stream' $null
        if ($isStream -is [bool] -and $isStream -eq $true) {
            [void]$events.Add([pscustomobject]@{ event = 'gateway_streaming_complete' })
        }
        else {
            [void]$events.Add([pscustomobject]@{ event = 'error' })
        }
    }
    if ($completes.Count -gt 0) {
        $native = $completes[-1]
        $actualModel = [string](Get-JsonProperty $native 'model_canonical' (Get-JsonProperty $native 'model' ''))
        [void]$events.Add([pscustomobject]@{ event = 'model_selected'; model = $actualModel })
        [void]$events.Add([pscustomobject]@{
            event = 'request_complete'
            status = [int](Get-JsonProperty $native 'status' 0)
            is_stream = Get-JsonProperty $native 'is_stream' $null
        })
    }
    for ($nativeIndex = 0; $nativeIndex -lt $nativeEvents.Count; $nativeIndex++) {
        $native = $nativeEvents[$nativeIndex]
        $event = [string](Get-JsonProperty $native 'event' '')
        if ($event -eq 'request_error') {
            $status = [int](Get-JsonProperty $native 'status' 0)
            [void]$events.Add([pscustomobject]@{ event = 'error' })
            if ($status -in @(429, 503)) {
                $priorCompleteCount = if ($nativeIndex -gt 0) {
                    @($nativeEvents[0..($nativeIndex - 1)] | Where-Object {
                        [string](Get-JsonProperty $_ 'event' '') -eq 'request_complete'
                    }).Count
                }
                else { 0 }
                [void]$events.Add([pscustomobject]@{
                    event = 'provider_capacity'
                    status = $status
                    output_seen = $priorCompleteCount -gt 0
                })
            }
        }
        elseif ($event -eq 'upstream_protocol_fallback') {
            [void]$events.Add([pscustomobject]@{ event = 'fallback' })
        }
    }
    return @($events)
}

function Invoke-ClientAttempt {
    param([pscustomobject]$Case, [string]$Executable, [string]$CaseRoot, [int]$Attempt, [string]$LaunchModel)
    $sentinel = "SENTINEL:codexhub-real-client-e2e:$($Case.case_id)"
    $sentinelPath = Join-Path $CaseRoot 'sentinel.txt'
    [System.IO.File]::WriteAllText($sentinelPath, $sentinel, $script:Utf8NoBom)
    $prompt = "Read the sentinel file once with one read-only tool call, then stream exactly $sentinel and stop."
    $arguments = @(Get-ClientArguments -Client $Case.client -Model $LaunchModel -WorkRoot $CaseRoot -Prompt $prompt)
    $environment = @{
        CODEXHUB_E2E_CASE = $Case.case_id
        CODEXHUB_E2E_CLIENT = $Case.client
        CODEXHUB_E2E_MODEL = $LaunchModel
        CODEXHUB_E2E_GATEWAY_MODEL = $Case.gateway_model
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
    $outputEvents = @($events | Where-Object { (Get-JsonProperty $_ 'event') -eq 'assistant_output' })
    $sentinelEvents = @($outputEvents | Where-Object { (Get-JsonProperty $_ 'text') -ceq $sentinel })
    $requestEvents = @($events | Where-Object { (Get-JsonProperty $_ 'event') -eq 'request_complete' })
    $terminalEvents = @($events | Where-Object { (Get-JsonProperty $_ 'event') -eq 'terminal' })
    $gatewayRequestEvents = @($events | Where-Object { (Get-JsonProperty $_ 'event') -eq 'gateway_request' })
    $gatewayCompleteEvents = @($events | Where-Object { (Get-JsonProperty $_ 'event') -eq 'gateway_complete' })
    $gatewayStreamingCompleteEvents = @($events | Where-Object { (Get-JsonProperty $_ 'event') -eq 'gateway_streaming_complete' })
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
    $capacityOutputSeen = if ($capacityEvents.Count -eq 1) {
        [bool](Get-JsonProperty $capacityEvents[0] 'output_seen' $true) -or
            $Attempt.malformed_count -gt 0 -or
            $allToolEvents.Count -gt 0 -or
            $outputEvents.Count -gt 0 -or
            $terminalEvents.Count -gt 0 -or
            $gatewayCompleteEvents.Count -gt 0 -or
            $requestEvents.Count -gt 0
    }
    else { $true }
    $retryableCapacity = $Attempt.process.exit_code -ne 0 -and
        -not $Attempt.process.timed_out -and
        $capacityEvents.Count -eq 1 -and
        $capacityStatus -in @(429, 503) -and
        -not $capacityOutputSeen -and
        $errorEvents.Count -eq 1 -and
        $fallbackEvents.Count -eq 0 -and
        $reconnectEvents.Count -eq 0
    $passed = -not $Attempt.process.timed_out -and
        $Attempt.process.exit_code -eq 0 -and
        $Attempt.malformed_count -eq 0 -and
        $modelEvents.Count -eq 1 -and
        (Get-JsonProperty $modelEvents[0] 'model') -ceq $Case.gateway_model -and
        $allToolEvents.Count -eq 1 -and $toolEvents.Count -eq 1 -and
        $gatewayRequestEvents.Count -eq ($allToolEvents.Count + 1) -and
        $gatewayCompleteEvents.Count -eq ($allToolEvents.Count + 1) -and
        $outputEvents.Count -eq 1 -and $sentinelEvents.Count -eq 1 -and
        $requestEvents.Count -eq 1 -and
        $httpStatus -eq 200 -and
        (Get-JsonProperty $requestEvents[0] 'is_stream' $false) -eq $true -and
        $gatewayStreamingCompleteEvents.Count -eq $gatewayCompleteEvents.Count -and
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
        streaming_request_count = $gatewayStreamingCompleteEvents.Count
        fallback_count = $fallbackEvents.Count
        error_event_count = $errorEvents.Count
        duplicate_terminal_count = [Math]::Max(0, $terminalEvents.Count - 1)
        gateway_request_count = $gatewayRequestEvents.Count
        gateway_complete_count = $gatewayCompleteEvents.Count
    }
}

function Invoke-AutomatedCase {
    param(
        [pscustomobject]$Case,
        [string]$Executable,
        [string]$ArtifactRoot,
        [string]$WorkRoot,
        [pscustomobject]$Configuration
    )
    $caseWorkRoot = Join-Path $WorkRoot $Case.case_id
    if (-not (Test-Path -LiteralPath $caseWorkRoot -PathType Container) -or $null -eq $Configuration) {
        throw 'client_configuration_materializer_contradiction'
    }
    $attempt = Invoke-ClientAttempt -Case $Case -Executable $Executable -CaseRoot $caseWorkRoot -Attempt 1 -LaunchModel $Configuration.launch_model
    $measurement = Measure-AutomatedAttempt -Attempt $attempt -Case $Case
    $retryClassification = 'not_needed'
    $duration = $attempt.process.duration_ms
    if (-not $measurement.passed -and $measurement.retryable_capacity) {
        $retryClassification = "capacity_$($measurement.capacity_status)_pre_output_retried"
        $attempt = Invoke-ClientAttempt -Case $Case -Executable $Executable -CaseRoot $caseWorkRoot -Attempt 2 -LaunchModel $Configuration.launch_model
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
        streaming_request_count = $measurement.streaming_request_count
        fallback_count = $measurement.fallback_count
        error_event_count = $measurement.error_event_count
        duplicate_terminal_count = $measurement.duplicate_terminal_count
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
    if (($topLevelNames -join ',') -cne 'candidate_sha,cases,gui_confirmed,login_confirmed,managed_client_config_sha,run_binding_sha256,schema') {
        throw 'manual_evidence_schema_invalid'
    }
    if ((Get-JsonProperty $evidence 'schema') -cne 'codexhub.real-client-manual-evidence.v2') {
        throw 'manual_evidence_schema_invalid'
    }
    if ((Get-JsonProperty $evidence 'candidate_sha') -cne $CandidateSha) {
        throw 'manual_evidence_candidate_sha_stale'
    }
    if ((Get-JsonProperty $evidence 'managed_client_config_sha') -cne $ManagedClientConfigSha) {
        throw 'manual_evidence_materializer_sha_stale'
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
            'streaming_request_count',
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
            [int](Get-JsonProperty $item 'streaming_request_count' 0) -eq 2 -and
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
            streaming_request_count = 2
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
            streaming_request_count = 0
            fallback_count = 0
            duplicate_terminal_count = 0
        }
    })
    Write-JsonFile -Path $Path -Value ([ordered]@{
        schema = 'codexhub.real-client-manual-evidence.v2'
        candidate_sha = $CandidateSha
        managed_client_config_sha = $ManagedClientConfigSha
        run_binding_sha256 = $RunBinding
        login_confirmed = $false
        gui_confirmed = $false
        cases = $cases
    })
}

function Test-ExecutableBoundToInstallLocation {
    param([string]$Executable, [string]$InstallLocation)
    if (-not $InstallLocation -or -not (Test-Path -LiteralPath $InstallLocation -PathType Container)) {
        return $false
    }
    $executablePath = [System.IO.Path]::GetFullPath($Executable)
    $installPrefix = [System.IO.Path]::GetFullPath($InstallLocation).TrimEnd('\') + '\'
    return $executablePath.StartsWith($installPrefix, [System.StringComparison]::OrdinalIgnoreCase)
}

function Test-StableVersionAtLeast {
    param([string]$Actual, [string]$Minimum, [int]$ComponentCount)
    $componentPattern = if ($ComponentCount -eq 4) {
        '^\d+\.\d+\.\d+\.\d+$'
    } else {
        '^\d+\.\d+\.\d+$'
    }
    if ($Actual -notmatch $componentPattern -or $Minimum -notmatch $componentPattern) {
        return $false
    }
    try {
        return ([version]$Actual).CompareTo([version]$Minimum) -ge 0
    }
    catch {
        return $false
    }
}

function Test-AbsoluteWindowsPath {
    param([string]$Path)
    return $Path -match '^(?:[A-Za-z]:\\|\\\\[^\\]+\\[^\\]+\\)'
}

function Get-CanonicalZCodeRootFromFilePath {
    param([string]$Path)
    if (-not (Test-AbsoluteWindowsPath -Path $Path) -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'preflight_zcode_install_metadata_invalid'
    }
    $parent = Split-Path -Parent $Path
    if (-not $parent -or -not (Test-Path -LiteralPath $parent -PathType Container)) {
        throw 'preflight_zcode_install_metadata_invalid'
    }
    return (Resolve-Path -LiteralPath $parent).Path.TrimEnd('\')
}

function Resolve-ZCodeInstallRoot {
    param([object]$Metadata, [string]$MinimumVersion, [string]$Executable)
    $displayName = ([string]$Metadata.display_name).Trim()
    $displayVersion = ([string]$Metadata.display_version).Trim()
    $publisher = ([string]$Metadata.publisher).Trim()
    if (-not (Test-StableVersionAtLeast -Actual $displayVersion -Minimum $MinimumVersion -ComponentCount 3) -or
        $publisher -cne 'ZCode' -or
        ($displayName -cne 'ZCode' -and $displayName -cne "ZCode $displayVersion")) {
        throw 'preflight_zcode_version_mismatch'
    }

    $roots = [System.Collections.Generic.List[string]]::new()
    $installLocation = ([string]$Metadata.install_location).Trim()
    if ($installLocation) {
        if (-not (Test-AbsoluteWindowsPath -Path $installLocation) -or
            -not (Test-Path -LiteralPath $installLocation -PathType Container)) {
            throw 'preflight_zcode_install_metadata_invalid'
        }
        [void]$roots.Add((Resolve-Path -LiteralPath $installLocation).Path.TrimEnd('\'))
    }

    $displayIcon = ([string]$Metadata.display_icon).Trim()
    if ($displayIcon) {
        $iconMatch = [regex]::Match($displayIcon, '^(?:"([^"]+)"|([^,]+?))(?:,\s*-?\d+)?$')
        if (-not $iconMatch.Success) {
            throw 'preflight_zcode_install_metadata_invalid'
        }
        $iconPath = if ($iconMatch.Groups[1].Success) { $iconMatch.Groups[1].Value } else { $iconMatch.Groups[2].Value.Trim() }
        [void]$roots.Add((Get-CanonicalZCodeRootFromFilePath -Path $iconPath))
    }

    $uninstallString = ([string]$Metadata.uninstall_string).Trim()
    if ($uninstallString) {
        $uninstallMatch = [regex]::Match($uninstallString, '^"([^"]+)"(?:\s+.*)?$')
        if (-not $uninstallMatch.Success) {
            throw 'preflight_zcode_install_metadata_invalid'
        }
        [void]$roots.Add((Get-CanonicalZCodeRootFromFilePath -Path $uninstallMatch.Groups[1].Value))
    }

    if ($roots.Count -eq 0) {
        throw 'preflight_zcode_install_metadata_invalid'
    }
    $distinctRoots = @($roots | Select-Object -Unique)
    if ($distinctRoots.Count -ne 1) {
        throw 'preflight_zcode_install_metadata_conflict'
    }
    $root = $distinctRoots[0]
    if (-not (Test-ExecutableBoundToInstallLocation -Executable $Executable -InstallLocation $root)) {
        throw 'preflight_zcode_executable_unbound'
    }
    return $root
}

function Get-DesktopInstallationMetadata {
    param([string]$Executable)
    if ($script:WindowsInstallMetadata) {
        return $script:WindowsInstallMetadata.desktop
    }
    $packages = @(Get-AppxPackage -Name 'OpenAI.Codex' -ErrorAction SilentlyContinue)
    $bound = @($packages | Where-Object {
        Test-ExecutableBoundToInstallLocation -Executable $Executable -InstallLocation ([string]$_.InstallLocation)
    })
    if ($bound.Count -ne 1) {
        throw 'preflight_desktop_executable_unbound'
    }
    $package = $bound[0]
    return [pscustomobject]@{
        package_name = [string]$package.Name
        package_version = [string]$package.Version
        install_location = [string]$package.InstallLocation
    }
}

function Get-ZCodeInstallationMetadata {
    param([string]$Executable)
    if ($script:WindowsInstallMetadata) {
        $entry = $script:WindowsInstallMetadata.zcode
    }
    else {
        $entries = [System.Collections.Generic.List[object]]::new()
        foreach ($root in @(
            'Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
            'Registry::HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'
        )) {
            if (-not (Test-Path -LiteralPath $root)) {
                continue
            }
            foreach ($key in @(Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue)) {
                try {
                    $candidate = Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction Stop
                    if ([string]$candidate.DisplayName -cmatch '^ZCode(?:\s|$)') {
                        [void]$entries.Add($candidate)
                    }
                }
                catch {
                    # Unreadable uninstall entries are not authoritative matches.
                }
            }
        }
        if ($entries.Count -eq 0) {
            throw 'preflight_zcode_install_metadata_invalid'
        }
        if ($entries.Count -ne 1) {
            throw 'preflight_zcode_install_metadata_ambiguous'
        }
        $entry = $entries[0]
    }
    return [pscustomobject]@{
        display_name = [string](Get-JsonProperty -Value $entry -Name 'DisplayName' -Default '')
        display_version = [string](Get-JsonProperty -Value $entry -Name 'DisplayVersion' -Default '')
        publisher = [string](Get-JsonProperty -Value $entry -Name 'Publisher' -Default '')
        install_location = [string](Get-JsonProperty -Value $entry -Name 'InstallLocation' -Default '')
        display_icon = [string](Get-JsonProperty -Value $entry -Name 'DisplayIcon' -Default '')
        uninstall_string = [string](Get-JsonProperty -Value $entry -Name 'UninstallString' -Default '')
        executable_product_version = if ($script:WindowsInstallMetadata) {
            [string](Get-JsonProperty -Value $entry -Name 'ExecutableProductVersion' -Default '')
        } else {
            [string][System.Diagnostics.FileVersionInfo]::GetVersionInfo($Executable).ProductVersion
        }
    }
}

function Get-NativeClientVersion {
    param([string]$Client, [string]$Executable, [string]$Minimum, [string]$ProbeRoot)
    $failureClient = $Client.Replace('-', '_')
    if ($Client -ceq 'desktop') {
        $metadata = Get-DesktopInstallationMetadata -Executable $Executable
        $packageVersion = ([string]$metadata.package_version).Trim()
        if ([string]$metadata.package_name -cne 'OpenAI.Codex' -or
            -not (Test-StableVersionAtLeast -Actual $packageVersion -Minimum $Minimum -ComponentCount 4)) {
            throw 'preflight_desktop_version_mismatch'
        }
        if (-not (Test-ExecutableBoundToInstallLocation -Executable $Executable -InstallLocation ([string]$metadata.install_location))) {
            throw 'preflight_desktop_executable_unbound'
        }
        return $packageVersion
    }
    if ($Client -ceq 'zcode') {
        $metadata = Get-ZCodeInstallationMetadata -Executable $Executable
        $displayVersion = ([string]$metadata.display_version).Trim()
        $productVersion = ([string]$metadata.executable_product_version).Trim()
        if ($productVersion -notmatch ('^' + [regex]::Escape($displayVersion) + '(?:\.\d+)?$')) {
            throw 'preflight_zcode_version_mismatch'
        }
        [void](Resolve-ZCodeInstallRoot -Metadata $metadata -MinimumVersion $Minimum -Executable $Executable)
        return $displayVersion
    }
    [void](New-Item -ItemType Directory -Force -Path $ProbeRoot)
    $result = Invoke-IsolatedProcess -Executable $Executable -Arguments @('--version') -CaseRoot $ProbeRoot -Environment @{
        CODEXHUB_E2E_VERSION_PROBE = '1'
        CODEXHUB_E2E_MINIMUM_VERSION = $Minimum
        CODEXHUB_E2E_CLIENT = $Client
    } -StandardInput '' -ProcessTimeoutSeconds 15
    $text = ($result.stdout + "`n" + $result.stderr).Trim()
    $versionMatches = @([regex]::Matches(
        $text,
        '(?<![0-9A-Za-z.])v?([0-9]+\.[0-9]+\.[0-9]+)(?![0-9A-Za-z.+-])'
    ))
    $normalizedVersion = if ($versionMatches.Count -eq 1) {
        [string]$versionMatches[0].Groups[1].Value
    }
    else { '' }
    if ($result.timed_out -or $result.exit_code -ne 0 -or
        -not (Test-StableVersionAtLeast -Actual $normalizedVersion -Minimum $Minimum -ComponentCount 3)) {
        throw "preflight_${failureClient}_version_mismatch"
    }
    return $normalizedVersion
}

function Assert-ManagedClientOutputKeys {
    param(
        [object]$Value,
        [string[]]$Required,
        [string[]]$Optional = @()
    )
    $actual = @($Value.PSObject.Properties.Name | Sort-Object)
    $allowed = @($Required + $Optional | Sort-Object -Unique)
    $missing = @($Required | Where-Object { $actual -notcontains $_ })
    $unknown = @($actual | Where-Object { $allowed -notcontains $_ })
    if ($missing.Count -gt 0 -or $unknown.Count -gt 0) {
        throw 'client_configuration_materializer_output_invalid'
    }
    foreach ($name in $Optional) {
        $property = $Value.PSObject.Properties[$name]
        if ($null -eq $property -or $null -eq $property.Value) {
            continue
        }
        if ($property.Value -isnot [string] -or
            $property.Value.Length -gt 512 -or
            $property.Value -match '(?i)(authorization|access_token|refresh_token|api[_-]?key|bearer\s)' -or
            $property.Value -match '(?i)(?:[a-z]:\\|\\\\[^\\]+\\[^\\]+\\)') {
            throw 'client_configuration_materializer_output_invalid'
        }
    }
}

function Invoke-ManagedClientConfigVerb {
    param(
        [string]$Verb,
        [string]$Client,
        [string]$Root,
        [string]$Model,
        [string]$SettingsPath = $script:ManagedClientSettingsPath,
        [string]$ProvidersPath = $script:ManagedClientProvidersPath,
        [string]$ProcessRoot,
        [string]$CatalogPath = ''
    )
    $arguments = @(
        'managed-client-config', $Verb,
        '--client', $Client,
        '--root', $Root,
        '--model', $Model,
        '--settings-path', $SettingsPath,
        '--providers-path', $ProvidersPath
    )
    if ($CatalogPath) {
        $arguments += @('--catalog-path', $CatalogPath)
    }
    $result = Invoke-IsolatedProcess -Executable $script:ManagedClientConfigBuild -Arguments $arguments -CaseRoot $ProcessRoot -Environment @{
        CODEXHUB_E2E_MATERIALIZER_LOG = $script:ManagedClientConfigLogPath
    } -StandardInput '' -ProcessTimeoutSeconds 30
    if ($result.timed_out) {
        throw 'client_configuration_materializer_timeout'
    }
    if ($result.exit_code -ne 0) {
        throw 'client_configuration_materializer_failed'
    }
    if ($result.stdout.Length -gt 8192 -or
        $result.stdout -match '(?i)(authorization|access_token|refresh_token|api[_-]?key|bearer\s)' -or
        $result.stdout -match '(?i)(?:[a-z]:\\|\\\\[^\\]+\\[^\\]+\\)') {
        throw 'client_configuration_materializer_output_invalid'
    }
    try {
        return $result.stdout | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw 'client_configuration_materializer_output_invalid'
    }
}

function Get-ManagedTargetSource {
    param([string]$ApplyRoot, [string]$RelativeName)
    if (-not $RelativeName -or [System.IO.Path]::IsPathRooted($RelativeName)) {
        throw 'client_configuration_materializer_output_invalid'
    }
    $normalizedName = $RelativeName -replace '/', '\'
    $segments = @($normalizedName -split '\\')
    $invalidCharacters = [System.IO.Path]::GetInvalidFileNameChars()
    if ($segments.Count -eq 0 -or @($segments | Where-Object {
        -not $_ -or $_ -in @('.', '..') -or $_.EndsWith('.') -or $_.EndsWith(' ') -or
        $_.IndexOfAny($invalidCharacters) -ge 0
    }).Count -ne 0) {
        throw 'client_configuration_materializer_output_invalid'
    }
    try {
        Assert-CanonicalNonReparseDirectory -Path $ApplyRoot -Failure 'client_configuration_materializer_output_invalid'
        $rootPath = [System.IO.Path]::GetFullPath($ApplyRoot).TrimEnd('\')
        $resolvedRoot = (Resolve-Path -LiteralPath $ApplyRoot -ErrorAction Stop).Path.TrimEnd('\')
    }
    catch {
        throw 'client_configuration_materializer_output_invalid'
    }
    if (-not $rootPath.Equals($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'client_configuration_materializer_output_invalid'
    }
    $rootPrefix = $resolvedRoot + '\'
    $exactPath = [System.IO.Path]::GetFullPath((Join-Path $resolvedRoot $normalizedName))
    if (-not $exactPath.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'client_configuration_materializer_output_invalid'
    }
    if (Test-Path -LiteralPath $exactPath -PathType Leaf) {
        try {
            $resolvedExact = (Resolve-Path -LiteralPath $exactPath -ErrorAction Stop).Path
            if (-not $resolvedExact.Equals($exactPath, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw 'client_configuration_materializer_output_invalid'
            }
            Assert-IsolatedRegularFile -Path $resolvedExact -IsolationRoot $resolvedRoot
            return $resolvedExact
        }
        catch {
            throw 'client_configuration_materializer_output_invalid'
        }
    }
    if ($segments.Count -ne 1) {
        throw 'client_configuration_materializer_output_invalid'
    }

    $maximumFiles = 64
    $maximumDirectories = 64
    $fileCount = 0
    $directoryCount = 0
    $matches = [System.Collections.Generic.List[string]]::new()
    $pending = [System.Collections.Generic.Queue[string]]::new()
    $pending.Enqueue($resolvedRoot)
    try {
        while ($pending.Count -gt 0) {
            $directoryPath = $pending.Dequeue()
            foreach ($item in @(Get-ChildItem -LiteralPath $directoryPath -Force -ErrorAction Stop)) {
                $itemLinkType = if ($item.PSObject.Properties['LinkType']) { [string]$item.LinkType } else { '' }
                if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or $itemLinkType) {
                    throw 'client_configuration_materializer_output_invalid'
                }
                $itemPath = [System.IO.Path]::GetFullPath($item.FullName)
                if (-not $itemPath.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                    throw 'client_configuration_materializer_output_invalid'
                }
                $resolvedItem = (Resolve-Path -LiteralPath $itemPath -ErrorAction Stop).Path
                if (-not $resolvedItem.Equals($itemPath, [System.StringComparison]::OrdinalIgnoreCase)) {
                    throw 'client_configuration_materializer_output_invalid'
                }
                if ($item.PSIsContainer) {
                    $directoryCount += 1
                    if ($directoryCount -gt $maximumDirectories) {
                        throw 'client_configuration_materializer_output_invalid'
                    }
                    $pending.Enqueue($resolvedItem)
                    continue
                }
                $fileCount += 1
                if ($fileCount -gt $maximumFiles) {
                    throw 'client_configuration_materializer_output_invalid'
                }
                Assert-IsolatedRegularFile -Path $resolvedItem -IsolationRoot $resolvedRoot
                if ($item.Name.Equals($RelativeName, [System.StringComparison]::OrdinalIgnoreCase)) {
                    $matches.Add($resolvedItem)
                }
            }
        }
    }
    catch {
        throw 'client_configuration_materializer_output_invalid'
    }
    if ($matches.Count -ne 1) {
        throw 'client_configuration_materializer_output_invalid'
    }
    return $matches[0]
}

function Publish-ManagedClientTargets {
    param([string]$Client, [string]$ApplyRoot, [string[]]$TargetNames, [string]$CaseRoot)
    $destinations = switch ($Client) {
        'codex' { @{ 'config.toml' = '.codex\config.toml' } }
        'opencode' { @{ 'opencode.json' = '.config\opencode\opencode.json' } }
        'pi' { @{ 'settings.json' = '.pi\agent\settings.json'; 'models.json' = '.pi\agent\models.json' } }
        'omp' { @{ 'config.yml' = '.omp\agent\config.yml'; 'models.yml' = '.omp\agent\models.yml' } }
        'zcode' { @{
            'codexhub.json' = 'appdata\roaming\ZCode\model-providers\codexhub.json'
            'config.json' = '.zcode\v2\config.json'
            'bots-model-cache.v2.json' = '.zcode\v2\bots-model-cache.v2.json'
        } }
        default { throw 'client_configuration_materializer_output_invalid' }
    }
    if ($TargetNames.Count -ne $destinations.Count) {
        throw 'client_configuration_materializer_contradiction'
    }
    $published = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($relativeName in $TargetNames) {
        $leaf = [System.IO.Path]::GetFileName(($relativeName -replace '/', '\'))
        if (-not $destinations.ContainsKey($leaf) -or -not $published.Add($leaf)) {
            throw 'client_configuration_materializer_contradiction'
        }
        $source = Get-ManagedTargetSource -ApplyRoot $ApplyRoot -RelativeName $relativeName
        $destination = Join-Path $CaseRoot $destinations[$leaf]
        [void](New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination))
        Copy-Item -LiteralPath $source -Destination $destination -Force
        if ((Get-Sha256 -Path $source) -cne (Get-Sha256 -Path $destination)) {
            throw 'client_configuration_materializer_contradiction'
        }
    }
    if ($Client -ceq 'codex') {
        Copy-Item -LiteralPath $script:AccountAuthPath -Destination (Join-Path $CaseRoot '.codex\auth.json') -Force
    }
}

function Initialize-ClientConfiguration {
    param(
        [string]$Client,
        [string]$CaseRoot,
        [string]$Model,
        [string]$SettingsPath = $script:ManagedClientSettingsPath,
        [string]$ProvidersPath = $script:ManagedClientProvidersPath,
        [string]$CatalogPath = ''
    )
    $managedClient = if ($Client -in @('desktop', 'codex-cli')) { 'codex' } else { $Client }
    $previewRoot = Join-Path $CaseRoot 'managed-preview'
    $applyRoot = Join-Path $CaseRoot 'managed-apply'
    if ((Test-Path -LiteralPath $previewRoot) -or (Test-Path -LiteralPath $applyRoot)) {
        throw 'client_configuration_materializer_root_not_fresh'
    }
    $preview = Invoke-ManagedClientConfigVerb -Verb 'preview' -Client $managedClient -Root $previewRoot -Model $Model -SettingsPath $SettingsPath -ProvidersPath $ProvidersPath -ProcessRoot $CaseRoot -CatalogPath $CatalogPath
    if ($managedClient -ceq 'codex') {
        Assert-ManagedClientOutputKeys -Value $preview -Required @('client_id', 'selector', 'model', 'route_protocol', 'target_names', 'overlay_args_relative')
    }
    else {
        Assert-ManagedClientOutputKeys -Value $preview -Required @('client_id', 'selector', 'model', 'route_protocol', 'target_names', 'next_redacted')
    }
    $apply = Invoke-ManagedClientConfigVerb -Verb 'apply' -Client $managedClient -Root $applyRoot -Model $Model -SettingsPath $SettingsPath -ProvidersPath $ProvidersPath -ProcessRoot $CaseRoot -CatalogPath $CatalogPath
    if ($managedClient -ceq 'codex') {
        Assert-ManagedClientOutputKeys -Value $apply -Required @('mode', 'proxy_running', 'proxy_port', 'proxy_build', 'message', 'gateway_lifecycle') -Optional @('history_sync_status', 'history_sync_message')
        if ([string](Get-JsonProperty $apply 'mode' '') -cne 'custom') {
            throw 'client_configuration_materializer_contradiction'
        }
    }
    else {
        Assert-ManagedClientOutputKeys -Value $apply -Required @('client_id', 'applied', 'selector', 'model', 'route_protocol', 'target_names', 'backup_dir_relative')
        if ((Get-JsonProperty $apply 'applied' $false) -ne $true) {
            throw 'client_configuration_materializer_contradiction'
        }
    }
    $readback = Invoke-ManagedClientConfigVerb -Verb 'readback' -Client $managedClient -Root $applyRoot -Model $Model -SettingsPath $SettingsPath -ProvidersPath $ProvidersPath -ProcessRoot $CaseRoot -CatalogPath $CatalogPath
    Assert-ManagedClientOutputKeys -Value $readback -Required @('client_id', 'ok', 'selector', 'model', 'route_protocol')
    if ((Get-JsonProperty $readback 'ok' $false) -ne $true -or
        [string](Get-JsonProperty $preview 'client_id' '') -cne $managedClient -or
        [string](Get-JsonProperty $readback 'client_id' '') -cne $managedClient -or
        [string](Get-JsonProperty $preview 'selector' '') -cne [string](Get-JsonProperty $readback 'selector' '') -or
        -not [string](Get-JsonProperty $readback 'selector' '') -or
        [string](Get-JsonProperty $preview 'model' '') -cne $Model -or
        [string](Get-JsonProperty $readback 'model' '') -cne $Model -or
        [string](Get-JsonProperty $preview 'route_protocol' '') -cne [string](Get-JsonProperty $readback 'route_protocol' '') -or
        -not [string](Get-JsonProperty $readback 'route_protocol' '')) {
        throw 'client_configuration_materializer_contradiction'
    }
    if ($managedClient -cne 'codex' -and (
        [string](Get-JsonProperty $apply 'selector' '') -cne [string](Get-JsonProperty $readback 'selector' '') -or
        [string](Get-JsonProperty $apply 'model' '') -cne $Model -or
        [string](Get-JsonProperty $apply 'route_protocol' '') -cne [string](Get-JsonProperty $readback 'route_protocol' '') -or
        (@(Get-JsonProperty $apply 'target_names' @()) -join ',') -cne (@(Get-JsonProperty $preview 'target_names' @()) -join ','))) {
        throw 'client_configuration_materializer_contradiction'
    }
    $targetNames = @((Get-JsonProperty $preview 'target_names' @()) | ForEach-Object { [string]$_ })
    Publish-ManagedClientTargets -Client $managedClient -ApplyRoot $applyRoot -TargetNames $targetNames -CaseRoot $CaseRoot
    return [pscustomobject]@{
        selector = [string](Get-JsonProperty $readback 'selector' '')
        canonical_model = [string](Get-JsonProperty $readback 'model' '')
        route_protocol = [string](Get-JsonProperty $readback 'route_protocol' '')
        launch_model = if ($managedClient -ceq 'codex') { [string](Get-JsonProperty $readback 'model' '') } else { [string](Get-JsonProperty $readback 'selector' '') }
    }
}

function Initialize-CandidateRuntime {
    param([string]$CandidateRoot)
    $script:CandidateRuntimeRoot = Join-Path $CandidateRoot 'runtime'
    $script:CandidateCodexRoot = Join-Path $CandidateRoot '.codex'
    $proxyRoot = Join-Path $script:CandidateRuntimeRoot 'proxy'
    [void](New-Item -ItemType Directory -Force -Path (Join-Path $proxyRoot 'config'))
    [void](New-Item -ItemType Directory -Force -Path $script:CandidateCodexRoot)
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
    $script:ManagedClientSettingsPath = Join-Path $proxyRoot 'settings.json'
    $script:ManagedClientProvidersPath = Join-Path $proxyRoot 'config\providers.toml'
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

$isInternalWorker = $InternalSupervisorToken -and
    $env:CODEXHUB_E2E_SUPERVISOR_TOKEN -and
    $InternalSupervisorToken -ceq $env:CODEXHUB_E2E_SUPERVISOR_TOKEN
if (-not $isInternalWorker) {
    if ($OverallTimeoutSeconds -lt 1 -or $OverallTimeoutSeconds -gt 7200) {
        $invalidOutput = [System.IO.Path]::GetFullPath($OutputDirectory)
        [void](New-Item -ItemType Directory -Force -Path $invalidOutput)
        Write-JsonFile -Path (Join-Path $invalidOutput 'summary.json') -Value (Get-FailureSummaryValue -FailureClassification 'preflight_timeout_invalid')
        exit 1
    }
    exit (Invoke-RunnerSupervisor)
}

try {
    $forwardedJson = [System.Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String([string]$env:CODEXHUB_E2E_SUPERVISOR_ARGUMENTS)
    )
    $forwardedArguments = $forwardedJson | ConvertFrom-Json -ErrorAction Stop
    Assert-ExactJsonProperties -Value $forwardedArguments -Names @(
        'CandidateSha', 'DebugBuild', 'ManagedClientConfigBuild',
        'ManagedClientConfigSha', 'LunaModel', 'VolcModel', 'OutputDirectory',
        'HostEnvironmentManifest', 'TestWindowsInstallMetadataFixture',
        'CodexDesktopPath', 'CodexCliPath', 'ZCodePath', 'OpenCodePath',
        'PiPath', 'OmpPath', 'TimeoutSeconds', 'ManualEvidenceTimeoutSeconds',
        'OverallTimeoutSeconds'
    ) -Failure 'preflight_supervisor_arguments_invalid'
    $CandidateSha = [string]$forwardedArguments.CandidateSha
    $DebugBuild = [string]$forwardedArguments.DebugBuild
    $ManagedClientConfigBuild = [string]$forwardedArguments.ManagedClientConfigBuild
    $ManagedClientConfigSha = [string]$forwardedArguments.ManagedClientConfigSha
    $LunaModel = [string]$forwardedArguments.LunaModel
    $VolcModel = [string]$forwardedArguments.VolcModel
    $OutputDirectory = [string]$forwardedArguments.OutputDirectory
    $HostEnvironmentManifest = [string]$forwardedArguments.HostEnvironmentManifest
    $TestWindowsInstallMetadataFixture = [string]$forwardedArguments.TestWindowsInstallMetadataFixture
    $CodexDesktopPath = [string]$forwardedArguments.CodexDesktopPath
    $CodexCliPath = [string]$forwardedArguments.CodexCliPath
    $ZCodePath = [string]$forwardedArguments.ZCodePath
    $OpenCodePath = [string]$forwardedArguments.OpenCodePath
    $PiPath = [string]$forwardedArguments.PiPath
    $OmpPath = [string]$forwardedArguments.OmpPath
    $TimeoutSeconds = [int]$forwardedArguments.TimeoutSeconds
    $ManualEvidenceTimeoutSeconds = [int]$forwardedArguments.ManualEvidenceTimeoutSeconds
    $OverallTimeoutSeconds = [int]$forwardedArguments.OverallTimeoutSeconds
}
catch {
    throw 'preflight_supervisor_arguments_invalid'
}

$CandidateSha = $CandidateSha.ToLowerInvariant()
$ManagedClientConfigSha = $ManagedClientConfigSha.ToLowerInvariant()
$failureOutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
[void](New-Item -ItemType Directory -Force -Path $failureOutputDirectory)
$script:WatchdogStatePath = Join-Path $failureOutputDirectory 'runner-watchdog-state'
Set-RunnerPhase -Phase 'preflight'
$failureSummaryPath = Join-Path $failureOutputDirectory 'summary.json'
$failureArtifactRoot = Join-Path $failureOutputDirectory 'artifacts'
$script:FailureArtifacts = [System.Collections.Generic.List[string]]::new()
try {
if ($CandidateSha -notmatch '^[0-9a-f]{40}$') {
    throw 'preflight_candidate_sha_invalid'
}
if ($ManagedClientConfigSha -notmatch '^[0-9a-f]{40}$') {
    throw 'preflight_materializer_sha_invalid'
}
if ($LunaModel -cne 'codexhub-openai/gpt-5.6-luna') {
    throw 'preflight_luna_model_invalid'
}
if ($VolcModel -cne 'codexhub-volc/glm-5.2') {
    throw 'preflight_volc_model_invalid'
}
if ($TimeoutSeconds -lt 1 -or $TimeoutSeconds -gt 900 -or
    $ManualEvidenceTimeoutSeconds -lt 1 -or $ManualEvidenceTimeoutSeconds -gt 3600 -or
    $OverallTimeoutSeconds -lt 1 -or $OverallTimeoutSeconds -gt 7200) {
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

foreach ($directory in @($isolationRoot, $configRoot)) {
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        throw 'preflight_isolated_directory_missing'
    }
}
Assert-CanonicalNonReparseDirectory -Path $OutputDirectory -Failure 'preflight_work_root_reparse'
Assert-CanonicalNonReparseDirectory -Path $isolationRoot -Failure 'preflight_work_root_reparse'
if (Test-Path -LiteralPath $workRoot) {
    $existingWorkRoot = Get-Item -LiteralPath $workRoot -Force
    $existingLinkType = if ($existingWorkRoot.PSObject.Properties['LinkType']) { [string]$existingWorkRoot.LinkType } else { '' }
    if (($existingWorkRoot.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or $existingLinkType) {
        throw 'preflight_work_root_reparse'
    }
    throw 'preflight_work_root_not_fresh'
}
[void](New-Item -ItemType Directory -Path $workRoot -ErrorAction Stop)
Assert-CanonicalNonReparseDirectory -Path $workRoot -Failure 'preflight_work_root_reparse'
if (@(Get-ChildItem -LiteralPath $workRoot -Force).Count -ne 0) {
    throw 'preflight_work_root_not_fresh'
}
foreach ($file in @($DebugBuild, $ManagedClientConfigBuild, $HostEnvironmentManifest, $accountPath, $accountAuthPath, $credentialPath, $gatewayConfigPath)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        throw 'preflight_required_file_missing'
    }
}
$DebugBuild = (Resolve-Path -LiteralPath $DebugBuild).Path
$ManagedClientConfigBuild = (Resolve-Path -LiteralPath $ManagedClientConfigBuild).Path
$script:ManagedClientConfigBuild = $ManagedClientConfigBuild
$script:ManagedClientConfigLogPath = Join-Path $workRoot 'managed-client-config-invocations.jsonl'
$HostEnvironmentManifest = (Resolve-Path -LiteralPath $HostEnvironmentManifest).Path
$shaSidecar = "$DebugBuild.candidate-sha"
if (-not (Test-Path -LiteralPath $shaSidecar -PathType Leaf)) {
    throw 'preflight_debug_build_sha_sidecar_missing'
}
if ((Get-Content -LiteralPath $shaSidecar -Raw).Trim() -cne $CandidateSha) {
    throw 'preflight_debug_build_sha_mismatch'
}
$materializerShaSidecar = "$ManagedClientConfigBuild.candidate-sha"
if (-not (Test-Path -LiteralPath $materializerShaSidecar -PathType Leaf)) {
    throw 'preflight_materializer_build_sha_sidecar_missing'
}
if ((Get-Content -LiteralPath $materializerShaSidecar -Raw).Trim() -cne $ManagedClientConfigSha) {
    throw 'preflight_materializer_build_sha_mismatch'
}
if (-not (Test-DebugPortableBuildResources -Executable $DebugBuild)) {
    Write-CandidateStartupDiagnostic -FailureClassification 'preflight_debug_build_not_portable' -DurationMilliseconds 0 -PortableResourcesReady $false -CandidateRunning $false -PythonChildSeen $false -ListenerSeen $false -HealthReady $false -DiagnosticsReady $false
    throw 'preflight_debug_build_not_portable'
}
if (-not (Test-DebugPortableBuildResources -Executable $ManagedClientConfigBuild)) {
    throw 'preflight_materializer_build_not_portable'
}
foreach ($isolatedInput in @($HostEnvironmentManifest, $accountPath, $accountAuthPath, $credentialPath, $gatewayConfigPath)) {
    Assert-IsolatedRegularFile -Path $isolatedInput -IsolationRoot $isolationRoot
}
$script:WindowsInstallMetadata = $null
if ($TestWindowsInstallMetadataFixture) {
    if (-not (Test-Path -LiteralPath $TestWindowsInstallMetadataFixture -PathType Leaf)) {
        throw 'preflight_windows_install_metadata_invalid'
    }
    $TestWindowsInstallMetadataFixture = (Resolve-Path -LiteralPath $TestWindowsInstallMetadataFixture).Path
    Assert-IsolatedRegularFile -Path $TestWindowsInstallMetadataFixture -IsolationRoot $isolationRoot
    $script:WindowsInstallMetadata = Read-JsonObject -Path $TestWindowsInstallMetadataFixture -Failure 'preflight_windows_install_metadata_invalid'
    Assert-ExactJsonProperties -Value $script:WindowsInstallMetadata -Names @('schema', 'desktop', 'zcode') -Failure 'preflight_windows_install_metadata_invalid'
    Assert-ExactJsonProperties -Value $script:WindowsInstallMetadata.desktop -Names @('package_name', 'package_version', 'install_location', 'executable_product_version') -Failure 'preflight_windows_install_metadata_invalid'
    $zcodeFixtureProperties = @('DisplayName', 'DisplayVersion', 'Publisher', 'DisplayIcon', 'UninstallString', 'ExecutableProductVersion')
    if ($null -ne $script:WindowsInstallMetadata.zcode.PSObject.Properties['InstallLocation']) {
        $zcodeFixtureProperties += 'InstallLocation'
    }
    Assert-ExactJsonProperties -Value $script:WindowsInstallMetadata.zcode -Names $zcodeFixtureProperties -Failure 'preflight_windows_install_metadata_invalid'
    if ([string]$script:WindowsInstallMetadata.schema -cne 'codexhub.real-client-windows-install-metadata.v1') {
        throw 'preflight_windows_install_metadata_invalid'
    }
}
$hostEnvironment = Read-JsonObject -Path $HostEnvironmentManifest -Failure 'preflight_host_environment_manifest_invalid'
Assert-ExactJsonProperties -Value $hostEnvironment -Names @('schema', 'environment', 'machine_binding_sha256') -Failure 'preflight_host_environment_manifest_invalid'
if ([string]$hostEnvironment.schema -cne 'codexhub.real-client-host-environment.v1' -or
    [string]$hostEnvironment.environment -cne 'codexhub-real-client-e2e' -or
    [string]$hostEnvironment.machine_binding_sha256 -cne (Get-HostMachineBindingSha256)) {
    throw 'preflight_host_environment_identity_mismatch'
}
$profile = Read-JsonObject -Path $accountPath -Failure 'preflight_account_profile_invalid'
Assert-ExactJsonProperties -Value $profile -Names @('schema', 'dedicated_account', 'codex_login_ready', 'gui_ready', 'host_session_reused') -Failure 'preflight_account_profile_invalid'
if ([string]$profile.schema -cne 'codexhub.real-client-account.v1' -or
    [bool]$profile.dedicated_account -ne $true -or [bool]$profile.codex_login_ready -ne $true -or [bool]$profile.gui_ready -ne $true -or
    [bool]$profile.host_session_reused -ne $false) {
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
if (Test-LoopbackListener -Port ([int]$script:GatewayConfig.listen_port)) {
    throw 'preflight_gateway_port_in_use'
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
if ($script:WindowsInstallMetadata) {
    $nonFixtureExecutables = @($executables.Values | Where-Object {
        [System.IO.Path]::GetExtension([string]$_) -inotmatch '^\.(cmd|bat)$'
    })
    if ($nonFixtureExecutables.Count -gt 0 -or
        [System.IO.Path]::GetExtension($DebugBuild) -inotmatch '^\.(cmd|bat)$' -or
        [System.IO.Path]::GetExtension($ManagedClientConfigBuild) -inotmatch '^\.(cmd|bat)$') {
        throw 'preflight_windows_install_metadata_fixture_forbidden'
    }
}
$actualVersions = [ordered]@{}
Set-RunnerPhase -Phase 'client_materialization'
foreach ($versionTarget in @(
    [pscustomobject]@{ client = 'desktop'; key = 'desktop' },
    [pscustomobject]@{ client = 'codex-cli'; key = 'codex_cli' },
    [pscustomobject]@{ client = 'zcode'; key = 'zcode' },
    [pscustomobject]@{ client = 'opencode'; key = 'opencode' },
    [pscustomobject]@{ client = 'pi'; key = 'pi' },
    [pscustomobject]@{ client = 'omp'; key = 'omp' }
)) {
    $actualVersions[$versionTarget.key] = Get-NativeClientVersion -Client $versionTarget.client -Executable $executables[$versionTarget.client] -Minimum $script:MinimumVersions[$versionTarget.key] -ProbeRoot (Join-Path $workRoot "version-$($versionTarget.client)")
}
[void](New-Item -ItemType Directory -Path $artifactRoot)
[void](New-Item -ItemType Directory -Path (Join-Path $artifactRoot 'cases'))

$manualCases = @(
    [pscustomobject]@{ case_id = 'desktop-luna'; client = 'desktop'; canonical_model = 'gpt-5.6-luna'; gateway_model = 'gpt-5.6-luna' },
    [pscustomobject]@{ case_id = 'desktop-volc'; client = 'desktop'; canonical_model = 'volc/glm-5.2'; gateway_model = 'volc/glm-5.2' },
    [pscustomobject]@{ case_id = 'zcode-luna'; client = 'zcode'; canonical_model = $LunaModel; gateway_model = 'openai/gpt-5.6-luna' },
    [pscustomobject]@{ case_id = 'zcode-volc'; client = 'zcode'; canonical_model = $VolcModel; gateway_model = 'volc/glm-5.2' }
)
$automatedCases = @(
    [pscustomobject]@{ case_id = 'codex-cli-luna'; client = 'codex-cli'; canonical_model = 'gpt-5.6-luna'; gateway_model = 'gpt-5.6-luna' },
    [pscustomobject]@{ case_id = 'codex-cli-volc'; client = 'codex-cli'; canonical_model = 'volc/glm-5.2'; gateway_model = 'volc/glm-5.2' },
    [pscustomobject]@{ case_id = 'opencode-luna'; client = 'opencode'; canonical_model = $LunaModel; gateway_model = 'openai/gpt-5.6-luna' },
    [pscustomobject]@{ case_id = 'opencode-volc'; client = 'opencode'; canonical_model = $VolcModel; gateway_model = 'volc/glm-5.2' },
    [pscustomobject]@{ case_id = 'pi-luna'; client = 'pi'; canonical_model = $LunaModel; gateway_model = 'openai/gpt-5.6-luna' },
    [pscustomobject]@{ case_id = 'pi-volc'; client = 'pi'; canonical_model = $VolcModel; gateway_model = 'volc/glm-5.2' },
    [pscustomobject]@{ case_id = 'omp-luna'; client = 'omp'; canonical_model = $LunaModel; gateway_model = 'openai/gpt-5.6-luna' },
    [pscustomobject]@{ case_id = 'omp-volc'; client = 'omp'; canonical_model = $VolcModel; gateway_model = 'volc/glm-5.2' }
)

$runBinding = New-RunBinding
Write-ManualEvidenceTemplate -Path $manualTemplatePath -ManualCases $manualCases -RunBinding $runBinding
if (Test-Path -LiteralPath $manualEvidencePath) {
    throw 'manual_evidence_preexisting'
}
$trackedProcesses = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
$nativeGuiProcesses = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
$manualSentinelPaths = [System.Collections.Generic.List[string]]::new()
$script:UnassignedProcessIds.Clear()
$script:ProcessJobHandle = if ($script:ProcessJobAvailable) {
    [CodexHubE2EJob]::CreateKillOnClose()
} else {
    [IntPtr]::Zero
}
try {
    $candidateRoot = Join-Path $workRoot 'candidate'
    [void](New-Item -ItemType Directory -Force -Path $candidateRoot)
    Initialize-CandidateRuntime -CandidateRoot $candidateRoot
    $candidateEnvironment = @{
        CODEXHUB_E2E_CANDIDATE_SHA = $CandidateSha
        CODEXHUB_E2E_GATEWAY_PORT = [string]$script:GatewayConfig.listen_port
        CODEXHUB_RUNTIME_HOME = $script:CandidateRuntimeRoot
        CODEXHUB_CODEX_TARGET_HOME = $script:CandidateCodexRoot
        CODEX_HOME = $script:CandidateCodexRoot
        CODEXHUB_CODEX_PATH = [string]$executables['codex-cli']
        CODEX_PROXY_GATEWAY_CLIENT_KEY = [string]$script:GatewayConfig.gateway_client_key
        VOLCENGINE_API_KEY = [string]$credential.api_key
        CODEXHUB_E2E_CONTRACT_PROBE_LOG = $script:ManagedClientConfigLogPath
    }
    Set-RunnerPhase -Phase 'candidate_startup'
    $candidateStartupBudgetMilliseconds = [Math]::Min($TimeoutSeconds, 30) * 1000
    $candidateStartupStopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    [void](Invoke-CandidateOfficialBootstrap -Executable $DebugBuild -CandidateRoot $candidateRoot -Environment $candidateEnvironment -TimeoutSeconds $TimeoutSeconds)
    $candidateCatalogPath = Join-Path $script:CandidateRuntimeRoot 'proxy\model-catalogs\codexhub-model-catalog.json'
    if (-not (Test-Path -LiteralPath $candidateCatalogPath -PathType Leaf)) {
        $candidateStartupStopwatch.Stop()
        Write-CandidateStartupDiagnostic -FailureClassification 'candidate_gateway_bootstrap_failed_context_budget' -DurationMilliseconds [int]$candidateStartupStopwatch.ElapsedMilliseconds -PortableResourcesReady $true -CandidateRunning $false -PythonChildSeen $false -ListenerSeen $false -HealthReady $false -DiagnosticsReady (Test-Path -LiteralPath $script:DiagnosticsPath -PathType Leaf)
        throw 'candidate_gateway_bootstrap_failed_context_budget'
    }
    $caseConfigurations = @{}
    foreach ($case in @($manualCases) + @($automatedCases)) {
        $caseRoot = if ($case.client -in @('desktop', 'zcode')) {
            Join-Path (Join-Path $workRoot ("gui-" + $case.client)) $case.case_id
        }
        else {
            Join-Path $workRoot $case.case_id
        }
        [void](New-Item -ItemType Directory -Path $caseRoot -Force)
        $caseConfigurations[$case.case_id] = Initialize-ClientConfiguration -Client $case.client -CaseRoot $caseRoot -Model $case.gateway_model -CatalogPath $candidateCatalogPath
    }
    [void](Initialize-ClientConfiguration -Client 'desktop' -CaseRoot $candidateRoot -Model 'gpt-5.6-luna' -CatalogPath $candidateCatalogPath)
    $candidateStartupStopwatch.Stop()
    $candidateStartupStopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    $remainingStartupMilliseconds = $candidateStartupBudgetMilliseconds - [int]$candidateStartupStopwatch.ElapsedMilliseconds
    if ($remainingStartupMilliseconds -le 0) {
        $candidateStartupStopwatch.Stop()
        Write-CandidateStartupDiagnostic -FailureClassification 'candidate_gateway_startup_timeout' -DurationMilliseconds $candidateStartupBudgetMilliseconds -PortableResourcesReady $true -CandidateRunning $false -PythonChildSeen $false -ListenerSeen $false -HealthReady $false -DiagnosticsReady (Test-Path -LiteralPath $script:DiagnosticsPath -PathType Leaf)
        throw 'candidate_gateway_startup_timeout'
    }
    $candidateProcess = Start-IsolatedProcess -Executable $DebugBuild -Arguments @() -CaseRoot $candidateRoot -Environment $candidateEnvironment
    [void]$trackedProcesses.Add($candidateProcess)
    $elapsedBeforeWaitMilliseconds = [int]$candidateStartupStopwatch.ElapsedMilliseconds
    $remainingStartupMilliseconds = $candidateStartupBudgetMilliseconds - $elapsedBeforeWaitMilliseconds
    if ($remainingStartupMilliseconds -le 0) {
        $candidateStartupStopwatch.Stop()
        Write-CandidateStartupDiagnostic -FailureClassification 'candidate_gateway_startup_timeout' -DurationMilliseconds $candidateStartupBudgetMilliseconds -PortableResourcesReady $true -CandidateRunning (-not $candidateProcess.HasExited) -PythonChildSeen $false -ListenerSeen $false -HealthReady $false -DiagnosticsReady (Test-Path -LiteralPath $script:DiagnosticsPath -PathType Leaf)
        throw 'candidate_gateway_startup_timeout'
    }
    Wait-CandidateGatewayReady -CandidateProcess $candidateProcess -TimeoutMilliseconds $remainingStartupMilliseconds -ElapsedBeforeWaitMilliseconds $elapsedBeforeWaitMilliseconds
    $candidateStartupStopwatch.Stop()

    Set-RunnerPhase -Phase 'manual_evidence'
    foreach ($guiCase in $manualCases) {
        $guiClient = $guiCase.client
        $guiRoot = Join-Path $workRoot ("gui-" + $guiClient)
        $guiCaseRoot = Join-Path $guiRoot $guiCase.case_id
        if ($null -eq $caseConfigurations[$guiCase.case_id]) {
            throw 'client_configuration_materializer_contradiction'
        }
        $guiSentinelPath = Join-Path $guiCaseRoot 'sentinel.txt'
        [System.IO.File]::WriteAllText($guiSentinelPath, "SENTINEL:codexhub-real-client-e2e:$($guiCase.case_id)", $script:Utf8NoBom)
        [void]$manualSentinelPaths.Add($guiSentinelPath)
        $guiProcess = Start-IsolatedProcess -Executable $executables[$guiClient] -Arguments @() -CaseRoot $guiCaseRoot -Environment @{
            CODEXHUB_E2E_GUI_CLIENT = $guiClient
            CODEXHUB_E2E_CASES = $guiCase.case_id
            CODEXHUB_E2E_MODELS = $guiCase.canonical_model
            CODEXHUB_E2E_MANUAL_TEMPLATE = $manualTemplatePath
            CODEXHUB_E2E_MANUAL_EVIDENCE = $manualEvidencePath
            CODEXHUB_E2E_GUI_LAUNCH_MARKER = (Join-Path $workRoot "gui-$($guiCase.case_id).launched")
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

    Set-RunnerPhase -Phase 'automated_cases'
    $automatedById = @{}
    foreach ($case in $automatedCases) {
        $automatedById[$case.case_id] = Invoke-AutomatedCase -Case $case -Executable $executables[$case.client] -ArtifactRoot $artifactRoot -WorkRoot $workRoot -Configuration $caseConfigurations[$case.case_id]
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
        managed_client_config_sha = $ManagedClientConfigSha
        run_binding_sha256 = $runBinding
        outcome = if ($passedCount -eq 12) { 'passed' } else { 'failed' }
        failure_classification = if ($passedCount -eq 12) { 'none' } else { 'case_failure' }
        hashes = [ordered]@{
            debug_build = Get-Sha256 -Path $DebugBuild
            managed_client_config_build = Get-Sha256 -Path $ManagedClientConfigBuild
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
    Set-RunnerPhase -Phase 'summary'
    Write-JsonFile -Path $summaryPath -Value $summary
    if ($passedCount -ne 12) {
        exit 1
    }
}
finally {
    if ($script:ProcessJobAvailable) {
        [CodexHubE2EJob]::Close($script:ProcessJobHandle)
    }
    $script:ProcessJobHandle = [IntPtr]::Zero
    $fallbackProcesses = @($trackedProcesses | Where-Object { $script:UnassignedProcessIds.Contains($_.Id) })
    Stop-TrackedProcesses -Processes $fallbackProcesses -TimeoutMilliseconds 5000
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
        managed_client_config_sha = if ($ManagedClientConfigSha -match '^[0-9a-f]{40}$') { $ManagedClientConfigSha } else { $null }
        outcome = 'failed'
        failure_classification = $failureClassification
        pinned_versions = $script:MinimumVersions
        canonical_models = @('gpt-5.6-luna', 'volc/glm-5.2', 'codexhub-openai/gpt-5.6-luna', 'codexhub-volc/glm-5.2')
        counts = [ordered]@{
            case_count = 0
            passed_count = 0
            failed_count = 0
            manual_case_count = 0
            automated_case_count = 0
        }
        cases = @()
        artifacts = @($script:FailureArtifacts)
    }
    Write-JsonFile -Path $failureSummaryPath -Value $failureSummary
    exit 1
}
