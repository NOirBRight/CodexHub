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
