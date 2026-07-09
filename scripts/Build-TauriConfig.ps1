[CmdletBinding()]
param(
    [ValidateSet("stable", "beta")]
    [string]$Flavor = "stable",
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$OutputRoot = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $OutputRoot = Join-Path $RepoRoot ".generated\tauri\$Flavor"
}

$manifestPath = Join-Path $RepoRoot "config\build-flavors.json"
$baseConfigPath = Join-Path $RepoRoot "src-tauri\tauri.conf.json"
$manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
$flavorConfig = $manifest.$Flavor
if ($null -eq $flavorConfig) {
    throw "Unknown CodexHub build flavor: $Flavor"
}

$config = Get-Content -Raw -LiteralPath $baseConfigPath | ConvertFrom-Json
$config.productName = [string]$flavorConfig.productName
$config.identifier = [string]$flavorConfig.identifier
$config.build.devUrl = "http://localhost:$($flavorConfig.frontendPort)"
$config.app.windows[0].title = [string]$flavorConfig.windowTitle
$config.plugins.updater.endpoints = @([string]$flavorConfig.updaterEndpoint)

New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null
$outputPath = Join-Path $OutputRoot "tauri.$Flavor.conf.json"
$json = $config | ConvertTo-Json -Depth 32
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText($outputPath, $json + [Environment]::NewLine, $utf8NoBom)
Write-Output $outputPath
