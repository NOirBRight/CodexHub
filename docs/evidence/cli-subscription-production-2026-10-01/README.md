# Production ordinary qualification — 2026-10-01

Candidate runtime: `47e45f0b0aedd37e4e1c0abed1ed66f577a1457a`.
Frozen runtime SHA-256: `29960b250ffb78cf5ae3febeac7531ebb21f5e8fe2c015c4b74e8ff4177b23c0`.
The Gateway was the candidate's actual `src-python/codex_proxy.py`; the
qualification harness froze its runtime files before launch/restart. No
prototype, Magpie binary, fallback Provider or model mapping was used.

The selected Cursor ID was exactly `gpt-5.6-luna-high`, routed as
`cursor-subscription/gpt-5.6-luna-high`. The installed official Cursor path
and the same-day discovery identify `2026.09.28-64d2043`. Claude requested the
historical explicit ID `claude-sonnet-4-5`, without substituting a model or an
API key. A local `claude --version` observation after these probes, at
`2026-10-01T00:57:59Z`, returned `2.1.286`; the harness did **not** record the
per-request Claude executable version, so this is an after-run observation,
not proof of the exact version for every request. The earlier foundation
observation `2.1.285` is historical.

Every run had a private `CODEX_HOME`, random loopback port and Gateway key,
fresh HTTP callers, no automatic generation retry, and private logs/config.
Source account HOME/XDG paths remained the official user's existing account;
normal configuration was not written. Private runtime, fixtures, credential
copies and logs were removed. The retained JSON contains status, latency,
content/Call-ID hashes, supplied-usage presence and lifecycle counts, not raw
prompts, credential values or tool results. Output was compared exactly,
including any zero-width characters; no output normalization was applied.

## Ordinary observations

| Behavior | Observation | Bound |
| --- | --- | --- |
| Cursor Chat text | Exact randomized response, 4.56 s | 60 s/request |
| Cursor Chat stream | Exact text and actual terminal event, 4.44 s | 60 s/request |
| Cursor Chat caller tool | One real `read_fixture` request with original Call identity, 5.33 s | 60 s/request |
| Cursor Chat real tool result | HTTP caller read a disposable random fixture exactly once; exact model response, 7.37 s | 60 s/request |
| Cursor Chat completed history after actual Gateway restart | Fresh HTTP caller supplied full Call/result/history; exact value recovered, 4.42 s | 60 s/request |
| Cursor Responses text / stream / caller tool | Passed, respectively 4.23 / 5.09 / 9.09 s | 60 s/request |
| Cursor Responses native tool Item → real result | HTTP 400 after 0.11 s; failed | 60 s/request |
| Cursor Messages text / stream / caller tool | Required `max_tokens=1024` retained; each HTTP 400 after 0.11 s; failed | 60 s/request |
| Claude production Gateway text | HTTP 403 `not-eligible` after 1.65 s; failed, zero successful generation | 30 s/request, 60 s/run |

The Responses failure also reproduces without inference at the public
`prepare_exchange` interface: `NonForwardable.code` is
`unsupported_protocol_semantics`. Native function Call fields are
`arguments, call_id, id, name, status, type`, with `status=completed`; its Item
ID is distinct from Call identity. The real result uses `call_id, output,
type`. The candidate's Chat-to-Responses output writes `status=completed`,
while its Responses input allowlist rejects `status`. No field was silently
removed by the harness. A later fix requires its own delta verification.

The candidate Cursor backend explicitly rejects generation parameters that
AgentService cannot represent, including `max_tokens`. Messages' required
parameter was preserved rather than deleted to produce a misleading pass.
The initial Gateway error wrapper was recorded as `upstream-error` with HTTP
400; it does not expose the original parameter enum directly. This is a
production compatibility failure, not a successful Messages qualification.
The later Claude explicit-parameter-rejection delta likewise blocks an
unsupported Messages `max_tokens` request even after account permission is
restored, until a genuine native mapping or declared policy is implemented.

## Cancellation and evidence audit

The original [Chat report](cursor-chat.json) reports its after-first-text
cancellation as passed because the process's total socket count returned to
baseline. **That cancellation is inconclusive:** the measurement did not
prove a vendor socket was still active when the caller disconnected. Its
original `ordinary_qualified=true` flag is not the reviewed acceptance
verdict. The stricter after-first-text attempt in
[the protocol report](cursor-protocols.json) remains failed; a naturally
completed reply is not proof of cancellation.

The separate [upstream-wait cancellation report](cursor-cancel.json) is the
valid observation. Before downstream headers, the caller observed a real
vendor TLS socket: established vendor TLS count `0 → 1 → 0`, total Gateway
sockets `1 → 1`. The fresh caller then shut down its actual HTTP socket; its
wait ended with failure, the vendor socket closed, and cleanup completed in
0.44 s within a 30 s/request, 45 s/run bound. This proves this interrupted-wait
lifecycle scenario. It does not claim cessation of vendor billing, cancellation
acknowledgement, or an after-first-text scenario.

## Acceptance still open

These are bounded ordinary observations, not release acceptance. Responses
real-result continuation and Messages are failed. Actual Code Mode, both
mixed-provider V2 directions and their full completed-history restart gates
remain separate. Claude live success remains blocked on the current account's
inference permission. Windows same-SHA qualification has not run: the current
`ssh yoga` attempt reported `No route to host`. No Linux observation substitutes
for Windows evidence.

Harness: [qualify_cli_subscriptions.py](../../../scripts/qualify_cli_subscriptions.py).
Public harness tests: 14 passed; direct-entry Python 3.11 cases skipped because
an incompatible interpreter was unavailable on this Linux environment.
Worker backend rejection delta: 44 Claude public exchange tests passed.
No full suite was run by this worker; the Orchestrator owns candidate review
and the affected full local matrix.

## Responses continuation delta — c0bc2ba

A bounded [new Responses-only run](cursor-responses-c0bc2ba.json) froze candidate
`c0bc2ba5323da831a96699980b129dc61c7f6719` after the completed function-Call
status fix. Runtime SHA-256 was
`1f5b8efda8b6e230e5a1694cf22b365974c22e540761d9e6fd0d903a5247f227`;
Cursor's resolved official package version was `2026.09.28-64d2043` before and
after the run. Text, stream and the real caller-tool request passed in
4.45, 7.05 and 5.41 seconds. Returning the complete native output and real
result still failed after 0.10 seconds with HTTP 400,
`unsupported_protocol_semantics`. No tool-result success or restart was
claimed. No Chat, Messages, Claude or cancellation case was repeated.
Bounds were 60 seconds/request and 360 seconds/run; total elapsed time was
17.68 seconds and all private artifacts were removed.

The public converter reproduces this second layer without inference:
`chat_completion_to_response_body` outputs both a completed message Item
(`content,id,role,status,type`) and a completed function Call Item
(`arguments,call_id,id,name,status,type`). Empty assistant `content=""` also
produces that message. The candidate accepts the completed function-Call
status, but its Responses message-input allowlist still excludes `status`.
`prepare_exchange` therefore rejects its own message output with
`NonForwardable.code=unsupported_protocol_semantics`, explaining the remaining
real roundtrip failure. The harness preserved the full Items and did not
remove completion fields or replace Call identity. Both historical failures
remain retained. A later completed-message fix needs a new tool-result and
restart delta, rather than rerunning already accepted text/stream/cancellation.

The harness now supports `--tool-continuation-only`, which runs exactly a new
actual caller tool request, its real result and completed-history continuation
after actual Gateway restart. It skips text, stream and cancellation; public
fixture tests assert those cases are not invoked. Qualification remains scoped
to the selected cases and does not imply advanced or dual-platform acceptance.

## Responses complete-history delta — 2a999072

After completed message-Item handling was fixed, a new
[tool-continuation-only run](cursor-responses-2a999072.json) froze candidate
`2a99907233b9fceda4ff2f057b4ed749c9c29072`. Runtime SHA-256 was
`fce99448b6cc7c0c31152da0a71a736a0be01206b2a0a1ba4f5be8e83452e5b0`;
installed Cursor version was `2026.09.28-64d2043` before and after the run.
Only three fresh-caller operations ran: one actual tool request, the real
result, and full completed-history continuation after actual Gateway restart.
No text, stream, cancellation, Claude or Messages probe was repeated. Each
request was bounded to 60 seconds, the run to 240 seconds; actual total was
9.96 seconds. The record was retained by `2026-10-01T01:17:37Z`.

The real caller tool passed in 5.22 seconds with an original Call-ID SHA-256 of
`6017aabfe75bafba5ef5408f420570f6f7fca2d701ff5927408a2eec5b76a452`.
Returning the full native output and actual caller result then passed in
3.73 seconds: the exact fixture response's SHA-256 was
`a67104ac187f64d01c535ec7bc8033c71726bec38fc1a01dbf177cd43b974ba7`.
The Gateway was actually restarted; the fresh HTTP caller's complete-history
continuation nevertheless failed with HTTP 400 after 0.12 seconds. The
current bounded wrapper classified that error as `upstream-error`, so the
specific restart rejection is not yet confirmed. No restart success, ordinary
qualification or product acceptance was claimed. Private artifacts were removed.

One concrete output defect remains: the nonstream Chat-to-Responses converter
uses the same message Item ID `msg_0` across separate responses, so the empty
tool-turn message and later completed answer can have duplicate Item IDs in
full history. This is a **diagnostic hypothesis** for the real restart failure,
not its confirmed classification: an isolated public
`build_tool_compatibility_plan([], selected_protocol="chat_tools").encode_history`
call accepted the reproduced two-output history. The Gateway's full request
boundary and history without current tool declarations still require diagnosis.
The harness retained both native completed outputs and original Call identity;
it did not remove empty messages, rename Items, supply fake results, or silently
repeat tool execution to get a pass. The repeated-ID producer and the actual
restart rejection require a later fix and a bounded new three-operation delta.
