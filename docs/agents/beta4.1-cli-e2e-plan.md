# Beta4.2 CLI E2E and release plan

This is the release qualification plan for the Beta4.2 Collaboration,
session-compatibility, deterministic tool-surface, transport, and third-party
adapter fixes. It carries forward the already shipped Beta4.1 regression gates
and complements the general Windows gate in
[`real-client-e2e.md`](real-client-e2e.md); it is intentionally CLI-only.

## Scope

The run must use the exact candidate Gateway build and the real Codex CLI
`0.146.1`, with a fresh isolated `CODEX_HOME` for every run. The harness may
record sanitized request/response metadata, but it must not rewrite the CLI's
request schema, inject `agent_type`, add binding sidecars, or synthesize tool
outputs. The existing `scripts/run_issue_283_cli_v2_lifecycle.py` remains
synthetic-fixture evidence because its `_inject_collaboration_agent_type()`
helper changes the request shape; it is not direct-schema evidence for this
plan.

The run requires a dedicated authenticated account, the credentials for the
historical Session's previous Provider, and a disposable copy of the
operator-supplied historical Session. Never modify the user's original Session
or shared Codex home. A current-model override does not remove the previous
Provider requirement: Codex CLI performs pre-sampling compaction with the
previous turn's model before it sends the current-model turn.

## Matrix

| Case | Native path | Required coverage | Pass evidence |
| --- | --- | --- | --- |
| `v1-lifecycle` | `multi_agent_v1` | spawn worker, send input, wait, close; verify terminal result | Native V1 namespace and argument shape remain unchanged; one complete lifecycle |
| `v2-lifecycle` | `collaboration` | spawn/list, send message, follow-up, wait, interrupt, and final readback | Native V2 lifecycle completes without a V1 rewrite or duplicate terminal event |
| `version-selection-replay` | V1 Session, then V2; fresh/forked V1 after V2 selection | replay a V1 Session after selecting V2, then prove a later V1 selection on a new Session or explicit fork | Existing history remains readable; each newly created/forked Session uses the selected version |
| `legacy-session-resume` | historical V1 Session | restore the supplied pre-Beta4 Session, continue it, wait, and close | Old worker calls with the exact native shape are accepted; continuation and history are preserved |
| `new-binding-integrity` | Beta4.1 worker binding | create a new worker with model/reasoning binding, then replay unchanged and tampered histories | Signed binding/readback matches; model, reasoning, signature, missing, duplicate, and malformed cases fail closed |
| `cli-restart-continuity` | real CLI process | stop and restart the CLI/Gateway while retaining the isolated home, then resume | Same Session identity, prior history, worker state, and selected version are read back after restart |
| `external-v1-boundary` | real CLI request to an external V1 route | capture the unmodified V1 declaration emitted by CLI `0.146.1` with the default role | The exact default-role schema and the full `agent_type` schema are accepted; any other schema difference is rejected |
| `external-v2-lifecycle` | real CLI request to `ollama-cloud/glm-5.2` through the Gateway adapter | prove a normal third-party structured-tool model can use V2 after CodexHub adapts the native namespace to ordinary function tools and maps calls back; run spawn/list, message, follow-up, wait, interrupt, wait, and final list readback | The captured upstream request contains six deterministic function aliases and no native namespace; mapped calls complete the exact client-owned V2 lifecycle with provider/model wire binding, stream/history, restart/readback, terminal/error replay, and the same target in the final `list_agents` state |
| `stable-tool-alias-replay` | external runtime compatibility | privately capture one unmodified CLI request, then transform the exact same bytes twice with fresh request contexts on the same route | Caller and upstream input hashes prove identical input; upstream body hashes and adapted aliases are identical; `prompt_cache_key` is preserved |
| `deferred-core-bounded` | external `deferred_core` route | capture a real request containing the 249-child namespace surface and pair it with the checked-in zero-child fixture | Both final surfaces retain the route's existing bounded core cardinality; no namespace-child alias survives; the eager and Official controls retain their existing behavior |

The `legacy-session-resume` case is the regression gate for #418. The
`new-binding-integrity` case must prove that the compatibility exception is
limited to the exact old V1 shape; an extended or partially edited old call
must still be rejected. `stable-tool-alias-replay` and
`deferred-core-bounded` are the release regressions for #424 and #425. The
`external-v2-lifecycle` adapter qualification is a mandatory Beta4.2 release
gate. Its result is specific to
`ollama-cloud/glm-5.2`; this is support **via the CodexHub Gateway adapter**,
not native provider V2 capability. A catalog entry, the CLI's initial
namespace, or a successful text-only response is not capability evidence. The
private replay body may not be attached to an Issue or Release; publish only
its stable hash and bounded structural counts.

The full cross-provider V2 lifecycle is a separate, known blocked gate.
OpenAI parent → non-OpenAI child handoff data can contain Official encrypted
content that the Gateway cannot decrypt. Rewriting `agent_message`, moving
fields, or removing an encrypted schema annotation cannot recover the original
task. Do not claim that opaque forwarding makes this combination work. The
upstream delivery contract must either keep encrypted content on OpenAI →
OpenAI, send provider-neutral plaintext user/message data (or re-encode after
upstream decryption) for non-OpenAI targets, and fail closed for unsupported
combinations. Until then, Beta4.2 evidence must label this gate
`blocked_upstream_provider_aware_delivery`, not a passed lifecycle.

Issue #429 is also in the Beta4.2 generic protocol-classification scope:
ordinary top-level functions whose names overlap Collaboration children must
remain ordinary provider tools, while an attempted Collaboration namespace
continues to fail closed when its frozen contract is incomplete or invalid.
Full DSH managed-client support is tracked separately in #430 and is not part
of this release.

## Beta4.2 scope ledger

Every implementation, test, and release-evidence change must map to one of
these rows. A row is not releasable until both its automated checks and its
required live/manual evidence are recorded.

| Scope ID | Requirement | Candidate implementation | Required evidence | Status |
| --- | --- | --- | --- | --- |
| `B42-418` | Legacy worker/session resume hardening | `7c7a49b2`, `76fa21c2`, `ac32867f` | Historical Session continuation with unchanged legacy call shape | pending live evidence |
| `B42-424` | Stable, collision-safe tool aliases and cache-prefix replay | `df926739` and follow-up compatibility commits | Identical caller/upstream hashes, aliases, and `prompt_cache_key`; no cache-hit-rate claim | pending live evidence |
| `B42-425` | Bounded `deferred_core` surface | `df926739`, `b86a6c6` | 249-child/zero-child parity plus eager and Official controls | pending live evidence |
| `B42-426` | Independent request-body write budget | `38ed1b69`, `f18527e0` | New/reused connection, `request_write`, read-timeout, and recovery tests | automated gate passed; live release evidence pending |
| `B42-429` | Generic Collaboration classifier boundary | `ec034882` and routing/runtime regressions | Ordinary overlap accepted; malformed Collaboration rejected; no client special case | automated gate passed; live release evidence pending |
| `B42-EXT-V2` | Third-party ordinary-tool V2 Gateway adapter | `ddd88b44`, `e3c1219b`, `0a3d7bb5` | Real `ollama-cloud/glm-5.2` lifecycle, stream/history, restart/readback, terminal/error replay | pending live qualification |
| `B42-MODELS` | OpenAI-compatible `/v1/models` projection and stable app-server model-list test boundary | `ec034882`, `e7c804a7` | HTTP `data[]`; no internal `models`/`fetched_at` fields; full serial Rust suite remains green under Windows process-start jitter | local HTTP smoke passed; packaged smoke pending |
| `B42-COPY` | Provider-qualified model copy | `ec034882` | Packaged UI clipboard reads `provider/model` for existing/new Provider | pending manual smoke |
| `B42-419` | Luna V1/V2 save contract and restart persistence | `454d1a39` and `ec034882` | Desktop saves each selection with `modelId`, then reads it back after restart | pending manual smoke |
| `B42-420` | Luna selector is exactly V1/V2 with dynamic `(Default)` marker | `454d1a39` | Desktop selector shows two choices and moves the marker with the catalog baseline | automated UI contract passed; pending manual smoke |
| `B42-421` | Official Codex quota card uses the weekly limit label | `454d1a39` | Desktop shows one quota card labeled Weekly | automated UI contract passed; pending manual smoke |
| `B42-UPSTREAM` | Cross-provider encrypted V2 boundary | upstream dependency | Record `blocked_upstream_provider_aware_delivery`; never record ciphertext | explicitly blocked |
| `B42-EVIDENCE` | Fail-closed sanitized Beta4.2 runner evidence contract | `scripts/beta42_evidence.py`, `tests/test_beta42_evidence.py` | Exact candidate/CLI binding, complete case outcomes, deterministic alias replay, adapter-owned inverse mapping, and upstream blocked classification | automated validator gate pending |
| `B42-PYTHON` | Repository-wide Python 3.13+ runtime selection; no ambient 3.11 fallback | `ae48a1eb`, `2e55d8d7`, `84fada6f`, `ff37f209`, `799d51ab`, `dd277ba7`, `03b5ea92`, `10a28af5`, `cf18d947`, `e1c285e6`, `66aa535b`, `1726c340`, plus the fixture-boundary follow-up: canonical resolver/contract, scripts compatibility import, direct `src-python/`/`scripts/`/evidence-validator entrypoints, fixture preflight and exact binding, Rust runtime resolver, E2E child-process launchers, and a separate source-vs-embedded runtime boundary | Launcher/version/preflight tests, nested bare-command PATH binding, Rust lifecycle fixtures, isolated E2E child-process checks, direct ambient-3.11 fail-closed checks for production and fixtures, explicit bundled-runtime selection, and a script-level `sys.executable -m pytest` regression | automated runtime boundary gate passed on candidate `1726c340` (2509 core tests, 1 skipped; partition checker complete; Python runtime tests 65 passed; Rust/frontend gates passed); live release evidence pending |
| `B42-RELEASE` | Beta4.2 versioned candidate and release assets | this candidate | Final main SHA, installer/portable assets, manifest/signature/SHA-256 and immutable tag | pending final release gate |

The following are explicitly outside this candidate: #430 managed-client
support, #427 Ultra evaluation, and #428 local-key rotation. They must not be
added through incidental commits or release evidence.

## Beta4.2 release notes (draft)

Beta4.2 provides a CodexHub Gateway tool-surface adapter that lets ordinary
third-party structured-tool models use the client-owned Collaboration V2
surface. This does **not** mean that a third-party provider natively supports
Collaboration V2. The OpenAI-parent → third-party-child encrypted handoff
remains limited by upstream provider-aware delivery and is recorded as
`blocked_upstream_provider_aware_delivery`; opaque forwarding is not claimed as
a fix. Beta4.2 does not include the #430 DSH managed-client/UI/YAML support.

## Execution phases

1. **Preflight**
   - Verify the candidate SHA, CLI version, Gateway health, loopback route,
     catalog model, and dedicated auth.
   - Create a new non-reparse isolated root and record only safe paths,
     versions, protocol names, and hashes.
   - Copy the historical Session into the isolated root with its identity
     intact. Redact prompts, tokens, account data, and local paths before any
     evidence leaves the machine.

2. **Direct-schema capture**
   - Run one request through the real CLI for each native path.
   - Capture the sanitized top-level request/response shape, tool namespace,
     call/output IDs, event order, model, reasoning, HTTP status, and Gateway
     correlation IDs.
   - Assert that the bytes/JSON emitted by the CLI reached the Gateway before
     any CodexHub compatibility translation. The harness may hash the input;
     it must not patch it.

3. **Tool-prefix and surface checks**
   - Preserve one external CLI request only inside the private isolated root.
     Transform those byte-identical caller bytes twice with fresh request
     contexts and the same route/capability configuration.
   - Compare the complete upstream-body hashes, ordered generated aliases,
     and `prompt_cache_key`; any request-scoped difference is a failure.
   - For the real 249-child request, record only the caller namespace/child
     counts and final upstream tool count. Run the checked-in zero-child
     fixture separately and require the same route-specific bounded core
     cardinality with no child alias; do not edit the captured CLI request.
   - Run the deterministic eager and Official controls from the candidate test
     suite. Eager must still expand its namespace children; Official must keep
     the native namespace and must not introduce a `__codexhub_*` alias.

4. **Lifecycle and history**
   - Execute the V1 and V2 cases in fresh Session roots.
   - Assert exactly one successful terminal outcome per turn, paired call and
     output IDs, preserved call order, and readable assistant/worker history.
   - Replay a V1 Session after selecting V2 and verify that old turns are not
     dropped or reclassified.
   - Verify a later V1 selection with a new Session or explicit fork. Codex CLI
     `0.146.1` pins Collaboration V2 in existing Session state, so an in-place
     V2-to-V1 downgrade is not a product acceptance condition.
   - Run the third-party-coordinator `external-v2-lifecycle` qualification
     against the real `ollama-cloud/glm-5.2` route. Require the exact
     eight-call order `spawn_agent`, `list_agents`, `send_message`,
     `followup_task`, `wait_agent`, `interrupt_agent`, `wait_agent`,
     `list_agents`; pair each call with its output and require the final
     readback to contain the same target ID in a terminal state. Record
     stream/history continuity, restart/readback, and both terminal and error
     replay outcomes. Require the private upstream capture to show six unique
     deterministic function aliases, zero native namespaces, paired adapted
     call/output history, and Gateway-owned evidence of inverse mapping back
     to the client-owned V2 lifecycle. The pre-Gateway capture alone is not
     adapter evidence. Any missing phase, provider/model mismatch, absent
     Gateway adapter evidence, or synthetic/legacy fixture use fails the
     adapter gate. Do not convert a failed OpenAI-parent → third-party-child
     handoff into a product failure or a false pass: record the separate
     upstream-blocked result and keep the full cross-provider gate unpassed.

5. **Historical recovery**
   - Start from the copied operator-supplied historical Session.
   - Resume without rewriting its old `multi_agent_v1.spawn_agent` calls.
   - Complete a new turn and verify that the old worker readbacks plus the new
     turn are accepted by the same Gateway process.

6. **Binding and tamper checks**
   - Record a new worker call with its model/reasoning binding and effective
     readback.
   - Replay the unchanged history, then independently alter the model,
     reasoning, signature, call identity, output, and call/output cardinality.
   - Require a bounded, sanitized rejection classification for every altered
     case. Do not log the prompt, model secrets, signatures, or raw payload.

7. **Restart and readback**
   - Stop the CLI after a completed worker turn and restart it against the same
     isolated home; repeat once with a Gateway restart if the candidate runner
     supports it.
   - Query/read the existing Session before creating a new one. Verify the
     Session ID, selected version, history, worker IDs, and next legal action.
   - For V1, restore the persisted worker with `resume_agent`, then wait/read
     its status before `close_agent`; a restarted CLI does not retain the old
     process-local worker registry.
   - Finish with a new turn and confirm no duplicate spawn or terminal event.

## Evidence contract

The planned runner should emit one sanitized summary with schema
`codexhub.beta42.cli-e2e.v1` and one record per matrix case. Each record may
contain:

- candidate SHA, CLI version, protocol (`v1`/`v2`), selected model, and
  reasoning level;
- lifecycle phase names and bounded counts for calls, outputs, stream events,
  restarts, and Gateway correlations;
- stable hashes for request/response bodies and the preserved Session history;
- stable hashes for the repeated caller/upstream tool-prefix bodies plus
  bounded namespace, child, deferred, eager, and final tool counts;
- safe result codes such as `accepted`, `legacy_native_spawn`,
  `binding_rejected`, `history_replayed`, or `restart_readback`.

It must not contain prompts, assistant text, authorization headers, tokens,
signatures, account identifiers, PIDs, absolute paths, or raw upstream
payloads. A missing, contradictory, duplicated, or unparseable event is a
failure, not an omitted field.

## Local and release checks

The checked-in `scripts/beta42_evidence.py` module is the fail-closed parser
and sanitized evidence validator for the private runner; its deterministic
tests must pass before the real matrix is executed on the dedicated Windows
host. The release checklist is:

- all supported Beta4.2 matrix gates pass with the exact CLI version and
  candidate SHA, including the real `ollama-cloud/glm-5.2` adapter
  qualification;
- the legacy Session continuation passes without changing its old call shape;
- direct-schema hashes show no harness mutation;
- repeated caller hashes and upstream hashes match for #424, and the 249-child
  #425 replay has the same final bounded-core count as the zero-child control;
- the sanitized #429 ordinary-overlapping-tool request reaches the selected
  provider without a Collaboration boundary rejection, while partial or
  schema-invalid Collaboration namespaces still fail closed;
- restart readback passes for both selected versions;
- no secret-bearing or raw Session artifact is written to the repository;
- the release evidence explicitly records the full cross-provider V2 gate as
  `blocked_upstream_provider_aware_delivery` until upstream delivery is
  provider-aware; Beta4.2 must not describe it as a completed lifecycle.

## Manual confirmation still required

- Real Codex CLI lifecycle, model switching, historical Session resume, and
  process restart require the dedicated authenticated Windows environment and
  cannot be proven by the local unit/UI checks alone.
- Desktop Beta4.1 carry-forward still needs a manual UI check: select Luna V1 and V2, save each,
  restart CodexHub, and confirm the selected value and `(Default)` marker are
  retained. The CLI plan does not replace this Desktop verification.
- If the historical Session is unavailable in the isolated test fixture, stop
  with `legacy_session_fixture_unavailable`; do not fall back to a newly
  created Session and call #418 verified.
- Do not record complete encrypted content in logs, comments, or evidence;
  record only the bounded fact that an Official encrypted payload was present
  and could not be decrypted for the non-OpenAI target.
