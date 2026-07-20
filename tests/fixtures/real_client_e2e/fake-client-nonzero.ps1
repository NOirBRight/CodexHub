param(
    [switch]$Ephemeral, [switch]$Json, [switch]$Print, [switch]$NoSession,
    [string]$m, [string]$s, [string]$a, [string]$C,
    [string]$Model, [string]$Format, [string]$Mode,
    [Parameter(ValueFromRemainingArguments = $true)][object[]]$RemainingArguments
)

if ($env:CODEXHUB_E2E_CASE) {
    [Console]::Error.WriteLine('Authorization: Bearer fixture-private-token C:\Users\private-account')
    exit 7
}
exit 0
