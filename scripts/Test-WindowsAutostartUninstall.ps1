param(
    [string]$Installer = "\\VBOXSVR\CodexHubSmoke\artifacts\CodexHub_0.1.6_x64-setup.exe",
    [ValidateSet("normal", "debug")]
    [string]$Flavor = "normal",
    [int]$LaunchTimeoutSeconds = 30,
    [int]$InstallTimeoutSeconds = 180,
    [switch]$RunUninstall,
    [switch]$KeepAppRunning
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$taskName = "CodexHubProxy"
$controlTaskName = "CodexHubUninstallControl"
$description = "CodexHub-owned per-user autostart"

function Get-Task([string]$Name) {
    $service = New-Object -ComObject "Schedule.Service"
    $service.Connect()
    $folder = $service.GetFolder("\")
    try { return $folder.GetTask($Name) }
    catch {
        if ($_.Exception.HResult -eq -2147024894) { return $null }
        throw
    }
}

function Remove-SmokeTask([string]$Name) {
    $service = New-Object -ComObject "Schedule.Service"
    $service.Connect()
    $folder = $service.GetFolder("\")
    try { $folder.DeleteTask($Name, 0) }
    catch { if ($_.Exception.HResult -ne -2147024894) { throw } }
}

function Register-ControlTask([string]$Name) {
    $service = New-Object -ComObject "Schedule.Service"
    $service.Connect()
    $folder = $service.GetFolder("\")
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $definition = $service.NewTask(0)
    $definition.RegistrationInfo.Description = "Unrelated uninstall smoke control"
    $definition.Principal.UserId = $sid
    $definition.Principal.LogonType = 3
    $definition.Principal.RunLevel = 0
    $trigger = $definition.Triggers.Create(9)
    $trigger.Enabled = $true
    $trigger.UserId = $sid
    $action = $definition.Actions.Create(0)
    $action.Path = "$env:WINDIR\System32\notepad.exe"
    $action.WorkingDirectory = "$env:WINDIR\System32"
    $null = $folder.RegisterTaskDefinition($Name, $definition, 6, $sid, $null, 3, $null)
}

function Install-Candidate {
    $install = Start-Process -FilePath $Installer -ArgumentList "/S" -Wait -PassThru
    if ($install.ExitCode -ne 0) { throw "Installer failed for $Flavor flavor." }
    $exe = Get-ChildItem "$env:LOCALAPPDATA\CodexHub" -Filter CodexHub.exe -Recurse |
        Select-Object -First 1 -ExpandProperty FullName
    if ([string]::IsNullOrWhiteSpace($exe)) { throw "Installed executable was not found." }
    return $exe
}

function Enable-Autostart([string]$Exe) {
    $enable = Start-Process -FilePath $Exe -ArgumentList "set-autostart", "true" -Wait -PassThru
    if ($enable.ExitCode -ne 0) { throw "Autostart enable failed." }
}

function Assert-OwnedTask([string]$Exe) {
    $task = Get-Task $taskName
    if ($null -eq $task) { throw "Owned registration is missing." }
    [xml]$xml = $task.Xml
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = $xml.Task.Principals.Principal
    $trigger = $xml.Task.Triggers.LogonTrigger
    $action = $xml.Task.Actions.Exec
    $commandMatches = [StringComparer]::OrdinalIgnoreCase.Equals(
        [IO.Path]::GetFullPath([string]$action.Command),
        [IO.Path]::GetFullPath($Exe)
    )
    $workingMatches = [StringComparer]::OrdinalIgnoreCase.Equals(
        [IO.Path]::GetFullPath([string]$action.WorkingDirectory),
        [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($Exe))
    )
    $triggerMatches = [StringComparer]::OrdinalIgnoreCase.Equals([string]$trigger.UserId, $identity.User.Value) -or
        [StringComparer]::OrdinalIgnoreCase.Equals([string]$trigger.UserId, $identity.Name)
    $runLevelNode = $principal.SelectSingleNode("RunLevel")
    $runLevel = if ($null -eq $runLevelNode) { "" } else { [string]$runLevelNode.InnerText }
    $argumentsNode = $action.SelectSingleNode("Arguments")
    $arguments = if ($null -eq $argumentsNode) { "" } else { [string]$argumentsNode.InnerText }
    if ([string]$xml.Task.RegistrationInfo.Description -ne $description -or
        -not $commandMatches -or -not $workingMatches -or
        -not [string]::IsNullOrWhiteSpace($arguments) -or
        [string]$principal.UserId -ne $identity.User.Value -or
        [string]$principal.LogonType -ne "InteractiveToken" -or
        (-not [string]::IsNullOrWhiteSpace($runLevel) -and $runLevel -ne "LeastPrivilege") -or
        -not $triggerMatches) {
        throw "Registration did not match the complete ownership contract."
    }
}

function Uninstall-Candidate([string]$Exe) {
    $uninstaller = Join-Path (Split-Path -Parent $Exe) "uninstall.exe"
    $uninstall = Start-Process -FilePath $uninstaller -ArgumentList "/S" -Wait -PassThru
    if ($uninstall.ExitCode -ne 0) { throw "Uninstaller failed." }
    $deadline = [DateTime]::UtcNow.AddSeconds($InstallTimeoutSeconds)
    while ((Test-Path -LiteralPath $Exe) -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 250
    }
    if (Test-Path -LiteralPath $Exe) { throw "Uninstaller did not complete in time." }
}

try {
    Remove-SmokeTask $taskName
    Remove-SmokeTask $controlTaskName
    Register-ControlTask $controlTaskName

    $exe = Install-Candidate
    Enable-Autostart $exe
    Assert-OwnedTask $exe
    Uninstall-Candidate $exe
    if ($null -ne (Get-Task $taskName)) { throw "Owned task survived uninstall." }
    if ($null -eq (Get-Task $controlTaskName)) { throw "Unrelated control task was deleted." }
    Write-Output "PASS: owned task removed; unrelated control task preserved."

    $exe = Install-Candidate
    Enable-Autostart $exe
    Assert-OwnedTask $exe
    Write-Output "PASS: reinstall created exactly one valid $Flavor registration."

    Register-ControlTask $taskName
    Uninstall-Candidate $exe
    $mismatch = Get-Task $taskName
    if ($null -eq $mismatch -or $mismatch.Xml -match [regex]::Escape($description)) {
        throw "Same-name mismatched task was not preserved unchanged."
    }
    Write-Output "PASS: same-name mismatch preserved; diagnostics contain no executable path."
}
finally {
    Remove-SmokeTask $taskName
    Remove-SmokeTask $controlTaskName
}
