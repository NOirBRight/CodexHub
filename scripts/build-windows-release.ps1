[CmdletBinding()]
param(
    [string]$PrivateKeyPath = (Join-Path $env:USERPROFILE ".codexhub\codexhub-updater.key"),
    [string]$PrivateKeyPassword = $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD,
    [string]$ReleaseBaseUrl = "https://github.com/NOirBRight/CodexHub/releases/latest/download",
    [string]$Notes = "",
    [switch]$SkipFrontendBuild
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$frontendDir = Join-Path $repoRoot "frontend"
$tauriDir = Join-Path $repoRoot "src-tauri"
$tauriConfigPath = Join-Path $tauriDir "tauri.conf.json"
$preparePythonRuntimePath = Join-Path $PSScriptRoot "Prepare-PythonRuntime.ps1"

if (-not (Test-Path -LiteralPath $PrivateKeyPath -PathType Leaf)) {
    throw "Updater private key was not found: $PrivateKeyPath"
}

$tauriConfig = Get-Content -Raw -LiteralPath $tauriConfigPath | ConvertFrom-Json
$productName = [string]$tauriConfig.productName
$version = [string]$tauriConfig.version

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
    Push-Location $frontendDir
    try {
        & npm run build
        if ($LASTEXITCODE -ne 0) {
            throw "Frontend build failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }
}

$previousSigningKey = $env:TAURI_SIGNING_PRIVATE_KEY
$previousSigningPassword = $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD

try {
    $env:TAURI_SIGNING_PRIVATE_KEY = (Resolve-Path -LiteralPath $PrivateKeyPath).Path
    if ([string]::IsNullOrEmpty($PrivateKeyPassword)) {
        Remove-Item Env:\TAURI_SIGNING_PRIVATE_KEY_PASSWORD -ErrorAction SilentlyContinue
    }
    else {
        $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = $PrivateKeyPassword
    }

    Push-Location $tauriDir
    try {
        & cargo tauri build --bundles nsis --ci
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
}

$bundleDir = Join-Path $tauriDir "target\release\bundle\nsis"
$installerName = "{0}_{1}_x64-setup.exe" -f $productName, $version
$installerPath = Join-Path $bundleDir $installerName
$signaturePath = "$installerPath.sig"

if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
    throw "Expected NSIS installer was not generated: $installerPath"
}

if (-not (Test-Path -LiteralPath $signaturePath -PathType Leaf)) {
    throw "Expected updater signature was not generated: $signaturePath"
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
            url = "$releaseBaseUrl/$([Uri]::EscapeDataString($installerName))"
        }
    }
}

$manifestPath = Join-Path $bundleDir "latest.json"
$manifestJson = $manifest | ConvertTo-Json -Depth 8
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($manifestPath, $manifestJson + [Environment]::NewLine, $utf8NoBom)

$roundTrip = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
$platform = $roundTrip.platforms."windows-x86_64"
if ($roundTrip.version -ne $version -or $platform.signature -ne $signature -or [string]::IsNullOrWhiteSpace($platform.url)) {
    throw "Generated latest.json failed validation: $manifestPath"
}

$installerHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $installerPath).Hash.ToLowerInvariant()

Write-Host "Windows release artifacts ready:"
Write-Host "  Installer: $installerPath"
Write-Host "  Signature: $signaturePath"
Write-Host "  Manifest:  $manifestPath"
Write-Host "  SHA256:    $installerHash"
