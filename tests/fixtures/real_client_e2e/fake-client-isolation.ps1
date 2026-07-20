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
$caseRoot = Split-Path -Parent $env:CODEXHUB_E2E_SENTINEL_PATH
foreach ($name in @('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'CODEX_HOME', 'XDG_CONFIG_HOME', 'TEMP', 'TMP')) {
    $value = [System.Environment]::GetEnvironmentVariable($name)
    if (-not $value -or -not $value.StartsWith($caseRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        @{ event = 'error'; classification = 'isolation_path_missing' } | ConvertTo-Json -Compress
        exit 11
    }
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
