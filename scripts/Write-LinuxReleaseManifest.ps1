[CmdletBinding()]
param(
    [ValidateSet("normal", "debug")]
    [string]$Flavor = "normal",
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [Parameter(Mandatory = $true)]
    [string]$AppImagePath,
    [Parameter(Mandatory = $true)]
    [string]$SignaturePath,
    [Parameter(Mandatory = $true)]
    [string]$ManifestPath,
    [string]$ReleaseBaseUrl = "",
    [string]$Notes = "",
    [string]$RepoRoot = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}

. (Join-Path $PSScriptRoot "ReleaseChannel.ps1")

Assert-ReleaseVersion -Version $Version
if ([string]::IsNullOrWhiteSpace($ReleaseBaseUrl)) {
    $ReleaseBaseUrl = "https://github.com/NOirBRight/CodexHub/releases/download/v$Version"
}

$appImageName = Get-LinuxReleaseArtifactName -Flavor $Flavor -Version $Version -Kind appimage
if ([System.IO.Path]::GetFileName($AppImagePath) -ne $appImageName) {
    throw "Linux AppImage must be named $appImageName."
}
if (-not (Test-Path -LiteralPath $AppImagePath -PathType Leaf)) {
    throw "Linux AppImage is missing: $AppImagePath"
}
if (-not (Test-Path -LiteralPath $SignaturePath -PathType Leaf)) {
    throw "Linux updater signature is missing: $SignaturePath"
}

$signature = (Get-Content -Raw -LiteralPath $SignaturePath).Trim()
if ([string]::IsNullOrWhiteSpace($signature)) {
    throw "Linux updater signature is empty: $SignaturePath"
}

if ([string]::IsNullOrWhiteSpace($Notes)) {
    $Notes = "CodexHub $Version"
}

$sourceRevision = (& git -C $RepoRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($sourceRevision)) {
    throw "Unable to resolve Linux release build commit."
}

$manifest = [ordered]@{
    version = $Version
    codexhub_flavor = $Flavor
    codexhub_source_revision = $sourceRevision
    notes = $Notes
    pub_date = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ", [Globalization.CultureInfo]::InvariantCulture)
    platforms = [ordered]@{
        "linux-x86_64" = [ordered]@{
            signature = $signature
            url = "$($ReleaseBaseUrl.TrimEnd('/'))/$([Uri]::EscapeDataString($appImageName))"
        }
    }
}

$manifestName = [System.IO.Path]::GetFileName($ManifestPath)
$expectedManifestName = Get-ReleaseManifestName -Flavor $Flavor
if ($manifestName -ne $expectedManifestName) {
    throw "Linux release manifest must be named $expectedManifestName."
}

$manifestDir = Split-Path -Parent $ManifestPath
if (-not (Test-Path -LiteralPath $manifestDir -PathType Container)) {
    New-Item -ItemType Directory -Path $manifestDir | Out-Null
}

$manifestJson = $manifest | ConvertTo-Json -Depth 8
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($ManifestPath, $manifestJson + [Environment]::NewLine, $utf8NoBom)

& (Join-Path $PSScriptRoot "Test-ReleaseManifest.ps1") `
    -Flavor $Flavor `
    -Version $Version `
    -ManifestPath $ManifestPath `
    -InstallerPath $AppImagePath `
    -SignaturePath $SignaturePath `
    -Platform linux-x86_64

Write-Output "Linux updater manifest ready: $ManifestPath"
