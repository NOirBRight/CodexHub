[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("stable", "beta")]
    [string]$Flavor,
    [string]$OutputRoot = "",
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = $RepoRoot
$frontendDir = Join-Path $repoRoot "frontend"
$tauriDir = Join-Path $repoRoot "src-tauri"
$flavorManifest = Get-Content -Raw -LiteralPath (Join-Path $repoRoot "config\build-flavors.json") | ConvertFrom-Json
$flavorConfig = $flavorManifest.$Flavor
if ($null -eq $flavorConfig) {
    throw "Unknown build flavor: $Flavor"
}

if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $OutputRoot = Join-Path $repoRoot "output\portable"
}

$generatedTauriConfigPath = (& (Join-Path $PSScriptRoot "Build-TauriConfig.ps1") -Flavor $Flavor -RepoRoot $repoRoot).Trim()
$generatedTauriConfig = Get-Content -Raw -LiteralPath $generatedTauriConfigPath | ConvertFrom-Json
$version = [string]$generatedTauriConfig.version
. (Join-Path $PSScriptRoot "ReleaseChannel.ps1")
Assert-ReleaseChannelVersion -Flavor $Flavor -Version $version

$generatedEndpoint = [string]$generatedTauriConfig.plugins.updater.endpoints[0]
if (
    [string]$generatedTauriConfig.productName -ne [string]$flavorConfig.productName -or
    [string]$generatedTauriConfig.identifier -ne [string]$flavorConfig.identifier -or
    [string]$generatedTauriConfig.app.windows[0].title -ne [string]$flavorConfig.windowTitle -or
    $generatedEndpoint -ne [string]$flavorConfig.updaterEndpoint
) {
    throw "Generated Tauri config does not match the requested $Flavor flavor."
}

$commit = (& git -C $repoRoot rev-parse --short=8 HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($commit)) {
    throw "Unable to resolve portable build commit."
}
$portableExecutable = "{0}.exe" -f ([string]$flavorConfig.executableBaseName)
$portableName = "{0}_{1}_portable_{2}" -f ([string]$flavorConfig.releaseAssetPrefix), $version, $commit
$portableDir = Join-Path $OutputRoot $portableName
$portableZip = "$portableDir.zip"

if ($DryRun) {
    [ordered]@{
        flavor = $Flavor
        version = $version
        executable = $portableExecutable
        portable_name = $portableName
        generated_config = [ordered]@{
            productName = [string]$generatedTauriConfig.productName
            identifier = [string]$generatedTauriConfig.identifier
            title = [string]$generatedTauriConfig.app.windows[0].title
            updaterEndpoint = $generatedEndpoint
        }
    } | ConvertTo-Json -Depth 4 -Compress
    return
}

& (Join-Path $PSScriptRoot "Prepare-PythonRuntime.ps1") -RepoRoot $repoRoot
if ($LASTEXITCODE -ne 0) {
    throw "Python runtime preparation failed with exit code $LASTEXITCODE."
}

$previousFrontendPort = $env:CODEXHUB_FRONTEND_PORT
Push-Location $frontendDir
try {
    $env:CODEXHUB_FRONTEND_PORT = [string]$flavorConfig.frontendPort
    & npm run build
    if ($LASTEXITCODE -ne 0) {
        throw "Frontend build failed with exit code $LASTEXITCODE."
    }
}
finally {
    if ($null -eq $previousFrontendPort) {
        Remove-Item Env:\CODEXHUB_FRONTEND_PORT -ErrorAction SilentlyContinue
    }
    else {
        $env:CODEXHUB_FRONTEND_PORT = $previousFrontendPort
    }
    Pop-Location
}

$previousBuildFlavor = $env:CODEXHUB_BUILD_FLAVOR
try {
    $env:CODEXHUB_BUILD_FLAVOR = $Flavor
    Push-Location $tauriDir
    try {
        & cargo tauri build --config $generatedTauriConfigPath --no-bundle --ci
        if ($LASTEXITCODE -ne 0) {
            throw "Tauri portable build failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }
}
finally {
    if ($null -eq $previousBuildFlavor) {
        Remove-Item Env:\CODEXHUB_BUILD_FLAVOR -ErrorAction SilentlyContinue
    }
    else {
        $env:CODEXHUB_BUILD_FLAVOR = $previousBuildFlavor
    }
}

if (Test-Path -LiteralPath $portableDir) {
    Remove-Item -LiteralPath $portableDir -Recurse -Force
}
if (Test-Path -LiteralPath $portableZip) {
    Remove-Item -LiteralPath $portableZip -Force
}
New-Item -ItemType Directory -Force -Path $portableDir | Out-Null

Copy-Item -LiteralPath (Join-Path $tauriDir "target\release\codexhub.exe") -Destination (Join-Path $portableDir $portableExecutable)
foreach ($resource in @("config", "src-python", "python")) {
    Copy-Item -LiteralPath (Join-Path $tauriDir "target\release\$resource") -Destination $portableDir -Recurse
}
Compress-Archive -Path (Join-Path $portableDir "*") -DestinationPath $portableZip -CompressionLevel Optimal

$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $portableZip).Hash.ToLowerInvariant()
Write-Host "Windows portable ready:"
Write-Host "  Directory: $portableDir"
Write-Host "  Archive:   $portableZip"
Write-Host "  SHA256:    $hash"
