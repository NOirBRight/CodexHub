# Console Go compaction rejects interleaved tool history

## Finding

The failure is a compatibility bug at the Gateway's structured history rewrite
boundary, `tool_surface_adapter.rewrite_structured_tool_input_items`.
The local tool result was present. During a compact request, declarations are
removed and ordinary functions become transcript messages, but collaboration
and node calls previously remained structured. This split can insert a message
between a parallel call and its result. Console Go's Responses endpoint rejects
that sequence with `No tool output found for tool call ...`, even when the
matching result appears later.

The reported thread was `01a096a6-c40c-7132-8c7e-23f772107167`. Its retained
history recorded this sequence on 2026-09-13 (Asia/Shanghai):

| Time | Item |
| --- | --- |
| 15:58:21 | `functions.exec_command` and `multi_agent_v1.wait_agent` calls |
| 15:58:21 | command result |
| 16:03:21 | wait result: `{"status":{},"timed_out":true}` |
| 16:27:53 | compact request failed with HTTP 400 |

The failing call ID was `call_01_ET_QAfcuKPpTCrSKyEOordp6790`.
Gateway request `86768682fa2d` selected `opencode_go`,
`deepseek-v4.1-flash`, and the Responses protocol. Its converted shape had
660 items including 10 calls and 10 results. Counts alone could not detect
this ordering incompatibility.

## Differential evidence

Only synthetic tool data was sent during live reproduction, using the existing
local Gateway and configured provider. No credentials or conversation bodies
were captured in diagnostic artifacts.

- Adjacent wait call/result: HTTP 200.
- The same pair with one transcript message inserted: HTTP 400,
  `No tool output found for tool call call_compact_wait.`
- Parallel command/wait history through the deployed adapter: the same 400.
- The identical parallel history adapted by the fixed source and submitted
  through the same Gateway/provider: HTTP 200, completed, `OK`.

This comparison identifies the incompatible ordering without needing to infer
Console Go's internal implementation. It does not claim access to its code.

## Fix

For compact requests in the structured V1 history adapter, render all function
calls and results as transcript messages. Reuse the existing renderer and keep
the original order, arguments, IDs, and outputs. Existing media adaptation
runs first. Do not synthesize missing results, reorder historical events, or
change main-generation and native V2 behavior.

The isolated worker branch is `codex/fix-compact-tool-history`, based on
`89ae3ef`. The patch was also applied to the active checkout, using its ongoing
`tool_history.internal_message` extraction instead of the baseline wrapper;
pre-existing edits were preserved.

## Verification

- `tests/test_compact_parallel_tool_history.py`: the two compact cases failed
  before the patch and passed afterward. A third test protects main-generation
  structured history.
- Actual retained history replay: 910 recorded items became 659 messages;
  the failing call and its real output remained present. This offline replay
  does not include the additional client compaction prompt and is not a claim
  of byte-for-byte reproduction of the unavailable original HTTP body.
- Integrated focused checks: **43 passed** across compaction, tool history,
  tool surface, and multimodal history tests.
- Isolated worker Python core suite: **2582 passed, 176 skipped, 16 failed**.
  All 16 failures were rerun against the unmodified baseline and failed there
  too (matrix artifact drift, cross-module private imports, and existing
  `prompt_cache_key`/`strict` expectations). They are not introduced by this fix.
- Integrated checkout Python core suite: **2596 passed, 176 skipped, 16 failed**;
  its failed test IDs exactly match the unmodified worker baseline.
- Report-only quality scan completed with zero parse errors. Existing findings
  remain report-only. `git diff --check` passed.

## Runtime state

The installed Gateway is running from the mounted 0.2.7 AppImage. Source edits
are not loaded by that process. This work has not replaced the AppImage,
restarted the Gateway, or modified the user's session history. The installed
runtime needs a rebuilt package and a Gateway restart to pick up the fix.

## Review-cycle follow-ups

The user requested a review/fix loop with no remaining findings before commit
and merge. The first independent Standards and Spec reviews of `6d6d3ef`
reported no findings. The existing Python failures were then addressed as
merge prerequisites, rather than suppressed or excluded:

- The earlier Console Go fix removed `strict`, `prompt_cache_key`, and output
  controls on every third-party route. Scope those restrictions to the
  `opencode_go` Responses route, including transparent calls without a model
  alias. Preserve other providers' existing contracts and Console Go Chat
  controls. Existing identity/cache assertions remain unchanged; add explicit
  provider/surface/alias regression coverage.
- Expose the route-specific sanitizer on the owning request module and read it
  through a module attribute, removing cross-module private imports (ADR-0007).
- The #66 matrix differed only in the `protocol_translation.py` SHA-256. The
  source changed in accepted commit `1f3e7ae` to preserve official Chat
  `web_search.external_web_access`. Refresh that evidence binding after the
  existing behavior tests pass. No matrix rows, invariants, or expectations
  change.

Focused verification after these follow-ups: 189 original tests passed with
2 live tests skipped; the new provider-scope and compaction checks plus existing
matrix/third-party checks passed (67 passed, 2 skipped).
