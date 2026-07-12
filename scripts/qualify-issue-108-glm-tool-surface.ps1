param(
    [string]$Workspace = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
    [string]$OutputDir = '',
    [string]$CodexCommand = '',
    [int]$TimeoutSeconds = 240,
    [int]$ProxyStartupSeconds = 20,
    [switch]$CaptureRequestShape,
    [switch]$UseCliSandboxBypass,
    [switch]$LifecycleReplay,
    [bool]$FailFastAfterFirstPostSuccessToolChoice = $true
)

$ErrorActionPreference = 'Stop'

function ConvertTo-ProcessArgument {
    param([AllowNull()][string]$Argument)

    if ($null -eq $Argument -or $Argument.Length -eq 0) {
        return '""'
    }
    if ($Argument -notmatch '[\s"]') {
        return $Argument
    }
    $escaped = $Argument -replace '(\\*)"', '$1$1\"'
    $escaped = $escaped -replace '(\\+)$', '$1$1'
    return '"' + $escaped + '"'
}

function Invoke-Checked {
    param(
        [string]$FileName,
        [string[]]$Arguments
    )

    & $FileName @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FileName exited with code $LASTEXITCODE"
    }
}

function Start-TrackedProcess {
    param(
        [string]$FileName,
        [string[]]$Arguments,
        [string]$WorkingDirectory,
        [hashtable]$Environment = @{}
    )

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FileName
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.UseShellExecute = $false
    # The desktop task injects a managed read-only profile and thread context
    # into its own process.  The qualification child must instead use its
    # explicit isolated workspace-write policy and temporary CODEX_HOME.
    foreach ($isolatedVariable in @('CODEX_PERMISSION_PROFILE', 'CODEX_THREAD_ID', 'CODEX_INTERNAL_ORIGINATOR_OVERRIDE')) {
        [void]$startInfo.Environment.Remove($isolatedVariable)
    }
    if ($null -ne $startInfo.ArgumentList) {
        foreach ($argument in $Arguments) {
            [void]$startInfo.ArgumentList.Add($argument)
        }
    }
    else {
        $startInfo.Arguments = ($Arguments | ForEach-Object { ConvertTo-ProcessArgument $_ }) -join ' '
    }
    foreach ($key in $Environment.Keys) {
        $startInfo.Environment[$key] = [string]$Environment[$key]
    }

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw "Failed to start $FileName"
    }
    [pscustomobject]@{
        Process = $process
        StdoutTask = $process.StandardOutput.ReadToEndAsync()
        StderrTask = $process.StandardError.ReadToEndAsync()
    }
}

function Stop-TrackedProcess {
    param($Tracked)

    if ($null -eq $Tracked -or $null -eq $Tracked.Process -or $Tracked.Process.HasExited) {
        return
    }

    $treeKillMethod = $Tracked.Process.GetType().GetMethod('Kill', [Type[]]@([bool]))
    if ($null -ne $treeKillMethod) {
        [void]$treeKillMethod.Invoke($Tracked.Process, [object[]]@($true))
    }
    else {
        $taskKillPath = Join-Path $env:SystemRoot 'System32\taskkill.exe'
        if (-not (Test-Path -LiteralPath $taskKillPath)) {
            throw 'retained_handle_tree_stop_unavailable'
        }
        & $taskKillPath '/PID' ([string]$Tracked.Process.Id) '/T' '/F' 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0 -and -not $Tracked.Process.HasExited) {
            throw 'retained_handle_tree_stop_failed'
        }
    }
    if (-not $Tracked.Process.WaitForExit(10000)) {
        throw 'retained_handle_tree_stop_timed_out'
    }
}

function Add-SanitizedFailure {
    param(
        [System.Collections.Generic.List[string]]$Failures,
        [string]$Code
    )

    if ($Failures -notcontains $Code) {
        [void]$Failures.Add($Code)
    }
}

function Add-SanitizedSummaryFailure {
    param(
        [System.Collections.IDictionary]$Summary,
        [string]$Code
    )

    $existing = @($Summary['failures'])
    if ($existing -notcontains $Code) {
        $Summary['failures'] = @($existing) + @($Code)
    }
    $Summary['passed'] = $false
}

function Get-SanitizedQualificationFailureCode {
    param([System.Management.Automation.ErrorRecord]$ErrorRecord)

    $message = [string]$ErrorRecord.Exception.Message
    if ($message -like 'First tool after the successful apply_patch result*') {
        return 'post_success_tool_choice_failed'
    }
    if ($message -like 'Codex CLI timed out*') {
        return 'cli_timeout'
    }
    if ($message -like 'Isolated proxy did not become healthy*') {
        return 'proxy_startup_failed'
    }
    return 'qualification_execution_failed'
}

function Complete-TrackedProcess {
    param(
        $Tracked,
        [string]$Name,
        [string]$StdoutPath,
        [string]$StderrPath,
        [System.Collections.IDictionary]$Summary,
        [bool]$CaptureOutput = $true
    )

    if ($null -eq $Tracked -or $null -eq $Tracked.Process) {
        return
    }
    try {
        Stop-TrackedProcess $Tracked
    }
    catch {
        Add-SanitizedSummaryFailure -Summary $Summary -Code "cleanup_${Name}_stop_failed"
    }

    try {
        if (-not $Tracked.Process.HasExited) {
            Add-SanitizedSummaryFailure -Summary $Summary -Code "cleanup_${Name}_child_remained"
            return
        }
    }
    catch {
        Add-SanitizedSummaryFailure -Summary $Summary -Code "cleanup_${Name}_state_unavailable"
        return
    }

    if (-not $CaptureOutput) {
        return
    }
    try {
        Save-BoundedText -Path $StdoutPath -Text $Tracked.StdoutTask.GetAwaiter().GetResult()
    }
    catch {
        Add-SanitizedSummaryFailure -Summary $Summary -Code "cleanup_${Name}_stdout_capture_failed"
    }
    try {
        Save-BoundedText -Path $StderrPath -Text $Tracked.StderrTask.GetAwaiter().GetResult()
    }
    catch {
        Add-SanitizedSummaryFailure -Summary $Summary -Code "cleanup_${Name}_stderr_capture_failed"
    }
}

function Invoke-LifecycleReplay {
    param([string]$ReplayOutputDir)

    $runId = '{0}-{1}' -f $PID, (Get-Date -Format 'yyyyMMddHHmmss')
    $runRoot = Join-Path $ReplayOutputDir "run-$runId"
    $summaryPath = Join-Path $runRoot 'summary.json'
    $childPidPath = Join-Path $runRoot 'tracked-child.pid'
    $tracked = $null
    $childProcessId = 0
    $failures = [System.Collections.Generic.List[string]]::new()
    $summary = [ordered]@{
        mode = 'lifecycle_replay'
        passed = $false
        failures = @()
        tracked_root_exited = $false
        tracked_child_exited = $false
        run_root = $runRoot
    }

    try {
        New-Item -ItemType Directory -Force -Path $runRoot | Out-Null
        $powershellPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        if (-not (Test-Path -LiteralPath $powershellPath)) {
            throw 'lifecycle_powershell_not_found'
        }
        $lifecycleChildCommand = @'
$child = Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') -ArgumentList @('-NoProfile', '-NonInteractive', '-Command', 'Start-Sleep -Seconds 5') -PassThru
[System.IO.File]::WriteAllText($env:CODEXHUB_LIFECYCLE_CHILD_PID_PATH, [string]$child.Id)
Start-Sleep -Seconds 5
'@
        $tracked = Start-TrackedProcess -FileName $powershellPath -Arguments @(
            '-NoProfile', '-NonInteractive', '-Command', $lifecycleChildCommand
        ) -WorkingDirectory $runRoot -Environment @{
            CODEXHUB_LIFECYCLE_CHILD_PID_PATH = $childPidPath
        }
        $childPidDeadline = (Get-Date).AddSeconds(5)
        while (-not (Test-Path -LiteralPath $childPidPath) -and (Get-Date) -lt $childPidDeadline) {
            Start-Sleep -Milliseconds 50
        }
        if (-not (Test-Path -LiteralPath $childPidPath)) {
            throw 'lifecycle_child_start_timed_out'
        }
        $childPidText = [System.IO.File]::ReadAllText($childPidPath).Trim()
        if (-not [int]::TryParse($childPidText, [ref]$childProcessId) -or $childProcessId -le 0) {
            throw 'lifecycle_child_pid_invalid'
        }
        Stop-TrackedProcess $tracked
    }
    catch {
        Add-SanitizedFailure -Failures $failures -Code 'lifecycle_stop_failed'
    }
    finally {
        try {
            if ($null -ne $tracked -and -not $tracked.Process.HasExited) {
                [void]$tracked.Process.WaitForExit(7000)
            }
            if ($null -eq $tracked -or -not $tracked.Process.HasExited) {
                Add-SanitizedFailure -Failures $failures -Code 'lifecycle_tracked_root_remained'
            }
            else {
                $summary.tracked_root_exited = $true
            }
        }
        catch {
            Add-SanitizedFailure -Failures $failures -Code 'lifecycle_tracked_root_state_unavailable'
        }
        try {
            if ($childProcessId -le 0) {
                Add-SanitizedFailure -Failures $failures -Code 'lifecycle_tracked_child_pid_unavailable'
            }
            else {
                try {
                    $childProcess = [System.Diagnostics.Process]::GetProcessById($childProcessId)
                    [void]$childProcess.WaitForExit(7000)
                    if ($childProcess.HasExited) {
                        $summary.tracked_child_exited = $true
                    }
                    else {
                        Add-SanitizedFailure -Failures $failures -Code 'lifecycle_tracked_child_remained'
                    }
                }
                catch [System.ArgumentException] {
                    $summary.tracked_child_exited = $true
                }
                catch {
                    Add-SanitizedFailure -Failures $failures -Code 'lifecycle_tracked_child_state_unavailable'
                }
            }
        }
        catch {
            Add-SanitizedFailure -Failures $failures -Code 'lifecycle_tracked_child_state_unavailable'
        }
        $summary.failures = @($failures)
        $summary.passed = $failures.Count -eq 0
        $summary | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $summaryPath -Encoding UTF8
    }

    Get-Content -LiteralPath $summaryPath -Raw
    if (-not $summary.passed) {
        exit 1
    }
}

function Save-BoundedText {
    param(
        [string]$Path,
        [string]$Text,
        [int]$MaximumCharacters = 1000000
    )

    if ($Text.Length -gt $MaximumCharacters) {
        $Text = $Text.Substring(0, $MaximumCharacters) + "`n[truncated]`n"
    }
    [System.IO.File]::WriteAllText($Path, $Text, [System.Text.UTF8Encoding]::new($false))
}

function Read-JsonLines {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return @()
    }
    $entries = [System.Collections.Generic.List[object]]::new()
    foreach ($line in Get-Content -LiteralPath $Path) {
        if (-not $line.Trim()) {
            continue
        }
        try {
            [void]$entries.Add(($line | ConvertFrom-Json))
        }
        catch {
            continue
        }
    }
    return $entries.ToArray()
}

function Wait-ProxyHealth {
    param(
        [string]$BaseUrl,
        [int]$StartupSeconds
    )

    $deadline = (Get-Date).AddSeconds($StartupSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $health = Invoke-RestMethod -Uri "$BaseUrl/health" -TimeoutSec 2
            if ($health.ok -eq $true) {
                return
            }
        }
        catch {
        }
        Start-Sleep -Milliseconds 200
    }
    throw "Isolated proxy did not become healthy at $BaseUrl"
}

function Start-TrackedCodex {
    param(
        [string]$CommandPath,
        [string[]]$Arguments,
        [string]$WorkingDirectory,
        [hashtable]$Environment
    )

    $extension = [System.IO.Path]::GetExtension($CommandPath).ToLowerInvariant()
    if ($extension -notin @('.cmd', '.bat')) {
        return Start-TrackedProcess -FileName $CommandPath -Arguments $Arguments -WorkingDirectory $WorkingDirectory -Environment $Environment
    }

    $commandLine = @(
        (ConvertTo-ProcessArgument $CommandPath)
        ($Arguments | ForEach-Object { ConvertTo-ProcessArgument $_ })
    ) -join ' '
    $commandProcessor = if ($env:ComSpec) { $env:ComSpec } else { 'cmd.exe' }
    return Start-TrackedProcess -FileName $commandProcessor -Arguments @('/d', '/s', '/c', $commandLine) -WorkingDirectory $WorkingDirectory -Environment $Environment
}

$Workspace = (Resolve-Path -LiteralPath $Workspace).Path
if (-not $OutputDir) {
    $OutputDir = Join-Path $Workspace 'test-results\issue-108-glm-tool-surface'
}
$OutputDir = [System.IO.Path]::GetFullPath($OutputDir)
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

if ($LifecycleReplay) {
    Invoke-LifecycleReplay -ReplayOutputDir $OutputDir
    exit $LASTEXITCODE
}

if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable('OLLAMA_API_KEY'))) {
    throw 'OLLAMA_API_KEY is required for the isolated GLM qualification.'
}

if (-not $CodexCommand) {
    $command = Get-Command 'codex.cmd' -ErrorAction SilentlyContinue
    if (-not $command) {
        $command = Get-Command 'codex' -ErrorAction Stop
    }
    $CodexCommand = $command.Source
}
if (-not (Test-Path -LiteralPath $CodexCommand)) {
    throw "Codex command was not found: $CodexCommand"
}

$PythonCommand = (Get-Command 'python' -ErrorAction Stop).Source
$SharedAuthPath = Join-Path $HOME '.codex\auth.json'
if (-not (Test-Path -LiteralPath $SharedAuthPath)) {
    throw 'The local Codex auth file is required for the isolated CLI process.'
}
$SharedModelsCachePath = Join-Path $HOME '.codex\models_cache.json'
if (-not (Test-Path -LiteralPath $SharedModelsCachePath)) {
    throw 'The local Codex model catalog is required for the isolated CLI process.'
}

$runId = '{0}-{1}' -f $PID, (Get-Date -Format 'yyyyMMddHHmmss')
$runRoot = Join-Path $OutputDir "run-$runId"
$runtimeHome = Join-Path $runRoot 'runtime'
$testWorkspace = Join-Path $runRoot 'workspace'
$cliStdoutPath = Join-Path $runRoot 'cli-stdout.jsonl'
$cliStderrPath = Join-Path $runRoot 'cli-stderr.txt'
$proxyStdoutPath = Join-Path $runRoot 'proxy-stdout.txt'
$proxyStderrPath = Join-Path $runRoot 'proxy-stderr.txt'
$requestShapePath = Join-Path $runRoot 'request-tool-shape.jsonl'
$summaryPath = Join-Path $runRoot 'summary.json'
$targetPath = Join-Path $testWorkspace 'qualification-target.txt'
$authCopyPath = Join-Path $runtimeHome 'auth.json'
$proxy = $null
$cli = $null
$cliExitCode = $null
$cliOutputCaptured = $false
$summary = [ordered]@{
    model = 'ollama-cloud/glm-5.2'
    expected_upstream = 'ollama_cloud'
    expected_route_mode = 'codexhub'
    cli_sandbox = if ($UseCliSandboxBypass) { 'isolated_bypass' } else { 'workspace_write' }
    fail_fast_after_first_post_success_tool_choice = $FailFastAfterFirstPostSuccessToolChoice
    run_root = $runRoot
    passed = $false
    failures = @()
}

try {
    New-Item -ItemType Directory -Force -Path $runtimeHome, $testWorkspace, (Join-Path $runtimeHome 'proxy\config') | Out-Null
    Copy-Item -LiteralPath (Join-Path $Workspace 'config\providers.toml') -Destination (Join-Path $runtimeHome 'proxy\config\providers.toml')
    Copy-Item -LiteralPath $SharedAuthPath -Destination $authCopyPath
    $modelsCache = Get-Content -LiteralPath $SharedModelsCachePath -Raw | ConvertFrom-Json
    $templateModel = @($modelsCache.models | Where-Object { $_.slug -eq 'gpt-5.6-terra' }) | Select-Object -First 1
    if ($null -eq $templateModel) {
        throw 'The local Codex model catalog did not contain the required freeform apply_patch metadata template.'
    }
    # The isolated CLI needs local metadata for the exact GLM slug to expose its freeform apply_patch tool.
    $glmModel = $templateModel | ConvertTo-Json -Depth 100 | ConvertFrom-Json
    $glmModel.slug = 'ollama-cloud/glm-5.2'
    $glmModel.display_name = 'GLM-5.2 (Ollama Cloud)'
    $glmModel.tool_mode = 'direct'
    $modelsCache.models = @($modelsCache.models) + @($glmModel)
    $modelCatalogJson = [pscustomobject]@{ models = @($modelsCache.models) } | ConvertTo-Json -Depth 100 -Compress
    $modelCatalogPath = Join-Path $runtimeHome 'model-catalog.json'
    [System.IO.File]::WriteAllText($modelCatalogPath, $modelCatalogJson, [System.Text.UTF8Encoding]::new($false))
    $modelCatalogConfigPath = $modelCatalogPath.Replace('\', '/')
    [System.IO.File]::WriteAllText($targetPath, "issue108-before`n", [System.Text.UTF8Encoding]::new($false))

    Invoke-Checked -FileName 'git' -Arguments @('-C', $testWorkspace, 'init', '--quiet')
    Invoke-Checked -FileName 'git' -Arguments @('-C', $testWorkspace, 'config', 'user.name', 'Issue 108 Qualification')
    Invoke-Checked -FileName 'git' -Arguments @('-C', $testWorkspace, 'config', 'user.email', 'issue108@example.invalid')
    Invoke-Checked -FileName 'git' -Arguments @('-C', $testWorkspace, 'add', 'qualification-target.txt')
    Invoke-Checked -FileName 'git' -Arguments @('-C', $testWorkspace, 'commit', '--quiet', '--no-gpg-sign', '-m', 'qualification baseline')

    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $proxyPort = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    $listener.Stop()
    $proxyBaseUrl = "http://127.0.0.1:$proxyPort"
    $childEnvironment = @{
        CODEX_HOME = $runtimeHome
        CODEX_PROXY_PORT = "$proxyPort"
    }
    $proxyArguments = @('-u', 'codex_proxy.py', '--port', "$proxyPort")
    $captureProxyEnabled = $CaptureRequestShape -or $FailFastAfterFirstPostSuccessToolChoice
    if ($captureProxyEnabled) {
        $captureProxyPath = Join-Path $runRoot 'capture-proxy.py'
        $captureProxy = @'
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.getcwd())
import codex_proxy


_capture_path = Path(os.environ["CODEXHUB_REQUEST_TOOL_SHAPE_PATH"])
_capture_count = 0
_sse_capture_count = 0
_apply_patch_capture_count = 0
_post_success_apply_patch_pending = False
_post_success_tool_choice_recorded = False
_original_compatible_request_body = codex_proxy.compatible_request_body
_original_compatible_sse_line = codex_proxy.compatible_sse_line
_original_apply_patch_events_for_event = codex_proxy._ThirdPartyApplyPatchStreamAdapter.events_for_event
_apply_patch_item_ids = set()


def _tool_shape(tools):
    if not isinstance(tools, list):
        return []
    result = []
    for tool in tools:
        if not isinstance(tool, dict):
            result.append({"kind": type(tool).__name__})
            continue
        item = {"type": tool.get("type"), "name": tool.get("name"), "keys": sorted(tool)}
        nested = tool.get("tools")
        if isinstance(nested, list):
            item["nested_tools"] = _tool_shape(nested)
        result.append(item)
    return result


def _tool_output_shape(value):
    text_parts = []

    def collect(candidate):
        if isinstance(candidate, str):
            text_parts.append(candidate)
        elif isinstance(candidate, list):
            for entry in candidate:
                collect(entry)
        elif isinstance(candidate, dict):
            for entry in candidate.values():
                collect(entry)

    collect(value)
    normalized = "\n".join(text_parts).lower()
    return {
        "kind": type(value).__name__,
        "text_fragment_count": len(text_parts),
        "text_character_count": sum(len(part) for part in text_parts),
        "contains_apply_patch_error": "apply_patch verification failed" in normalized,
        "contains_expected_lines_error": "failed to find expected lines" in normalized,
        "contains_success_marker": "success" in normalized or "applied" in normalized,
    }


def _append_capture_record(record):
    with _capture_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")


def _collect_text(value):
    text_parts = []

    def collect(candidate):
        if isinstance(candidate, str):
            text_parts.append(candidate)
        elif isinstance(candidate, list):
            for entry in candidate:
                collect(entry)
        elif isinstance(candidate, dict):
            for entry in candidate.values():
                collect(entry)

    collect(value)
    return "\n".join(text_parts).lower()


def _is_successful_apply_patch_output(value):
    normalized = _collect_text(value)
    return (
        "apply_patch verification failed" not in normalized
        and "failed to find expected lines" not in normalized
        and ("success" in normalized or "updated" in normalized or "applied" in normalized)
    )


def _is_exact_apply_patch_history_pair(call, result):
    return (
        isinstance(call, dict)
        and set(call) == {"type", "status", "call_id", "name", "input"}
        and call.get("type") == "custom_tool_call"
        and call.get("status") == "completed"
        and call.get("name") == "apply_patch"
        and isinstance(call.get("call_id"), str)
        and bool(call["call_id"])
        and isinstance(call.get("input"), str)
        and bool(call["input"].strip())
        and isinstance(result, dict)
        and set(result) == {"type", "call_id", "output"}
        and result.get("type") == "custom_tool_call_output"
        and result.get("call_id") == call["call_id"]
    )


def _note_post_success_apply_patch_history(payload):
    global _post_success_apply_patch_pending
    if _post_success_apply_patch_pending or not isinstance(payload, dict):
        return
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return
    for index, call in enumerate(input_items[:-1]):
        result = input_items[index + 1]
        if _is_exact_apply_patch_history_pair(call, result) and _is_successful_apply_patch_output(result["output"]):
            _post_success_apply_patch_pending = True
            _append_capture_record({"stage": "post_success_apply_patch_result"})
            return


def _record_post_success_tool_choice(line):
    global _post_success_tool_choice_recorded
    if not _post_success_apply_patch_pending or _post_success_tool_choice_recorded:
        return
    if not isinstance(line, bytes) or not line.startswith(b"data: "):
        return
    try:
        payload = json.loads(line[6:].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return
    item = payload.get("item") if isinstance(payload, dict) else None
    if not isinstance(item, dict) or item.get("type") not in {"function_call", "custom_tool_call"}:
        return
    name = item.get("name")
    if not isinstance(name, str) or not name:
        return
    _post_success_tool_choice_recorded = True
    _append_capture_record(
        {
            "stage": "post_success_tool_choice",
            "choice": name,
            "outcome": "expected" if name == "shell_command" else "wrong",
        }
    )


def _patch_structure(arguments):
    if isinstance(arguments, str):
        try:
            decoded = json.loads(arguments)
        except (TypeError, ValueError):
            decoded = None
    elif isinstance(arguments, dict):
        decoded = arguments
    else:
        decoded = None
    patch = decoded.get("patch") if isinstance(decoded, dict) else None
    if not isinstance(patch, str):
        return None
    operation_markers = (
        "*** Add File:",
        "*** Delete File:",
        "*** Update File:",
        "*** Move to:",
    )
    lines = patch.splitlines()
    operation_count = sum(patch.count(marker) for marker in operation_markers)
    return {
        "character_count": len(patch),
        "line_count": len(lines),
        "begin_marker_count": patch.count("*** Begin Patch"),
        "end_marker_count": patch.count("*** End Patch"),
        "operation_count": operation_count,
        "hunk_count": patch.count("@@"),
        "added_line_count": sum(
            line.startswith("+") and not line.startswith("+++") for line in lines
        ),
        "removed_line_count": sum(
            line.startswith("-") and not line.startswith("---") for line in lines
        ),
        "has_exact_single_wrapper": (
            patch.count("*** Begin Patch") == 1
            and patch.count("*** End Patch") == 1
            and operation_count == 1
        ),
    }


def _request_shape(body):
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"parseable": False}
    if not isinstance(payload, dict):
        return {"parseable": False}
    input_items = payload.get("input")
    input_shape = []
    apply_patch_call_ids = set()
    if isinstance(input_items, list):
        for item in input_items:
            if not isinstance(item, dict):
                input_shape.append({"kind": type(item).__name__})
                continue
            entry = {"type": item.get("type"), "role": item.get("role"), "keys": sorted(item)}
            if item.get("type") == "function_call" and item.get("name") == "apply_patch":
                call_id = item.get("call_id")
                if isinstance(call_id, str) and call_id:
                    apply_patch_call_ids.add(call_id)
            if item.get("type") == "additional_tools":
                entry["tools"] = _tool_shape(item.get("tools"))
            if item.get("type") == "custom_tool_call_output":
                entry["output_shape"] = _tool_output_shape(item.get("output"))
            input_shape.append(entry)
    structured_history_pair_count = 0
    if isinstance(input_items, list):
        structured_history_pair_count = sum(
            1
            for item in input_items
            if isinstance(item, dict)
            and item.get("type") == "function_call_output"
            and item.get("call_id") in apply_patch_call_ids
        )
    return {
        "parseable": True,
        "input": input_shape,
        "tools": _tool_shape(payload.get("tools")),
        "apply_patch_structured_history_pair_count": structured_history_pair_count,
    }


def _capture(stage, body):
    global _capture_count
    if _capture_count >= 8:
        return
    _capture_count += 1
    record = {"stage": stage, "shape": _request_shape(body)}
    with _capture_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")


def _compatible_request_body_with_capture(body, *args, **kwargs):
    _capture("before", body)
    payload = None
    try:
        candidate = json.loads(body.decode("utf-8-sig"))
        if isinstance(candidate, dict):
            payload = candidate
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    if payload is not None:
        _note_post_success_apply_patch_history(payload)
    rewritten = _original_compatible_request_body(body, *args, **kwargs)
    _capture("after", rewritten)
    return rewritten


def _apply_patch_response_shape(line):
    if not isinstance(line, bytes) or not line.startswith(b"data: "):
        return None
    try:
        payload = json.loads(line[6:].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    candidates = []
    item = payload.get("item")
    if isinstance(item, dict):
        candidates.append(item)
    response = payload.get("response")
    if isinstance(response, dict):
        output = response.get("output")
        if isinstance(output, list):
            candidates.extend(value for value in output if isinstance(value, dict))
    for candidate in candidates:
        if candidate.get("type") != "function_call" or candidate.get("name") != "apply_patch":
            continue
        arguments = candidate.get("arguments")
        argument_keys = None
        if isinstance(arguments, str):
            try:
                decoded = json.loads(arguments)
            except (TypeError, ValueError):
                decoded = None
            if isinstance(decoded, dict):
                argument_keys = sorted(decoded)
        elif isinstance(arguments, dict):
            argument_keys = sorted(arguments)
        return {
            "event_type": payload.get("type"),
            "item_keys": sorted(candidate),
            "arguments_kind": type(arguments).__name__,
            "argument_keys": argument_keys,
            "patch_structure": _patch_structure(arguments),
        }
    return None


def _compatible_sse_line_with_capture(line, *args, **kwargs):
    global _sse_capture_count
    _record_post_success_tool_choice(line)
    shape = _apply_patch_response_shape(line)
    if shape is not None and _sse_capture_count < 8:
        _sse_capture_count += 1
        record = {"stage": "sse_before", "shape": shape}
        with _capture_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")
    return _original_compatible_sse_line(line, *args, **kwargs)


def _apply_patch_event_shape(event):
    if not isinstance(event, dict):
        return None
    candidate = event.get("item")
    if isinstance(candidate, dict) and candidate.get("type") == "function_call" and candidate.get("name") == "apply_patch":
        item_id = candidate.get("id")
        if isinstance(item_id, str) and item_id:
            _apply_patch_item_ids.add(item_id)
    elif event.get("item_id") in _apply_patch_item_ids:
        candidate = event
    else:
        return None
    arguments = candidate.get("arguments") if isinstance(candidate, dict) else None
    argument_keys = None
    if isinstance(arguments, str):
        try:
            decoded = json.loads(arguments)
        except (TypeError, ValueError):
            decoded = None
        if isinstance(decoded, dict):
            argument_keys = sorted(decoded)
    elif isinstance(arguments, dict):
        argument_keys = sorted(arguments)
    return {
        "event_type": event.get("type"),
        "event_keys": sorted(event),
        "item_keys": sorted(candidate) if isinstance(candidate, dict) else None,
        "arguments_kind": type(arguments).__name__,
        "argument_keys": argument_keys,
        "patch_structure": _patch_structure(arguments),
    }


def _apply_patch_events_for_event_with_capture(self, event):
    shape = _apply_patch_event_shape(event)
    if shape is not None:
        global _apply_patch_capture_count
        if _apply_patch_capture_count < 20:
            _apply_patch_capture_count += 1
            record = {"stage": "apply_patch_event_before", "shape": shape}
            with _capture_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")
    return _original_apply_patch_events_for_event(self, event)


codex_proxy.compatible_request_body = _compatible_request_body_with_capture
codex_proxy.compatible_sse_line = _compatible_sse_line_with_capture
codex_proxy._ThirdPartyApplyPatchStreamAdapter.events_for_event = _apply_patch_events_for_event_with_capture
raise SystemExit(codex_proxy.main())
'@
        [System.IO.File]::WriteAllText($captureProxyPath, $captureProxy, [System.Text.UTF8Encoding]::new($false))
        $childEnvironment['CODEXHUB_REQUEST_TOOL_SHAPE_PATH'] = $requestShapePath
        $proxyArguments = @('-u', $captureProxyPath, '--port', "$proxyPort")
    }
    $cliConfig = @"
model_provider = "custom"
model_catalog_json = "$modelCatalogConfigPath"

[model_providers.custom]
name = "Issue108"
base_url = "$proxyBaseUrl/v1"
wire_api = "responses"
requires_openai_auth = true
"@
    [System.IO.File]::WriteAllText((Join-Path $runtimeHome 'config.toml'), $cliConfig, [System.Text.UTF8Encoding]::new($false))
    $proxy = Start-TrackedProcess -FileName $PythonCommand -Arguments $proxyArguments -WorkingDirectory (Join-Path $Workspace 'src-python') -Environment $childEnvironment
    Wait-ProxyHealth -BaseUrl $proxyBaseUrl -StartupSeconds $ProxyStartupSeconds

    $prompt = @'
Automated qualification: follow this exact tool sequence and use no other tools.
1. Call shell_command exactly once to read only qualification-target.txt.
2. Call apply_patch exactly once to replace only issue108-before with issue108-after in qualification-target.txt. Do not use shell to write the file.
3. Call shell_command exactly once to read only qualification-target.txt and verify issue108-after.
After a successful apply_patch result, step 2 is complete. Do not call apply_patch again for any reason: your next and only tool call must be the step-3 shell_command. Treat a repeated apply_patch call as an incorrect result.
For the apply_patch action, emit exactly one upstream function-call argument named patch whose value is exactly this one-operation patch, with no duplicate hunks or additional file operations:
*** Begin Patch
*** Update File: qualification-target.txt
@@
-issue108-before
+issue108-after
*** End Patch
Do not use an empty argument name or additional arguments.
Do not call tool_search, collaboration tools, namespaces, or any other tool. Do not edit or create any other file. Finish with exactly: SENTINEL:issue108-shell-apply-shell
'@
    if ($UseCliSandboxBypass -and -not $testWorkspace.StartsWith("$runRoot\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'The CLI sandbox bypass is permitted only for the generated isolated workspace.'
    }
    $cliArguments = @()
    if ($UseCliSandboxBypass) {
        # The Windows workspace-write setup is unavailable on this host. This
        # opt-in path is confined to the generated one-file Git workspace and
        # still verifies the exact tool sequence and final diff below.
        $cliArguments += '--dangerously-bypass-approvals-and-sandbox'
    }
    else {
        $cliArguments += '-a', 'never'
    }
    $cliArguments += @(
        'exec', '--strict-config', '--ephemeral', '--json',
        '-C', $testWorkspace,
        '--add-dir', $testWorkspace,
        '-m', 'ollama-cloud/glm-5.2'
    )
    if (-not $UseCliSandboxBypass) {
        $cliArguments += @('-s', 'workspace-write')
    }
    $cliArguments += @(
        '-'
    )
    $cli = Start-TrackedCodex -CommandPath $CodexCommand -Arguments $cliArguments -WorkingDirectory $testWorkspace -Environment $childEnvironment
    $cli.Process.StandardInput.Write($prompt)
    $cli.Process.StandardInput.Close()
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $postSuccessToolChoice = $null
    $stoppedForWrongPostSuccessChoice = $false
    while (-not $cli.Process.HasExited -and (Get-Date) -lt $deadline) {
        [void]$cli.Process.WaitForExit(250)
        $choiceRecord = @(
            Read-JsonLines -Path $requestShapePath |
                Where-Object { $_.stage -eq 'post_success_tool_choice' } |
                Select-Object -First 1
        )
        if ($choiceRecord.Count -eq 0) {
            continue
        }
        $postSuccessToolChoice = [string]$choiceRecord[0].choice
        $summary.post_success_tool_choice = $postSuccessToolChoice
        if ($FailFastAfterFirstPostSuccessToolChoice -and $postSuccessToolChoice -ne 'shell_command') {
            $stoppedForWrongPostSuccessChoice = $true
            Stop-TrackedProcess $cli
            break
        }
    }
    if ($stoppedForWrongPostSuccessChoice) {
        throw "First tool after the successful apply_patch result was $postSuccessToolChoice, not shell_command."
    }
    if (-not $cli.Process.HasExited) {
        Stop-TrackedProcess $cli
        throw "Codex CLI timed out after $TimeoutSeconds seconds."
    }
    $cliExitCode = $cli.Process.ExitCode
    $cliStdout = $cli.StdoutTask.GetAwaiter().GetResult()
    $cliStderr = $cli.StderrTask.GetAwaiter().GetResult()
    Save-BoundedText -Path $cliStdoutPath -Text $cliStdout
    Save-BoundedText -Path $cliStderrPath -Text $cliStderr
    $cliOutputCaptured = $true
    Start-Sleep -Milliseconds 750

    $events = Read-JsonLines -Path (Join-Path $runtimeHome 'proxy\codex-proxy-events.jsonl')
    $captureEvents = Read-JsonLines -Path $requestShapePath
    $postSuccessChoiceEvents = @($captureEvents | Where-Object { $_.stage -eq 'post_success_tool_choice' })
    if ($null -eq $postSuccessToolChoice -and $postSuccessChoiceEvents.Count -gt 0) {
        $postSuccessToolChoice = [string]$postSuccessChoiceEvents[0].choice
    }
    $cliEvents = Read-JsonLines -Path $cliStdoutPath
    $toolSequence = [System.Collections.Generic.List[string]]::new()
    foreach ($event in $cliEvents) {
        if ($event.type -ne 'item.completed' -or $null -eq $event.item) {
            continue
        }
        if ($event.item.type -eq 'command_execution') {
            [void]$toolSequence.Add('shell_command')
        }
        elseif ($event.item.type -eq 'custom_tool_call' -and $event.item.name -eq 'apply_patch') {
            [void]$toolSequence.Add('apply_patch')
        }
        elseif ($event.item.type -eq 'file_change') {
            [void]$toolSequence.Add('apply_patch')
        }
    }
    $requestStarts = @($events | Where-Object { $_.event -eq 'request_start' })
    $surfaceEvents = @($events | Where-Object { $_.event -eq 'external_tool_surface_prepared' })
    $adapterEvents = @($events | Where-Object { $_.event -eq 'third_party_apply_patch_freeform_adapter' })
    $historyAdapterEvents = @($events | Where-Object { $_.event -eq 'third_party_apply_patch_freeform_history_adapter' })
    $requestErrors = @($events | Where-Object { $_.event -eq 'request_error' })
    $postSuccessStructuredHistoryPairCounts = [System.Collections.Generic.List[int]]::new()
    for ($index = 0; $index -lt $captureEvents.Count; $index++) {
        if ($captureEvents[$index].stage -ne 'post_success_apply_patch_result') {
            continue
        }
        $afterRecord = $null
        for ($nextIndex = $index + 1; $nextIndex -lt $captureEvents.Count; $nextIndex++) {
            $candidate = $captureEvents[$nextIndex]
            if ($candidate.stage -eq 'after') {
                $afterRecord = $candidate
                break
            }
            if ($candidate.stage -eq 'before') {
                break
            }
        }
        $pairCount = 0
        if ($null -ne $afterRecord) {
            try {
                $pairCount = [int]$afterRecord.shape.apply_patch_structured_history_pair_count
            }
            catch {
                $pairCount = 0
            }
        }
        [void]$postSuccessStructuredHistoryPairCounts.Add($pairCount)
    }
    $statusLines = @(& git -C $testWorkspace status --porcelain)
    $numstat = ((& git -C $testWorkspace diff --numstat) | Out-String).Trim()
    $targetText = [System.IO.File]::ReadAllText($targetPath)

    $failures = [System.Collections.Generic.List[string]]::new()
    if ($cliExitCode -ne 0) {
        [void]$failures.Add("Codex CLI exited with code $cliExitCode")
    }
    if ($targetText.Trim() -ne 'issue108-after') {
        [void]$failures.Add('qualification target did not contain only issue108-after')
    }
    if (($statusLines -join "`n") -ne ' M qualification-target.txt') {
        [void]$failures.Add('qualification workspace changed more than the one target file')
    }
    if ($numstat -ne "1`t1`tqualification-target.txt") {
        [void]$failures.Add("qualification target was not exactly one-line replacement: $numstat")
    }
    if (($toolSequence -join ',') -ne 'shell_command,apply_patch,shell_command') {
        [void]$failures.Add("unexpected CLI tool sequence: $($toolSequence -join ',')")
    }
    if ([string]::IsNullOrWhiteSpace($postSuccessToolChoice)) {
        [void]$failures.Add('the harness did not observe the first post-success tool choice')
    }
    elseif ($postSuccessToolChoice -ne 'shell_command') {
        [void]$failures.Add("first post-success tool choice was $postSuccessToolChoice, not shell_command")
    }
    if ($requestStarts.Count -eq 0) {
        [void]$failures.Add('proxy did not record a request_start event')
    }
    foreach ($request in $requestStarts) {
        $model = [string]$request.model
        if ($request.upstream -ne 'ollama_cloud' -or $request.provider_id -ne 'ollama_cloud' -or $request.route_mode -ne 'codexhub' -or $model -notmatch '^(ollama-cloud/)?glm-5\.2$') {
            [void]$failures.Add('request identity was not GLM/ollama_cloud/codexhub')
            break
        }
    }
    if (@($requestStarts | Where-Object { [string]$_.model -match '(?i)terra|luna' }).Count -gt 0) {
        [void]$failures.Add('request telemetry showed a Luna or Terra fallback')
    }
    if ($surfaceEvents.Count -eq 0 -or @($surfaceEvents | Where-Object { $_.tool_surface_strategy -ne 'deferred_core' }).Count -gt 0) {
        [void]$failures.Add('deferred_core tool-surface telemetry was not recorded for every prepared request')
    }
    if (@($adapterEvents | Where-Object { $_.outcome -eq 'adapted' }).Count -eq 0) {
        [void]$failures.Add('the third-party apply_patch freeform adapter never reported adapted')
    }
    if (@($historyAdapterEvents | Where-Object { $_.outcome -eq 'adapted' }).Count -eq 0) {
        [void]$failures.Add('the third-party apply_patch freeform history adapter never reported adapted')
    }
    if ($postSuccessStructuredHistoryPairCounts.Count -eq 0 -or @(
        $postSuccessStructuredHistoryPairCounts | Where-Object { $_ -ne 1 }
    ).Count -gt 0) {
        [void]$failures.Add('post-success apply_patch history was not preserved as exactly one structured pair')
    }
    if ($requestErrors.Count -gt 0) {
        [void]$failures.Add('proxy recorded a request_error during qualification')
    }

    $summary.cli_exit_code = $cliExitCode
    $summary.tool_sequence = @($toolSequence)
    $summary.request_start_count = $requestStarts.Count
    $summary.deferred_surface_event_count = $surfaceEvents.Count
    $summary.apply_patch_adapter_outcomes = @($adapterEvents | ForEach-Object { [string]$_.outcome })
    $summary.apply_patch_history_adapter_outcomes = @($historyAdapterEvents | ForEach-Object { [string]$_.outcome })
    $summary.post_success_tool_choice = $postSuccessToolChoice
    $summary.post_success_structured_history_pair_counts = @($postSuccessStructuredHistoryPairCounts)
    $summary.git_status = @($statusLines)
    $summary.git_numstat = $numstat
    $summary.failures = @($failures)
    $summary.passed = $failures.Count -eq 0
}
catch {
    Add-SanitizedSummaryFailure -Summary $summary -Code (Get-SanitizedQualificationFailureCode -ErrorRecord $_)
}
finally {
    if ($null -ne $cli) {
        Complete-TrackedProcess -Tracked $cli -Name 'cli' -StdoutPath $cliStdoutPath -StderrPath $cliStderrPath -Summary $summary -CaptureOutput (-not $cliOutputCaptured)
    }
    if ($null -ne $proxy) {
        Complete-TrackedProcess -Tracked $proxy -Name 'proxy' -StdoutPath $proxyStdoutPath -StderrPath $proxyStderrPath -Summary $summary
    }
    try {
        if (Test-Path -LiteralPath $authCopyPath) {
            Remove-Item -LiteralPath $authCopyPath -Force
        }
    }
    catch {
        Add-SanitizedSummaryFailure -Summary $summary -Code 'cleanup_auth_copy_remove_failed'
    }
    $summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $summaryPath -Encoding UTF8
}

Get-Content -LiteralPath $summaryPath -Raw
if (-not $summary.passed) {
    exit 1
}
