# Claude coexistence, family mapping and usage statistics: implementation plan

Date: 2026-09-23. Reconciled: 2026-09-25. Status: specification published as
[#559](https://github.com/NOirBRight/CodexHub/issues/559), split into #560–#564;
implementation and candidate work exists in PR #565. Product acceptance is
incomplete. The reconciliation below is the proposed completion plan, not a
claim of implementation, release qualification, or a published spec amendment.
Based on the user-confirmed [discussion](2026-09-23-claude-coexistence-planning.md).
Planning reference checkout: `a5ec2c5706d900d129cc344e57d7078078539293`.
Implementation must reconcile this work with the latest development baseline in
an isolated checkout before building another candidate.

## Product contract

- One Claude Code session offers native subscription models and Gateway models.
  Choosing a native model preserves its identity and subscription authentication,
  while its model requests traverse Gateway for forwarding and measurement.
- Family mappings use the native client's alias-resolution settings. Mapping
  `opus` must not replace an explicitly supplied `claude-opus-5-5` on the
  current invocation. To retain an old conversation's native identity, resume
  with `claude --resume --model <original-full-id>`; plain `--resume` follows
  Claude Code's current default/family mapping. Gateway performs no second
  family-name rewrite of complete IDs and cannot infer earlier selections.
- Preserve all native choices; mapped family entries and explicit subscription
  version entries must remain distinguishable. External entries use the existing
  Client Projection and Flat Label concepts, with the actual source identified.
- Connect alone does not select a new default. The user can edit the default
  separately. Editing a family mapping can deliberately change the effective
  target of a default using that alias; the preview discloses this.
- All new model requests through the connected route enter the existing Usage
  Statistics page, including main turns, agents, compaction and auxiliary calls.
  No historical import, quota dashboard or independent subscription polling.
- No implicit cross-provider fallback. Disabled mappings and unsupported native
  IDs produce actionable errors for affected requests rather than substitution.
- Existing subscription login and refresh remain Claude-owned. CodexHub does not
  acquire, persist or refresh Claude subscription tokens for use by other clients.

## Delivery order and dependencies

| Stage | Deliverable | Completion evidence | Depends on |
| --- | --- | --- | --- |
| A | Verified client/auth/model contract | Pinned CLI fixtures plus isolated family/explicit-ID, picker and independent local-auth checks | Latest isolated baseline |
| B | Native subscription pass-through and external credential separation | Public HTTP route tests, unchanged native model/body/semantic headers, independent auth and usage observation seam | A |
| C | Connect/settings/picker integration | Preview/readback/rollback tests, native versions retained after family mapping, default unchanged on Connect | A, B |
| D | Native request metering in existing statistics | Messages JSON/SSE accounting through persistence and public statistics output, cache ratio and cancellation/retry cases | B |
| E | Combined candidate verification | Packaged candidate, real CLI and statistics UI evidence in isolation, affected full suites | C, D |

Each stage is independently reviewable. Stage A is a bounded extension of the
existing harnesses, not a new test framework. Do not ship a settings UI before
the native route and credential contract it configures work.

## Stage A: facts that must be established before implementation choices freeze

1. Confirm native family variables resolve aliases without changing explicit
   full IDs in `/model`, `--model`, and explicitly modeled resume invocations.
   Cover the four supported families, unset mapping, `opusplan`, inherited and
   explicitly pinned subagents. Use deterministic fixtures for the broad matrix.
2. Verify mapped built-in rows and appended native full-ID entries coexist in
   the actual picker. Establish duplicate handling and native-version discovery
   without copying a private mutable model catalog into CodexHub.
3. Verify an independent local Gateway credential can travel alongside Claude
   OAuth without replacing it. A dedicated custom header is a candidate to test,
   not yet an accepted wire contract. Wrong/missing local credentials must fail;
   merely having an arbitrary bearer token must not grant Gateway access.
4. Verify the supported path for forwarding native JSON/SSE, count_tokens,
   subscription-required beta headers, retry and rate-limit response headers.
5. Optionally verify request-purpose hints for attribution. Unknown/absent hints
   never prevent routing or counting the request, and never trigger a model swap.

If a probe contradicts a confirmed product contract, report the contradiction
and revise the design before implementation; do not silently weaken acceptance.

## Stages B/C: route and configuration behavior

- Reuse the existing route selection, transport, Client Projection and managed
  configuration boundaries. Keep the native pass-through distinguishable from
  exported external provider IDs, including external Claude-named models.
- Native subscription credentials can reach only the fixed official Anthropic
  destination. Strip them before external dispatch; external routes use their
  own provider credentials. Do not follow redirects with subscription credentials.
- Preserve native request semantics and stream incrementally. Measurement is an
  observer of that stream, not a reason to convert native traffic through another
  protocol, buffer the whole answer or edit model content.
- Preserve user settings/custom headers and exact owned-key rollback. Report
  environment/project/managed conflicts rather than claiming an ineffective
  route is connected. Republish is not permission to change the default model.
- Detect old managed Gateway-token configuration and migrate only owned values
  on explicit Apply. Do not clear user-owned API credentials or helpers. Existing
  processes require restart under the new configuration before resume.
- A missing/disabled mapping does not revoke unrelated native model access.
  Preserve an invalid selection visibly rather than silently choosing a new one.
- Gateway unavailability cannot silently become direct unmeasured traffic.
  Explain failure and the existing Disconnect recovery route; direct requests
  after disconnect are outside Gateway statistics.

## Stage D: measurement contract

Reuse the existing event store, statistics commands and Usage Statistics page.
Record actual native subscription or external Provider/model identity, caller,
time, status and duration. Original requested identity can remain diagnostic;
never report DeepSeek usage as Opus merely because an Opus alias selected it.

Normalize Anthropic input into total input plus cache read/write breakdown.
Preserve enough raw numeric detail to reconcile uncached input, cache creation
and cache reads without counting parent counters and their TTL subtotals twice.
Output and any separately reported reasoning follow the upstream's inclusion
rules. Missing fields are unavailable, not fabricated zeroes.

For records with sufficient usage, token cache-hit rate is summed cache-read
tokens divided by summed total input tokens. Do not average percentages or mix
unknown-input rows into a misleading denominator. Cache writes are not cache
hits. Keep Gateway-local response-cache hits distinct from upstream prompt-cache
reads if the existing UI exposes both.

For SSE, merge start/delta usage with cumulative-counter semantics and finalize
one attempt. A downstream request and an upstream attempt are separate counts:
keep the existing public request-count convention while retaining known usage
from additional attempts and avoiding duplicate event delivery. A cancellation
can retain partial usage; HTTP 200 followed by an SSE error is not a success.
Failures without tokens still need request/status visibility.

Schema changes are additive and preserve existing statistics. Do not rewrite
historical missing data or import native transcript history. Existing API-value
estimates remain clearly distinct from actual subscription charges.

## Stage E: acceptance matrix

| Scenario | Observable acceptance |
| --- | --- |
| Connect with an existing native default | Default unchanged; native call succeeds through Gateway and appears in statistics |
| Mapped family and explicit native version | Alias reaches configured provider; full native ID and resumed full-ID session remain native |
| Picker coexistence | Native versions remain selectable after family mapping; external rows clearly identify source |
| Three upstream formats | Official DeepSeek Flash Chat/Messages and Codex Luna Responses keep their correct identities and usage; no OpenCode Go substitution |
| Same-session switching | Native to external to native preserves ordinary/tool history or surfaces a declared incompatibility |
| Compression | Manual, automatic and too-long recovery use the correct effective model and record their usage; no silent fallback |
| Native cached request sequence | Reported input/output/cache counts reconcile with persisted values and visible aggregates |
| Failure/cancel/retry | Status and count are truthful; known consumption retained, unknown usage shown, no unsafe replay |
| OAuth/local auth | Missing/wrong local key rejected; OAuth goes only to official Anthropic, never external provider or telemetry |
| Config migration and disconnect | Owned fields restored; user edits/login unchanged; restart/resume requirements accurate |
| Other clients | Existing provider routing, authentication, model lists and statistics retain their behavior |

Use synthetic known-usage responses to prove arithmetic, stream edge cases and
retry accounting. Use real Claude Code with subscription Haiku and a bounded
explicit Opus 5.5 check for native identity, official DeepSeek for external
Messages/Chat, and Codex Luna for Responses. Verify at least one full path from
real upstream usage through persistence to the existing statistics UI. Record
the exact SHA, CLI version, request/time/token bounds and sanitized artifacts;
do not treat earlier prototype success as this candidate's acceptance evidence.

Run with temporary HOME/config/history/runtime, independent ports and isolated
provider credentials. Never test against the installed Gateway port or change
the user's current sessions. Do not install the candidate as part of testing.

Implementation is **strict** under the verification policy: targeted tests at
public HTTP/config/statistics boundaries, affected Python/Rust/frontend suites
once on the reviewed candidate, and the required isolated real-client/UI matrix.
Do not expand testing by repeating already-passed suites without a changed risk.
If publishing Linux and Windows candidates, use the same SHA and the repository's
isolated Windows build procedure.

## Scope and handoff

No task-purpose routing engine, historical transcript importer, subscription
quota polling, generic Claude OAuth provider, or automatic cross-provider fallback
is introduced. Separate Default subagent configuration remains native and does
not become a second routing engine.

The tracker specification is [#559](https://github.com/NOirBRight/CodexHub/issues/559).
Its dependency-ordered children are #560 (qualification), #561 (native route),
#562 (settings/picker), #563 (usage), and #564 (candidate evidence). They remain
open as of 2026-09-25. Source changes or a portable build do not close their
observable acceptance criteria.

## 2026-09-25 reconciliation and completion plan

### What was already decided, and what was missed

Audit baseline: PR #565, `012b98547041aa5a45c22b4fe50023011faf38a8`.
Reconcile against the latest development baseline in an isolated checkout before
implementation; do not modify the dirty primary checkout or installed runtime.

| Requirement | Earlier contract | Observed gap / next action |
| --- | --- | --- |
| Native/external coexistence and complete native IDs | #559, #560, #561, #562 | Route and client code exists; retain it and requalify the combined candidate rather than rebuilding it |
| Truthful names, source labels, native versions | #559 and #562 explicitly require them | Append-mode labels do not replace built-in mapped family rows; duplicate family rows remain |
| Remember mappings after Disconnect/reconnect | Not an explicit #562 acceptance criterion | Readback uses active Claude settings, which Disconnect restores; add independent preference persistence and lifecycle acceptance |
| Native request/token/cache statistics | #559 and #563 explicitly require them | Native accounting code exists; candidate acceptance still needs a trace from upstream usage to persisted rows and rendered statistics |
| Automatic compaction and switching | #559 and #564 explicitly require them | Prior evidence must be matched to its exact SHA/case; the new picker probe does not qualify a compaction cycle |
| Documentation/tracker state | Plan originally preceded implementation | The old “not implemented / no children” status was stale; keep a per-criterion evidence ledger rather than inferring completion from commits |

The current implementation is partial, not absent. The shortcomings above include
both previously required behavior that remains incomplete and newly clarified
acceptance. Do not label every new request as a prior specification violation.

### Facts established by the isolated picker probe

On 2026-09-25, real Claude Code 2.1.282 UI and wire probes, plus 2.1.280 model-list
checks, ran with temporary HOME/config, synthetic credentials, and loopback-only
network isolation. No real subscription inference or full compaction was tested.

- Replacement picker mode displays chosen labels and preserves explicit native
  IDs even when the corresponding family maps externally. Append mode cannot
  relabel the built-in family row.
- SDK initialization exposes `models` with `resolvedModel`; it is affected by
  existing mappings and picker settings. It describes the current picker, not
  an exhaustive subscription-entitlement catalog.
- Default and the current model survive replacement. A custom Default row adds
  another row rather than replacing the built-in Default.
- Explicit external `[1m]` reports a 1M client context window; the same model
  without the suffix reports 200K in the tested configuration. Both send the
  same unsuffixed wire model ID. Default's displayed `[1m]` alone did not prove
  a 1M execution window: the short `--model default` run reported 200K.
- Global 1M disable also constrains genuine native Claude windows. `behavesAs`
  is a client behavior profile, not authoritative external capacity metadata.

Raw local artifacts: `/tmp/codexhub-picker-validation-20260925/`. Before relying
on these in a remote ticket, preserve the sanitized cases in the existing
qualification harness/evidence layout; temporary files are not durable acceptance.

### Delivery slices and completion criteria

**0. Reconcile the existing work — #559/#560/#564.**

Keep existing issues and dependencies. Amend #559/#562/#564 with the confirmed
new acceptance rather than creating a second campaign. Record each criterion as
implemented, verified, failed, or unverified, with SHA, CLI version and artifact.
Reopen ADR-0015's additive-only picker decision: replacement is justified only
after the native snapshot and ownership behavior below is qualified. This plan
proposes that amendment; the current ADR remains historical implementation policy
until the amendment accompanies implementation.

Done: each original criterion has a disposition, each new requirement has an
owning issue, and unresolved client behavior is visible before dependent work.

**1. Persist Claude Model Mapping preferences — #562.**

Reuse application settings persistence. Store stable exported model identities,
not labels or credentials. Keep saved intent separate from currently applied
Claude configuration and connection status. Default selection and Default
subagent remain independent controls.

- Initially import valid CodexHub-owned active mappings only when no saved
  preference exists; do not overwrite a saved preference during readback.
- Disconnect restores the Injected Block but retains preferences. Reconnect
  previews and reapplies them. Clearing a mapping is an explicit saved action.
- Provider removal/disable preserves the saved invalid choice visibly; it does
  not silently select another model. Invalid mapping application reports an
  actionable error while leaving existing configuration intact.
- Save/apply failure, concurrent edits, and partial publication cannot show a
  false “applied” status. Use existing locking/rollback and Toast behavior.
- Connect/republish does not change the separately selected default or reconnect
  a disconnected client. Name the exact Claude process restart requirement.

Done: public settings/UI tests demonstrate set -> Disconnect -> restart CodexHub
-> reconnect -> identical mapping, explicit clear, stale target and failed apply.

**2. Publish a truthful mixed picker — #560 then #562.**

Obtain native choices through a bounded isolated CLI initialization using a
pre-projection configuration. Remove only CodexHub-owned mappings/picker fields
from that snapshot, preserving account/policy constraints and user-owned choices.
Do not edit host settings or refresh the live subscription token. Qualify the
snapshot with the real account context before claiming account-specific coverage.

Compose native exact-ID rows with the enabled external Client Projection. Use
Display Name/Flat Label for the row and source for its description; family names
belong in mapping controls, not external model names. Deduplicate by exact route
identity, not display label; native subscription and external Claude API routes
remain distinct. Preserve qualified native 1M variants and explicit user IDs.

Use replacement mode only after successful enumeration/validation. An enumeration
timeout, unsupported CLI, managed-policy conflict, or foreign picker ownership
must preserve the last applied configuration and report why publication failed.
First connection without a valid snapshot must not publish an external-only list.
Refresh with the existing Connect/republish lifecycle; keep snapshot provenance
and detect relevant CLI/account/policy changes rather than adding a background
catalog service. A snapshot is never represented as all subscription entitlements.

Done: actual `/model` UI shows truthful external labels, no duplicate mapped
family rows, available native exact IDs, and correct behavior on refresh/failure.
Default/current are documented client-owned exceptions; custom labels cannot
promise to override them. Preserve a manual complete-native-ID path.

**3. Qualify and implement context/1M handling — #560/#562/#564.**

This is a release blocker for claiming the complete requested behavior. Generated
external rows must not advertise unsupported 1M. Preserve genuine native 1M and
avoid a global limit that silently reduces every native model's window.

Before choosing a fix, extend the existing real-CLI harness for explicit external
selection, Default, family aliases, interactive switching, and resumed tagged
sessions. Measure effective context and actual auto-compaction, not only menu
text or result metadata. Verify supported per-model client controls and any
observable context markers across the actual request boundary. Do not assume
Gateway can detect a stripped suffix or infer the user's prior selection.

If supported controls can express truthful per-model limits, implement them and
qualify the full cycle. If not, report the concrete limitation and settle the
product tradeoff before declaring completion. The minimum recovery guidance is
explicit selection/resume with the unsuffixed exported ID; guidance alone is not
proof that unsupported tagged sessions are prevented or that compression works.
Do not invent a family rewrite or global native-capability reduction to pass it.

Done: automatic compaction triggers and completes on the effective supported
model, the next turn succeeds, native 1M remains usable, and old tagged-session
behavior has a tested, explicitly accepted outcome. Manual compaction is a
separate case and cannot substitute for this result.

**4. Regress native routing and usage end to end — #561/#563.**

Reuse current native relay, credential separation, event store and Usage
Statistics UI. Explicit native IDs stay native; mapped aliases use the configured
Provider/model. Verify request counts, input/output/cache totals, aggregate hit
rate, errors/cancellation and compaction usage using known fixtures and bounded
live evidence. Do not equate Claude's model pricing estimate with subscription
charges. No separate quota dashboard or history import is introduced.

Done: captured upstream numeric usage reconciles to persisted records and the
rendered statistics page, attributed to the actual route rather than its family.

**5. Qualify and deliver one candidate — #564.**

Use temporary HOME/config/history/runtime, independent ports and isolated
processes; preserve host settings and credentials. On the reviewed candidate,
cover reconnect persistence, real picker selection, native -> external -> native
with tool history, explicit-model resume, manual and automatic compaction, and
statistics. Use native subscription Haiku plus bounded explicit Opus, official
DeepSeek Chat/Messages, and Codex Luna Responses with max effort as previously
specified. Record limits before live dispatch and distinguish fixtures from real
upstreams. Reuse prior evidence only for the exact scope and SHA it establishes.

Run the relevant strict verification suites once on the candidate, with targeted
reruns after fixes. Linux and Windows artifacts use the same SHA; Windows builds
and checks use the isolated `yoga` checkout. An unavailable platform is a blocked
row, not a pass. Deliver the portable artifact, SHA, checksums and evidence matrix
without installing it or restarting active user sessions.

### Dependency and closure discipline

Slice 0 precedes changes. Slice 1 can proceed while the slice 2/3 client contract
is qualified. Picker replacement depends on the verified snapshot contract;
context behavior remains a separate gate. Slices 1–4 feed the single slice 5
candidate. Existing #561/#563 work is reused and tested, not reimplemented.

Close an issue only when every required criterion has evidence or an explicit
accepted scope amendment. “Build succeeded”, “unit tests passed”, and “a previous
prototype worked” are not substitutes for the user-visible workflow. Leave this
plan marked incomplete until the combined candidate meets those criteria.

### Implementation verification — 2026-09-25

The completion delta on `feat/claude-subscription-coexistence` now persists mapping
intent independently of the connected client, migrates existing owned mappings,
and restores it on reconnect. Replacement picker rows combine dynamically
initialized native IDs with named external rows. Native snapshot provenance is
retained, disconnect restores the prior picker policy, and enumeration failure
leaves client configuration unchanged. Explicit native `[1m]` resume commands
are quoted. The generic frontend settings normalizer preserves mapping intent.

Verification on Linux, using an isolated checkout and Claude Code 2.1.282:

- `scripts/verify-linux.sh`: passed; Python 3262 passed / 189 skipped / 283
  subtests, Rust 789 passed / 1 ignored, clippy, and physical window input E2E.
- `npm run test:ui-contract`: passed.
- Final built-app `scripts/e2e_claude_client_settings.py --claude-bin ...`:
  passed in a network namespace with loopback only. Includes settings migration,
  restart/reconnect, clearing, default preservation, conflict and discovery failure,
  plus real CLI text roundtrip and picker metadata against the isolated fixture.
- Standards and spec review findings were fixed; delta reviews found no remaining
  code findings. Windows command-wrapper handling is covered in source but has
  not executed on Windows: SSH host `yoga` was unreachable.

This is not complete candidate release qualification. The final delta has not
repeated real subscription inference, native/external interactive switching, or
an automatic compaction cycle. Historical evidence belongs to its recorded SHA.
An explicit external `[1m]` remains a Claude client override whose suffix is
stripped before Gateway sees the request; picker replacement cannot prohibit it.
The choice between retaining genuine native 1M with that documented boundary and
a global 200K restriction remains pending user decision. No global restriction
was applied. Installed software, live client settings and active sessions were
not changed by this verification.
