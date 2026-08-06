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

The retained Desktop capture is historical: it was captured on 2026-07-12
with Codex CLI `0.144.0-alpha.4`, source commit
`9e552e9d15ba52bed7077d5357f3e18e330f8f38`. It must not be relabeled as the
later CLI 0.146.0 release. The separate
`codex-0.146-source-contract.json` records the 0.146.0 source contract (tag
`rust-v0.146.0`, attested commit `e363b08c9175ac1cbe5893615dd2cb9ddf95043b`,
and exact binary hash) with `capture_status=not_observed` and
`qualification_status=unqualified`. At that
historical revision, the dynamic
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

Its candidate provenance is explicitly `capture_status=not_observed` and is
bound to the same 0.146 source contract; the retained historical capture
metadata remains recorded separately rather than being promoted to 0.146.

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
  --source-contract docs/evidence/issue-62/codex-0.146-source-contract.json `
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

The artifact is bound to CLI floor `0.146.0` and to the candidate identity
from the unobserved 0.146 source contract (`cli_version=0.146.0`, source
commit `e363b08c9175ac1cbe5893615dd2cb9ddf95043b`, candidate revision
`accab8ff6eb4d6ebd93cda84585fb5f6cb89da82`, official Responses route). The
historical trace and wire fixture remain explicitly bound as 0.144.0 evidence.
The audit carries the 0.146 source-contract provenance with
`capture_status=not_observed` and nested historical-capture metadata; it is not
a 0.146 runtime capture. The candidate is version-eligible, but
`qualification.ready_for_beta2` remains
`false`: planner completeness, clean current-binding cold start,
independently fingerprinted full pre/post request and response bodies,
non-streaming/terminal/error/hosted/unknown controls, and wire replay evidence
are still incomplete. This is evidence for #62's downstream gates, not a #65
qualification or capability unlock. The generator rejects an explicitly
supplied CLI/source value that does not match the trace, binds route/provider/
model fields across trace and wire fixtures (including pre/post models, catalog
binding, and route profile), and records a canonical-LF SHA-256 manifest for
all four input artifacts (including the source contract). The audit sanitizer
must receive that source-contract path so reruns retain the exact
`capture_status=not_observed` 0.146 provenance and nested historical 0.144
capture metadata. It never fabricates a capability disposition for a gate the
artifacts do not qualify.

The qualification also has a separate `wire_identity_replay` gate. A full
request/response fingerprint is not treated as replay proof by itself: a
future capture must record fail-closed identity, mutation, deletion, and loss
cases as complete/met, with each case observed and bound to the current wire
fixture SHA-256 plus an output digest. The current sanitized evidence has no
such wire replay record, so this gate remains `not_captured`. Likewise,
`sse_identity` requires an independent pre/post stream-sequence digest bound to
the same wire fixture; full-body request/response equality alone is not enough.

### Disposition vocabulary

| Disposition | Meaning |
| --- | --- |
| `preserved` | Observed and carried through unchanged |
| `reversibly_adapted` | Observed with a documented reversible adaptation |
| `local_consume` | Observed and consumed locally without upstream I/O |
| `Unsupported` | Out of scope for the beta.1 core contract |
| `Unqualified` | The bounded artifacts do not qualify the item; a separately authorized live control window must capture it before any capability claim |

### Taxonomy coverage

Core items (`preserved`/`reversibly_adapted` where the bounded artifacts prove
them): core text streaming, multi-turn history, item/call IDs, streaming SSE
event kinds, standard function declaration/call/result, and identity
(request/response/item/call IDs). Function replay remains `Unqualified`: the
sanitized call-link fixture proves the shape of call/result pairs, while the
tool-membership replay does not prove a complete real-wire function replay.

Live-control items are `Unqualified` until a coordinated live window captures
them: non-streaming text, choice controls (the wire fixture carries a contract
sentinel; the bounded audit observes `tool_choice`/`parallel_tool_calls` but
full pre/post choice identity requires a live control), terminal events, errors,
hosted-only declarations, unknown tagged sentinels, and default runtime fields.

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
python scripts/build_issue_62_runtime_inventory.py --check-drift
python scripts/build_issue_62_runtime_inventory.py --replay-case mutation
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check-codex-thread-tool-surface.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check-codex-thread-tool-surface.ps1 -InventoryReplayCase mutation
```

Regeneration is deterministic and the `--check-drift` mode compares a fresh
generation to this committed JSON artifact without writing it. The PowerShell
reconciliation invokes the same drift check, then independently checks the
input fingerprints, rejects duplicate scopes, and requires each core scope to
point at its declared evidence path. A zero
`unclassified_core_items` count therefore describes vocabulary validity only;
`qualification.ready_for_beta2` is the separate completion gate. That gate also
consumes planner completeness, current-binding cold-start, full-wire
fingerprinting, non-streaming, and identity-replay statuses; item dispositions
alone cannot make an incomplete evidence set ready.

The reconciler also binds every taxonomy item's `evidence_source` to the
declared fixture path/scope (including advanced and live-control sentinels).
The `identity_response_ids` source is an explicit pair, written as
`pre_gateway.response.streaming.response_id|post_gateway.response.streaming.response_id`;
both pointers must exist and contain equal non-empty aliases.
Core claims already proven by this bounded capture must remain `preserved` or
`reversibly_adapted`; `local_consume` and `Unsupported` cannot downgrade those
claims. Candidate source and Codex source commits must agree, and the trace,
wire fixture, and capture identity are reopened and compared when an evidence
root is supplied. Contradictory self-contained candidate metadata is rejected
even for in-memory replay checks without a live or upstream control.

`identity_control.unknown_tagged_source_count` is recomputed from the bound wire
fixture during reconciliation; changing the count without changing the source
evidence fails closed.

The live-control-required gates remain open until the separately authorized
live control window documented above captures real evidence for each one.

### Sanitized control manifest

`scripts/build_issue_62_control_manifest.py` joins sanitized pre/post sidecar
records with the eight live-control labels.  The manifest stores only
allow-listed shapes, byte/SSE SHA-256/HMAC fingerprints, aggregate reference
counts, and candidate provenance; it never stores request/response bodies,
URLs, headers, credentials, prompts, tool arguments, or wire identifiers.
`synthetic_fixture_only` is always ineligible for Issue #62.  Reconciliation
validates the canonical manifest schema and fails closed on mutation,
deletion, loss, missing fingerprints, or route/catalog mismatch.

For Codex CLI `0.146.0`, the package metadata does not include `gitHead`.
Evidence may use `cli_source_commit: null` with
`cli_source_commit_status: not_published_by_registry`; a fabricated SHA is
not acceptable.  If the npm provenance attestation has been independently
verified, its exact SLSA resolved-dependency commit may instead be recorded
with status `published` (for `0.146.0`, the attested release commit is
`e363b08c9175ac1cbe5893615dd2cb9ddf95043b`).

## Isolated live-evidence sidecar lane

`scripts/capture_issue_62_live_evidence.py` is a standalone, standard-library
capture sidecar for a future, separately authorized live-control window. It is
not imported by Desktop or Gateway, has no settings/environment activation
path, and refuses to listen without `--enable-live-capture`. It accepts only a
loopback listen address.

Run two independent instances around an isolated Gateway process:

```text
isolated client -> pre sidecar -> isolated Gateway -> post sidecar -> upstream
```

The two commands require distinct output directories and a shared, isolated
32-byte-or-longer HMAC key file. The operator must replace every angle-bracket
token only after the live window identifies the exact candidate, isolated
Gateway port, authorized upstream base URL, bounds, and cleanup owner.

```powershell
py -3.13 scripts/capture_issue_62_live_evidence.py `
  --enable-live-capture `
  --hop pre `
  --listen-host 127.0.0.1 `
  --listen-port 19162 `
  --forward-base-url http://127.0.0.1:<ISOLATED_GATEWAY_PORT> `
  --output-dir <ISOLATED_OUTPUT_ROOT>\pre `
  --hmac-key-file <ISOLATED_HMAC_KEY_FILE> `
  --max-request-bytes <AUTHORIZED_REQUEST_CAP> `
  --max-response-bytes <AUTHORIZED_RESPONSE_CAP> `
  --connect-timeout-seconds <AUTHORIZED_CONNECT_TIMEOUT> `
  --read-timeout-seconds <AUTHORIZED_READ_TIMEOUT> `
  --overall-timeout-seconds <AUTHORIZED_OVERALL_TIMEOUT>

py -3.13 scripts/capture_issue_62_live_evidence.py `
  --enable-live-capture `
  --hop post `
  --listen-host 127.0.0.1 `
  --listen-port 19163 `
  --forward-base-url <AUTHORIZED_UPSTREAM_BASE_URL> `
  --output-dir <ISOLATED_OUTPUT_ROOT>\post `
  --hmac-key-file <ISOLATED_HMAC_KEY_FILE> `
  --max-request-bytes <AUTHORIZED_REQUEST_CAP> `
  --max-response-bytes <AUTHORIZED_RESPONSE_CAP> `
  --connect-timeout-seconds <AUTHORIZED_CONNECT_TIMEOUT> `
  --read-timeout-seconds <AUTHORIZED_READ_TIMEOUT> `
  --overall-timeout-seconds <AUTHORIZED_OVERALL_TIMEOUT>
```

Each hop writes only an atomic sanitized JSON record: complete application-body
byte counts, SHA-256/HMAC-SHA-256 values, an ordered SSE-frame digest, bounded
terminal classifications, or a fixed incomplete failure code. URLs, paths,
headers, credentials, key material, raw bodies, prompt/tool content, wire
identifiers, and exception text are never artifact fields. Overflow, timeout,
cancellation, forwarding failure, incomplete SSE framing, and server lifecycle
failure cannot produce a complete record and leave no `.partial` artifact.

The focused tests use loopback fake servers only:

```powershell
py -3.13 -m pytest -q tests/test_issue_62_live_evidence_sidecar.py
```

A passing test means only `synthetic_lane_verified`. Do not aim these commands
at the currently running Desktop or Gateway, perform an upstream request,
change the runtime inventory, or infer Issue #62 qualification without a new
maintainer-authorized live-control window and artifact readback.
