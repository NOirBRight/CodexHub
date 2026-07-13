[CmdletBinding()]
param(
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$Version = "",
    [string]$NormalInstaller = "",
    [string]$DebugInstaller = "",
    [string]$SettingsPath = "",
    [string]$InstalledExe = "",
    [switch]$RunInstall,
    [switch]$LaunchAfterInstall,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($RunInstall -and $DryRun) {
    throw "-RunInstall and -DryRun cannot be used together."
}
if ($LaunchAfterInstall -and -not $RunInstall) {
    throw "-LaunchAfterInstall requires -RunInstall."
}

. (Join-Path $PSScriptRoot "ReleaseChannel.ps1")

$flavorManifestPath = Join-Path $RepoRoot "config\build-flavors.json"
$tauriConfigPath = Join-Path $RepoRoot "src-tauri\tauri.conf.json"
$flavorManifest = Get-Content -Raw -LiteralPath $flavorManifestPath | ConvertFrom-Json
$tauriConfig = Get-Content -Raw -LiteralPath $tauriConfigPath | ConvertFrom-Json
$normal = $flavorManifest.normal
$debug = $flavorManifest.debug
if ($null -eq $normal -or $null -eq $debug) {
    throw "Build flavor manifest must define both normal and debug."
}

if ([string]::IsNullOrWhiteSpace($Version)) {
    $Version = [string]$tauriConfig.version
}
Assert-ReleaseFlavorVersion -Flavor normal -Version $Version
Assert-ReleaseFlavorVersion -Flavor debug -Version $Version

$sharedIdentityKeys = @(
    "productName",
    "executableBaseName",
    "identifier",
    "windowTitle",
    "frontendPort",
    "bridgePort",
    "gatewayPort",
    "routingOwner",
    "defaultCodexHome",
    "codexTargetHome",
    "autostartTaskName",
    "macosLabel",
    "macosPlistFile",
    "linuxServiceFile"
)
foreach ($key in $sharedIdentityKeys) {
    if ([string]$normal.$key -ne [string]$debug.$key) {
        throw "normal and debug must share $key for same-version replacement."
    }
}

$normalInstallerName = Get-ReleaseArtifactName -Flavor normal -Version $Version
$debugInstallerName = Get-ReleaseArtifactName -Flavor debug -Version $Version
$sequence = @(
    [pscustomobject]@{ Flavor = "normal"; InstallerName = $normalInstallerName },
    [pscustomobject]@{ Flavor = "debug"; InstallerName = $debugInstallerName },
    [pscustomobject]@{ Flavor = "normal"; InstallerName = $normalInstallerName }
)

$plan = [ordered]@{
    version = $Version
    sequence = @($sequence | ForEach-Object { $_.Flavor })
    normal_installer = $normalInstallerName
    debug_installer = $debugInstallerName
    application_identity = [ordered]@{
        product_name = [string]$normal.productName
        identifier = [string]$normal.identifier
        executable = "{0}.exe" -f ([string]$normal.executableBaseName)
    }
    runtime = [ordered]@{
        home = [string]$normal.defaultCodexHome
        routing_owner = [string]$normal.routingOwner
        gateway_port = [int]$normal.gatewayPort
        expected_gateway_owner_count = 1
    }
}

if ($DryRun) {
    $plan | ConvertTo-Json -Depth 8 -Compress
    return
}

if (-not $RunInstall) {
    Write-Host "Replacement contract verified. Use -RunInstall in a dedicated Windows test environment to exercise the installer sequence."
    return
}

if ([string]::IsNullOrWhiteSpace($NormalInstaller) -or [string]::IsNullOrWhiteSpace($DebugInstaller)) {
    throw "-RunInstall requires both -NormalInstaller and -DebugInstaller."
}
if ([string]::IsNullOrWhiteSpace($SettingsPath) -or -not (Test-Path -LiteralPath $SettingsPath -PathType Leaf)) {
    throw "-RunInstall requires an existing -SettingsPath to prove supported settings survive replacement."
}
if ($LaunchAfterInstall -and ([string]::IsNullOrWhiteSpace($InstalledExe) -or -not (Test-Path -LiteralPath $InstalledExe -PathType Leaf))) {
    throw "-LaunchAfterInstall requires an existing -InstalledExe."
}

function Assert-InstallerPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedName
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Installer was not found: $Path"
    }
    if ((Split-Path -Leaf $Path) -ne $ExpectedName) {
        throw "Installer name mismatch: expected $ExpectedName, got $(Split-Path -Leaf $Path)."
    }
}

function Get-SettingsHash {
    param([Parameter(Mandatory = $true)][string]$Path)

    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

function Assert-OneGatewayOwner {
    param([Parameter(Mandatory = $true)][int]$Port)

    $owners = @(
        Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
    )
    if ($owners.Count -ne 1) {
        throw "Expected exactly one Gateway owner on port $Port, found $($owners -join ', ')."
    }
}

Assert-InstallerPath -Path $NormalInstaller -ExpectedName $normalInstallerName
Assert-InstallerPath -Path $DebugInstaller -ExpectedName $debugInstallerName
$settingsHash = Get-SettingsHash -Path $SettingsPath
$installerPaths = @{ normal = $NormalInstaller; debug = $DebugInstaller }

foreach ($step in $sequence) {
    & $installerPaths[$step.Flavor] /S
    if ($LASTEXITCODE -ne 0) {
        throw "Installer replacement failed for $($step.Flavor) with exit code $LASTEXITCODE."
    }
    if ((Get-SettingsHash -Path $SettingsPath) -ne $settingsHash) {
        throw "Supported settings changed while replacing with $($step.Flavor)."
    }

    if ($LaunchAfterInstall) {
        $process = Start-Process -FilePath (Resolve-Path -LiteralPath $InstalledExe).Path -PassThru -WindowStyle Hidden
        try {
            Start-Sleep -Milliseconds 750
            Assert-OneGatewayOwner -Port ([int]$normal.gatewayPort)
        }
        finally {
            if (-not $process.HasExited) {
                Stop-Process -Id $process.Id -Force
            }
        }
    }
}

Write-Host "Normal -> debug -> normal same-version replacement preserved settings and kept exactly one Gateway owner."
