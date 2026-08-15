[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($MyInvocation.InvocationName -ne '.') {
    throw 'Enter-CodexHubPython.ps1 must be dot-sourced so it can update the caller shell: . .\scripts\Enter-CodexHubPython.ps1'
}

. (Join-Path $PSScriptRoot 'Resolve-CodexHubPython.ps1')
$python = Resolve-CodexHubPythonPath -Root $RepoRoot

Write-Output "CodexHub Python runtime: $python"
Write-Output 'This shell now resolves python and pytest through the CodexHub runtime.'
