$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $PSCommandPath
$RepoRoot = Split-Path -Parent $ScriptDir
$ProxyDir = Join-Path $RepoRoot 'src-python'
$ConfigDir = Join-Path $RepoRoot 'config'
$ProxyPort = '9099'
$env:CODEX_PROXY_PORT = $ProxyPort
$env:CODEX_HOME = Join-Path $env:USERPROFILE '.codex'
$pythonResolver = Join-Path $ScriptDir 'Resolve-CodexHubPython.ps1'
. $pythonResolver
$Python = Resolve-CodexHubPythonPath -Root $RepoRoot -PreferBundled
Set-Location -LiteralPath $ProxyDir
$CatalogPath = Join-Path $env:USERPROFILE '.codex\model-catalogs\codexhub-model-catalog.json'
if (-not (Test-Path -LiteralPath $CatalogPath)) {
    & $Python (Join-Path $ProxyDir 'catalog_sync.py') --sync | Out-Null
}
& $Python (Join-Path $ProxyDir 'codex_proxy.py') --port $ProxyPort
