# ChatGPT Web #567–#575 completion and quality audit

Date: 2026-09-28. Owner: this integration thread, assigned to the repository
operator on all nine GitHub Issues. **Decision: not accepted as a complete
ChatGPT Web release. No child Issue qualifies for closure yet.**

Reviewed source: `11119246457a710daa8b06e22a6754b73dd491c6`.
Delivered application candidate: `7608bb175c03bba3df73014fd295e562696d93ef`.
The intervening changes are a test-only UTF-8 correction and evidence; packaged
application sources are identical. The user-confirmed review baseline remains
`cfb6f893`; its three-dot comparison has multiple merge bases. This audit pins
`aad053cf4f953fc57b523aefe3b3c701bb00836e` → `11119246` for reproducibility and
also reads the cumulative implementation already present at the boundary.

## Evidence policy

The original Issue criteria remain authoritative, with the user-approved
settings responsibility refinement in ADR-0017: account/Tunnel/connector
settings belong in the external Runtime Settings page; native Provider
Connection owns service credentials, models, and routing. Saving only prompts
for restart. This refinement does not waive original client/V2 acceptance.

A passing synthetic contract, a passing client request-format capture, an
actual provider turn, and a complete release matrix are different evidence.
Older results retain their original SHA, runtime pin, platform, and limits.
Neither open checkboxes nor passing tests alone determine completion.

[The 59-criterion matrix](acceptance-matrix.json) maps every original child
criterion to `verified`, `partial`, `not_verified`, or `blocked`. `verified`
only covers the named criterion and its stated evidence scope; it does not
close its parent Issue. No completion percentage is inferred from test counts.

## Current acceptance by Issue

| Issue | Supported by existing or new evidence | Required before closure |
|---|---|---|
| #568 | Paired runtime builds/pins, Linux real-account readiness, external settings, dual-platform normal lifecycle, secret redaction | Complete fresh-account/install/config-ownership matrix on both platforms; Windows real authorization still absent |
| #569 | Current package first Codex CLI streamed text succeeds | **F1: same-thread resume fails**; Desktop two-turn/model switch and actual cancel/cleanup |
| #570 | Historical Linux real Codex CLI file-tool round trip; replay/permission fixtures | Resolve F1; current multi-turn tool sequence, Desktop, real cancellation/failure/parallel cases |
| #571 | Shared Chat adapter fixtures; real OpenCode 1.18.32 request session header resolves | Actual OpenCode two-turn/tool final answer, concurrency, cancel and config readback |
| #572 | Messages streaming/tool/termination fixtures | **F2: real Claude Code session header is ignored**; then real tools/multi-turn/cancel and cross-client isolation |
| #573 | Plaintext spawn/call/result, ciphertext rejection and bounded-capacity fixtures | Six real V2 operations on CLI/Desktop; parent/child, saturation/cancel and cross-provider handoff |
| #574 | Image/compaction/epoch/fault fixtures; layered readiness UI | Actual image/compaction/model switch/failure recovery matrix; prerequisite F1/F2 |
| #575 | Same-SHA Linux/Windows packages, normal lifecycle, checksums and engineering checks | **F3: startup failure does not restore previous-good**; in-flight drain and full client/V2 release matrix |

#567 remains the parent tracking record. #568–#575 contain implementation work
and partial acceptance; they are not eight untouched tasks. Conversely,
settings acceptance in #584–#590 does not complete these broader requirements.

## Spec — three reproduced blocking defects

### F1 / #569: resumed Codex CLI turn is rejected

On Linux, actual Codex CLI 0.157.1 used packaged `7608bb17`, the existing
user-authorized isolated ChatGPT login, and model `chatgpt-web/gpt-5.6-sol`.
A fresh client/workspace sent a random code in the first prompt and then used
`codex exec resume --last` to ask for that code without repeating it. The
client call bound was 180 seconds per step. Expected: the same thread answers
the context-dependent second prompt. Actual: first text passes in 18.94s;
the second keeps the same Codex thread but fails in 6.68s with:

> ChatGPT web turn is missing cwd in trusted Codex environment context

The client retried (six SSE responses observed), and the planned subsequent
tool step was not run. Account text/tool readiness remained current, proving
readiness alone does not establish continuation correctness. This is a real
provider/client failure, not a simulated tool result. See
[live-codex-context.json](live-codex-context.json).

Fix scope: reconcile the actual client's continuation environment with the
pinned runtime's trusted-environment contract. Preserve real session/turn
identity; do not fabricate a cwd, permission scope, or generic environment.
Retest ordinary continuation and post-compaction tool continuation separately.
The exact layer dropping or failing to recover the context is not yet isolated.

### F2 / #572: Claude Code session header is not recognized

A credentials-free loopback capture ran actual Claude Code 2.1.283 and OpenCode
1.18.32 in separate config/data directories. Each call had a 45-second bound;
the capture endpoint intentionally returned 400 and made no provider calls.
It recorded only field/header names and booleans, then passed each request
through the candidate's public header/session parsers.

Claude sends `x-claude-code-session-id` (and session-bearing `metadata.user_id`),
but `gateway_request.request_context_from_headers` does not recognize that
header. `chatgpt_web_client_session.client_session_id` therefore returns None,
and `prepare_responses_exchange` rejects before its readiness/upstream work.
OpenCode sends `x-session-id`, which both parsers recognize. See
[client-request-contracts.json](client-request-contracts.json).

Source: `src-python/gateway_request.py:722`,
`src-python/chatgpt_web_client_session.py:35` and `:78`.
Fix scope: accept the observed explicit Claude session identity at the shared
request-context boundary, with conflict/precedence and cross-client isolation
checks. Do not treat arbitrary `metadata.user_id` as a trusted shared session.
Capture success is not model/tool acceptance; both clients still need the
original real-provider acceptance matrix.

### F3 / #575: failed new startup leaves the broken generation current

The public runtime API was exercised with two isolated synthetic archives,
using existing test fixture builders solely to prepare the executable payloads.
The good generation started successfully. The second archive had a matching
verified pin, upgraded successfully, then exited with code 17 at startup.
Expected under #575 criterion 3: recover the previous usable version. Actual:
`previous-good` remains on disk, but `current` stays on the failed generation
and no runtime is running. Cleanup stopped the fixture successfully; no real
account or production installation was involved. See
[startup-rollback.json](startup-rollback.json).

`upgrade_runtime` rolls back promotion errors, while a later `start_runtime`
health failure cleans up and raises without restoring the prior generation.
Relevant sources: `src-python/chatgpt_web_runtime.py:1222`, `:2367`, `:2434`.
Fix scope: implement and verify failure recovery across the promotion → explicit
start boundary, including pin compatibility, settings/account preservation and
owned-process cleanup. Restoring a directory is not sufficient unless the old
version can actually start safely with the recorded compatible configuration.

## Standards — current-tree adjudication

The repository's Python 3.13 launcher, isolated-worktree and no-production-write
rules were followed. The current settings controls retain the shared Toast
lifecycle and name the ChatGPT component in restart reminders. Earlier review
wording quoting unnamed restart toasts described an older tree and is not a
current violation.

Two maintainability observations remain, separate from the three reproduced
functional defects:

- `chatgpt_web_collab.install(globals())` replaces route entrypoints through a
  dictionary of names. The implicit call graph makes tracing and patching harder
  than explicit owner-module calls described in ADR-0007.
- Runtime/route/client-session modules call each other's underscore helpers and
  duplicate parts of turn/permit handling. This raises change-coupling risk;
  extract a shared public boundary when repairing an affected path, rather than
  require a speculative whole-module split before recording acceptance.

These are design review observations, not claims of an additional reproduced
runtime failure. The two automated review drafts used obsolete comparison
ranges/evidence; their stale findings were not accepted as current-tree proof.
Final acceptance here rests on direct source verification, named existing
records, and the three new reproducible probes above.

## Retained engineering evidence and next gates

[PR #589 qualification](../issue-589/review-and-portable.md) retains the complete
engineering results, invocation corrections, exact package hashes and source
matching. [Settings acceptance](../issue-588/settings-acceptance.md) retains
historical Linux real client text/tool evidence and Windows synthetic lifecycle
scope. No full suite was rerun solely for this read-only acceptance audit.

Priority order:

1. Repair and regress F1 (#569) and F2 (#572); validate same-session real text,
   then tools and cancellation in the actual clients.
2. Repair and inject-test F3 (#575), including both platform process/pin rules.
3. Complete #570/#571/#572 real client matrices; then #573 six-operation V2 and
   #574 image/context/recovery; assemble #575 same-SHA release qualification.

This audit changes tracking and evidence only. It does not fix the three
reproduced defects, close any of #567–#575, publish a release, or install into
production. The delivered portable remains a test candidate with these newly
identified limitations.
