param(
    [string]$TracePath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'docs\evidence\issue-62\current-codexhub-thread-tool-surface.json'),
    [string]$WireFixturePath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'docs\evidence\issue-62\codexhub-runtime-wire-fixture.json'),
    [ValidateSet('identity', 'mutation', 'deletion', 'loss', 'required-set-deletion', 'required-membership-mutation')]
    [string]$ReplayCase = 'identity'
)

$ErrorActionPreference = 'Stop'

foreach ($path in @($TracePath, $WireFixturePath)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Evidence file not found: $path"
    }
}

$trace = Get-Content -Raw -LiteralPath $TracePath | ConvertFrom-Json
$wire = Get-Content -Raw -LiteralPath $WireFixturePath | ConvertFrom-Json
$mismatches = [System.Collections.Generic.List[string]]::new()

function Add-Mismatch {
    param([string]$Message)
    $script:mismatches.Add($Message)
}

function Assert-Set {
    param(
        [string]$Name,
        [string[]]$Expected,
        [string[]]$Actual
    )

    $expected = @($Expected | Sort-Object -Unique)
    $actual = @($Actual | Sort-Object -Unique)
    $missing = @($expected | Where-Object { $_ -notin $actual })
    $unexpected = @($actual | Where-Object { $_ -notin $expected })
    if ($missing.Count -gt 0 -or $unexpected.Count -gt 0) {
        Add-Mismatch "$Name missing=[$($missing -join ', ')] unexpected=[$($unexpected -join ', ')]"
    }
}

function Get-Namespace {
    param(
        [object[]]$Namespaces,
        [string]$Name,
        [string]$Plane
    )

    $matches = @($Namespaces | Where-Object { $_.name -eq $Name })
    if ($matches.Count -ne 1) {
        Add-Mismatch "$Plane expected exactly one namespace named $Name but found $($matches.Count)"
        return $null
    }
    return $matches[0]
}

$registered = @($trace.registered_codex_app_tools)
$direct = @($trace.dynamic_tool_exposure.direct)
$deferred = @($trace.dynamic_tool_exposure.deferred)
$observed = @($trace.observed_callable_codex_app_tools)
$required = @($trace.required_thread_tools)
$expectedRequiredThreadTools = @(
    'fork_thread',
    'handoff_thread',
    'get_handoff_status',
    'list_projects',
    'create_thread',
    'list_threads',
    'read_thread',
    'send_message_to_thread',
    'set_thread_pinned',
    'set_thread_archived',
    'set_thread_title'
)
$modelPlan = $trace.planner_gates.model_visible_plan
$discoverable = @($modelPlan.codex_app_deferred_tools_discoverable_through_tool_search)

$dynamicEntries = [System.Collections.Generic.List[object]]::new()
foreach ($contributor in @($trace.dynamic_tool_contributors)) {
    foreach ($tool in @($contributor.tools)) {
        $dynamicEntries.Add($tool)
    }
}
$dynamicNames = @($dynamicEntries | ForEach-Object { $_.name })
$dynamicDirect = @($dynamicEntries | Where-Object { $_.planner_exposure -eq 'Direct' } | ForEach-Object { $_.name })
$dynamicDeferred = @($dynamicEntries | Where-Object { $_.planner_exposure -eq 'Deferred' } | ForEach-Object { $_.name })

if ($trace.source.PSObject.Properties.Name -contains 'session_id') {
    Add-Mismatch 'sanitized trace retains a session_id'
}
if ($trace.PSObject.Properties.Name -contains 'missing_visible_thread_tools') {
    Add-Mismatch 'trace retains the legacy missing Deferred-tools assertion'
}
if ($trace.diagnosis.PSObject.Properties.Name -contains 'root_cause') {
    Add-Mismatch 'trace retains a confirmed root_cause assertion'
}
if ($trace.diagnosis.status -ne 'fact_hypothesis_split') {
    Add-Mismatch "diagnosis status is $($trace.diagnosis.status), not fact_hypothesis_split"
}

Assert-Set -Name 'registered versus contributor tools' -Expected $registered -Actual $dynamicNames
Assert-Set -Name 'registered versus exposure union' -Expected $registered -Actual @($direct + $deferred)
Assert-Set -Name 'direct exposure versus contributor metadata' -Expected $direct -Actual $dynamicDirect
Assert-Set -Name 'deferred exposure versus contributor metadata' -Expected $deferred -Actual $dynamicDeferred
Assert-Set -Name 'observed callable versus Direct tools' -Expected $direct -Actual $observed
Assert-Set -Name 'model-plan direct versus Direct tools' -Expected $direct -Actual @($modelPlan.codex_app_direct_tools)

foreach ($entry in $dynamicEntries) {
    if ($entry.planner_exposure -eq 'Direct' -and $entry.deferLoading -ne $false) {
        Add-Mismatch "Direct tool $($entry.name) does not have effective deferLoading=false"
    }
    if ($entry.planner_exposure -eq 'Deferred' -and $entry.deferLoading -ne $true) {
        Add-Mismatch "Deferred tool $($entry.name) does not have deferLoading=true"
    }
}

if (
    $trace.planner_gates.caller_request.additional_tools_contains_tool_search -ne $true -or
    $trace.planner_gates.caller_request.tool_search_execution -ne 'client' -or
    $modelPlan.tool_search_available -ne $true
) {
    Add-Mismatch 'caller request and model-visible plan do not retain client-executed tool_search'
}
if ($trace.gateway_route.upstream -ne 'official' -or $wire.route.upstream_route -ne 'official') {
    Add-Mismatch 'Gateway upstream route is not the recorded official route'
}
if (
    $trace.gateway_observability.request_prefix_equality_observed -ne $true -or
    $trace.gateway_observability.request_prefix_bytes_observed -ne 65536 -or
    $trace.gateway_observability.full_request_body_fingerprint -ne 'not_captured' -or
    $trace.gateway_observability.full_response_body_fingerprint -ne 'not_captured'
) {
    Add-Mismatch 'bounded request-prefix observation is invalid'
}
if ($wire.route.classification_basis -notmatch 'never configured_provider_id alone') {
    Add-Mismatch 'wire fixture permits provider-id route classification'
}
if (
    $wire.evidence_limit.transport_observation -notmatch 'no full request or response body fingerprint' -or
    $wire.evidence_limit.replay_fixture -notmatch 'not independent full-wire identity'
) {
    Add-Mismatch 'wire fixture overstates its transport evidence'
}

$expectedStates = @('Direct', 'DirectModelOnly', 'Deferred', 'Hidden', 'hosted-only', 'host-unavailable')
Assert-Set -Name 'exposure-state catalog' -Expected $expectedStates -Actual @($trace.exposure_state_catalog | ForEach-Object { $_.state })
Assert-Set -Name 'exposure-state catalog versus wire tags' -Expected $expectedStates -Actual @($wire.exposure_state_tags)

$preNamespace = Get-Namespace -Namespaces @($wire.pre_gateway.tool_surface.namespaces) -Name 'codex_app' -Plane 'pre-Gateway'
$postNamespace = Get-Namespace -Namespaces @($wire.post_gateway.tool_surface.namespaces) -Name 'codex_app' -Plane 'post-Gateway'

$preDirect = if ($null -ne $preNamespace) { @($preNamespace.direct_tools) } else { @() }
$preDeferred = if ($null -ne $preNamespace) { @($preNamespace.deferred_tools) } else { @() }
$postDirect = if ($null -ne $postNamespace) { @($postNamespace.direct_tools) } else { @() }
$postDeferred = if ($null -ne $postNamespace) { @($postNamespace.deferred_tools) } else { @() }
$modelPlanDeferred = @($modelPlan.codex_app_deferred_tools_discoverable_through_tool_search)
$requiredForReplay = @($required)
$callLinks = @($wire.history.call_links)

switch ($ReplayCase) {
    'mutation' {
        if ($postDirect.Count -gt 0) {
            $postDirect[0] = "$($postDirect[0])_mutated"
        }
    }
    'deletion' {
        $postDeferred = @($postDeferred | Where-Object { $_ -ne 'fork_thread' })
    }
    'loss' {
        $modelPlanDeferred = @($modelPlanDeferred | Where-Object { $_ -ne 'fork_thread' })
    }
    'required-set-deletion' {
        $requiredForReplay = @($requiredForReplay | Where-Object { $_ -ne 'fork_thread' })
    }
    'required-membership-mutation' {
        $modelPlanDeferred = @(
            $modelPlanDeferred | ForEach-Object {
                if ($_ -eq 'fork_thread') { 'fork_thread_mutated' } else { $_ }
            }
        )
    }
}

Assert-Set -Name 'trace Direct versus pre-Gateway' -Expected $direct -Actual $preDirect
Assert-Set -Name 'pre-Gateway Direct versus post-Gateway' -Expected $preDirect -Actual $postDirect
Assert-Set -Name 'trace Deferred versus pre-Gateway' -Expected $deferred -Actual $preDeferred
Assert-Set -Name 'pre-Gateway Deferred versus post-Gateway' -Expected $preDeferred -Actual $postDeferred
Assert-Set -Name 'model-plan discoverable versus Deferred tools' -Expected $deferred -Actual $modelPlanDeferred
Assert-Set -Name 'required thread tool contract' -Expected $expectedRequiredThreadTools -Actual $requiredForReplay

$requiredNotRegistered = @($requiredForReplay | Where-Object { $_ -notin $registered })
$requiredNotDeferred = @($requiredForReplay | Where-Object { $_ -notin $deferred })
$requiredNotDiscoverable = @($requiredForReplay | Where-Object { $_ -notin $modelPlanDeferred })
if (
    $requiredNotRegistered.Count -gt 0 -or
    $requiredNotDeferred.Count -gt 0 -or
    $requiredNotDiscoverable.Count -gt 0
) {
    Add-Mismatch (
        "required tool membership failed registered=[$($requiredNotRegistered -join ', ')] " +
        "deferred=[$($requiredNotDeferred -join ', ')] " +
        "discoverable=[$($requiredNotDiscoverable -join ', ')]"
    )
}

if (
    $wire.pre_gateway.request_id -ne $wire.post_gateway.request_id -or
    $wire.pre_gateway.stream -ne $wire.post_gateway.stream -or
    $wire.pre_gateway.model -ne $wire.post_gateway.model
) {
    Add-Mismatch 'pre-Gateway and post-Gateway request identity changed'
}
if (
    $wire.pre_gateway.tool_surface.tool_search.type -ne 'tool_search' -or
    $wire.pre_gateway.tool_surface.tool_search.execution -ne 'client' -or
    $wire.post_gateway.tool_surface.tool_search.type -ne 'tool_search' -or
    $wire.post_gateway.tool_surface.tool_search.execution -ne 'client'
) {
    Add-Mismatch 'tool_search identity changed across the Gateway route'
}
$preResponse = $wire.pre_gateway.response | ConvertTo-Json -Depth 20 -Compress
$postResponse = $wire.post_gateway.response | ConvertTo-Json -Depth 20 -Compress
if ($preResponse -ne $postResponse) {
    Add-Mismatch 'pre-Gateway and post-Gateway response/SSE identity changed'
}
$preChoice = $wire.pre_gateway.choice_controls
$postChoice = $wire.post_gateway.choice_controls
if (
    $preChoice.tool_choice -ne 'auto' -or
    $preChoice.fixture_kind -ne 'contract_sentinel' -or
    $preChoice.captured -ne $false -or
    ($preChoice | ConvertTo-Json -Depth 10 -Compress) -ne ($postChoice | ConvertTo-Json -Depth 10 -Compress)
) {
    Add-Mismatch 'pre-Gateway and post-Gateway choice-control fixture is invalid'
}

$requiredCallIds = @($wire.history.required_call_ids)
$linkedCallIds = @($callLinks | ForEach-Object { $_.call_id })
Assert-Set -Name 'history call links' -Expected $requiredCallIds -Actual $linkedCallIds
foreach ($link in $callLinks) {
    if (
        [string]::IsNullOrWhiteSpace($link.call_item_id) -or
        [string]::IsNullOrWhiteSpace($link.output_item_id) -or
        $link.call_type -notin @('function_call', 'custom_tool_call') -or
        $link.output_type -notin @('function_call_output', 'custom_tool_call_output')
    ) {
        Add-Mismatch "invalid history call link for $($link.call_id)"
    }
}

$streamUnknown = @($wire.response.streaming.events | Where-Object { $_.tag -eq 'unknown' })
$nonStreamingUnknown = @($wire.response.non_streaming.response_items | Where-Object { $_.tag -eq 'unknown' })
if ($streamUnknown.Count -ne 1 -or $nonStreamingUnknown.Count -ne 1) {
    Add-Mismatch 'unknown tagged sentinels were not preserved in both response modes'
}
if (
    $wire.response.streaming.captured -ne $true -or
    $wire.response.non_streaming.captured -ne $false -or
    $wire.response.non_streaming.fixture_kind -ne 'contract_sentinel'
) {
    Add-Mismatch 'streaming and non-streaming fixture boundary is invalid'
}
if (@($wire.response.streaming.observed_event_counts).Count -eq 0) {
    Add-Mismatch 'streaming SSE event evidence is empty'
}

Write-Output "Capture: $($trace.source.capture_id)"
Write-Output "Provider/model: $($trace.source.configured_provider_id) / $($trace.source.model)"
Write-Output "Gateway route: $($trace.gateway_route.behavior_profile)"
Write-Output "Registered Codex app tools: $($registered.Count)"
Write-Output "Direct / Deferred: $($direct.Count) / $($deferred.Count)"
Write-Output "Deferred tools discoverable through tool_search: $($discoverable.Count)"
Write-Output "Replay case: $ReplayCase"

if ($mismatches.Count -gt 0) {
    [Console]::Error.WriteLine('RECONCILIATION_MISMATCH: ' + ($mismatches -join ' | '))
    exit 1
}

if ($ReplayCase -ne 'identity') {
    [Console]::Error.WriteLine("NEGATIVE_REPLAY_CONTROL_DID_NOT_FAIL: $ReplayCase")
    exit 2
}

Write-Output 'THREAD_TOOL_SURFACE_COMPLETE'
exit 0
