# Reviewed Subscription Provider Linux E2E

Candidate `29ee769689816511fbd7b010ee6339dddf61bc8f`, after the
[full-PR review/repair loop](../subscription-review-loop-2026-10-01.md).
All probe snapshots have the same production runtime SHA-256:
`d0f6b9eecea3920755fb628b9060a710b378e380efb2220adac5faecda9d4083`.
Both public harnesses rejected uncommitted runtime changes before admission.
No production source changed during the probes. Subsequent evidence-only
commits do not relabel these results as tests of a different commit.

Linux used Python 3.14.7 through the repository launcher. Actual caller Codex
was `0.159.3`; Cursor's installed official version was
`2026.09.28-64d2043`, observed before/after ordinary qualification. The advanced
harness records the actual Codex version; the parallel ordinary record supplies
the installed Cursor observation, rather than claiming a per-request CLI
process observation for the direct AgentService transport. Claude's official
version was `2.1.286`, observed before/after its bounded admission attempt.
The exact Cursor selection stayed `cursor-subscription/gpt-5.6-luna-high`;
Official used `gpt-6-astra`. No model substitution was used.

## Actual Codex Code Mode and Collaboration V2

[Complete sanitized report](codemode-v2.json), public production harness
`scripts/qualify_subscription_codemode.py`. Bound: 180 seconds/case,
720 seconds/entire run; four distinct cases, no automatic retry.

| Case | Result | Seconds | Observed HTTP requests |
| --- | --- | ---: | ---: |
| Actual custom exec reads an unpredictable fixture | Passed | 21.114 | 2 |
| Official parent → Cursor child | Passed | 78.584 | 15 |
| Cursor parent → Official child | Passed | 80.697 | 11 |
| Completed Code Mode history after Gateway restart | Passed | 34.251 | 3 |

All 31 observed requests returned HTTP 200. Success required actual custom
exec effects paired with original Call identity and exact fixture output,
including zero-width characters. Each V2 direction required exact parent/child
rollout models, one child, two actual child reads, same-child followup/wait,
no parent fixture execution, completed-call replay after an actual Gateway
restart and a fresh caller with exact final output. A passive observer forwarded
original bytes; no substitute codec, task repair or Gateway executor was added.
All private case trees and the frozen candidate tree were removed.

The actual frames also delimit the historical recovery gap. In
`official-to-cursor.requests[1]`, the child receives an `agent_message` with
empty `history_calls` and `tool_outputs`; the matching native spawn Call/result
is in the parent's request `[2]`. In the reverse direction, child request `[2]`
also has no assignment source pair. Later child requests preserve their own
Calls/results, not the parent's spawn/followup provenance. A request-local
collector wired only to synthetic source pairs would therefore not solve the
observed production input gap. A trusted caller association protocol needs
actor/child-thread/root scope and source execution evidence before a persistent
assignment journal can safely be designed. Current request headers and V1
model/effort signatures do not establish that association. Neither a later
same-address message nor "Continue" proves that an old opaque task is replaced.

## Ordinary protocols and cancellation

[Ordinary report](cursor-ordinary.json), public production harness
`scripts/qualify_cli_subscriptions.py`. Bound: 60 seconds/exchange,
600 seconds/run; total 62.36 seconds. Chat and Responses each passed exact
text, actual stream terminal, caller-tool request, caller-executed fixture
result and completed-history continuation after Gateway restart. Ten protocol
cases passed. Messages text, stream and caller-tool admission each failed
HTTP 400 while retaining its mandatory `max_tokens=1024`; no budget support
or Messages qualification is claimed. The overall ordinary flag remains false.

The separate upstream-wait cancellation case passed in 0.25 seconds:
the caller closed its real socket before downstream headers, the interrupted
wait failed visibly, and vendor TLS count changed `0 → 1 → 0`.
This does not claim billing cessation.

[After-first-text observation](cursor-after-first-text.json) used one fresh
production Gateway and actual Chat SSE caller, with a 90-second outer bound,
60-second receive timeout and three-second cleanup observation. It inspected
the first nonempty SSE content delta, measured Gateway-established vendor TLS
sockets immediately before disconnect, then closed the caller socket and
checked cleanup. HTTP 200 and first text were observed, but TLS count was
already zero at disconnect (`0 → 0 → 0`). This 48.531-second result is
**inconclusive cancellation evidence**, not a pass; no retry or token/billing
claim was made. All ordinary and cancellation private artifacts were removed.

## Claude and platform blockers

[Claude admission](claude-admission.json) used the actual restricted backend
with the explicit historical selection `claude-sonnet-4-5` and no fallback:
HTTP 403 `not-eligible` in 2.62 seconds. Bound: 30 seconds/exchange,
60 seconds/run; total 3.23 seconds. It stopped after that single failed
generation admission. Private artifacts were removed. This confirms the
current account blocker, not Claude inference qualification.

Windows host `yoga` remained unreachable during this campaign turn:
`192.168.50.140:22`, `No route to host`, five-second SSH connection bound.
No Windows E2E or same-SHA dual-platform release pass is claimed.

## Native engineering E2E

The same source's frontend build and 175 UI contract tests passed; the
custom-protocol native build passed with Rust 1.98.1. The canonical
`scripts/e2e_linux_window_input.py` ran under the documented 90-second
watchdog with dbus and Xvfb. In the actual 820×620 native window, all 13
physical clicks remained inside `[(0,0,820,620)]`; drawer open/close/reopen/
reclose each showed a `0.573` rendered-state change. Exit status: zero.

Initial Xvfb startup failed before the app launched because xkbcomp's
hard-coded `/tmp/server-0.xkm` hit the existing `/tmp` quota. A dedicated
bubblewrap mount namespace bound a private workdisk directory at `/tmp` and
retained device access with `--dev-bind /dev /dev`; the unchanged canonical
test then passed. Original `/tmp` and desktop configuration were untouched.
Test HOME, Xvfb credentials and owned processes were removed.

These results qualify the listed Linux behaviors. The two required Spec
implementation gaps (Messages budgets and authoritative historical assignment
recovery), Claude permission, after-first-text cancellation evidence and Windows
qualification remain open. PR #614 remains a draft; no release is authorized
by this record.
