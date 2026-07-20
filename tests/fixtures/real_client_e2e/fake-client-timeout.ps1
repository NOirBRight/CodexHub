param(
    [switch]$Ephemeral, [switch]$Json, [switch]$Print, [switch]$NoSession,
    [string]$m, [string]$s, [string]$a, [string]$C,
    [string]$Model, [string]$Format, [string]$Mode,
    [Parameter(ValueFromRemainingArguments = $true)][object[]]$RemainingArguments
)

if (-not $env:CODEXHUB_E2E_CASE) {
    exit 0
}
if ($env:CODEXHUB_E2E_CASE -eq 'codex-cli-volc') {
    [Console]::Error.WriteLine('Authorization: Bearer fixture-private-token C:\Users\private-account')
    exit 7
}
$startInfo = [System.Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$startInfo.Arguments = '-NoProfile -NonInteractive -Command "[System.Threading.Thread]::Sleep(60000)"'
$startInfo.UseShellExecute = $false
$child = [System.Diagnostics.Process]::Start($startInfo)
[System.IO.File]::WriteAllText(
    "$($env:CODEXHUB_E2E_SENTINEL_PATH).child-pid",
    [string]$child.Id,
    [System.Text.Encoding]::ASCII
)
[System.Threading.Thread]::Sleep(60000)
