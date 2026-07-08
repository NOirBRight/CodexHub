[CmdletBinding()]
param(
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonVersion = "3.13.14",
    [string]$PythonZipUrl = "https://www.python.org/ftp/python/3.13.14/python-3.13.14-embed-amd64.zip",
    [string]$PythonZipSha256 = "90b4e5b9898b72d744650524bff92377c367f44bd5fbd09e3148656c080ad907",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRootPath = [System.IO.Path]::GetFullPath($RepoRoot)
$tauriDir = Join-Path $repoRootPath "src-tauri"
$resourcesDir = Join-Path $tauriDir "resources"
$runtimeDir = Join-Path $resourcesDir "python"
$downloadDir = Join-Path $resourcesDir "downloads"
$zipName = "python-$PythonVersion-embed-amd64.zip"
$zipPath = Join-Path $downloadDir $zipName
$runtimeManifestPath = Join-Path $runtimeDir "codexhub-python-runtime.json"

function Assert-UnderPath([string]$Path, [string]$Root) {
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $fullRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $fullPath.StartsWith($fullRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to operate outside ${fullRoot}: $fullPath"
    }
}

function Test-Hash([string]$Path, [string]$ExpectedSha256) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
    return $actual -eq $ExpectedSha256.ToLowerInvariant()
}

function Invoke-Download([string]$Url, [string]$Destination) {
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($null -ne $curl) {
        & $curl.Source -L --fail --retry 5 --retry-delay 2 --output $Destination $Url
        if ($LASTEXITCODE -ne 0) {
            throw "curl failed with exit code $LASTEXITCODE while downloading $Url"
        }
        return
    }

    Invoke-WebRequest -Uri $Url -OutFile $Destination
}

function Test-PythonRuntimeReady {
    $python = Join-Path $runtimeDir "python.exe"
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        return $false
    }

    & $python -c "import http.server, pathlib, sqlite3, tomllib, urllib.request; print('ok')" *> $null
    if ($LASTEXITCODE -ne 0) {
        return $false
    }

    $proxyScript = Join-Path $repoRootPath "src-python\codex_proxy.py"
    & $python $proxyScript --help *> $null
    return $LASTEXITCODE -eq 0
}

function Update-PythonPathFile {
    $pathFile = Get-ChildItem -LiteralPath $runtimeDir -Filter "python*._pth" -File |
        Select-Object -First 1
    if ($null -eq $pathFile) {
        throw "Python embeddable ._pth file was not found in $runtimeDir"
    }

    $lines = [System.Collections.Generic.List[string]]::new()
    foreach ($line in [System.IO.File]::ReadAllLines($pathFile.FullName)) {
        $lines.Add($line) | Out-Null
    }
    foreach ($relativePath in @("..\src-python", "..\..\..\src-python")) {
        if (-not $lines.Contains($relativePath)) {
            $lines.Add($relativePath) | Out-Null
        }
    }
    [System.IO.File]::WriteAllLines($pathFile.FullName, $lines, [System.Text.UTF8Encoding]::new($false))
}

Assert-UnderPath $runtimeDir $repoRootPath
Assert-UnderPath $downloadDir $repoRootPath

New-Item -ItemType Directory -Force -Path $resourcesDir, $downloadDir | Out-Null

if ($Force -and (Test-Path -LiteralPath $runtimeDir)) {
    Remove-Item -LiteralPath $runtimeDir -Recurse -Force
}

if (Test-PythonRuntimeReady) {
    Write-Host "Python runtime already prepared: $runtimeDir"
    exit 0
}

if (-not (Test-Hash $zipPath $PythonZipSha256)) {
    if (Test-Path -LiteralPath $zipPath -PathType Leaf) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    Write-Host "Downloading Python $PythonVersion embeddable runtime..."
    Invoke-Download $PythonZipUrl $zipPath
}

if (-not (Test-Hash $zipPath $PythonZipSha256)) {
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash.ToLowerInvariant()
    throw "Python runtime hash mismatch. Expected $PythonZipSha256, got $actual."
}

$tempDir = Join-Path $downloadDir "python-extract-$PythonVersion"
Assert-UnderPath $tempDir $repoRootPath
if (Test-Path -LiteralPath $tempDir) {
    Remove-Item -LiteralPath $tempDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $tempDir | Out-Null

Expand-Archive -LiteralPath $zipPath -DestinationPath $tempDir -Force

if (Test-Path -LiteralPath $runtimeDir) {
    Remove-Item -LiteralPath $runtimeDir -Recurse -Force
}
Move-Item -LiteralPath $tempDir -Destination $runtimeDir
Update-PythonPathFile

$manifest = [ordered]@{
    python_version = $PythonVersion
    source_url = $PythonZipUrl
    sha256 = $PythonZipSha256.ToLowerInvariant()
    prepared_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ", [Globalization.CultureInfo]::InvariantCulture)
}
$manifestJson = $manifest | ConvertTo-Json -Depth 4
[System.IO.File]::WriteAllText($runtimeManifestPath, $manifestJson + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))

if (-not (Test-PythonRuntimeReady)) {
    throw "Prepared Python runtime failed validation: $runtimeDir"
}

Write-Host "Python runtime prepared:"
Write-Host "  Runtime: $runtimeDir"
Write-Host "  ZIP:     $zipPath"
Write-Host "  SHA256:  $PythonZipSha256"
