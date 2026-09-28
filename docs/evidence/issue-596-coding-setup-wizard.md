# #596 ChatGPT Coding Setup wizard — verification note

Date: 2026-09-28
SHA: record at merge time from `feat/chatgpt-coding-wizard`.

## Deterministic seams

- `tests/test_chatgpt_web_settings.py` and `tests/test_chatgpt_web_tool_probe.py` on the implementation branch.
- Combined local run after review fixes: **47 passed** (settings + tool probe).
- Probe success is independent of `readiness.tools_ready` / doctor flags.
- Generation invalidation covers account/config/runtime process changes.
- Review fixes on top of `a6d6b79b...HEAD`:
  - Call identity only (`call_id`); never copy Item identity `id` into `call_id`.
  - `response.incomplete` never counts as success; correlation token/`probe_id` required.
  - Prior `passed` becomes `stale` when tunnel/connector/runtime preconditions fail.
  - Wizard shows actionable probe reason codes; extension help only when account is not connected; advanced options only on return visits; Provider entry opens “coding setup”.

## Live account tool roundtrip

A live ChatGPT account + Tunnel + connector roundtrip was **not** executed in this implementation turn. The default exchange posts to the managed runtime `/v1/responses` path with the fixed `codexhub_setup_probe` tool; operators can run it from Runtime Settings step 5 when a full coding configuration is active. Record any live pass under a follow-up evidence folder without secrets.

## Out of scope (remain open)

- #567 real-client matrix
- #592 remaining real daily-browser acceptance on Windows / extension install
