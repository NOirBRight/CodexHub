[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("normal", "debug")]
    [string]$Flavor,
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [Parameter(Mandatory = $true)]
    [string]$ManifestPath,
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,
    [Parameter(Mandatory = $true)]
    [string]$SignaturePath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "ReleaseChannel.ps1")

Assert-ReleaseFlavorVersion -Flavor $Flavor -Version $Version

foreach ($path in @($ManifestPath, $InstallerPath, $SignaturePath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required release artifact is missing: $path"
    }
}

$manifestName = [System.IO.Path]::GetFileName($ManifestPath)
$installerName = [System.IO.Path]::GetFileName($InstallerPath)
$expectedManifestName = Get-ReleaseManifestName -Flavor $Flavor
$expectedInstallerName = Get-ReleaseArtifactName -Flavor $Flavor -Version $Version

if ($manifestName -ne $expectedManifestName) {
    throw "$Flavor release manifest must be named $expectedManifestName."
}
if ($installerName -ne $expectedInstallerName) {
    throw "$Flavor installer must be named $expectedInstallerName."
}
if ([System.IO.Path]::GetFileName($SignaturePath) -ne "$expectedInstallerName.sig") {
    throw "$Flavor signature must be paired with $expectedInstallerName."
}
$manifest = Get-Content -Raw -LiteralPath $ManifestPath | ConvertFrom-Json
$platform = $manifest.platforms."windows-x86_64"
$signature = (Get-Content -Raw -LiteralPath $SignaturePath).Trim()
if ($manifest.version -ne $Version) {
    throw "Manifest version does not match $Version."
}
if ($manifest.codexhub_flavor -ne $Flavor) {
    throw "Manifest flavor does not match $Flavor."
}
if ([string]::IsNullOrWhiteSpace($signature) -or $platform.signature -ne $signature) {
    throw "Manifest signature does not match the paired signature artifact."
}
$immutablePath = "/releases/download/v$Version/"
if (-not ([string]$platform.url).Contains($immutablePath)) {
    throw "Manifest asset URL must use the immutable version tag v$Version."
}
if (-not ([string]$platform.url).EndsWith("/$expectedInstallerName")) {
    throw "Manifest asset URL must point to $expectedInstallerName."
}

Write-Output "Validated $Flavor release manifest: $manifestName"
