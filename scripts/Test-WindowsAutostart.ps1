param(
    [Parameter(Mandatory = $true)]
    [string]$Executable,
    [ValidateSet("installed", "portable")]
    [string]$Distribution = "installed",
    [switch]$InvokeTask,
    [switch]$VerifyDisabled
)

$ErrorActionPreference = "Stop"
$taskName = if ($env:CODEXHUB_APP_FLAVOR -eq "debug") { "CodexHubBetaProxy" } else { "CodexHubProxy" }
$expected = [System.IO.Path]::GetFullPath($Executable)

function Get-OwnedTaskXml {
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = & schtasks /Query /TN $taskName /XML 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    if ($exitCode -eq 0) {
        return ($output -join "`n")
    }

    try {
        $service = New-Object -ComObject "Schedule.Service"
        $service.Connect()
        $folder = $service.GetFolder("\")
        $null = $folder.GetTask($taskName)
    }
    catch {
        if ($_.Exception.HResult -eq -2147024894) {
            return $null
        }
        throw "Unable to determine whether the CodexHub-owned autostart registration is absent."
    }

    throw "Unable to query the existing CodexHub-owned autostart registration (exit code $exitCode)."
}

$xmlText = Get-OwnedTaskXml
if ($VerifyDisabled) {
    if ($null -ne $xmlText) {
        throw "CodexHub-owned autostart registration still exists after disable."
    }
    Write-Output "PASS: CodexHub-owned autostart registration is absent."
    exit 0
}

if ($null -eq $xmlText) {
    throw "CodexHub-owned autostart registration is missing."
}

[xml]$task = $xmlText
$command = [System.IO.Path]::GetFullPath([string]$task.Task.Actions.Exec.Command)
$workingDirectoryText = [string]$task.Task.Actions.Exec.WorkingDirectory
$arguments = [string]$task.Task.Actions.Exec.Arguments
$currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$currentSid = $currentIdentity.User.Value
$currentName = $currentIdentity.Name
$principal = $task.Task.Principals.Principal
$trigger = $task.Task.Triggers.LogonTrigger
$triggerUser = [string]$trigger.UserId
if (-not [System.StringComparer]::OrdinalIgnoreCase.Equals($command, $expected)) {
    throw "Autostart action is stale or points to a different executable."
}
if ([string]::IsNullOrWhiteSpace($workingDirectoryText)) {
    throw "Autostart working directory is missing."
}
$workingDirectory = [System.IO.Path]::GetFullPath($workingDirectoryText)
$expectedWorkingDirectory = [System.IO.Path]::GetDirectoryName($expected)
if (-not [System.StringComparer]::OrdinalIgnoreCase.Equals($workingDirectory, $expectedWorkingDirectory)) {
    throw "Autostart working directory is stale or malformed."
}
if (-not [string]::IsNullOrWhiteSpace($arguments) -or $null -eq $trigger) {
    throw "Autostart action or logon trigger is malformed."
}
if ([string]$task.Task.RegistrationInfo.Description -ne "CodexHub-owned per-user autostart" -or
    [string]$principal.UserId -ne $currentSid -or
    -not ([System.StringComparer]::OrdinalIgnoreCase.Equals($triggerUser, $currentSid) -or
        [System.StringComparer]::OrdinalIgnoreCase.Equals($triggerUser, $currentName)) -or
    [string]$principal.LogonType -ne "InteractiveToken" -or
    (-not [string]::IsNullOrWhiteSpace([string]$principal.RunLevel) -and
        [string]$principal.RunLevel -ne "LeastPrivilege")) {
    throw "Autostart principal is not the current unelevated interactive user."
}

if ($InvokeTask) {
    $before = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and [System.StringComparer]::OrdinalIgnoreCase.Equals($_.ExecutablePath, $expected) }).Count
    & schtasks /Run /TN $taskName | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Deterministic task invocation failed." }
    Start-Sleep -Seconds 5
    $after = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and [System.StringComparer]::OrdinalIgnoreCase.Equals($_.ExecutablePath, $expected) }).Count
    if ($after -ne [Math]::Max(1, $before)) {
        throw "Expected exactly one CodexHub process after deterministic task invocation."
    }
}

Write-Output "PASS: $Distribution autostart registration is valid and owned; no user path was printed."
