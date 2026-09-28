# #596 ChatGPT Coding Setup wizard — verification note

Date: 2026-09-28
Branch: `feat/chatgpt-coding-wizard`
Base for review: `a6d6b79b` (feat/chatgpt-daily-browser-login)
HEAD: `18e517bf` (includes final review delta).

## Deterministic seams

- `tests/test_chatgpt_web_settings.py` + `tests/test_chatgpt_web_tool_probe.py`
- Combined local run after final review fixes: **47 passed** (Python 3.14.7 / pytest 9.1.1 via repo launcher binding).
- Probe success is independent of `readiness.tools_ready` / doctor flags.
- Generation invalidation covers account/config/runtime process changes.
- Review fixes on top of `a6d6b79b...HEAD`:
  - Call identity only (`call_id`); never copy Item identity `id` into `call_id`.
  - `response.incomplete` never counts as success; correlation token/`probe_id` required.
  - Prior `passed` becomes `stale` when tunnel/connector/runtime preconditions fail.
  - Wizard shows actionable probe reason codes; extension help only when account is not connected; advanced options only on return visits; Provider entry opens “coding setup”.
  - Final delta: `coding_setup_complete()` is the single completion predicate in status payload; HTTP 409 reasons shared via `HTTP_CONFLICT_REASONS`; hashlib import hoisted.

## Page contract (static)

- Production `chatgpt_web_settings.html` has no prototype switcher, scenario lab, or simulate controls.
- Five studio steps, `/api/tool-probe` wiring, `focus-visible`, bundled `/codexhub.svg` and `/openai.svg` present.
- `tauri.conf.json` resources include the HTML page and both logos.

## Live account tool roundtrip

A live ChatGPT account + Tunnel + connector roundtrip was **not** executed in this implementation turn. The default exchange posts to the managed runtime `/v1/responses` path with the fixed `codexhub_setup_probe` tool; operators can run it from Runtime Settings step 5 when a full coding configuration is active. Record any live pass under a follow-up evidence folder without secrets.

## Code review

- Fixed point: `a6d6b79b` → HEAD (user-confirmed for PR #600).
- Standards axis: no hard documented-standard violations after final delta; remaining notes are judgement-only (private binding/write helpers, single-file settings page, blocking probe wait).
- Spec axis: product path complete in code; live operator probe and broader #567/#592 remain intentionally open.

## Out of scope (remain open)

- #567 real-client matrix
- #592 remaining real daily-browser acceptance on Windows / extension install
