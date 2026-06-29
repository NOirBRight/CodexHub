$ErrorActionPreference = 'Stop'
$ProxyDir = Split-Path -Parent $PSCommandPath
$ProxyPort = '9099'
$env:CODEX_PROXY_PORT = $ProxyPort
Set-Location -LiteralPath $ProxyDir
$CatalogPath = Join-Path $env:USERPROFILE '.codex\model-catalogs\codex-proxy-official-ollama.json'
if (-not (Test-Path -LiteralPath $CatalogPath)) {
    python (Join-Path $ProxyDir 'catalog_sync.py') --sync | Out-Null
}
python (Join-Path $ProxyDir 'codex_proxy.py') --port $ProxyPort
