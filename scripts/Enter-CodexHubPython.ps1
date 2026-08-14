[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

. (Join-Path $PSScriptRoot 'Resolve-CodexHubPython.ps1')
$python = Resolve-CodexHubPythonPath -Root $RepoRoot

Write-Output "CodexHub Python runtime: $python"
Write-Output 'This shell now resolves python and pytest through the CodexHub runtime.'
