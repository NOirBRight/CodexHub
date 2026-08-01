# Issue #62 runtime-plan evidence

This evidence set captures one sanitized current-side Codex runtime plan and
one replay-consistency fixture. Opaque aliases replace request, response, call,
and item identifiers. Prompt text, tool arguments, tool output, and upstream
payloads are redacted.

## Facts established by the capture

- The captured codex_app dynamic namespace registers 15 functions.
- Three functions omit deferLoading; the installed runtime maps that to Direct.
  Twelve functions set deferLoading true; the runtime maps those to Deferred.
- The caller request includes client-executed tool_search. Its codex_app
  namespace contains the three Direct functions; Deferred functions remain
  discoverable through tool_search.
- The captured Gateway route is official Responses-to-Responses. Route
  classification comes from the Gateway upstream route and catalog binding, not
  from configured provider id custom.
- A caller/upstream request-prefix match was observed for 65,536 bytes.
  Full-body request and response fingerprints were not captured. The manually
  derived replay fixture checks internal consistency only; it does not rule out
  Gateway filtering beyond the observed prefix. The exact-version Desktop core
  and Code Mode app-server controls pass.

The source snapshot is OpenAI Codex commit
9e552e9d15ba52bed7077d5357f3e18e330f8f38. At that revision, the dynamic
tool protocol defines optional deferLoading; the dynamic handler maps true to
Deferred and missing or false to Direct. ToolExposure keeps Direct,
DirectModelOnly, Deferred, and Hidden distinct. Tool search is planned only
when model supports_search_tool and provider namespace_tools are both true.

## State coverage

| State | Evidence status | Meaning in this artifact |
| --- | --- | --- |
| Direct | Observed | Three codex_app functions omit deferLoading. |
| DirectModelOnly | Source contract | Distinct planner state; not used by the captured namespace. |
| Deferred | Observed | Twelve codex_app functions set deferLoading true. |
| Hidden | Source contract | Distinct planner state; not used by the captured namespace. |
| hosted-only | Sentinel | Host-binding tag retained distinctly; not inferred as a planner enum. |
| host-unavailable | Sentinel | Host-binding tag retained distinctly; not inferred as a planner enum. |

Unknown tags in the wire fixture are deliberately opaque sentinels. A replay
must preserve them rather than delete or normalize them.

## Wire and replay coverage

The wire fixture records sanitized pre-Gateway and post-Gateway request and
response/SSE shapes, request/history/response item aliases, call/item links,
observed streaming SSE event kinds, a non-streaming contract sentinel, and a
separate choice-control sentinel. The catalog source includes a read-only
fingerprint and model-entry validation for the captured catalog binding. The
replay checks:

1. reconcile registered, contributor, pre-Gateway, and post-Gateway tool
   surfaces in the replay fixture;
2. validate request/history/response call-to-output identities;
3. preserve tagged unknown SSE and non-streaming items; and
4. assert every required thread tool is registered, Deferred, and discoverable;
   and
5. fail visibly for in-memory mutation, deletion, loss, required-set deletion,
   and required-membership mutation controls.

## Fact/hypothesis boundary and remaining gap

Observed: the Desktop host/model did not select an available tool_search during
this trace. The retained evidence is insufficient to conclude whether Gateway
filtering contributed outside the observed request prefix.

The complete installed model-visible plan remains partial: this retained
sanitized capture contains the codex_app contributor only. Other contributors
and namespaces have not been inferred from this one capture.

Unproven: that a post-rewrite catalog timeline created a stale
StaticModelsManager, or that a clean restart for the current CodexHub binding
changes selection. The in-process rewrite and clean-cold-start cases remain
separate; no shared-runtime restart or configuration experiment was run for
this evidence update. Reluctant-model and tool_search lifecycle work belongs
to #63.

## Bounded read-only gate audit

`read-only-gate-audit.json` is a frozen, sanitized audit of the already-existing
Codex request log and Gateway telemetry database. The reusable auditor opens
both SQLite inputs in `mode=ro` and emits only schema names, field presence,
counts, booleans, and enums. It never emits paths, headers, credentials,
request bodies, prompts, descriptions, arguments, results, HMAC values, or any
session, task, turn, call, item, request, or response identifiers.

The bounded audit establishes these additional facts without a restart,
reconnect, configuration write, or production-handler change:

- Forty-three retained Sol transport rows resolve to three actual
  model-visible planner surfaces. The largest retained surface includes the
  base functions, collaboration namespace, goal functions, image generation,
  the three Direct codex_app functions, and client-executed tool_search.
- Every retained Sol surface is a real streaming request with
  `tool_choice=auto` and `parallel_tool_calls=false`. This replaces the prior
  choice-control sentinel with observed request evidence, but it does not
  supply a non-streaming control.
- The bounded request rows contain eight classified input item types and zero
  unknown item types. This is request-side classification only; it cannot
  satisfy full pre/post identity while full response evidence is absent.
- The current Gateway-process window contains 525 official Responses identity
  route starts. All 525 have equal caller/upstream 65,536-byte prefix HMACs and
  no prefix mismatches. All 525 are streaming, all 525 deliberately skipped
  full-body HMACs, and the telemetry schema has no response-body fingerprint.
- The current app-server process predates the current configuration write,
  there are no Gateway requests after that app-server start, and the retained
  post-start Sol transport rows classify as direct official endpoints. A clean
  cold start for the current binding is therefore not proved. The configured
  provider id is not used as route provenance.

The recovery observation remains deliberately non-causal: repeated task-level
system errors affected both continued and fresh Terra tasks on unchanged clean
branches, unrelated already-running Terra tasks continued, and fresh Sol tasks
started normally with no intervening shared-state mutation. This supports only
task-start recovery and model-binding fallback classification. The route-level
cause is unknown, model-only causality is not claimed, and full collaboration
lifecycle closure remains owned by #64.

Run the sanitizer with explicit bounded inputs and observation cutoffs:

```powershell
python scripts/audit_issue_62_runtime_artifacts.py `
  --codex-log-db <codex-log-db> `
  --gateway-db <gateway-telemetry-db> `
  --model gpt-5.6-sol `
  --gateway-started-at <gateway-start-utc> `
  --app-server-started-at <app-server-start-utc> `
  --config-written-at <config-write-utc> `
  --catalog-written-at <catalog-write-utc> `
  --snapshot-ended-at <snapshot-end-utc>
```

The remaining gates require a separately authorized live control: complete
registered contributor/defer-loading capture, a clean current-binding cold
start, independently fingerprinted full caller/upstream/downstream requests
and responses, a real non-streaming request, and observed non-Direct states.

## Versioned runtime/wire inventory

`runtime-wire-inventory.json` is the versioned inventory artifact that feeds
#249 (the beta.1 capability gate) and #66 (the Chat conversion matrix). It
records one per-scope disposition for every taxonomy item the Codex CLI
exposes over the core Responses contract and the explicitly-deferred advanced
capabilities.

The artifact is bound to CLI floor `0.145.0` and to the candidate identity
derived from the existing sanitized artifacts (`cli_version=0.144.0-alpha.4`,
source commit `9e552e9d15ba52bed7077d5357f3e18e330f8f38`, official Responses
route). Because the captured CLI is below the floor, the generated
`qualification.ready_for_beta1` is `false` and the candidate is explicitly
marked `legacy_below_floor`; this evidence cannot be used as the beta.1
candidate. The generator rejects an explicitly supplied CLI/source value that
does not match the trace, binds route/provider/model fields across trace and
wire fixtures (including pre/post models, catalog binding, and route profile),
and records a canonical-LF SHA-256 manifest for all three input artifacts.
It never fabricates a `Supported` disposition for a gate the artifacts mark
`live_control_required`.

### Disposition vocabulary

| Disposition | Meaning |
| --- | --- |
| `preserved` | Observed and carried through unchanged |
| `reversibly_adapted` | Observed with a documented reversible adaptation |
| `local_consume` | Observed and consumed locally without upstream I/O |
| `Unsupported` | Out of scope for the beta.1 core contract |
| `Unqualified` | Observed but not qualified by accepted evidence |
| `live_control_required` | The bounded artifacts do not prove the item; a separately authorized live control window must capture it before any `Supported` claim |

### Taxonomy coverage

Core items (`preserved`/`reversibly_adapted` where the bounded artifacts prove
them): core text streaming, multi-turn history, item/call IDs, streaming SSE
event kinds, standard function declaration/call/result, and identity
(request/response/item/call IDs). Function replay remains
`live_control_required`: the sanitized call-link fixture proves the shape of
call/result pairs, while the tool-membership replay does not prove a complete
real-wire function replay.

Live-control items (`live_control_required` until a coordinated live window
captures them): non-streaming text, choice controls (the wire fixture carries a
contract sentinel; the bounded audit observes `tool_choice`/`parallel_tool_calls`
but full pre/post choice identity requires a live control), terminal events,
errors, hosted-only declarations, unknown tagged sentinels, and default runtime
fields.

Advanced capabilities (`Unsupported`/`Unqualified`): Code Mode, `tool_search`,
Collaboration V2, and Chat conversion are explicitly deferred for beta.1 per
#248/#258 and are not advertised.

### Identity control

The inventory reports `unclassified_core_items: 0` and fails closed on
mutation, deletion, and loss. Replay cases run through both the Python
generator (`scripts/build_issue_62_runtime_inventory.py --replay-case`) and
the PowerShell reconciliation (`scripts/check-codex-thread-tool-surface.ps1
-InventoryReplayCase`):

```powershell
python scripts/build_issue_62_runtime_inventory.py
python scripts/build_issue_62_runtime_inventory.py --replay-case mutation
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check-codex-thread-tool-surface.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check-codex-thread-tool-surface.ps1 -InventoryReplayCase mutation
```

Regeneration is deterministic and covered by a test that compares the
generated object to this committed JSON artifact. The PowerShell reconciliation
also checks the input fingerprints, rejects duplicate scopes, and requires
each core scope to point at its declared evidence path. A zero
`unclassified_core_items` count therefore describes vocabulary validity only;
`qualification.ready_for_beta1` is the separate completion gate. That gate also
consumes planner completeness, current-binding cold-start, full-wire
fingerprinting, non-streaming, and identity-replay statuses; item dispositions
alone cannot make an incomplete evidence set ready.

The live-control-required gates remain open until the separately authorized
live control window documented above captures real evidence for each one.
