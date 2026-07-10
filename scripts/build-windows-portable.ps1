[CmdletBinding()]
param(
    [ValidateSet("stable", "beta")]
    [string]$Flavor = "stable",
    [string]$OutputRoot = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
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

$version = [string](Get-Content -Raw -LiteralPath $generatedTauriConfigPath | ConvertFrom-Json).version
$commit = (& git -C $repoRoot rev-parse --short=8 HEAD).Trim()
$portableName = "{0}_{1}_portable_{2}" -f ([string]$flavorConfig.releaseAssetPrefix), $version, $commit
$portableDir = Join-Path $OutputRoot $portableName
$portableZip = "$portableDir.zip"

if (Test-Path -LiteralPath $portableDir) {
    Remove-Item -LiteralPath $portableDir -Recurse -Force
}
if (Test-Path -LiteralPath $portableZip) {
    Remove-Item -LiteralPath $portableZip -Force
}
New-Item -ItemType Directory -Force -Path $portableDir | Out-Null

$portableExecutable = "{0}.exe" -f ([string]$flavorConfig.executableBaseName)
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
