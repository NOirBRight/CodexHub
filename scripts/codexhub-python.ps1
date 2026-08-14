[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$PythonArguments
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

. (Join-Path $PSScriptRoot 'Resolve-CodexHubPython.ps1')
$requiresPytest = @($PythonArguments).Count -ge 2 -and
    $PythonArguments[0] -eq '-m' -and
    $PythonArguments[1] -eq 'pytest'
$python = Resolve-CodexHubPythonPath -Root $RepoRoot -RequirePytest:$requiresPytest

# Keep Python subprocesses on the same interpreter as the top-level command.
# This is especially important for Rust/Python lifecycle fixtures.
$env:CODEXHUB_PYTHON = $python
$env:CODEXHUB_PROXY_PYTHON = $python
$env:CODEXHUB_E2E_PYTHON = $python

& $python @PythonArguments
exit $LASTEXITCODE
