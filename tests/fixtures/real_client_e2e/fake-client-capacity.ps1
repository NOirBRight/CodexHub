param(
    [switch]$Ephemeral, [switch]$Json, [switch]$Print, [switch]$NoSession,
    [string]$m, [string]$s, [string]$a, [string]$C,
    [string]$Model, [string]$Format, [string]$Mode,
    [Parameter(ValueFromRemainingArguments = $true)][object[]]$RemainingArguments
)

$caseId = $env:CODEXHUB_E2E_CASE
if (-not $caseId) {
    exit 0
}
if ($env:CODEXHUB_E2E_ATTEMPT -eq '1') {
    @{ event = 'provider_capacity'; status = 429; output_seen = $false } | ConvertTo-Json -Compress
    exit 9
}
$model = $env:CODEXHUB_E2E_MODEL
$sentinel = $env:CODEXHUB_E2E_SENTINEL
@(
    @{ event = 'model_selected'; model = $model },
    @{ event = 'tool_call'; tool = 'read_file'; read_only = $true },
    @{ event = 'stream_delta'; text = $sentinel },
    @{ event = 'request_complete'; status = 200 },
    @{ event = 'terminal'; classification = 'completed' }
) | ForEach-Object { $_ | ConvertTo-Json -Compress }
