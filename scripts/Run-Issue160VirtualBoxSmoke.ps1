param(
    [int]$LaunchTimeoutSeconds = 30,
    [int]$InstallTimeoutSeconds = 180,
    [switch]$RunUninstall,
    [switch]$KeepAppRunning
)

$ErrorActionPreference = "Stop"
$root = "\\VBOXSVR\CodexHubSmoke"
if (-not $RunUninstall) { throw "Issue #160 smoke requires the uninstall boundary." }
if ($KeepAppRunning) { throw "Issue #160 smoke cannot leave the app running." }
$debugFlag = Join-Path $root "artifacts\issue160-debug.flag"
$flavor = if (Test-Path -LiteralPath $debugFlag) { "debug" } else { "normal" }
$installerName = if ($flavor -eq "debug") {
    "CodexHub_0.1.6_debug_x64-setup.exe"
} else {
    "CodexHub_0.1.6_x64-setup.exe"
}
$installer = Join-Path $root "artifacts\$installerName"
$deadline = [DateTime]::UtcNow.AddSeconds($LaunchTimeoutSeconds)
while (-not (Test-Path -LiteralPath $installer) -and [DateTime]::UtcNow -lt $deadline) {
    Start-Sleep -Milliseconds 250
}
if (-not (Test-Path -LiteralPath $installer)) { throw "Selected installer was not available." }

& (Join-Path $root "scripts\Test-WindowsAutostartUninstall.ps1") `
    -Installer $installer `
    -Flavor $flavor `
    -InstallTimeoutSeconds $InstallTimeoutSeconds
exit $LASTEXITCODE
