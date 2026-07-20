param(
    [switch]$Ephemeral, [switch]$Json, [switch]$Print, [switch]$NoSession,
    [string]$m, [string]$s, [string]$a, [string]$C,
    [string]$Model, [string]$Format, [string]$Mode,
    [Parameter(ValueFromRemainingArguments = $true)][object[]]$RemainingArguments
)

if (-not $env:CODEXHUB_E2E_CASE) {
    exit 0
}
@{ event = 'error'; status = 500 } | ConvertTo-Json -Compress
'Authorization: Bearer fixture-private-token C:\Users\private-account ' + ('x' * 70000)
[Console]::Error.WriteLine('api_key=fixture-private-token')
exit 0
