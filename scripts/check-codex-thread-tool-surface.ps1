param(
    [string]$SourceContractPath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'docs\evidence\issue-62\codex-0.146-source-contract.json'),
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

foreach ($path in @($SourceContractPath, $TracePath, $WireFixturePath, $AuditPath, $InventoryPath)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Evidence file not found: $path"
    }
}

$sourceContract = Get-Content -Raw -LiteralPath $SourceContractPath | ConvertFrom-Json
$trace = Get-Content -Raw -LiteralPath $TracePath | ConvertFrom-Json
$wire = Get-Content -Raw -LiteralPath $WireFixturePath | ConvertFrom-Json
$audit = Get-Content -Raw -LiteralPath $AuditPath | ConvertFrom-Json
$inventory = Get-Content -Raw -LiteralPath $InventoryPath | ConvertFrom-Json
$mismatches = [System.Collections.Generic.List[string]]::new()

function Add-Mismatch {
    param([string]$Message)
    $script:mismatches.Add($Message)
}

$expectedFamilySchemas = @(
    @{ Name = 'plain_function'; RuntimeType = 'function'; WireType = 'function'; Executor = 'codex_client'; Observation = 'not_observed_source_contract_only'; LossBoundary = 'preserve declaration and inverse call/result/history IDs'; DeclarationType = 'function'; CallType = 'function_call'; ResultType = 'function_call_output'; DeclarationRequired = @('name', 'parameters'); CallRequired = @('item_id', 'call_id', 'arguments'); ResultRequired = @('item_id', 'call_id', 'output'); HistoryRequired = @('call_id', 'call_item_id', 'output_item_id') },
    @{ Name = 'custom_freeform'; RuntimeType = 'custom'; WireType = 'custom'; Executor = 'codex_client'; Observation = 'not_observed_source_contract_only'; LossBoundary = 'preserve declaration and inverse call/result/history IDs'; DeclarationType = 'custom'; CallType = 'custom_tool_call'; ResultType = 'custom_tool_call_output'; DeclarationRequired = @('name', 'format'); CallRequired = @('item_id', 'call_id', 'input'); ResultRequired = @('item_id', 'call_id', 'output'); HistoryRequired = @('call_id', 'call_item_id', 'output_item_id') },
    @{ Name = 'namespace'; RuntimeType = 'namespace'; WireType = 'namespace'; Executor = 'codex_client'; Observation = 'not_observed_source_contract_only'; LossBoundary = 'preserve declaration and inverse call/result/history IDs'; DeclarationType = 'namespace'; CallType = 'function_call'; ResultType = 'function_call_output'; DeclarationRequired = @('name', 'tools'); CallRequired = @('item_id', 'call_id', 'namespace', 'arguments'); ResultRequired = @('item_id', 'call_id', 'output'); HistoryRequired = @('call_id', 'call_item_id', 'output_item_id', 'namespace') },
    @{ Name = 'client_executed_tool_discovery'; RuntimeType = 'tool_search'; WireType = 'tool_search'; Executor = 'codex_client'; Observation = 'not_observed_source_contract_only'; LossBoundary = 'discovery request/result stays client-executed'; DeclarationType = 'tool_search'; CallType = 'tool_search_call'; ResultType = 'tool_search_output'; DeclarationRequired = @('execution', 'parameters'); CallRequired = @('item_id', 'call_id', 'execution', 'arguments'); ResultRequired = @('item_id', 'call_id', 'execution', 'tools'); HistoryRequired = @('call_id', 'call_item_id', 'output_item_id', 'executor') },
    @{ Name = 'selected_provider_hosted'; RuntimeType = 'web_search'; WireType = 'web_search'; Executor = 'selected_provider'; Observation = 'not_observed_selected_provider_control_required'; LossBoundary = 'optional unsupported hosted capability is omitted; required capability fails visibly'; DeclarationType = 'web_search'; CallType = 'web_search_call'; ResultType = 'web_search_call'; DeclarationRequired = @('executor', 'provider_scope'); CallRequired = @('item_id', 'status', 'action'); ResultRequired = @('item_id', 'status', 'provider_scope'); HistoryRequired = @('call_id', 'call_item_id', 'output_item_id', 'executor', 'cross_provider_proxy') },
    @{ Name = 'unknown_future_kind'; RuntimeType = 'unknown'; WireType = '<unknown>'; Executor = 'unknown'; Observation = 'opaque_sentinel_only'; LossBoundary = 'retain tag and opaque payload; do not normalize'; DeclarationType = 'unknown'; CallType = 'unknown'; ResultType = 'unknown'; DeclarationRequired = @('tag', 'opaque_payload'); CallRequired = @('tag', 'opaque_payload'); ResultRequired = @('tag', 'opaque_payload'); HistoryRequired = @('call_id', 'call_item_id', 'output_item_id', 'loss_rule') }
)
$sourceContractSchemaValid = $true
$sourceContractFamilies = @($sourceContract.runtime_wire_surface.declaration_families)
$sourceContractExamples = $sourceContract.runtime_wire_surface.declaration_family_examples
foreach ($expected in $expectedFamilySchemas) {
    $family = @($sourceContractFamilies | Where-Object { $_.family -eq $expected.Name }) | Select-Object -First 1
    $exampleProperty = if ($null -ne $sourceContractExamples) { $sourceContractExamples.PSObject.Properties[$expected.Name] } else { $null }
    $example = if ($null -ne $exampleProperty) { $exampleProperty.Value } else { $null }
    if ($null -eq $family -or $null -eq $example -or
        $family.runtime_type -ne $expected.RuntimeType -or
        $family.wire_declaration_type -ne $expected.WireType -or
        $family.executor -ne $expected.Executor -or
        $family.observation -ne $expected.Observation -or
        $family.loss_boundary -ne $expected.LossBoundary) {
        $sourceContractSchemaValid = $false
        continue
    }
    $sections = @(
        @{ Name = 'declaration'; Type = $expected.DeclarationType; Required = $expected.DeclarationRequired },
        @{ Name = 'call'; Type = $expected.CallType; Required = $expected.CallRequired },
        @{ Name = 'result'; Type = $expected.ResultType; Required = $expected.ResultRequired },
        @{ Name = 'history'; Type = $null; Required = $expected.HistoryRequired }
    )
    foreach ($sectionSpec in $sections) {
        $sectionProperty = $example.PSObject.Properties[$sectionSpec.Name]
        $section = if ($null -ne $sectionProperty) { $sectionProperty.Value } else { $null }
        if ($null -eq $section -or ($null -ne $sectionSpec.Type -and $section.type -ne $sectionSpec.Type)) {
            $sourceContractSchemaValid = $false
            continue
        }
        foreach ($required in $sectionSpec.Required) {
            if ($null -eq $section.PSObject.Properties[$required]) {
                $sourceContractSchemaValid = $false
            }
        }
    }
}

# Rebuild the inventory from the bound evidence and compare it with the
# committed artifact.  The PowerShell checks below remain an independent
# reconciliation, while this call catches stale generated fields/notes that
# a hand-maintained mirror could otherwise miss.
$inventoryGenerator = Join-Path $PSScriptRoot 'build_issue_62_runtime_inventory.py'
$python = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $python) {
    Add-Mismatch 'generated inventory drift check requires the python interpreter'
} else {
    try {
        $generatorOutput = & $python.Source $inventoryGenerator `
            --source-contract $SourceContractPath `
            --trace $TracePath `
            --wire-fixture $WireFixturePath `
            --audit $AuditPath `
            --out $InventoryPath `
            --check-drift 2>&1
        if ($LASTEXITCODE -ne 0) {
            Add-Mismatch "generated inventory drift check failed: $($generatorOutput -join ' ')"
        }
    } catch {
        Add-Mismatch "generated inventory drift check failed: $($_.Exception.Message)"
    }
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

function Get-UnknownTaggedSourceCount {
    param([object]$Value)

    if ($null -eq $Value) { return 0 }
    $count = 0
    if ($Value.PSObject.Properties.Name -contains 'tag' -and $Value.tag -eq 'unknown') {
        $count++
    }
    if ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [string]) {
        foreach ($child in $Value) {
            $count += Get-UnknownTaggedSourceCount -Value $child
        }
    } elseif ($Value.PSObject.Properties.Count -gt 0) {
        foreach ($property in $Value.PSObject.Properties) {
            $count += Get-UnknownTaggedSourceCount -Value $property.Value
        }
    }
    return $count
}

function Get-WireIdentityReplayStatus {
    param(
        [object]$Audit,
        [string]$WireFixtureSha256
    )

    $replay = $Audit.wire_identity_replay
    if ($null -eq $replay) { return 'not_captured' }
    $status = if ($replay.PSObject.Properties.Name -contains 'status') {
        [string]$replay.status
    } else {
        'not_captured'
    }
    if ($status -notin @('complete', 'met')) { return $status }
    $caseNames = @('identity', 'mutation', 'deletion', 'loss')
    $sourceHash = [string]$replay.wire_fixture_sha256
    if (
        $replay.fail_closed -eq $true -and
        $sourceHash -match '^[0-9a-f]{64}$' -and
        $sourceHash -eq $WireFixtureSha256 -and
        ($caseNames | Where-Object {
            $case = $replay.cases.$_
            $case.status -notin @('complete','met') -or
            $case.observed -ne $true -or
            ([string]$case.output_sha256) -notmatch '^[0-9a-f]{64}$'
        }).Count -eq 0
    ) {
        return $status
    }
    return 'not_captured'
}

function Get-SseIdentityStatus {
    param(
        [object]$Audit,
        [string]$WireFixtureSha256
    )

    $evidence = $Audit.sse_identity
    if ($null -eq $evidence) { return 'not_captured' }
    $status = if ($evidence.PSObject.Properties.Name -contains 'status') {
        [string]$evidence.status
    } else {
        'not_captured'
    }
    if ($status -notin @('complete', 'met')) { return $status }
    if (
        $evidence.fail_closed -eq $true -and
        ([string]$evidence.wire_fixture_sha256) -match '^[0-9a-f]{64}$' -and
        [string]$evidence.wire_fixture_sha256 -eq $WireFixtureSha256 -and
        ([string]$evidence.pre_stream_sequence_sha256) -match '^[0-9a-f]{64}$' -and
        [string]$evidence.post_stream_sequence_sha256 -eq [string]$evidence.pre_stream_sequence_sha256 -and
        $evidence.event_count -gt 0
    ) {
        return $status
    }
    return 'not_captured'
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

if (
    $trace.schema_version -ne 4 -or
    $wire.schema_version -ne 1 -or
    $wire.fixture_kind -ne 'sanitized_artifact_backed_replay'
) {
    Add-Mismatch 'sanitized trace/wire schema identity is invalid'
}

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
    $trace.gateway_observability.full_request_body_fingerprint -notin @('not_captured','captured','complete','met') -or
    $trace.gateway_observability.full_response_body_fingerprint -notin @('not_captured','captured','complete','met')
) {
    Add-Mismatch 'bounded request-prefix observation is invalid'
}
if ($wire.route.classification_basis -notmatch 'never configured_provider_id alone') {
    Add-Mismatch 'wire fixture permits provider-id route classification'
}
if (
    [string]::IsNullOrWhiteSpace([string]$wire.evidence_limit.transport_observation) -or
    [string]::IsNullOrWhiteSpace([string]$wire.evidence_limit.replay_fixture)
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
$responseUnknownSource = [PSCustomObject]@{
    streaming = $wire.response.streaming
    non_streaming = $wire.response.non_streaming
}
$unknownTaggedSourceCount = Get-UnknownTaggedSourceCount -Value $responseUnknownSource
if (
    $inventory.identity_control.unknown_tagged_source_count -le 0 -or
    $inventory.identity_control.unknown_tagged_source_count -ne $unknownTaggedSourceCount
) {
    Add-Mismatch 'inventory unknown_tagged_source_count does not match the bound wire evidence'
}
if ($streamUnknown.Count -eq 0 -or $nonStreamingUnknown.Count -eq 0) {
    Add-Mismatch 'unknown tagged sentinels were not preserved in both response modes'
}
if ($wire.response.streaming.captured -ne $true) {
    Add-Mismatch 'streaming fixture boundary is invalid'
}
$nonStreaming = $wire.response.non_streaming
if ($nonStreaming.captured -eq $false) {
    if ($nonStreaming.fixture_kind -ne 'contract_sentinel') {
        Add-Mismatch 'uncaptured non-streaming fixture must remain an explicit contract sentinel'
    }
} elseif ($nonStreaming.captured -eq $true) {
    if (
        $nonStreaming.fixture_kind -eq 'contract_sentinel' -or
        $nonStreaming.request_stream -ne $false -or
        @($nonStreaming.response_items).Count -eq 0
    ) {
        Add-Mismatch 'captured non-streaming fixture lacks real stream=false response evidence'
    }
} else {
    Add-Mismatch 'non-streaming fixture captured flag is invalid'
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
    ($auditGateway.streaming_requests + $auditGateway.non_streaming_requests) -ne $auditGateway.request_starts -or
    $auditGateway.non_streaming_requests -lt 0 -or
    $auditGateway.prefix_equal -ne $auditGateway.request_starts -or
    $auditGateway.prefix_mismatch -ne 0 -or
    $auditGateway.prefix_unavailable -ne 0
) {
    Add-Mismatch 'bounded Gateway identity evidence or its full-wire boundary is invalid'
}
if ($auditGateway.full_body_hmac_pairs -eq 0) {
    if (
        $auditGateway.full_body_hmac_both_skipped -ne $auditGateway.request_starts -or
        $auditGateway.response_body_fingerprint_fields_present -ne $false
    ) {
        Add-Mismatch 'unavailable full-wire evidence must remain explicitly skipped'
    }
} else {
    if (
        $auditGateway.full_body_hmac_pairs -ne $auditGateway.request_starts -or
        $auditGateway.full_body_hmac_equal -ne $auditGateway.request_starts -or
        $auditGateway.full_body_hmac_mismatch -ne 0 -or
        $auditGateway.full_body_hmac_unavailable -ne 0 -or
        $auditGateway.response_body_fingerprint_fields_present -ne $true -or
        $auditGateway.response_body_fingerprint_equal -ne $auditGateway.request_starts -or
        $auditGateway.response_body_fingerprint_mismatch -ne 0 -or
        $auditGateway.response_body_fingerprint_unavailable -ne 0
    ) {
        Add-Mismatch 'full-wire evidence is not an equal, complete pre/post identity'
    }
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
        $variant.stream -notin @($true,$false) -or
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
    $auditTimeline.config_written_after_app_server_start -notin @($true,$false) -or
    $auditTimeline.clean_cold_start_for_current_binding_proven -notin @($true,$false) -or
    $auditTimeline.gateway_requests_after_app_server_start -lt 0 -or
    $auditTimeline.current_request_endpoint_classes.official_direct -le 0
) {
    Add-Mismatch 'bounded runtime timeline no longer preserves the missing current-binding cold-start control'
}

$auditGates = $audit.gate_classification
if (
    $auditGates.choice_controls -notin @('observed','met','complete') -or
    $auditGates.complete_contributors_runtime_gate -notin @('partial','met','complete') -or
    $auditGates.zero_unclassified_identity -notin @('partial','met','complete') -or
    $auditGates.clean_cold_start_current_binding -notin @('live_control_required','met','complete') -or
    $auditGates.full_pre_post_request_response -notin @('live_control_required','observed','met','complete') -or
    $auditGates.non_streaming -notin @('live_control_required','observed','met','complete') -or
    $auditGates.non_direct_states -notin @('live_control_required','observed','met','complete','pass')
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
    if ($entry.disposition -notin @('preserved','reversibly_adapted','local_consume','Unsupported','Unqualified')) {
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
    $inventory.cli_version_floor -ne '0.146.0'
) {
    Add-Mismatch 'inventory artifact identity or CLI version floor is invalid'
}
$inventoryCandidate = $inventory.candidate_identity
if ([string]$inventoryCandidate.source_commit -notmatch '^[0-9a-f]{40}$') {
    Add-Mismatch 'inventory candidate source commit is not a lowercase 40-character SHA-1'
}
if ($null -eq (Get-CliVersionKey -Value ([string]$inventoryCandidate.cli_version))) {
    Add-Mismatch 'inventory candidate CLI version is malformed'
}
if ([string]$inventoryCandidate.codex_source_commit -notmatch '^[0-9a-f]{40}$') {
    Add-Mismatch 'inventory candidate Codex source commit is not a lowercase 40-character SHA-1'
}
if ($inventoryCandidate.source_commit -ne $inventoryCandidate.codex_source_commit) {
    Add-Mismatch 'inventory candidate source and Codex source commits contradict each other'
}
foreach ($field in @(
    'route_upstream',
    'inbound_format',
    'upstream_format',
    'configured_provider_id',
    'model',
    'catalog_binding',
    'catalog_model_entry_id',
    'route_behavior_profile'
)) {
    if ([string]::IsNullOrWhiteSpace([string]$inventoryCandidate.$field)) {
        Add-Mismatch "inventory candidate $field is missing or blank"
    }
}
foreach ($field in @('catalog_snapshot_sha256','evidence_manifest_sha256')) {
    if ([string]$inventoryCandidate.$field -notmatch '^[0-9a-f]{64}$') {
        Add-Mismatch "inventory candidate $field is not a lowercase 64-character SHA-256"
    }
}
if ([string]$inventoryCandidate.candidate_revision -notmatch '^[0-9a-f]{40}$') {
    Add-Mismatch 'inventory candidate revision is not a lowercase 40-character SHA-1'
}
if ([string]$inventoryCandidate.cli_binary_sha256 -notmatch '^[0-9a-f]{64}$') {
    Add-Mismatch 'inventory candidate CLI binary SHA-256 is invalid'
}
if ([string]$inventoryCandidate.cli_binary_sha256 -ne 'bc343ba420dc2e2e9f59e6fc5e5bf0aae1cd8c771fc319665241fc9c0271fddb') {
    Add-Mismatch 'inventory candidate CLI binary SHA-256 is not the retained Codex 0.146.0 binary'
}
if ([string]$inventoryCandidate.cli_source_commit_status -notin @('published_attested','not_published_by_registry')) {
    Add-Mismatch 'inventory candidate CLI source commit status is invalid'
}
if ([string]::IsNullOrWhiteSpace([string]$inventoryCandidate.cli_source_tag)) {
    Add-Mismatch 'inventory candidate CLI source tag is missing or blank'
}
if (
    $sourceContract.schema_version -ne 1 -or
    $sourceContract.fixture_kind -ne 'codex_cli_source_contract' -or
    $sourceContract.capture_status -ne 'not_observed' -or
    $sourceContract.qualification_status -ne 'unqualified' -or
    $null -ne $sourceContract.captured_at -or
    $sourceContract.provenance.cli_version -ne '0.146.0' -or
    $sourceContract.provenance.source_commit -ne 'e363b08c9175ac1cbe5893615dd2cb9ddf95043b' -or
    $sourceContract.provenance.cli_source_tag -ne 'rust-v0.146.0' -or
    $sourceContract.provenance.cli_source_commit_status -ne 'published_attested' -or
    $sourceContract.provenance.cli_binary_sha256 -ne 'bc343ba420dc2e2e9f59e6fc5e5bf0aae1cd8c771fc319665241fc9c0271fddb' -or
    $sourceContract.provenance.candidate_revision -ne 'accab8ff6eb4d6ebd93cda84585fb5f6cb89da82' -or
    -not $sourceContractSchemaValid -or
    $sourceContract.runtime_wire_surface.declaration_families.Count -ne 6 -or
    ($sourceContract.runtime_wire_surface.declaration_families | Where-Object { $_.observed -ne $false }).Count -ne 0 -or
    ($sourceContract.runtime_wire_surface.declaration_families | Where-Object { $_.observation -notin @('not_observed_source_contract_only', 'not_observed_selected_provider_control_required', 'opaque_sentinel_only') }).Count -ne 0 -or
    $sourceContract.runtime_wire_surface.request_shape.non_streaming_control.captured -ne $false -or
    $sourceContract.runtime_wire_surface.request_shape.non_streaming_control.status -ne 'unqualified' -or
    [string]::IsNullOrWhiteSpace([string]$trace.source.capture_id) -or
    [string]::IsNullOrWhiteSpace([string]$wire.provenance.capture_id) -or
    $trace.captured_at -ne '2026-07-12T14:57:55+08:00' -or
    $wire.provenance.captured_at -ne '2026-07-12T14:57:55+08:00'
) {
    Add-Mismatch 'source contract or historical trace/wire provenance is invalid'
}
if (
    $trace.source.cli_version -ne '0.144.0-alpha.4' -or
    $trace.planner_gates.source_commit -ne '9e552e9d15ba52bed7077d5357f3e18e330f8f38' -or
    $trace.captured_at -ne '2026-07-12T14:57:55+08:00' -or
    $wire.provenance.cli_version -ne $trace.source.cli_version -or
    $wire.provenance.source_commit -ne $trace.planner_gates.source_commit -or
    $wire.provenance.captured_at -ne $trace.captured_at -or
    $inventoryCandidate.cli_version -ne $sourceContract.provenance.cli_version -or
    $inventoryCandidate.source_commit -ne $sourceContract.provenance.source_commit -or
    $inventoryCandidate.codex_source_commit -ne $sourceContract.provenance.source_commit -or
    $inventoryCandidate.candidate_revision -ne $sourceContract.provenance.candidate_revision -or
    $inventoryCandidate.cli_binary_sha256 -ne $sourceContract.provenance.cli_binary_sha256 -or
    $inventoryCandidate.cli_source_commit_status -ne $sourceContract.provenance.cli_source_commit_status -or
    $inventoryCandidate.cli_source_tag -ne $sourceContract.provenance.cli_source_tag -or
    $audit.provenance.capture_status -ne 'not_observed' -or
    $audit.provenance.cli_version -ne $sourceContract.provenance.cli_version -or
    $audit.provenance.source_commit -ne $sourceContract.provenance.source_commit -or
    $audit.provenance.candidate_revision -ne $sourceContract.provenance.candidate_revision -or
    $audit.provenance.cli_binary_sha256 -ne $sourceContract.provenance.cli_binary_sha256 -or
    $audit.provenance.cli_source_commit_status -ne $sourceContract.provenance.cli_source_commit_status -or
    $audit.provenance.cli_source_tag -ne $sourceContract.provenance.cli_source_tag -or
    $audit.provenance.historical_capture.captured_at -ne $trace.captured_at -or
    $audit.provenance.historical_capture.cli_version -ne $trace.source.cli_version -or
    $audit.provenance.historical_capture.source_commit -ne $trace.planner_gates.source_commit -or
    $inventoryCandidate.route_upstream -ne $wire.route.upstream_route -or
    $inventoryCandidate.inbound_format -ne $wire.route.inbound_format -or
    $inventoryCandidate.upstream_format -ne $wire.route.upstream_format -or
    $inventoryCandidate.configured_provider_id -ne $trace.source.configured_provider_id -or
    $inventoryCandidate.model -ne $trace.source.model -or
    $inventoryCandidate.catalog_binding -ne $trace.gateway_route.catalog_binding -or
    $inventoryCandidate.catalog_snapshot_sha256 -ne $trace.planner_gates.catalog_source.read_only_snapshot_validation.sha256 -or
    $inventoryCandidate.catalog_model_entry_id -ne $trace.planner_gates.catalog_source.read_only_snapshot_validation.model_entry_id -or
    $inventoryCandidate.route_behavior_profile -ne $trace.gateway_route.behavior_profile -or
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
    $wire.route.catalog_snapshot_sha256 -ne $trace.planner_gates.catalog_source.read_only_snapshot_validation.sha256 -or
    $wire.route.catalog_model_entry_id -ne $trace.planner_gates.catalog_source.read_only_snapshot_validation.model_entry_id -or
    $wire.route.catalog_model_supports_search_tool -ne $trace.planner_gates.catalog_source.read_only_snapshot_validation.model_entry_supports_search_tool -or
    $wire.provenance.capture_id -ne $trace.source.capture_id -or
    $wire.pre_gateway.model -ne $trace.source.model -or
    $wire.post_gateway.model -ne $trace.source.model
) {
    Add-Mismatch 'inventory candidate identity does not bind to the exact trace and wire candidate route'
}
$evidenceBindings = @{
    source_contract = $SourceContractPath
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
$manifestParts = foreach ($name in @('audit','source_contract','trace','wire_fixture')) {
    $binding = $inventory.evidence_binding.$name
    '{0}:{1}:{2}' -f $name, $binding.file, $binding.sha256
}
$expectedManifest = Get-Sha256Text -Text ($manifestParts -join "`n")
if ($inventoryCandidate.evidence_manifest_sha256 -ne $expectedManifest) {
    Add-Mismatch 'inventory candidate evidence manifest is stale'
}
$coreEvidence = @{
    core_text_streaming = 'codexhub-runtime-wire-fixture.json#response.streaming.captured'
    core_text_non_streaming = 'codexhub-runtime-wire-fixture.json#response.non_streaming.captured'
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
    identity_response_ids = 'codexhub-runtime-wire-fixture.json#pre_gateway.response.streaming.response_id|post_gateway.response.streaming.response_id'
    identity_request_ids = 'codexhub-runtime-wire-fixture.json#pre_gateway.request_id'
}
$expectedEvidenceSources = @{
    core_text_streaming = $coreEvidence.core_text_streaming
    core_text_non_streaming = $coreEvidence.core_text_non_streaming
    core_history_multiturn = $coreEvidence.core_history_multiturn
    core_history_item_ids = $coreEvidence.core_history_item_ids
    core_history_call_ids = $coreEvidence.core_history_call_ids
    core_sse_streaming_events = $coreEvidence.core_sse_streaming_events
    core_sse_terminal_events = $coreEvidence.core_sse_terminal_events
    core_sse_errors = $coreEvidence.core_sse_errors
    core_function_declaration = $coreEvidence.core_function_declaration
    core_function_call = $coreEvidence.core_function_call
    core_function_result = $coreEvidence.core_function_result
    core_function_replay = $coreEvidence.core_function_replay
    identity_item_call_ids = $coreEvidence.identity_item_call_ids
    identity_response_ids = $coreEvidence.identity_response_ids
    identity_request_ids = $coreEvidence.identity_request_ids
    choice_controls = @(
        'codexhub-runtime-wire-fixture.json#pre_gateway.choice_controls.captured=false',
        'codexhub-runtime-wire-fixture.json#pre_gateway.choice_controls.captured=true'
    )
    terminal_events = 'read-only-gate-audit.json#gate_classification.full_pre_post_request_response=live_control_required'
    errors = 'read-only-gate-audit.json#gate_classification.full_pre_post_request_response=live_control_required'
    hosted_only_declarations = 'current-codexhub-thread-tool-surface.json#exposure_state_catalog'
    unknown_tagged_sentinels = 'codexhub-runtime-wire-fixture.json#response.streaming.events.tag=unknown'
    default_runtime_fields = 'read-only-gate-audit.json#model_visible_request_plan.top_level_field_presence'
    code_mode = 'issue-248#beta.1-scope'
    tool_search = 'current-codexhub-thread-tool-surface.json#planner_gates.caller_request'
    collaboration_v2 = 'issue-248#beta.3-scope'
    chat_conversion = 'issue-248#beta.4-scope'
}
$coreAllowedDispositions = @('preserved','reversibly_adapted','Unqualified')
$coreRequiredPreservedScopes = @(
    'core_text_streaming',
    'core_history_multiturn',
    'core_history_item_ids',
    'core_history_call_ids',
    'core_sse_streaming_events',
    'core_function_declaration',
    'core_function_call',
    'core_function_result',
    'identity_item_call_ids',
    'identity_response_ids',
    'identity_request_ids'
)
foreach ($scope in $coreEvidence.Keys) {
    $entry = @($inventoryItems | Where-Object { $_.scope -eq $scope })[0]
    if (-not $entry) { continue }
    $expectedSources = @($expectedEvidenceSources[$scope])
    if ($entry.evidence_source -notin $expectedSources) {
        Add-Mismatch "inventory core scope $scope has evidence_source $($entry.evidence_source), expected fixture path/scope $($expectedSources -join ', ')"
    }
    if ($entry.disposition -notin $coreAllowedDispositions) {
        Add-Mismatch "inventory core scope $scope has disallowed disposition $($entry.disposition)"
    }
    if ($scope -in $coreRequiredPreservedScopes -and $entry.disposition -notin @('preserved','reversibly_adapted')) {
        Add-Mismatch "inventory core scope $scope requires preserved or reversibly_adapted disposition"
    }
}
foreach ($scope in $allRequiredScopes) {
    $entry = @($inventoryItems | Where-Object { $_.scope -eq $scope })[0]
    if (-not $entry) { continue }
    $expectedSources = @($expectedEvidenceSources[$scope])
    if ($entry.evidence_source -notin $expectedSources) {
        Add-Mismatch "inventory scope $scope has evidence_source $($entry.evidence_source), expected fixture path/scope $($expectedSources -join ', ')"
    }
}
$preResponseId = [string]$wire.pre_gateway.response.streaming.response_id
$postResponseId = [string]$wire.post_gateway.response.streaming.response_id
if (
    [string]::IsNullOrWhiteSpace($preResponseId) -or
    [string]::IsNullOrWhiteSpace($postResponseId) -or
    $preResponseId -ne $postResponseId
) {
    Add-Mismatch 'wire identity_response_ids evidence pointer is missing or pre/post aliases differ'
}
$qualification = $inventory.qualification
if (-not $qualification -or $qualification.candidate_version_status -notin @('eligible','legacy_below_floor')) {
    Add-Mismatch 'inventory qualification candidate version status is invalid'
}
$blockingScopes = @(
    $coreEvidence.Keys + $requiredLiveControlScopes + $requiredChoiceScope |
        Sort-Object -Unique
)
$observedBlockingScopes = @($inventoryItems | Where-Object {
    $_.scope -in $blockingScopes -and $_.disposition -eq 'Unqualified'
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
$wireFixtureSha256 = Get-Sha256Hex -Path $WireFixturePath
$expectedEvidenceGates = [ordered]@{
    complete_model_visible_plan = $trace.capture_coverage.complete_model_visible_plan.status
    clean_cold_start_current_binding = $trace.capture_coverage.clean_cold_start_current_binding.status
    full_pre_post_request_response = $audit.gate_classification.full_pre_post_request_response
    full_request_fingerprint = $trace.gateway_observability.full_request_body_fingerprint
    full_response_fingerprint = $trace.gateway_observability.full_response_body_fingerprint
    sse_identity = Get-SseIdentityStatus -Audit $audit -WireFixtureSha256 $wireFixtureSha256
    terminal_events = if ($audit.gate_classification.full_pre_post_request_response -in @('complete','met') -and @($wire.response.streaming.events | Where-Object { $_.event -eq 'response.completed' }).Count -gt 0) { 'met' } else { 'not_captured' }
    error_events = if ($audit.gate_classification.full_pre_post_request_response -in @('complete','met') -and @($wire.response.streaming.events | Where-Object { $_.event -match 'error' -or $_.tag -eq 'error' }).Count -gt 0) { 'met' } else { 'not_captured' }
    non_streaming = $audit.gate_classification.non_streaming
    non_streaming_fixture = if ($wire.response.non_streaming.captured -eq $true -and $wire.response.non_streaming.fixture_kind -ne 'contract_sentinel' -and $wire.response.non_streaming.request_stream -eq $false -and @($wire.response.non_streaming.response_items).Count -gt 0) { 'met' } else { 'not_captured' }
    identity_replay = $audit.gate_classification.zero_unclassified_identity
    wire_identity_replay = Get-WireIdentityReplayStatus -Audit $audit -WireFixtureSha256 $wireFixtureSha256
}
$acceptedEvidenceGateStatuses = @{
    complete_model_visible_plan = @('complete')
    clean_cold_start_current_binding = @('complete','pass')
    full_pre_post_request_response = @('complete','met')
    full_request_fingerprint = @('captured','complete','met')
    full_response_fingerprint = @('captured','complete','met')
    sse_identity = @('complete','met')
    terminal_events = @('complete','met')
    error_events = @('complete','met')
    non_streaming = @('complete','met')
    non_streaming_fixture = @('captured','complete','met')
    identity_replay = @('complete','met')
    wire_identity_replay = @('complete','met')
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
$expectedLiveDispositions = @{
    core_text_non_streaming = if ($expectedEvidenceGates.non_streaming_fixture -in $acceptedEvidenceGateStatuses.non_streaming_fixture) { 'preserved' } else { 'Unqualified' }
    core_sse_terminal_events = if ($expectedEvidenceGates.terminal_events -in $acceptedEvidenceGateStatuses.terminal_events) { 'preserved' } else { 'Unqualified' }
    core_sse_errors = if ($expectedEvidenceGates.error_events -in $acceptedEvidenceGateStatuses.error_events) { 'preserved' } else { 'Unqualified' }
    terminal_events = if ($expectedEvidenceGates.terminal_events -in $acceptedEvidenceGateStatuses.terminal_events) { 'preserved' } else { 'Unqualified' }
    errors = if ($expectedEvidenceGates.error_events -in $acceptedEvidenceGateStatuses.error_events) { 'preserved' } else { 'Unqualified' }
    core_function_replay = if ($expectedEvidenceGates.wire_identity_replay -in $acceptedEvidenceGateStatuses.wire_identity_replay) { 'preserved' } else { 'Unqualified' }
    hosted_only_declarations = if ($audit.gate_classification.non_direct_states -in @('observed','complete','met','pass')) { 'preserved' } else { 'Unqualified' }
    unknown_tagged_sentinels = if ($expectedEvidenceGates.full_pre_post_request_response -in $acceptedEvidenceGateStatuses.full_pre_post_request_response -and $expectedEvidenceGates.non_streaming_fixture -in $acceptedEvidenceGateStatuses.non_streaming_fixture -and $streamUnknown.Count -gt 0) { 'preserved' } else { 'Unqualified' }
    default_runtime_fields = if ($expectedEvidenceGates.full_pre_post_request_response -in $acceptedEvidenceGateStatuses.full_pre_post_request_response) { 'preserved' } else { 'Unqualified' }
}
foreach ($scope in $expectedLiveDispositions.Keys) {
    $entry = @($inventoryItems | Where-Object { $_.scope -eq $scope })[0]
    if (-not $entry -or $entry.disposition -ne $expectedLiveDispositions[$scope]) {
        Add-Mismatch "inventory dynamic scope $scope disposition does not match evidence (expected $($expectedLiveDispositions[$scope]))"
    }
}
if ($inventory.identity_control.unclassified_core_items -ne 0) {
    Add-Mismatch 'inventory identity control reports unclassified core items'
}
$expectedReplayCases = @('identity','mutation','deletion','loss')
if ($inventory.identity_control.fail_closed -ne $true) {
    Add-Mismatch 'inventory identity control is not fail-closed'
}
if ((@($inventory.identity_control.replay_cases) -join '|') -ne ($expectedReplayCases -join '|')) {
    Add-Mismatch 'inventory identity control replay cases are invalid'
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
    if ($entry.disposition -notin @('preserved','reversibly_adapted','local_consume','Unsupported','Unqualified')) {
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
