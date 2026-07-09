[CmdletBinding()]
param(
    [ValidateSet("stable", "beta")]
    [string]$Flavor = "stable",
    [string]$PrivateKeyPath = (Join-Path $env:USERPROFILE ".codexhub\codexhub-updater.key"),
    [string]$PrivateKeyPassword = $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD,
    [string]$ReleaseBaseUrl = "",
    [string]$Notes = "",
    [switch]$SkipFrontendBuild
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$frontendDir = Join-Path $repoRoot "frontend"
$tauriDir = Join-Path $repoRoot "src-tauri"
$preparePythonRuntimePath = Join-Path $PSScriptRoot "Prepare-PythonRuntime.ps1"
$flavorManifestPath = Join-Path $repoRoot "config\build-flavors.json"
$flavorManifest = Get-Content -Raw -LiteralPath $flavorManifestPath | ConvertFrom-Json
$flavorConfig = $flavorManifest.$Flavor
if ($null -eq $flavorConfig) {
    throw "Unknown build flavor: $Flavor"
}
if ([string]::IsNullOrWhiteSpace($ReleaseBaseUrl)) {
    if ($Flavor -eq "beta") {
        $ReleaseBaseUrl = "https://github.com/NOirBRight/CodexHub/releases/download/beta"
    }
    else {
        $ReleaseBaseUrl = "https://github.com/NOirBRight/CodexHub/releases/latest/download"
    }
}
$generatedTauriConfigPath = (& (Join-Path $PSScriptRoot "Build-TauriConfig.ps1") -Flavor $Flavor -RepoRoot $repoRoot).Trim()
$tauriConfigPath = $generatedTauriConfigPath

if (-not (Test-Path -LiteralPath $PrivateKeyPath -PathType Leaf)) {
    throw "Updater private key was not found: $PrivateKeyPath"
}

$tauriConfig = Get-Content -Raw -LiteralPath $tauriConfigPath | ConvertFrom-Json
$productName = [string]$tauriConfig.productName
$version = [string]$tauriConfig.version
$bundleDir = Join-Path $tauriDir "target\release\bundle\nsis"
$assetPrefix = [string]$flavorConfig.releaseAssetPrefix
$canonicalInstallerName = "{0}_{1}_x64-setup.exe" -f $assetPrefix, $version

$installerNameCandidates = [System.Collections.Generic.List[string]]::new()
foreach ($nameCandidate in @(
    $assetPrefix,
    $productName,
    ($productName -replace "\s+", ""),
    ($productName -replace "\s+", "_")
)) {
    if (-not [string]::IsNullOrWhiteSpace($nameCandidate)) {
        $installerName = "{0}_{1}_x64-setup.exe" -f $nameCandidate, $version
        if (-not $installerNameCandidates.Contains($installerName)) {
            $installerNameCandidates.Add($installerName)
        }
    }
}

if ([string]::IsNullOrWhiteSpace($productName)) {
    throw "tauri.conf.json is missing productName."
}

if ([string]::IsNullOrWhiteSpace($version)) {
    throw "tauri.conf.json is missing version."
}

if (-not ($tauriConfig.bundle.targets -contains "nsis")) {
    throw "tauri.conf.json must include bundle.targets = [""nsis""] for the Windows release."
}

if ($tauriConfig.bundle.createUpdaterArtifacts -ne $true) {
    throw "tauri.conf.json must set bundle.createUpdaterArtifacts = true."
}

& $preparePythonRuntimePath -RepoRoot $repoRoot
if ($LASTEXITCODE -ne 0) {
    throw "Python runtime preparation failed with exit code $LASTEXITCODE."
}

if (-not $SkipFrontendBuild) {
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
}

$previousSigningKey = $env:TAURI_SIGNING_PRIVATE_KEY
$previousSigningPassword = $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD
$previousBuildFlavor = $env:CODEXHUB_BUILD_FLAVOR
$previousTauriConfig = $env:TAURI_CONFIG

try {
    $env:TAURI_SIGNING_PRIVATE_KEY = (Resolve-Path -LiteralPath $PrivateKeyPath).Path
    if ([string]::IsNullOrEmpty($PrivateKeyPassword)) {
        Remove-Item Env:\TAURI_SIGNING_PRIVATE_KEY_PASSWORD -ErrorAction SilentlyContinue
    }
    else {
        $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = $PrivateKeyPassword
    }
    $env:CODEXHUB_BUILD_FLAVOR = $Flavor
    $env:TAURI_CONFIG = $generatedTauriConfigPath

    if (Test-Path -LiteralPath $bundleDir -PathType Container) {
        foreach ($installerName in $installerNameCandidates) {
            $artifactPath = Join-Path $bundleDir $installerName
            Remove-Item -LiteralPath $artifactPath -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath "$artifactPath.sig" -Force -ErrorAction SilentlyContinue
        }
    }

    Push-Location $tauriDir
    try {
        & cargo tauri build --config $generatedTauriConfigPath --bundles nsis --ci
        if ($LASTEXITCODE -ne 0) {
            throw "Tauri Windows release build failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }
}
finally {
    if ($null -eq $previousSigningKey) {
        Remove-Item Env:\TAURI_SIGNING_PRIVATE_KEY -ErrorAction SilentlyContinue
    }
    else {
        $env:TAURI_SIGNING_PRIVATE_KEY = $previousSigningKey
    }

    if ($null -eq $previousSigningPassword) {
        Remove-Item Env:\TAURI_SIGNING_PRIVATE_KEY_PASSWORD -ErrorAction SilentlyContinue
    }
    else {
        $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = $previousSigningPassword
    }

    if ($null -eq $previousBuildFlavor) {
        Remove-Item Env:\CODEXHUB_BUILD_FLAVOR -ErrorAction SilentlyContinue
    }
    else {
        $env:CODEXHUB_BUILD_FLAVOR = $previousBuildFlavor
    }

    if ($null -eq $previousTauriConfig) {
        Remove-Item Env:\TAURI_CONFIG -ErrorAction SilentlyContinue
    }
    else {
        $env:TAURI_CONFIG = $previousTauriConfig
    }
}

$installerPath = Join-Path $bundleDir $canonicalInstallerName
$signaturePath = "$installerPath.sig"
$expectedCandidateList = ($installerNameCandidates | ForEach-Object { "$_ (+ .sig)" }) -join ", "

$resolvedInstaller = foreach ($installerName in $installerNameCandidates) {
    $candidateInstallerPath = Join-Path $bundleDir $installerName
    $candidateSignaturePath = "$candidateInstallerPath.sig"
    if ((Test-Path -LiteralPath $candidateInstallerPath -PathType Leaf) -and
        (Test-Path -LiteralPath $candidateSignaturePath -PathType Leaf)) {
        [pscustomobject]@{
            InstallerPath = $candidateInstallerPath
            SignaturePath = $candidateSignaturePath
            IsCanonical = ($installerName -eq $canonicalInstallerName)
        }
    }
}

if ($null -eq $resolvedInstaller) {
    throw "Expected NSIS installer/signature pair was not generated for flavor '$Flavor'. Looked for: $expectedCandidateList"
}

if ($resolvedInstaller -is [array]) {
    $resolvedInstaller = $resolvedInstaller | Select-Object -First 1
}

if (-not $resolvedInstaller.IsCanonical) {
    Move-Item -LiteralPath $resolvedInstaller.InstallerPath -Destination $installerPath -Force
    Move-Item -LiteralPath $resolvedInstaller.SignaturePath -Destination $signaturePath -Force
}

if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
    throw "Expected NSIS installer was not generated after canonicalization: $installerPath"
}

if (-not (Test-Path -LiteralPath $signaturePath -PathType Leaf)) {
    throw "Expected updater signature was not generated after canonicalization: $signaturePath"
}

$signature = (Get-Content -Raw -LiteralPath $signaturePath).Trim()
if ([string]::IsNullOrWhiteSpace($signature)) {
    throw "Updater signature is empty: $signaturePath"
}

if ([string]::IsNullOrWhiteSpace($Notes)) {
    $Notes = "$productName $version"
}

$releaseBaseUrl = $ReleaseBaseUrl.TrimEnd("/")
$manifest = [ordered]@{
    version = $version
    notes = $Notes
    pub_date = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ", [Globalization.CultureInfo]::InvariantCulture)
    platforms = [ordered]@{
        "windows-x86_64" = [ordered]@{
            signature = $signature
            url = "$releaseBaseUrl/$([Uri]::EscapeDataString($canonicalInstallerName))"
        }
    }
}

$manifestPath = Join-Path $bundleDir ([string]$flavorConfig.updaterManifestName)
$manifestName = [System.IO.Path]::GetFileName($manifestPath)
$manifestJson = $manifest | ConvertTo-Json -Depth 8
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($manifestPath, $manifestJson + [Environment]::NewLine, $utf8NoBom)

$roundTrip = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
$platform = $roundTrip.platforms."windows-x86_64"
if ($roundTrip.version -ne $version -or $platform.signature -ne $signature -or [string]::IsNullOrWhiteSpace($platform.url)) {
    throw "Generated $manifestName failed validation: $manifestPath"
}

$installerHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $installerPath).Hash.ToLowerInvariant()

Write-Host "Windows release artifacts ready:"
Write-Host "  Installer: $installerPath"
Write-Host "  Signature: $signaturePath"
Write-Host "  Manifest:  $manifestPath"
Write-Host "  SHA256:    $installerHash"
