# Subscription Provider full-PR review and repair loop

PR [#614](https://github.com/NOirBRight/CodexHub/pull/614), strict risk class.
The full first review pinned the PR merge-base `61e63514` and candidate
`df3493f9986c80566cfaf57f5d2d945085694dc6`; local `main` was stale and was
not used as the baseline. Standards and Spec ran independently. Later reviews
were limited to repair deltas: `df3493f9...5dc2bd5a`, then
`5dc2bd5a...29ee7696`. Final reviewed implementation candidate:
`29ee769689816511fbd7b010ee6339dddf61bc8f`.

## Standards

Round 1 found five actionable defects: completed Claude Call ID reuse (P1),
malformed encryption markers accepted as plaintext (P2), ignored logprobs
controls (P2), ordinary qualification falsely accepting staged/untracked
runtime changes (P2), and capture limits enforced only after subprocess
completion/unbounded rollout reads (P2).

All five were repaired. Claude rejects reused IDs before delivery. Inverse
mapping accepts absent or actual empty-list encryption markers only. Both
backends reject requested unsupported logprobs controls before admission.
Ordinary and advanced qualifiers check staged, unstaged and untracked runtime
changes before snapshot/account/process admission. Active stdout capture has
a hard write limit; rollouts have file-count and aggregate read bounds plus
growth monitoring and a Linux per-file write limit. The aggregate disk-growth
watchdog does not establish an instantaneous aggregate disk-write ceiling.

Round 2 and round 3: **zero actionable Standards findings**. The reviewers
also inspected Claude ordered buffering, alias collisions, role-model evidence
and cleanup. No baseline smell was promoted to a hard violation.

## Spec

Round 1 found five actionable defects: completed Claude Call ID reuse (P1),
reserved ALIAS collisions bypassing plaintext prevention (P2), tool-to-text
stream order reversal (P2), V2 evidence checking aggregate models without exact
parent/child rollout models (P2), and ignored logprobs controls (P2).

All five were repaired. Recognized collaboration with an unowned alias now
fails before mutation. Validated tool Calls and subsequent text retain native
block order, with a bounded deferred buffer; earlier text still streams. The
V2 oracle requires exact observed parent/child model roles. Round 2 found one
additional P2 defect: the advanced qualifier recorded a dirty runtime but
could still execute and report success. Round 3 confirmed its pre-snapshot
admission gate and final exact-runtime success condition close that path.

Final actionable defects: **zero**. Two required implementation gaps remain:

1. Messages' mandatory `max_tokens` has no qualified native whole-response
   budget mapping for both subscription transports.
2. Assignment recovery has no trusted production author/source-Call input;
   the recovery helper remains test-only and old opaque-history handling is
   incomplete. Source prevention is implemented; invented provenance, task
   reconstruction from ciphertext and fabricated continuation remain forbidden.

Claude inference entitlement, Windows host reachability and final-candidate
live results are acceptance surfaces separate from these review counts.

Standards: **0**, no remaining worst issue. Spec: **0 actionable defects + 2
required implementation gaps**; both gaps remain open, without ranking them
against Standards. This is not an all-requirements-clear or release verdict.

## Regression evidence

The new backend regressions reproduced 43 failures against the initial source
(33 positive controls passed), without vendor inference. The backend repair's
targeted checks passed 509 tests. Integrated focused checks at `5dc2bd5a`:
**515 passed, 14.71 seconds**. The advanced admission repair's public checks
at `29ee7696`: **27 passed, 0.92 seconds**, including staged, unstaged and
untracked Git fixtures that assert snapshot and case execution are unreachable.
Fixed-range diff hygiene passed. Historical evidence retains its own source
identity; subsequent runtime results are recorded separately.

Final reviewed candidate Python core: **3,949 passed, zero failed, 201 skipped,
283 subtests passed**, 240.21 seconds. Test partitions: 4,314 total,
4,150 core and 164 synthetic, disjoint and complete. The real-client synthetic
contract surface did not change. Report-only quality scan: zero parse errors;
reported dead code/duplicate names remain non-blocking under repository policy.
No Rust/frontend production source changed in these repairs; earlier Rust
802-pass/one-ignored and successful Clippy gates retain their original source
identity. The current frontend build, 175 UI-contract tests, native build and
physical pointer E2E passed. Exact same-source live results and remaining
blockers are in [the final-candidate E2E record](subscription-reviewed-29ee7696-2026-10-01/README.md).
