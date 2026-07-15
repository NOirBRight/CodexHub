param(
    [string]$Workspace = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
    [string]$OutputDir = '',
    [string]$CodexCommand = '',
    [int]$TimeoutSeconds = 240,
    [int]$GatewayStartupSeconds = 20,
    [int]$ReadinessTimeoutSeconds = 60
)

$ErrorActionPreference = 'Stop'
$Workspace = (Resolve-Path -LiteralPath $Workspace).Path
if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    $OutputDir = Join-Path ([System.IO.Path]::GetTempPath()) (
        'codexhub-issue140-{0}-{1}' -f $PID, (Get-Date -Format 'yyyyMMddHHmmss')
    )
}
$OutputDir = [System.IO.Path]::GetFullPath($OutputDir)
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$qualificationScript = Join-Path $PSScriptRoot 'qualify-issue-108-glm-tool-surface.ps1'
$powerShellPath = (Get-Process -Id $PID).Path
$runs = [System.Collections.Generic.List[object]]::new()

foreach ($runNumber in 1..2) {
    $runOutputDir = Join-Path $OutputDir ("pass-{0}" -f $runNumber)
    New-Item -ItemType Directory -Force -Path $runOutputDir | Out-Null
    $arguments = @(
        '-NoLogo',
        '-NoProfile',
        '-File', $qualificationScript,
        '-Workspace', $Workspace,
        '-OutputDir', $runOutputDir,
        '-TimeoutSeconds', "$TimeoutSeconds",
        '-GatewayStartupSeconds', "$GatewayStartupSeconds",
        '-ReadinessTimeoutSeconds', "$ReadinessTimeoutSeconds",
        '-ExternalIsolationQualification',
        '-Issue140NativeResponsesQualification'
    )
    if (-not [string]::IsNullOrWhiteSpace($CodexCommand)) {
        $arguments += @('-CodexCommand', $CodexCommand)
    }

    $childOutput = (& $powerShellPath @arguments 2>&1 | Out-String)
    $childExitCode = $LASTEXITCODE
    $runDirectory = @(
        Get-ChildItem -LiteralPath $runOutputDir -Directory -Filter 'run-*' |
            Sort-Object LastWriteTimeUtc -Descending
    ) | Select-Object -First 1
    $childSummary = $null
    if ($null -ne $runDirectory) {
        $childSummaryPath = Join-Path $runDirectory.FullName 'summary.json'
        if (Test-Path -LiteralPath $childSummaryPath) {
            $childSummary = Get-Content -LiteralPath $childSummaryPath -Raw | ConvertFrom-Json
        }
    }

    $toolSequence = if ($null -ne $childSummary) {
        @($childSummary.tool_sequence)
    }
    else {
        @()
    }
    $mutationValid = (
        $null -ne $childSummary -and
        (@($childSummary.git_status) -join "`n") -eq ' M qualification-target.txt' -and
        [string]$childSummary.git_numstat -eq "1`t1`tqualification-target.txt"
    )
    $runPassed = (
        $childExitCode -eq 0 -and
        $null -ne $childSummary -and
        [bool]$childSummary.passed -and
        [bool]$childSummary.native_responses_contract_evidence_validated -and
        ($toolSequence -join ',') -eq 'shell_command,apply_patch,shell_command' -and
        $mutationValid -and
        [int]$childSummary.native_responses_tool_codec_adapted_count -gt 0 -and
        [int]$childSummary.caller_apply_patch_grammar_contract_count -gt 0 -and
        [int]$childSummary.upstream_strict_apply_patch_contract_count -gt 0 -and
        [int]$childSummary.request_error_event_count -eq 0 -and
        [int]$childSummary.upstream_retry_event_count -eq 0 -and
        [int]$childSummary.upstream_protocol_fallback_event_count -eq 0
    )
    [void]$runs.Add([ordered]@{
        run_number = $runNumber
        child_exit_code = $childExitCode
        passed = $runPassed
        tool_sequence = @($toolSequence)
        mutation_valid = $mutationValid
        native_responses_tool_codec_adapted_count = if ($null -ne $childSummary) {
            [int]$childSummary.native_responses_tool_codec_adapted_count
        }
        else { 0 }
        request_error_event_count = if ($null -ne $childSummary) {
            [int]$childSummary.request_error_event_count
        }
        else { 0 }
        upstream_retry_event_count = if ($null -ne $childSummary) {
            [int]$childSummary.upstream_retry_event_count
        }
        else { 0 }
        upstream_protocol_fallback_event_count = if ($null -ne $childSummary) {
            [int]$childSummary.upstream_protocol_fallback_event_count
        }
        else { 0 }
        child_output_present = -not [string]::IsNullOrWhiteSpace($childOutput)
    })
}

$summary = [ordered]@{
    schema = 'codexhub.issue140.native-responses-tools-qualification.v1'
    model = 'ollama-cloud/glm-5.2'
    expected_run_count = 2
    completed_run_count = @($runs | Where-Object { $_.passed }).Count
    runs = @($runs)
    passed = @($runs).Count -eq 2 -and @($runs | Where-Object { -not $_.passed }).Count -eq 0
}
$summaryPath = Join-Path $OutputDir 'summary.json'
$summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $summaryPath -Encoding UTF8
Get-Content -LiteralPath $summaryPath -Raw
if (-not $summary.passed) {
    exit 1
}
