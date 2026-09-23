# Claude coexistence, family mapping and usage statistics: implementation plan

Date: 2026-09-23. Status: specification published as
[#559](https://github.com/NOirBRight/CodexHub/issues/559), marked ready-for-agent;
not implemented or released.
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
It restates the confirmed behavior, test boundaries and predecessor amendments
without depending on unpublished local documents. Dependency-ordered child
issues have not been created; implementation and candidate delivery have not begun.
