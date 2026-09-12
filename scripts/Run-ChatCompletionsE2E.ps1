# Sibling inbound Chat Completions live gate.
# Do not write into the eight-case CLI summary.
# Keep Run-RealClientE2E.ps1 gateway_enable_chat_completions false.
param(
    [Parameter(Mandatory = $true)]
    [string]$Bin,
    [string]$Output = 'test-results\chat-completions-e2e.json',
    [string]$Auth = '',
    [string]$Providers = '',
    [string]$Settings = '',
    [string]$Catalog = '',
    [string]$OpenCodeGoCredentials = '',
    [string]$KeepRuntime = '',
    [string[]]$Case = @(),
    [switch]$Capabilities,
    [switch]$DumpConversion,
    [int]$TimeoutSeconds = 180,
    [string]$Proxy = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$scriptRoot = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($scriptRoot)) {
    $scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$launcher = Join-Path $scriptRoot 'codexhub-python.cmd'
$script = Join-Path $scriptRoot 'e2e_chat_completions.py'
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "missing repository Python launcher: $launcher"
}
if (-not (Test-Path -LiteralPath $script -PathType Leaf)) {
    throw "missing Chat Completions E2E script: $script"
}
if (-not (Test-Path -LiteralPath $Bin -PathType Leaf)) {
    throw "missing candidate --bin: $Bin"
}

if (-not [string]::IsNullOrWhiteSpace($Proxy)) {
    $env:HTTP_PROXY = $Proxy
    $env:HTTPS_PROXY = $Proxy
    $env:ALL_PROXY = $Proxy
    if ([string]::IsNullOrWhiteSpace($env:NO_PROXY)) {
        $env:NO_PROXY = 'localhost,127.0.0.1'
    }
}

$arguments = @(
    $script,
    '--bin', $Bin,
    '--output', $Output,
    '--timeout', [string]$TimeoutSeconds
)
if (-not [string]::IsNullOrWhiteSpace($Auth)) {
    $arguments += @('--auth', $Auth)
}
if (-not [string]::IsNullOrWhiteSpace($Providers)) {
    $arguments += @('--providers', $Providers)
}
if (-not [string]::IsNullOrWhiteSpace($Settings)) {
    $arguments += @('--settings', $Settings)
}
if (-not [string]::IsNullOrWhiteSpace($Catalog)) {
    $arguments += @('--catalog', $Catalog)
}
if (-not [string]::IsNullOrWhiteSpace($OpenCodeGoCredentials)) {
    $arguments += @('--opencode-go-credentials', $OpenCodeGoCredentials)
}
if (-not [string]::IsNullOrWhiteSpace($KeepRuntime)) {
    $arguments += @('--keep-runtime', $KeepRuntime)
}
foreach ($caseId in $Case) {
    if (-not [string]::IsNullOrWhiteSpace($caseId)) {
        $arguments += @('--case', $caseId)
    }
}
if ($Capabilities) {
    $arguments += '--capabilities'
}
if ($DumpConversion) {
    $arguments += '--dump-conversion'
}

& $launcher @arguments
exit $LASTEXITCODE
