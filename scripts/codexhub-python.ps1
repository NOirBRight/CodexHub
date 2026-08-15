[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$PythonArguments
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$scriptRoot = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($scriptRoot)) {
    $scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = (Resolve-Path (Join-Path $scriptRoot '..')).Path
}

. (Join-Path $scriptRoot 'Resolve-CodexHubPython.ps1')
$requiresPytest = @($PythonArguments).Count -ge 2 -and
    $PythonArguments[0] -eq '-m' -and
    $PythonArguments[1] -eq 'pytest'
$python = Resolve-CodexHubPythonPath `
    -Root $RepoRoot `
    -RequirePytest:$requiresPytest

# Keep Python subprocesses on the same interpreter as the top-level command.
# This is especially important for Rust/Python lifecycle fixtures.
$env:CODEXHUB_PYTHON = $python
$env:CODEXHUB_PROXY_PYTHON = $python
$env:CODEXHUB_E2E_PYTHON = $python

& $python @PythonArguments
exit $LASTEXITCODE
