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
Apply it in an upstream checkout, then rebuild the runtime bundle. The product
release pin and original installed acceptance bundle have not been modified.
Adopting this patch in a distributed build requires packaging the patched
artifact and recording its checksum; it must not masquerade as the original
release archive.

## Verification

- Public CodexHub CLI regression failed before the fix on
  `extraHighAvailable == false` and passed after it. All 21 runtime tests passed;
  the expanded seven-case restart matrix also passed, including unchanged
  login, downgrade, missing state/marker, malformed marker, unverified login,
  and non-boolean capability values.
- Python core suite: 3271 passed, 185 skipped, 280 subtests passed; two failures
  reproduced on the unmodified `cc1b36b3` baseline: `_proxy_post_request` is
  501 lines (limit 500), and the direct-entrypoint list omits
  `src-python/chatgpt_web_runtime.py`.
- Upstream real-DOM regression failed before the model-label fix and passed
  after it. Full upstream suite: 816 passed, 2 skipped, 3 failed because the
  isolated checkout initially lacked the existing Electron dependency. After
  linking the already installed launcher dependencies, the affected
  `zero-risk-adapter.test.ts` file passed in full. TypeScript checking passed.
- Both `chatgpt-web/gpt-6-pro` and `chatgpt-web/gpt-5.6-sol` completed through a
  rebuilt, isolated runtime's Responses endpoint and the real ChatGPT website,
  returning exactly `ACCEPT7K3`. Existing saved login state was reused; no new
  login was requested for these runs. See [sanitized results](model-selection-repair.json).
- Diff whitespace checks passed. Report-only quality checks completed with no
  parse errors; existing unused/dead/duplicate reports remain non-blocking.

Only the text/model-selection path was exercised live. No Gateway, client-tool,
V2 collaboration, Windows build, release, or original installation update is
claimed by these results.
