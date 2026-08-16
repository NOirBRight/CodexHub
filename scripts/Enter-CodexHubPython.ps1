[CmdletBinding()]
param(
    [string]$RepoRoot
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($MyInvocation.InvocationName -ne '.') {
    throw 'Enter-CodexHubPython.ps1 must be dot-sourced so it can update the caller shell: . .\scripts\Enter-CodexHubPython.ps1'
}

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    if ([string]::IsNullOrWhiteSpace($PSScriptRoot)) {
        throw 'CodexHub Python activation could not determine its script directory; pass -RepoRoot explicitly.'
    }
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}

. (Join-Path $PSScriptRoot 'Resolve-CodexHubPython.ps1')
$python = Resolve-CodexHubPythonPath -Root $RepoRoot

# PATH changes are sufficient for new processes, but PowerShell may retain a
# previously discovered ``python`` command in its command table.  Bind the
# two documented interactive commands explicitly so the current shell cannot
# fall back to an ambient 3.11 interpreter after activation.
$global:CodexHubPythonRuntime = $python
function global:python {
    & $global:CodexHubPythonRuntime @args
}
function global:pytest {
    & $global:CodexHubPythonRuntime -m pytest @args
}

Write-Output "CodexHub Python runtime: $python"
Write-Output 'This shell now resolves python and pytest through the CodexHub runtime.'
