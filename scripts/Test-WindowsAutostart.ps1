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
    $output = & schtasks /Query /TN $taskName /XML 2>&1
    if ($LASTEXITCODE -ne 0) {
        return $null
    }
    return ($output -join "`n")
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
$arguments = [string]$task.Task.Actions.Exec.Arguments
$currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$principal = $task.Task.Principals.Principal
$trigger = $task.Task.Triggers.LogonTrigger
if (-not [System.StringComparer]::OrdinalIgnoreCase.Equals($command, $expected)) {
    throw "Autostart action is stale or points to a different executable."
}
if (-not [string]::IsNullOrWhiteSpace($arguments) -or $null -eq $trigger) {
    throw "Autostart action or logon trigger is malformed."
}
if ([string]$task.Task.RegistrationInfo.Description -ne "CodexHub-owned per-user autostart" -or
    [string]$principal.UserId -ne $currentSid -or
    [string]$trigger.UserId -ne $currentSid -or
    [string]$principal.LogonType -ne "InteractiveToken" -or
    [string]$principal.RunLevel -ne "LeastPrivilege") {
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
