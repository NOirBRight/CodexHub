[CmdletBinding()]
param(
    [ValidateSet("normal", "debug")]
    [string]$Flavor = "normal",
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
Assert-ReleaseFlavorVersion -Flavor $Flavor -Version $version
$targetRoot = Get-FlavorTargetRoot -TauriDir $tauriDir -Flavor $Flavor

$generatedEndpoint = [string]$generatedTauriConfig.plugins.updater.endpoints[0]
if (
    [string]$generatedTauriConfig.productName -ne [string]$flavorConfig.productName -or
    [string]$generatedTauriConfig.identifier -ne [string]$flavorConfig.identifier -or
    [string]$generatedTauriConfig.app.windows[0].title -ne [string]$flavorConfig.windowTitle -or
    $generatedEndpoint -ne [string]$flavorConfig.updaterEndpoint
) {
    throw "Generated Tauri config does not match the requested $Flavor flavor."
}

$sourceRevision = (& git -C $repoRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($sourceRevision)) {
    throw "Unable to resolve portable build commit."
}
$commit = $sourceRevision.Substring(0, [Math]::Min(8, $sourceRevision.Length))
$portableExecutable = "{0}.exe" -f ([string]$flavorConfig.executableBaseName)
$releaseAssetSuffix = [string]$flavorConfig.releaseAssetSuffix
$portableName = "{0}_{1}{2}_portable_{3}" -f ([string]$flavorConfig.releaseAssetPrefix), $version, $releaseAssetSuffix, $commit
$portableDir = Join-Path $OutputRoot $portableName
$portableZip = "$portableDir.zip"

if ($DryRun) {
    [ordered]@{
        flavor = $Flavor
        version = $version
        source_revision = $sourceRevision
        executable = $portableExecutable
        portable_name = $portableName
        installer_name = (Get-ReleaseArtifactName -Flavor $Flavor -Version $version)
        updater_manifest = (Get-ReleaseManifestName -Flavor $Flavor)
        release_optimized = $true
        debug_diagnostics_enabled = ($Flavor -eq "debug")
        generated_config = [ordered]@{
            productName = [string]$generatedTauriConfig.productName
            identifier = [string]$generatedTauriConfig.identifier
            title = [string]$generatedTauriConfig.app.windows[0].title
            bridgePort = [int]$flavorConfig.bridgePort
            gatewayPort = [int]$flavorConfig.gatewayPort
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
$previousCargoTarget = $env:CARGO_TARGET_DIR
try {
    $env:CODEXHUB_BUILD_FLAVOR = $Flavor
    $env:CARGO_TARGET_DIR = $targetRoot
    Push-Location $tauriDir
    try {
        $tauriBuildArgs = @("tauri", "build", "--config", $generatedTauriConfigPath, "--no-bundle", "--ci")
        if ($Flavor -eq "debug") {
            $tauriBuildArgs += @("--features", "debug-diagnostics")
        }
        & cargo @tauriBuildArgs
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
    if ($null -eq $previousCargoTarget) {
        Remove-Item Env:\CARGO_TARGET_DIR -ErrorAction SilentlyContinue
    }
    else {
        $env:CARGO_TARGET_DIR = $previousCargoTarget
    }
}

if (Test-Path -LiteralPath $portableDir) {
    Remove-Item -LiteralPath $portableDir -Recurse -Force
}
if (Test-Path -LiteralPath $portableZip) {
    Remove-Item -LiteralPath $portableZip -Force
}
New-Item -ItemType Directory -Force -Path $portableDir | Out-Null

Copy-Item -LiteralPath (Join-Path $targetRoot "release\codexhub.exe") -Destination (Join-Path $portableDir $portableExecutable)
foreach ($resource in @("config", "src-python", "python")) {
    Copy-Item -LiteralPath (Join-Path $targetRoot "release\$resource") -Destination $portableDir -Recurse
}
Compress-Archive -Path (Join-Path $portableDir "*") -DestinationPath $portableZip -CompressionLevel Optimal

$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $portableZip).Hash.ToLowerInvariant()
Write-Host "Windows portable ready:"
Write-Host "  Directory: $portableDir"
Write-Host "  Archive:   $portableZip"
Write-Host "  SHA256:    $hash"
