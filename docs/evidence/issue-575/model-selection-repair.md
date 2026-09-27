# ChatGPT Web model selection repair — 2026-09-27

Scope: strict, targeted repair of two blockers on the `cc1b36b3` acceptance
baseline. This is not release qualification or a replacement for the remaining
Gateway/client/V2 matrix in #575.

## Confirmed causes and changes

1. CodexHub rebuilt runtime config with Extra High and Pro disabled regardless
   of the private account's verified login record. Startup now restores the
   boolean capabilities from that record using the pinned runtime's login-marker
   contract. Missing/unverified state closes capabilities; a downgrade overrides
   the old config. Pro and Extra High still require Sol.
2. Runtime 6.1.1 required a version in the slider's accessibility announcement.
   The actual announcement was `Pro, 5 of 5.` while the visible model toggle
   separately displayed `6` and `Pro`. The attached upstream patch reads those
   explicit labels inside the owned menu. It retains the checked-family and
   slider checks, rejects ambiguous/unknown labels and conflicting versioned
   announcements, and does not equate a generic `Latest` or `Pro` with model 6.

The runtime patch applies to upstream commit
`a13cd09950969f43e3b7e25c71fa43efaf5446c5` (v6.1.1):
[chatgpt-web-model-selection.patch](chatgpt-web-model-selection.patch).
The reviewed runtime revision is `81b270efbd561835c1d4b78e30bb488944e0c790`.
The Linux test portable includes its rebuilt archive with a distinct SHA256 and
build revision. The original installed acceptance bundle remains unchanged.

## Verification

- Public CodexHub CLI regression failed before the fix on
  `extraHighAvailable == false` and passed after it. All 21 runtime tests passed;
  the expanded seven-case restart matrix also passed, including unchanged
  login, downgrade, missing state/marker, malformed marker, unverified login,
  and non-boolean capability values.
- Python core: 3279 passed, 186 skipped, 280 subtests passed; partition check passed.
  The two baseline gate failures were corrected (entrypoint preflight and function line budget).
- Upstream full suite: 819 passed, 2 skipped, 0 failed; TypeScript checking and
  relocatable runtime smoke passed. Model fixture tests: 3 passed, 30 assertions.
- Rust: 784 passed, 1 ignored; clippy and custom-protocol build passed.
  An initial run inherited a Python override that invalidated a candidate-order
  assertion; rerun without that override passed.
- Linux physical pointer E2E passed; GNOME first-launch and portable-upgrade
  icon E2E passed. Packaging tests: 15 passed, 10 platform skips.
- Both `chatgpt-web/gpt-6-pro` and `chatgpt-web/gpt-5.6-sol` completed through a
  rebuilt, isolated runtime's Responses endpoint and the real ChatGPT website,
  returning exactly `ACCEPT7K3`. Existing saved login state was reused; no new
  login was requested for these runs. See [sanitized results](model-selection-repair.json).
- Diff whitespace checks passed. Report-only quality checks completed with no
  parse errors; existing unused/dead/duplicate reports remain non-blocking.

Only the text/model-selection path was exercised live. No Gateway, client-tool,
V2 collaboration, Windows build, release, or original installation update is
claimed by these results.

## Final portable acceptance

The bundled runtime was installed through the public supervisor CLI without a
source override. Its digest matches the final archive pin. Both model requests
completed with `ACCEPT7K3` using the existing private login. The first Pro request
encountered an unavailable composer during page startup; Sol then loaded and
completed, and the Pro retry completed. This transient attempt is retained in the
sanitized evidence rather than counted as a success. Owned test processes were stopped.

See [review-loop report](review-loop.md) for provenance and final review results.
