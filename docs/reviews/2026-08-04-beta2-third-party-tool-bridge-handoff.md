# 0.1.8 Beta2 Third-Party Tool Bridge Handoff

## Task

Replan CodexHub 0.1.8-beta.2 around one runtime-derived, model-agnostic tool
bridge for all third-party models. Preserve exact provider/model identity, prevent
all model fallback, and use GLM-5.2 and K2.7 Code only as initial real-world
tracer bullets. Do not continue implementing the current two-model capability
gate as though it were the production routing architecture.

Before changing production code or GitHub dependencies, reconcile the findings
and proposed decisions in this document with the maintainer. Then update the ADR,
Issues, and dependency graph before implementation resumes.

## Context

The current Beta2 roadmap was intended to qualify two positive benchmark routes:

- `ollama-cloud/glm-5.2`
- `ollama-cloud/kimi-k2.7-code`

During planning, three different concerns became coupled:

1. exact provider/model route identity;
2. tool protocol encoding and adaptation;
3. model-specific qualification evidence and release claims.

That coupling is the core design problem. Exact model identity must remain
model-specific. Qualification evidence may also be recorded per model. Tool
protocol adaptation must not be keyed by model name: it must be derived from the
installed Codex runtime plan, declaration kind, wire lifecycle, execution owner,
and upstream transport protocol.

The user explicitly rejected a design in which every configured model needs a
hand-maintained capability matrix before it is allowed to run. CodexHub is a
general-purpose application. Unknown or unqualified third-party models should use
the same best-effort bridge while remaining unadvertised, rather than being denied
or silently routed to Terra.

## Audit boundary

This handoff is based on a read-only audit performed on 2026-08-04.

- Repository: `NOirBRight/CodexHub`
- Local checkout: `D:\Workstation\CodexHub`
- Local `HEAD`: `cc9df197a709fb4c7548021819ecb8fa716ed664`
- Audited architecture baseline: `origin/dev@accab8ff6eb4d6ebd93cda84585fb5f6cb89da82`
- Official Codex 0.146 source-equivalent release commit inspected:
  `e363b08c9175ac1cbe5893615dd2cb9ddf95043b2`

The local checkout is behind `origin/dev` and contains unrelated user-owned
untracked files and temporary directories. Do not clean, delete, reset, or reuse
those paths as implementation scratch space. Use an independent clean worktree for
future implementation.

No code, Issue, PR, configuration, or Git state was changed during the audit. This
handoff document is the only new file produced by the side conversation.

## Non-negotiable decisions requested by the user

- Never fallback from the selected third-party model to Terra, Luna, or another
  model because of unknown, unqualified, or incompatible tool capabilities.
- Keep the same exact provider/model throughout direct passthrough, adaptation,
  repair, and any permitted retry.
- Treat GLM-5.2 and K2.7 Code as initial conformance samples, not as architecture
  branches or an exhaustive allowlist.
- Cover all Codex tool declaration and call families generically. Do not maintain
  an ever-growing hard-coded list of tool names.
- Rewrite incompatible Codex internal tool encodings, including Collaboration
  V1/V2, instead of expecting most third-party models to natively understand them.
- Prefer bounded best-effort execution. Explicit failure is the final safety
  boundary, not the default response to an unknown capability.
- Gateway adaptation must not turn CodexHub into a tool executor or a second
  subagent scheduler.

## Three planes that must remain separate

### RoutePlan

Model-specific and provider-specific facts only:

- exact provider and model identity;
- endpoint, authentication, and upstream protocol;
- Responses/Chat transport selection;
- retry, usage, and connection policy.

The model remains in `RoutePlan` for accurate routing, accounting, catalog, and
context behavior. It must not select a hard-coded tool conversion algorithm.

### ToolBridgePlan

Request-scoped and model-agnostic:

- runtime tool inventory;
- declaration encoding;
- request-scoped alias mapping;
- call/result/history inverse mapping;
- SSE lifecycle assembly;
- execution ownership;
- deterministic structural normalization;
- bounded same-model repair eligibility.

This should become one deep Module with a small interface. Callers and tests should
not need to know namespace flattening, custom envelopes, streaming state, name
limits, or inverse history rules.

### QualificationRecord

Evidence and product claims only:

- exact candidate SHA and Codex CLI version;
- provider/model/protocol identity used by a tracer;
- accepted evidence hashes and case results;
- Supported, Unsupported, Unqualified, Temporarily unavailable, or Degraded
  claims;
- capability ceiling advertised in the release.

`Unqualified` means CodexHub does not formally advertise the capability. It must
not automatically mean the user-selected model is prohibited from a best-effort
attempt.

## What the current Codex tool surface looks like

There is no permanent fixed tool count. Official Codex 0.146 defines at least five
model-visible declaration kinds:

- `function`
- `custom`
- `namespace`
- `tool_search`
- `web_search`

The actual set varies with Codex version, mode, Features, MCP, Apps, plugins,
dynamic contributors, Collaboration backend, Code Mode, host policy, and provider
hosted capabilities.

The retained Issue #62 evidence observed 16 top-level declarations. Expanding the
visible namespaces produced approximately 23 directly callable semantics, plus 12
Deferred `codex_app` functions discoverable through `tool_search`: at least 35
unique tool semantics in that trace.

### Direct functions observed

- `shell_command`
- `list_mcp_resources`
- `list_mcp_resource_templates`
- `read_mcp_resource`
- `update_plan`
- `request_user_input`
- `request_plugin_install`
- `view_image`
- `get_goal`
- `create_goal`
- `update_goal`

### Custom tool observed

- `apply_patch`

### Collaboration V2 namespace observed

- `spawn_agent`
- `send_message`
- `followup_task`
- `wait_agent`
- `interrupt_agent`
- `list_agents`

### Other namespace semantics observed

- `image_gen.imagegen`
- `codex_app.load_workspace_dependencies`
- `codex_app.navigate_to_codex_page`
- `codex_app.read_thread_terminal`

### Deferred Codex App tools observed

- `automation_update`
- `fork_thread`
- `handoff_thread`
- `get_handoff_status`
- `list_projects`
- `create_thread`
- `list_threads`
- `read_thread`
- `send_message_to_thread`
- `set_thread_pinned`
- `set_thread_archived`
- `set_thread_title`

### Discovery tool observed

- client-executed `tool_search`

This is an observed sample, not an authoritative fixed list. The committed Issue
#62 artifact is bound to legacy CLI `0.144.0-alpha.4`, below its declared 0.145
floor. Exact 0.146 inventory remains an open acceptance gap.

## Current implementation support matrix

| Tool or protocol family | Current state | Important limitation |
| --- | --- | --- |
| Plain `function` | Basic structural support | Individual model behavior is not guaranteed |
| `apply_patch` custom | Partial, special-cased | Not a general custom-tool bridge |
| Arbitrary custom tool | Not generally supported | Code Mode `exec` and future custom tools need a generic envelope |
| `codex_app` and `mcp__*` namespace | Partial | Existing flattener recognizes only these families and route policy can still strip them |
| Arbitrary namespace | Not generally supported | Collaboration V2, `image_gen`, and future dynamic namespaces lack generic flattening |
| Collaboration V1 | Partial and heavily hard-coded | Five V1 tools plus Gateway repair/scheduling/state behavior |
| Collaboration V2 | Not implemented in the Python Gateway | No generic support for the six V2 tools, task paths, or `fork_turns` |
| `tool_search` | Partial | Function wrapper and empty-miss guard exist; full Deferred registry lifecycle does not |
| Code Mode | Not generally supported | Custom `exec` plus function `wait` need reversible generic encoding |
| Provider-hosted `web_search` | Provider-dependent | Gateway cannot fabricate a hosted capability |
| Unknown future tools/items | Safe mainly on transparent same-format routes | Compatibility/conversion paths can still drop, transcript-convert, or reject them |

### Concrete code findings

- `_supports_explicit_namespace_alias()` accepts only `codex_app` and names
  beginning with `mcp__`.
- `deferred_core` strips every raw namespace and suppresses flattened namespace
  additions. A namespace the flattener does not understand can disappear.
- The current Python Gateway contains V1 names and repair logic but no matches for
  the V2-only names `followup_task`, `send_message`, `interrupt_agent`,
  `list_agents`, `fork_turns`, or `multi_agent_v2`.
- `INTERNAL_INPUT_ITEM_TYPES` does not include the observed `agent_message` input
  type. Newer official item families are not covered generically.
- The `apply_patch` SSE codec validates an exact set of fields on
  `response.output_item.added`. Providers that send identity first and arguments in
  later delta/done events can be rejected before the lifecycle is complete.
- `ExecutionOwner` currently contains only `CODEX_CLIENT`, so provider-hosted
  execution cannot be represented honestly.
- Bundled `ollama-cloud/glm-5.2` selects model-level
  `tool_surface_strategy = "deferred_core"` and
  `native_responses_tool_codec = "strict_apply_patch"`; K2.7 does not select the
  same policy. This is direct evidence of tool policy drifting into a model
  whitelist.

## Historical path that created the model-specific bias

- #105 added a narrow GLM-shaped `apply_patch` response/history adapter.
- #108 introduced `deferred_core` after GLM failed with hundreds of eagerly
  expanded namespace tools.
- #140 made the `apply_patch` request/response codec symmetric, but retained
  provider/model selection.
- #144 explicitly enabled the codec for an evidence-backed model whitelist.

Those changes addressed real failures, but the cumulative result is that transport
and schema adaptation are selected through model metadata. Beta2 should consolidate
the valid adaptations into a universal third-party Tool Bridge rather than extend
the whitelist to two more model rows.

## Execution and adaptation ownership

The phrase `local_consume` is ambiguous and should not be used as a final
disposition without naming an owner. The Issue #62 vocabulary includes it, but the
current inventory assigns no item to it.

Use two independent properties:

```text
execution_owner:
  codex_client
  upstream_provider

adaptation_owner:
  none
  codexhub_gateway
```

Examples:

- shell, apply_patch, MCP, Apps, user input, Collaboration, Code Mode, and
  client-executed `tool_search` are executed by the Codex client/host;
- hosted web search is executed by the upstream provider only when that provider
  implements it;
- CodexHub Gateway only adapts schemas and wire items. It must not execute tools,
  create agents, forge results, or act as a second scheduler.

## Recommended ThirdPartyToolBridge lifecycle

```text
Codex runtime tool plan
  -> runtime-derived Canonical Tool IR
  -> compile request declarations for the upstream transport
  -> send to the exact selected provider/model
  -> assemble response added/delta/done lifecycle
  -> inverse-map aliases, calls, IDs, and custom input
  -> return native Codex items
  -> Codex Client executes the actual tool
  -> encode the real result/history for the same model
```

### Request compilation rules

- Plain function: preserve when possible; alias only for provider name limits or
  collisions.
- Namespace: encode every runtime-provided child function through an injective,
  request-scoped alias map. Do not concatenate names naively if that can collide or
  exceed provider limits.
- Custom/freeform: encode with a reversible string envelope, such as one required
  `input` field, while retaining grammar/description in bounded form.
- `tool_search`: encode as a client-executed function bridge and translate the call,
  output, discovered declarations, subsequent call/result, and replay history.
- Hosted tools: preserve only for a provider that genuinely supports execution.
  Do not relabel a different local tool as the hosted capability.
- Unknown declaration: preserve opaquely on an approved transparent route; otherwise
  use a generic reversible envelope when call/result semantics are representable.
  Never silently delete it.

### Response compilation rules

- Assemble `output_item.added`, argument/input deltas, argument/input done, item
  done, and response terminal before final structural validation.
- Preserve `call_id`, item ID, output index, ordering, and history links.
- Convert upstream aliases back to the exact native namespace/name/custom identity.
- Do not translate reasoning text into a fake message merely to keep a malformed
  tool lifecycle alive.
- Do not fabricate tool success, agent lifecycle events, or execution results.

## Best-effort ladder

All steps retain the exact same provider/model.

1. Direct native passthrough when the upstream transport explicitly supports the
   relevant Codex dialect.
2. Universal reversible Tool Bridge encoding.
3. Deterministic, unambiguous structural normalization.
4. One alternate-codec retry only before any model output or tool side effect.
5. One bounded same-model format-repair attempt when output is malformed but no
   tool has executed.
6. If an optional tool cannot be represented, continue with remaining tools/text
   and produce a visible sanitized unavailable diagnostic; do not silently drop it.
7. Stop only at the final safety boundary.

The following conditions justify stopping:

- `tool_choice` explicitly requires a tool with no real execution owner;
- namespace/name/call/result identity cannot be recovered uniquely;
- V1 and V2 state would be mixed or corrupted;
- a tool already produced a side effect and replay could execute it twice;
- a provider-hosted capability is absent and no true Codex-client equivalent exists;
- downstream output has already been exposed and retry would duplicate output;
- the protocol cannot carry a required semantic without loss or fabrication.

`fail closed` remains correct for exact model identity, catalog visibility,
configuration ownership, ambiguous call identity, and duplicate side effects. It is
too broad as the default treatment of an unknown model/tool qualification state.

## Beta2 Issue review and recommended changes

### Completed prerequisites to retain

- #267 — diagnostic control exactly-once race. Correct and closed.
- #269 — Rust restart fixture current-session identity. Correct and closed.
- #273 — official visibility/internal model filtering. Correct and closed.
- #274 — exact provider/model identity and no alias/catalog fallback. Correct,
  closed, and the authoritative model-fallback safety boundary.
- #327 — preserve explicit catalog overrides and user-owned catalog paths. Correct
  and closed.

### #62 — exact runtime and wire inventory

Keep:

- runtime-derived inventory;
- exact CLI/candidate binding;
- declaration/call/result/history/SSE coverage;
- zero silent disappearance;
- representative complete wire lifecycle.

Reduce or move to Beta6/RC:

- full evidence-platform hardening unrelated to the bridge decision;
- every hosted/unknown state appearing in a live run;
- complete dual-sidecar provenance machinery as a prerequisite to designing the
  adapter;
- unrelated process-tree hardening that can be verified later.

The body currently says Code Mode, tool_search, Collaboration V2, and Chat do not
block core inventory, while later comments demand they be inventoried. Resolve this
scope contradiction explicitly.

Current state: open and `ready-for-human`. PR #349 is a Draft evidence-hardening PR,
not product routing. Its Hosted `CI / gate` is green, but #62 remains incomplete and
does not unlock #65.

### #65 — two-model core Responses capability matrix

Rewrite as a generated conformance matrix with rows for:

- declaration kind;
- exposure state;
- request encoding;
- response item type;
- streaming lifecycle;
- history replay;
- execution owner;
- chosen adapter and loss classification.

GLM and K2.7 should be two real-E2E observation columns, not the source of runtime
dispatch policy. Missing model evidence must leave a route unadvertised, not
unexecutable.

### #249 — human capability decision

Retain a human release decision, but approve:

- the universal Tool Bridge contract;
- best-effort and side-effect safety rules;
- Beta2's advertised ceiling;
- accepted known limitations.

Move final GO/PARTIAL/NO-GO after both model tracer bullets. Use ADR-0002 as the
architecture approval before implementation.

### #275 and #276 — GLM and K2.7 tracer bullets

Retain both as independent real CLI tracers of the same bridge.

- No `if model == glm` or `if model == kimi` production branches.
- No capability inheritance between rows.
- Both assert exact provider/model identity throughout the turn.
- Both record model behavior separately from transport/adapter failures.
- They may execute serially for credentials/environment safety, but K2.7 should not
  be logically qualified by or inherit from GLM.

### #58 — capability-driven two-model routing

This Issue requires the largest rewrite. Suggested title:

`0.1.8 Beta2: Build a runtime-derived Tool Bridge for all third-party Responses routes`

The production outcome should be the Canonical Tool IR, generic codecs, lifecycle
assembler, inverse history mapping, exact identity, execution ownership, best-effort
ladder, and sanitized diagnostics described above.

Remove acceptance language that treats every unqualified tool semantic as a reason
to fail before upstream I/O. Retain fail-closed behavior for exact identity,
irreversible semantic loss, and side-effect safety.

### #318 — Beta2 publication gate

Retain the publication gate and required order:

```text
implementation complete
  -> exact-head SHA review
  -> CLI/manual verification
  -> Hosted CI / gate
  -> final tag and prerelease
```

Change the product condition from two model-specific routing policies to:

- one generic Tool Bridge used by both tracers;
- exact identity and Beta1 regressions preserved;
- no model fallback;
- unqualified custom models receive the same best-effort bridge;
- only independently accepted capabilities are advertised.

### Related later-phase Issues

- #198 V1/V2 corruption prevention: move the safety portion before Beta2 bridge
  enablement. V1 repair must never touch V2, even when V2 is not advertised.
- #64 V1/V2 boundary: split a minimal runtime/schema boundary needed by Beta2 from
  the full Beta4 lifecycle qualification.
- #63 tool_search: put the generic bridge foundation in Beta2; retain the later
  ticket for reluctant-model behavior and complete real lifecycle qualification.
- #251 Code Mode: put the generic custom/freeform codec foundation in Beta2; retain
  the later ticket for full real CLI Code Mode qualification and advertising.
- #252 Collaboration V2: put namespace/function encoding and V1 isolation in Beta2;
  retain full task lifecycle qualification in Beta4.
- #57 and #67: the original runtime-derived complete-capability direction is
  architecturally correct, but broad provider/model qualification can remain 0.1.9.
  Do not defer the universal adapter foundation merely because the broad matrix is
  deferred.

## ADR state and recommendation

The repository currently has only ADR-0001:

`docs/adr/0001-claude-messages-intermediate-representation.md`

It governs a future Anthropic Messages seam and does not authorize Beta2 routing.
Its principles are still useful: preserve opaque IDs, never silently drop fields,
and return explicit adaptations or a non-forwardable result.

Add ADR-0002, suggested title:

`Runtime-derived third-party Tool Bridge`

The ADR should decide:

1. installed runtime plan/trace is the authoritative inventory;
2. Canonical Tool IR is an open-set representation;
3. adapters select by tool semantics and upstream protocol, never model name;
4. exact model identity never changes;
5. namespace/custom aliases are injective and request-scoped;
6. streaming is assembled before final validation;
7. Gateway adapts but does not execute or schedule;
8. execution owner distinguishes Codex Client and upstream provider;
9. qualification evidence controls advertising, not default execution eligibility;
10. best-effort retry/repair stops at output and side-effect boundaries;
11. no tool, item, ID, or result disappears silently;
12. GLM/K2.7 are tracer bullets, not architecture branches;
13. unknown future tools pass opaquely where safe or receive an explicit scoped
    disposition without triggering model fallback.

## Recommended dependency graph

```text
ADR-0002 maintainer decision
  + reduced exact-0.146 #62 inventory
  + minimal #64 V1/V2 boundary and #198 corruption guard
        |
        v
#58 universal ThirdPartyToolBridge
  + rewritten #65 semantic/protocol conformance matrix
        |
        +--> #275 GLM tracer
        |
        +--> #276 K2.7 tracer
                 |
                 v
        #249 human advertised-scope decision
                 |
                 v
        exact SHA review
          -> CLI/manual validation
          -> Hosted CI / gate
          -> #318 tag and prerelease
```

## Current Beta2 state at handoff

- Closed prerequisites: #267, #269, #273, #274, #327.
- Open release blockers: #62, #65, #249, #275, #276, #58, #318.
- #62 remains the active formal blocker.
- PR #349 is open Draft, evidence-only, and does not close #62.
- The current internal candidate has an 8/8 CLI result and a green Hosted gate, but
  those results do not complete the open Issue chain and do not authorize a
  `v0.1.8-beta.2` tag.
- No universal ThirdPartyToolBridge implementation has yet been accepted under the
  corrected design.

## What was tried and why it is insufficient

- A complete runtime/wire evidence pipeline was expanded under #62. It improved
  provenance and replay tooling but became the dominant blocker and does not itself
  implement generic tool adaptation.
- Model-level codecs and surface strategies made selected GLM workflows usable.
  They validated several adaptations but caused reusable transport behavior to be
  expressed as per-model capability metadata.
- The Beta2 roadmap narrowed the advertised ceiling to core Responses and deferred
  advanced tools. That is acceptable for release claims, but it accidentally also
  deferred the generic namespace/custom/tool_search/V1/V2 encoding foundation.
- Capability binding was used as execution eligibility. This protects formally
  advertised routes but is unsuitable for user-configured models in a general
  application.
- `fail closed` language was applied to unknown tool qualification. The same rule is
  correct for identity and side effects but too aggressive for model/tool discovery.

## Acceptance criteria for the corrected Beta2 design

- [ ] ADR-0002 is reviewed and accepted before production bridge implementation.
- [ ] `RoutePlan`, `ToolBridgePlan`, and `QualificationRecord` have separate
      responsibilities.
- [ ] No production adapter selects behavior by GLM/K2.7 model name.
- [ ] Every runtime-provided declaration has a preserved, reversible, hosted,
      optional-unavailable, or required-unavailable disposition; nothing disappears
      silently.
- [ ] Plain function, namespace, custom/freeform, client `tool_search`, hosted, and
      unknown declaration families are represented.
- [ ] Function/custom/search call, result, history, and SSE lifecycles preserve IDs
      and ordering.
- [ ] Added/delta/done event ordering variations are assembled before final
      validation.
- [ ] Collaboration V1 repair is structurally unable to run on V2.
- [ ] Gateway never executes tools, creates agents, or fabricates lifecycle success.
- [ ] Unknown/unqualified user models use the universal best-effort bridge and never
      fallback to another model.
- [ ] Alternate codec retry and format repair are bounded to the same model and to
      the pre-output/pre-side-effect window.
- [ ] GLM and K2.7 independently exercise the same implementation with exact route
      identity.
- [ ] Qualification state controls release advertising without becoming a hidden
      model allowlist.
- [ ] Implementation receives exact-head review before CLI/manual verification;
      Hosted CI runs only after review and manual verification are accepted.
- [ ] No tag or prerelease is created until every revised Beta2 gate is complete.

## Constraints

- Preserve all accepted Beta1 usability regressions, including third-party to
  Official continuation and provider-scoped context-window behavior.
- Preserve #273 visibility, #274 exact identity, and #327 user catalog ownership.
- Do not edit the native Official model cache.
- Do not treat display name, alias, ordering, or nearest catalog entry as identity.
- Do not turn an unqualified capability into a supported release claim because one
  request returned HTTP 200.
- Do not use retries, wider timeouts, or selective reruns to conceal deterministic
  failures.
- Do not log prompts, reasoning, tool arguments/results, credentials, call IDs,
  task paths, or user content.
- Do not clean or overwrite the user's existing dirty/untracked workspace state.
- Do not publish a candidate build as the formal Beta2 release before the required
  implementation, review, CLI/manual, Hosted, and release gates complete.

## Relevant files

- `src-python/codex_proxy.py` — current RoutePlan, tool exposure, namespace
  flattening, apply_patch codec, search and V1 compatibility logic.
- `src-python/codex_semantic_adapter.py` — argument and semantic normalization.
- `src-python/protocol_translation.py` — Responses/Chat request, response, history,
  and SSE conversion.
- `src-python/subagent_protocol.py` — existing V1-oriented subagent protocol logic.
- `src-python/subagent_state.py` — Gateway-owned V1 lifecycle state.
- `src-python/subagent_scheduler.py` — existing Gateway scheduler behavior that must
  not leak into V2 or the generic bridge.
- `config/providers.toml` — current model-level `deferred_core` and
  `strict_apply_patch` selection.
- `docs/evidence/issue-62/runtime-wire-inventory.json` — versioned but legacy
  runtime/wire inventory.
- `docs/evidence/issue-62/current-codexhub-thread-tool-surface.json` — retained
  observed App tool and planner surface.
- `docs/evidence/issue-62/read-only-gate-audit.json` — observed request item types
  and tool surface variants.
- `docs/adr/0001-claude-messages-intermediate-representation.md` — reusable
  preservation/non-forwardable principles, but not a Beta2 decision.

## Relevant Issues and PR

- Roadmap: https://github.com/NOirBRight/CodexHub/issues/248
- Universal runtime capability spike: https://github.com/NOirBRight/CodexHub/issues/57
- Current Beta2 routing Issue: https://github.com/NOirBRight/CodexHub/issues/58
- Typed RoutePlan foundation: https://github.com/NOirBRight/CodexHub/issues/61
- Runtime/wire inventory: https://github.com/NOirBRight/CodexHub/issues/62
- Two-model matrix: https://github.com/NOirBRight/CodexHub/issues/65
- Human capability decision: https://github.com/NOirBRight/CodexHub/issues/249
- GLM tracer: https://github.com/NOirBRight/CodexHub/issues/275
- K2.7 tracer: https://github.com/NOirBRight/CodexHub/issues/276
- Beta2 publication gate: https://github.com/NOirBRight/CodexHub/issues/318
- V1/V2 corruption guard: https://github.com/NOirBRight/CodexHub/issues/198
- Draft Issue #62 hardening PR: https://github.com/NOirBRight/CodexHub/pull/349

## Suggested next action for the receiving agent

Do not start by editing `codex_proxy.py` or continuing to expand PR #349. First:

1. present the three-plane separation and Beta2 scope decision to the maintainer;
2. confirm that Beta2 should implement generic protocol coverage while later betas
   retain full advanced-workflow qualification and advertising;
3. draft ADR-0002;
4. rewrite #58, #65, #249, #275, #276, and #318 acceptance criteria;
5. split the minimum Beta2 foundations from #63, #64, #198, #251, and #252;
6. rebuild the native dependency graph;
7. only then create a clean implementation worktree and implementation plan.

