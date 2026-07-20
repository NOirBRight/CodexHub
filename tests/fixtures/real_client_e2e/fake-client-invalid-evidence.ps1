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
$model = $env:CODEXHUB_E2E_MODEL
$sentinel = $env:CODEXHUB_E2E_SENTINEL
@(
    @{ event = 'model_selected'; model = $model },
    @{ event = 'tool_call'; tool = 'read_file'; read_only = $true },
    @{ event = 'stream_delta'; text = $sentinel },
    @{ event = 'request_complete'; status = 200 },
    @{ event = 'fallback' },
    @{ event = 'reconnect' },
    @{ event = 'terminal'; classification = 'completed' },
    @{ event = 'terminal'; classification = 'completed' }
) | ForEach-Object { $_ | ConvertTo-Json -Compress }
