[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("normal", "debug")]
    [string]$Flavor,
    [Parameter(Mandatory = $true)]
    [string]$RepoRoot,
    [Parameter(Mandatory = $true)]
    [string]$GeneratedConfig
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

. (Join-Path $RepoRoot "scripts/ReleaseChannel.ps1")

$cfg = Get-Content -Raw -LiteralPath $GeneratedConfig | ConvertFrom-Json
Assert-ReleaseVersion -Version ([string]$cfg.version)
$flavors = Get-Content -Raw -LiteralPath (Join-Path $RepoRoot "config/build-flavors.json") | ConvertFrom-Json
$flavorCfg = $flavors.$Flavor
if ($null -eq $flavorCfg) {
    throw "Unknown build flavor: $Flavor"
}

$pairs = [ordered]@{
    version = [string]$cfg.version
    productName = [string]$cfg.productName
    identifier = [string]$cfg.identifier
    windowTitle = [string]$cfg.app.windows[0].title
    updaterEndpoint = [string]$cfg.plugins.updater.endpoints[0]
    executableBaseName = [string]$flavorCfg.executableBaseName
    releaseAssetPrefix = [string]$flavorCfg.releaseAssetPrefix
    releaseAssetSuffix = [string]$flavorCfg.releaseAssetSuffix
    frontendPort = [string]$flavorCfg.frontendPort
    bridgePort = [string]$flavorCfg.bridgePort
    gatewayPort = [string]$flavorCfg.gatewayPort
    appimageName = (Get-LinuxReleaseArtifactName -Flavor $Flavor -Version ([string]$cfg.version) -Kind appimage)
    debName = (Get-LinuxReleaseArtifactName -Flavor $Flavor -Version ([string]$cfg.version) -Kind deb)
    manifestName = (Get-ReleaseManifestName -Flavor $Flavor)
    targetRoot = (Get-FlavorTargetRoot -TauriDir (Join-Path $RepoRoot "src-tauri") -Flavor $Flavor)
}

foreach ($entry in $pairs.GetEnumerator()) {
    $value = [string]$entry.Value
    if ($value -ne "" -and $value -notmatch '^[A-Za-z0-9._+/:@=-]+$') {
        throw ("unsafe Linux release metadata value for {0}: {1}" -f $entry.Key, $value)
    }
    Write-Output ("{0}={1}" -f $entry.Key, $value)
}
