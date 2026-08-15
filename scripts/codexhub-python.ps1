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
# Prefer the prepared bundled runtime for repository scripts.  Environment and
# bootstrap modules must stay on the local or host 3.13 runtime because the
# embedded application runtime intentionally does not contain venv, pip, or
# pytest.
$bootstrapModule = @($PythonArguments).Count -ge 2 -and
    $PythonArguments[0] -eq '-m' -and
    $PythonArguments[1] -in @('pip', 'venv', 'ensurepip', 'pytest')
$python = Resolve-CodexHubPythonPath `
    -Root $RepoRoot `
    -PreferBundled:(-not $bootstrapModule) `
    -RequirePytest:$requiresPytest

# Keep Python subprocesses on the same interpreter as the top-level command.
# This is especially important for Rust/Python lifecycle fixtures.
$env:CODEXHUB_PYTHON = $python
$env:CODEXHUB_PROXY_PYTHON = $python
$env:CODEXHUB_E2E_PYTHON = $python

& $python @PythonArguments
exit $LASTEXITCODE
