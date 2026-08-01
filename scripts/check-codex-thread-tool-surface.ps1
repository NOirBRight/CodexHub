param(
    [string]$TracePath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'docs\evidence\issue-62\current-codexhub-thread-tool-surface.json'),
    [string]$WireFixturePath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'docs\evidence\issue-62\codexhub-runtime-wire-fixture.json'),
    [string]$AuditPath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'docs\evidence\issue-62\read-only-gate-audit.json'),
    [string]$InventoryPath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'docs\evidence\issue-62\runtime-wire-inventory.json'),
    [ValidateSet('identity', 'mutation', 'deletion', 'loss', 'required-set-deletion', 'required-membership-mutation')]
    [string]$ReplayCase = 'identity',
    [ValidateSet('identity', 'mutation', 'deletion', 'loss')]
    [string]$InventoryReplayCase = 'identity'
)

$ErrorActionPreference = 'Stop'

foreach ($path in @($TracePath, $WireFixturePath, $AuditPath, $InventoryPath)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Evidence file not found: $path"
    }
}

$trace = Get-Content -Raw -LiteralPath $TracePath | ConvertFrom-Json
$wire = Get-Content -Raw -LiteralPath $WireFixturePath | ConvertFrom-Json
$audit = Get-Content -Raw -LiteralPath $AuditPath | ConvertFrom-Json
$inventory = Get-Content -Raw -LiteralPath $InventoryPath | ConvertFrom-Json
$mismatches = [System.Collections.Generic.List[string]]::new()

function Add-Mismatch {
    param([string]$Message)
    $script:mismatches.Add($Message)
}

function Get-Sha256Hex {
    param([string]$Path)

    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.IO.File]::ReadAllBytes($Path)
        $text = [System.Text.Encoding]::UTF8.GetString($bytes).Replace("`r`n", "`n")
        $canonicalBytes = [System.Text.Encoding]::UTF8.GetBytes($text)
        return ([System.BitConverter]::ToString($sha.ComputeHash($canonicalBytes))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Get-Sha256Text {
    param([string]$Text)

    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Get-CliVersionKey {
    param([string]$Value)

    $match = [regex]::Match($Value, '^(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$')
    if (-not $match.Success) { return $null }
    return [PSCustomObject]@{
        Core = [version]::new([int]$match.Groups[1].Value, [int]$match.Groups[2].Value, [int]$match.Groups[3].Value)
        Stable = [string]::IsNullOrEmpty($match.Groups[4].Value)
    }
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

if ($audit.schema_version -ne 1 -or $audit.capture_kind -ne 'sanitized_bounded_read_only_audit') {
    Add-Mismatch 'bounded read-only audit schema is invalid'
}
$auditGateway = $audit.gateway_identity_route
if (
    $auditGateway.request_starts -le 0 -or
    $auditGateway.streaming_requests -ne $auditGateway.request_starts -or
    $auditGateway.non_streaming_requests -ne 0 -or
    $auditGateway.prefix_equal -ne $auditGateway.request_starts -or
    $auditGateway.prefix_mismatch -ne 0 -or
    $auditGateway.prefix_unavailable -ne 0 -or
    $auditGateway.full_body_hmac_pairs -ne 0 -or
    $auditGateway.full_body_hmac_both_skipped -ne $auditGateway.request_starts -or
    $auditGateway.response_body_fingerprint_fields_present -ne $false
) {
    Add-Mismatch 'bounded Gateway identity evidence or its full-wire boundary is invalid'
}
if (@($auditGateway.observed_sse_event_type_counts.PSObject.Properties).Count -eq 0) {
    Add-Mismatch 'bounded Gateway SSE event-type evidence is empty'
}

$auditPlan = $audit.model_visible_request_plan
if (
    $auditPlan.model -ne 'gpt-5.6-sol' -or
    $auditPlan.transport_log_rows -le 0 -or
    @($auditPlan.unclassified_item_types).Count -ne 0 -or
    @($auditPlan.plan_variants).Count -eq 0
) {
    Add-Mismatch 'bounded model-visible request-plan evidence is invalid'
}
foreach ($variant in @($auditPlan.plan_variants)) {
    if (
        $variant.stream -ne $true -or
        $variant.tool_choice -ne 'auto' -or
        $variant.parallel_tool_calls -ne $false -or
        -not ($auditPlan.tool_surfaces.PSObject.Properties.Name -contains $variant.tool_surface)
    ) {
        Add-Mismatch "invalid bounded planner variant $($variant.plan)"
    }
}

$auditTimeline = $audit.runtime_timeline
if (
    $auditTimeline.catalog_written_before_app_server_start -ne $true -or
    $auditTimeline.config_written_after_app_server_start -ne $true -or
    $auditTimeline.clean_cold_start_for_current_binding_proven -ne $false -or
    $auditTimeline.gateway_requests_after_app_server_start -ne 0 -or
    $auditTimeline.current_request_endpoint_classes.official_direct -le 0
) {
    Add-Mismatch 'bounded runtime timeline no longer preserves the missing current-binding cold-start control'
}

$auditGates = $audit.gate_classification
if (
    $auditGates.choice_controls -ne 'observed' -or
    $auditGates.complete_contributors_runtime_gate -ne 'partial' -or
    $auditGates.zero_unclassified_identity -ne 'partial' -or
    $auditGates.clean_cold_start_current_binding -ne 'live_control_required' -or
    $auditGates.full_pre_post_request_response -ne 'live_control_required' -or
    $auditGates.non_streaming -ne 'live_control_required' -or
    $auditGates.non_direct_states -ne 'live_control_required'
) {
    Add-Mismatch 'bounded audit overstates or misclassifies a remaining Issue #62 gate'
}

$recovery = $audit.recovery_observation
if (
    $recovery.route_level_cause -ne 'unknown' -or
    $recovery.causal_attribution -ne 'not_assigned_to_model_alone' -or
    $recovery.intervening_shared_state_mutation -ne $false -or
    $recovery.collaboration_lifecycle_owner -ne '#64'
) {
    Add-Mismatch 'recovery observation exceeds its non-causal classification boundary'
}
$sanitization = $audit.sanitization
if (
    $sanitization.emits_full_bodies -ne $false -or
    $sanitization.emits_headers_or_credentials -ne $false -or
    $sanitization.emits_paths -ne $false -or
    $sanitization.emits_prompt_arguments_or_outputs -ne $false -or
    $sanitization.emits_session_task_or_call_identifiers -ne $false
) {
    Add-Mismatch 'bounded audit sanitization contract is invalid'
}

$inventoryItems = @($inventory.items)
$knownInventoryScopes = @(
    'core_text_streaming',
    'core_text_non_streaming',
    'core_history_multiturn',
    'core_history_item_ids',
    'core_history_call_ids',
    'core_sse_streaming_events',
    'core_sse_terminal_events',
    'core_sse_errors',
    'core_function_declaration',
    'core_function_call',
    'core_function_result',
    'core_function_replay',
    'identity_item_call_ids',
    'identity_response_ids',
    'identity_request_ids',
    'choice_controls',
    'terminal_events',
    'errors',
    'hosted_only_declarations',
    'unknown_tagged_sentinels',
    'default_runtime_fields',
    'code_mode',
    'tool_search',
    'collaboration_v2',
    'chat_conversion'
)
$inventoryScopes = [System.Collections.Generic.HashSet[string]]::new()
foreach ($entry in $inventoryItems) {
    if (-not $inventoryScopes.Add($entry.scope)) {
        Add-Mismatch "inventory contains duplicate scope: $($entry.scope)"
    }
    if ($entry.scope -notin $knownInventoryScopes) {
        Add-Mismatch "inventory contains unknown scope: $($entry.scope)"
    }
    if ($entry.disposition -notin @('preserved','reversibly_adapted','local_consume','Unsupported','Unqualified','live_control_required')) {
        Add-Mismatch "inventory item $($entry.scope) has disallowed disposition $($entry.disposition)"
    }
    if (-not $entry.evidence_source) {
        Add-Mismatch "inventory item $($entry.scope) is missing evidence_source"
    }
}
$requiredCoreScopes = @(
    'core_text_streaming',
    'core_history_multiturn',
    'core_history_item_ids',
    'core_history_call_ids',
    'core_function_declaration',
    'core_function_call',
    'core_function_result',
    'core_function_replay',
    'identity_item_call_ids',
    'identity_response_ids',
    'identity_request_ids',
    'core_sse_streaming_events'
)
$requiredLiveControlScopes = @(
    'core_text_non_streaming',
    'core_sse_terminal_events',
    'core_sse_errors',
    'terminal_events',
    'errors',
    'hosted_only_declarations',
    'unknown_tagged_sentinels',
    'default_runtime_fields'
)
$requiredAdvancedScopes = @('code_mode','tool_search','collaboration_v2','chat_conversion')
$requiredChoiceScope = @('choice_controls')
$allRequiredScopes = $requiredCoreScopes + $requiredLiveControlScopes + $requiredAdvancedScopes + $requiredChoiceScope
foreach ($scope in $allRequiredScopes) {
    if (-not $inventoryScopes.Contains($scope)) {
        Add-Mismatch "inventory is missing required scope: $scope"
    }
}
if (
    $inventory.artifact_kind -ne 'runtime_wire_inventory' -or
    $inventory.schema_version -ne 1 -or
    $inventory.cli_version_floor -ne '0.145.0'
) {
    Add-Mismatch 'inventory artifact identity or CLI version floor is invalid'
}
$inventoryCandidate = $inventory.candidate_identity
if (
    $inventoryCandidate.cli_version -ne $trace.source.cli_version -or
    $inventoryCandidate.source_commit -ne $trace.planner_gates.source_commit -or
    $inventoryCandidate.codex_source_commit -ne $trace.planner_gates.source_commit -or
    $inventoryCandidate.route_upstream -ne $wire.route.upstream_route -or
    $inventoryCandidate.inbound_format -ne $wire.route.inbound_format -or
    $inventoryCandidate.upstream_format -ne $wire.route.upstream_format -or
    $inventoryCandidate.configured_provider_id -ne $trace.source.configured_provider_id -or
    $inventoryCandidate.model -ne $trace.source.model -or
    $wire.route.upstream_route -ne $trace.gateway_route.upstream -or
    $wire.route.inbound_format -ne $trace.gateway_route.inbound_format -or
    $wire.route.upstream_format -ne $trace.gateway_route.upstream_format -or
    $wire.route.configured_provider_id -ne $trace.source.configured_provider_id -or
    $wire.route.catalog_binding -ne $trace.gateway_route.catalog_binding -or
    $wire.route.behavior_profile -ne $trace.gateway_route.behavior_profile -or
    $wire.route.route_mode -ne $trace.gateway_route.route_mode -or
    $wire.route.wire_format_adapter -ne $trace.gateway_route.wire_format_adapter -or
    $wire.route.codex_semantic_adapter -ne $trace.gateway_route.codex_semantic_adapter -or
    $wire.route.repair_policy -ne $trace.gateway_route.repair_policy -or
    $wire.pre_gateway.model -ne $trace.source.model -or
    $wire.post_gateway.model -ne $trace.source.model
) {
    Add-Mismatch 'inventory candidate identity does not bind to the exact trace and wire candidate route'
}
$evidenceBindings = @{
    trace = $TracePath
    wire_fixture = $WireFixturePath
    audit = $AuditPath
}
foreach ($name in $evidenceBindings.Keys) {
    $binding = $inventory.evidence_binding.$name
    $expectedHash = if ($binding) { [string]$binding.sha256 } else { '' }
    if (-not $binding -or $binding.file -ne [System.IO.Path]::GetFileName($evidenceBindings[$name])) {
        Add-Mismatch "inventory evidence binding $name has the wrong file name"
        continue
    }
    $actualHash = Get-Sha256Hex -Path $evidenceBindings[$name]
    if ($expectedHash -ne $actualHash) {
        Add-Mismatch "inventory evidence binding $name hash does not match the input artifact"
    }
}
$manifestParts = foreach ($name in @('audit','trace','wire_fixture')) {
    $binding = $inventory.evidence_binding.$name
    '{0}:{1}:{2}' -f $name, $binding.file, $binding.sha256
}
$expectedManifest = Get-Sha256Text -Text ($manifestParts -join "`n")
if ($inventoryCandidate.evidence_manifest_sha256 -ne $expectedManifest) {
    Add-Mismatch 'inventory candidate evidence manifest is stale'
}
$coreEvidence = @{
    core_text_streaming = 'codexhub-runtime-wire-fixture.json#response.streaming.captured'
    core_text_non_streaming = 'codexhub-runtime-wire-fixture.json#response.non_streaming.captured=false'
    core_history_multiturn = 'codexhub-runtime-wire-fixture.json#history.captured_source_counts.paired_calls'
    core_history_item_ids = 'codexhub-runtime-wire-fixture.json#history.call_links'
    core_history_call_ids = 'codexhub-runtime-wire-fixture.json#history.required_call_ids'
    core_sse_streaming_events = 'codexhub-runtime-wire-fixture.json#response.streaming.events'
    core_sse_terminal_events = 'read-only-gate-audit.json#gateway_identity_route.observed_sse_event_type_counts'
    core_sse_errors = 'read-only-gate-audit.json#gate_classification.full_pre_post_request_response'
    core_function_declaration = 'codexhub-runtime-wire-fixture.json#pre_gateway.tool_surface.namespaces'
    core_function_call = 'codexhub-runtime-wire-fixture.json#history.call_links'
    core_function_result = 'codexhub-runtime-wire-fixture.json#history.call_links'
    core_function_replay = 'codexhub-runtime-wire-fixture.json#history.call_links'
    identity_item_call_ids = 'codexhub-runtime-wire-fixture.json#history.call_links'
    identity_response_ids = 'codexhub-runtime-wire-fixture.json#response.streaming.response_id'
    identity_request_ids = 'codexhub-runtime-wire-fixture.json#pre_gateway.request_id'
}
$coreAllowedDispositions = @('preserved','reversibly_adapted','live_control_required')
foreach ($scope in $coreEvidence.Keys) {
    $entry = @($inventoryItems | Where-Object { $_.scope -eq $scope })[0]
    if (-not $entry) { continue }
    if ($entry.evidence_source -ne $coreEvidence[$scope]) {
        Add-Mismatch "inventory core scope $scope has evidence_source $($entry.evidence_source), expected $($coreEvidence[$scope])"
    }
    if ($entry.disposition -notin $coreAllowedDispositions) {
        Add-Mismatch "inventory core scope $scope has disallowed incomplete disposition $($entry.disposition)"
    }
}
$qualification = $inventory.qualification
if (-not $qualification -or $qualification.candidate_version_status -notin @('eligible','legacy_below_floor')) {
    Add-Mismatch 'inventory qualification candidate version status is invalid'
}
$observedBlockingScopes = @($inventoryItems | Where-Object {
    $_.disposition -eq 'live_control_required' -or
    ($coreEvidence.ContainsKey($_.scope) -and $_.disposition -in @('Unqualified','Unsupported'))
} | ForEach-Object { $_.scope } | Sort-Object -Unique)
$reportedBlockingScopes = @($qualification.blocking_scopes | Sort-Object -Unique)
if (($observedBlockingScopes -join '|') -ne ($reportedBlockingScopes -join '|')) {
    Add-Mismatch 'inventory qualification blocking_scopes does not match item dispositions'
}
$expectedCandidateEligible = ($qualification.candidate_version_status -eq 'eligible')
$candidateVersionKey = Get-CliVersionKey -Value ([string]$inventoryCandidate.cli_version)
$floorVersionKey = Get-CliVersionKey -Value ([string]$inventory.cli_version_floor)
if (-not $candidateVersionKey -or -not $floorVersionKey) {
    Add-Mismatch 'inventory candidate CLI version or floor is malformed'
} else {
    $versionComparison = $candidateVersionKey.Core.CompareTo($floorVersionKey.Core)
    $expectedCandidateStatus = if ($versionComparison -gt 0 -or ($versionComparison -eq 0 -and $candidateVersionKey.Stable -and $floorVersionKey.Stable)) { 'eligible' } else { 'legacy_below_floor' }
    if ($qualification.candidate_version_status -ne $expectedCandidateStatus) {
        Add-Mismatch 'inventory qualification candidate version status does not match the CLI floor'
    }
}
if ([bool]$qualification.candidate_version_eligible -ne $expectedCandidateEligible) {
    Add-Mismatch 'inventory qualification candidate_version_eligible is inconsistent with status'
}
$expectedEvidenceGates = [ordered]@{
    complete_model_visible_plan = $trace.capture_coverage.complete_model_visible_plan.status
    clean_cold_start_current_binding = $trace.capture_coverage.clean_cold_start_current_binding.status
    full_pre_post_request_response = $audit.gate_classification.full_pre_post_request_response
    non_streaming = $audit.gate_classification.non_streaming
    identity_replay = $audit.gate_classification.zero_unclassified_identity
}
$acceptedEvidenceGateStatuses = @{
    complete_model_visible_plan = @('complete')
    clean_cold_start_current_binding = @('complete','pass')
    full_pre_post_request_response = @('complete','observed')
    non_streaming = @('complete','observed')
    identity_replay = @('complete','observed')
}
$observedBlockingGates = @(
    foreach ($gate in $expectedEvidenceGates.Keys) {
        if ($expectedEvidenceGates[$gate] -notin $acceptedEvidenceGateStatuses[$gate]) { $gate }
    }
)
$reportedEvidenceGates = @($qualification.evidence_gates.PSObject.Properties.Name)
if ((($reportedEvidenceGates | Sort-Object) -join '|') -ne (($expectedEvidenceGates.Keys | Sort-Object) -join '|')) {
    Add-Mismatch 'inventory qualification evidence_gates has an unexpected key set'
}
foreach ($gate in $expectedEvidenceGates.Keys) {
    if ($qualification.evidence_gates.$gate -ne $expectedEvidenceGates[$gate]) {
        Add-Mismatch "inventory qualification evidence gate $gate does not match trace/audit"
    }
}
if ((($qualification.blocking_gates | Sort-Object) -join '|') -ne (($observedBlockingGates | Sort-Object) -join '|')) {
    Add-Mismatch 'inventory qualification blocking_gates does not match trace/audit'
}
$expectedReady = $expectedCandidateEligible -and $observedBlockingScopes.Count -eq 0 -and $observedBlockingGates.Count -eq 0
if ([bool]$qualification.ready_for_beta1 -ne $expectedReady) {
    Add-Mismatch 'inventory qualification ready_for_beta1 is inconsistent with evidence blockers'
}
$advancedScopes = @('code_mode','tool_search','collaboration_v2','chat_conversion')
foreach ($scope in $advancedScopes) {
    $entry = @($inventoryItems | Where-Object { $_.scope -eq $scope })[0]
    if (-not $entry -or $entry.disposition -notin @('Unsupported','Unqualified')) {
        Add-Mismatch "inventory advanced scope $scope is not Unsupported or Unqualified"
    }
}
$liveControlScopes = @(
    'core_text_non_streaming',
    'core_sse_terminal_events',
    'core_sse_errors',
    'terminal_events',
    'errors',
    'hosted_only_declarations',
    'unknown_tagged_sentinels',
    'default_runtime_fields'
)
foreach ($scope in $liveControlScopes) {
    $entry = @($inventoryItems | Where-Object { $_.scope -eq $scope })[0]
    if (-not $entry -or $entry.disposition -ne 'live_control_required') {
        Add-Mismatch "inventory live-control scope $scope is not marked live_control_required"
    }
}
if ($inventory.identity_control.unclassified_core_items -ne 0) {
    Add-Mismatch 'inventory identity control reports unclassified core items'
}

switch ($InventoryReplayCase) {
    'mutation' {
        $target = @($inventoryItems | Where-Object { $_.scope -eq 'core_history_call_ids' })[0]
        if ($target) {
            $target.disposition = 'Supported'
            $inventory.identity_control.unclassified_core_items = ($inventory.identity_control.unclassified_core_items + 1)
        }
    }
    'deletion' {
        $inventory.items = @($inventoryItems | Where-Object { $_.scope -ne 'core_text_streaming' })
    }
    'loss' {
        $kept = [ordered]@{}
        foreach ($prop in $inventory.candidate_identity.PSObject.Properties) {
            if ($prop.Name -ne 'route_upstream') {
                $kept[$prop.Name] = $prop.Value
            }
        }
        $inventory.candidate_identity = [PSCustomObject]$kept
    }
}

$inventoryMismatches = [System.Collections.Generic.List[string]]::new()
$replayScopes = [System.Collections.Generic.HashSet[string]]::new()
$observedUnclassified = 0
foreach ($entry in $inventory.items) {
    if (-not $replayScopes.Add($entry.scope)) {
        $inventoryMismatches.Add("mutation: duplicate scope $($entry.scope)")
    }
    if ($entry.disposition -notin @('preserved','reversibly_adapted','local_consume','Unsupported','Unqualified','live_control_required')) {
        $inventoryMismatches.Add("mutation: $($entry.scope) disposition $($entry.disposition) not allowed")
        $observedUnclassified += 1
    }
}
foreach ($scope in $allRequiredScopes) {
    if (-not $replayScopes.Contains($scope)) {
        $inventoryMismatches.Add("deletion: missing scope $scope")
    }
}
if (-not ($inventory.candidate_identity.PSObject.Properties.Name -contains 'route_upstream')) {
    $inventoryMismatches.Add('loss: candidate_identity.route_upstream is missing')
}
$reportedUnclassified = $inventory.identity_control.unclassified_core_items
if ($reportedUnclassified -ne $observedUnclassified) {
    $inventoryMismatches.Add("identity_control.unclassified_core_items=$reportedUnclassified does not match observed unclassified items=$observedUnclassified")
}
if ($InventoryReplayCase -ne 'identity' -and $inventoryMismatches.Count -eq 0) {
    $inventoryMismatches.Add("NEGATIVE_INVENTORY_REPLAY_CONTROL_DID_NOT_FAIL: $InventoryReplayCase")
}
foreach ($m in $inventoryMismatches) {
    Add-Mismatch "INVENTORY_IDENTITY_MISMATCH: $m"
}

Write-Output "Capture: $($trace.source.capture_id)"
Write-Output "Provider/model: $($trace.source.configured_provider_id) / $($trace.source.model)"
Write-Output "Gateway route: $($trace.gateway_route.behavior_profile)"
Write-Output "Registered Codex app tools: $($registered.Count)"
Write-Output "Direct / Deferred: $($direct.Count) / $($deferred.Count)"
Write-Output "Deferred tools discoverable through tool_search: $($discoverable.Count)"
Write-Output "Bounded audit transport rows / Gateway starts: $($auditPlan.transport_log_rows) / $($auditGateway.request_starts)"
Write-Output "Replay case: $ReplayCase"
Write-Output "Inventory replay case: $InventoryReplayCase"

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
