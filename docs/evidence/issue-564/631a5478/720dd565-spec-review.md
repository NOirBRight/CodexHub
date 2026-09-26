Spec review — `631a5478...720dd565` (commit `720dd565`)

Finding:

- **Medium — The snapshot artifact does not establish its origin or the separate refresh outcome.** Issue #564 requires that “the canonical configured identities are recorded rather than inferred from old issues” and that tests “use temporary home/config/history/runtime and ports … and avoid the installed Gateway or active sessions.” The new runner artifact records only `mode` and the catalog SHA-256. When `-OfficialCatalog` is supplied, the runner skips `Invoke-CandidateOfficialBootstrap`; neither the snapshot’s source nor a separate `refresh-models` result appears in that run’s evidence. A passing CLI summary therefore proves checks against the copied catalog, but cannot substantiate its qualification origin or whether refresh was blocked/passed. Keep the refresh result explicitly separate and attach verifiable snapshot provenance before treating this as complete gate evidence. The docs added by this commit also require: “Keep the snapshot's origin and the separate refresh result in the evidence.”

No other missing or altered #564 CLI requirements found in this diff: the four-client × two-model contract, production materialization, and live route/tool/streaming/correlation checks remain in the runner path.


## Addendum — 2026-09-27 evidence-index check

The evidence-only finding is resolved by `docs/evidence/issue-564/631a5478/README.md` and its linked artifacts. The Windows CLI summary binds the 8/8 pass to product SHA `631a5478b474d46ea78690767971642de80deda2` and harness `b7f7a0fb`; its `official-catalog-input.json` hash matches `catalog-provenance.json` (`c46ce534c0f316943d01c26c5deebc3d1651c943b29c2514df1853b5130c2ce0)). Provenance names the staged input source. The linked refresh diagnostic is bound to the same candidate SHA and explicitly records a blocked `refresh-models` result, unchanged source auth, no Official state write, and no Desktop stop/restart. The README makes no refresh-pass claim and explicitly says there was no waiver, release, merge, or installation.

No runner artifact schema field is needed. Attribution nit: `catalog-provenance.json`'s `same_input_used_by` list omits `631a5478 Windows CLI`, despite the Windows run artifact proving the identical hash. Add that entry if the list is intended to enumerate every use.
