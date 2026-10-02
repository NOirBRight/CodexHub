# Subscription Provider engineering verification

This is engineering evidence, not complete Provider qualification or release
acceptance. The implementation PR is #614; #605, #609 and #610 remain open.

## Frozen Linux candidate

`19c4ad01bb7e5ecc5070f5ad645c0a45d6b8d63c`, Linux, Python 3.14.7,
Rust 1.98.1. Python was selected through `scripts/codexhub-python.sh` with
an explicit compatible development interpreter. Private test scratch used a
workdisk `TMPDIR` because the ambient `/tmp` quota was exhausted.

- Focused public-seam checks: 258 passed, 11.06 seconds.
- Standards and Spec delta reviews through this SHA: no new finding. Known
  qualification gaps remain explicitly open.
- Python core: **failed**, 3,859 passed, 5 failed, 201 skipped, 283 subtests
  passed, 246.20 seconds. Four failures were old encrypted-task-drop success
  expectations; the fifth was the static chat conversion matrix artifact.
- Python partition completeness: passed; 4,229 total, 4,065 core and 164
  synthetic; disjoint and complete.
- Rust: **failed**, 801 passed, one failed and one ignored. The bundled-runtime
  preference test observed the explicit `CODEXHUB_PYTHON` binding used by the
  encompassing verification script, instead of its temporary bundled fixture.
- Clippy: **failed**, one Rust 1.98 `collapsible_match` in Pi cleanup.
- Frontend production build and UI contracts: passed.
- Custom-protocol native app build and physical pointer E2E: passed. The
  settings drawer opened and closed through physical X11 input; all 13 clicks
  stayed inside the 820×620 window's full input region. Native capture and
  rendered drawer transitions were both checked.
- Report-only quality scan: no parse errors; its reported findings were not
  converted into hidden merge gates.

## Resolved Rust verification deltas

`1da7a38` replaces the nested Pi cleanup boolean assignment with an equivalent
boolean accumulation. No cleanup behavior or error propagation changes.

Rust full suite with the explicit Python override variables removed from this
leg and the compatible development interpreter retained on `PATH`: **802
passed, zero failed, one ignored**, 53.05 seconds. This is a corrected test
invocation; no runtime-resolution code or assertion was changed.

Clippy `--locked --all-targets -- -D warnings`: **passed**, 8.12 seconds.

## Integrated source and final core gate

Production source `e9449930e3cc6e5c8812a2862471b1ca88c36a5d` adds the scoped
Official compatibility alias/SSE inverse and fixed safe Cursor transport stage
errors. Standards and Spec delta reviews found no actionable production
regression. Integrated focused public-seam checks: **136 passed**, 2.78 seconds.

The complete core run at this source found one remaining old issue-509 test
requiring an unchanged upstream namespace name: 3,883 passed, one failed, 201
skipped, 283 subtests passed, 234.95 seconds. Its corrected test retains the
namespace structure, complete declaration schema and all six inverse-context
handlers; it now expects the reviewed plaintext namespace alias. No production
source was changed. Its associated tests passed: **47**, 0.38 seconds. Both
review axes approved this test/evidence-only delta.

Final candidate `2f2e6e840cb1ee1143f275b3efae5e666e34a02c` complete Python core:
**3,884 passed, zero failed, 201 skipped and 283 subtests passed**, 242.59 seconds.
Partition completeness at the unchanged production source: 4,249 total tests,
4,085 core, 164 synthetic, disjoint and complete. The excluded synthetic
real-client surface did not change in this campaign; its Windows watchdog
suite was not substituted with Linux skips.

On exact clean production source `e9449930`, the bounded real Responses
tool/result/Gateway-restart probe passed all three cases in 18.16 seconds.
The bounded Official-parent → Cursor-child V2/followup/restart probe passed in
72.996 seconds, with 15 HTTP200 and only plaintext child task parts. Both
snapshots and private account trees were removed. Earlier failures remain
historical records with their original source identity; the old 502 cause is
unknown. Earlier Code Mode/opposite-direction successes retain their own
source snapshots, rather than being relabelled as same-SHA qualification.

Remaining acceptance is explicit: Messages native output-budget mapping,
authoritative opaque-assignment recovery/unrelated old opaque history, Claude
inference permission, after-first-text cancellation evidence, and same-SHA
Windows qualification. This successful local matrix is not full Provider or
dual-platform release acceptance.

## Manual Provider workspace

The real Rust web bridge and frontend ran against isolated application/config
homes with both new Providers disabled. The original account/configuration was
not changed. Official CLI account/transport labels, absent API credential
fields and absent quota claims were inspected. Claude enable was disabled
until explicit priority consent; consenting did not automatically enable it.
Saving used the shared Toast and reported the exact Codex restart requirement.
Read-only Claude discovery reported the existing account eligibility denial.

Live Provider records, including retained failures and cleanup results, are in
`cli-subscription-production-2026-10-01/` and the separate Code Mode/V2 evidence
files. Windows same-source verification remains blocked because SSH host
`yoga` was unreachable. No release or tag was created.
