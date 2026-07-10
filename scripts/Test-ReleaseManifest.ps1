[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("stable", "beta")]
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

foreach ($path in @($ManifestPath, $InstallerPath, $SignaturePath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required release artifact is missing: $path"
    }
}

$manifestName = [System.IO.Path]::GetFileName($ManifestPath)
$installerName = [System.IO.Path]::GetFileName($InstallerPath)
$expectedManifestName = if ($Flavor -eq "beta") { "latest-beta.json" } else { "latest.json" }
$expectedPrefix = if ($Flavor -eq "beta") { "CodexHubBeta" } else { "CodexHub" }
$expectedInstallerName = "${expectedPrefix}_${Version}_x64-setup.exe"

if ($manifestName -ne $expectedManifestName) {
    throw "$Flavor release manifest must be named $expectedManifestName."
}
if ($installerName -ne $expectedInstallerName) {
    throw "$Flavor installer must be named $expectedInstallerName."
}
if ([System.IO.Path]::GetFileName($SignaturePath) -ne "$expectedInstallerName.sig") {
    throw "$Flavor signature must be paired with $expectedInstallerName."
}
if ($Flavor -eq "beta" -and $Version -notmatch '^0\.1\.4-beta\.[1-9][0-9]*$') {
    throw "Beta release requires a v0.1.4-beta.N prerelease version."
}
if ($Flavor -eq "stable" -and $Version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') {
    throw "Stable release requires a stable version."
}

$manifest = Get-Content -Raw -LiteralPath $ManifestPath | ConvertFrom-Json
$platform = $manifest.platforms."windows-x86_64"
$signature = (Get-Content -Raw -LiteralPath $SignaturePath).Trim()
if ($manifest.version -ne $Version) {
    throw "Manifest version does not match $Version."
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
