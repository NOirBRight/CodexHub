# Actual Codex subscription Code Mode/V2 qualification

Date: 2026-10-01. Tracker: #609. Linux only; no Windows qualification claimed.
Candidate: `e13e2fd0df3562f44da4d98dd88554871388d505`.
Actual client: `codex-cli 0.159.3`.
Both runs froze identical production runtime bytes:
`1f5b8efda8b6e230e5a1694cf22b365974c22e540761d9e6fd0d903a5247f227`.
Source runtime was clean. Raw accounts, CLI captures, Gateway logs, and child
rollouts were private and removed; cleanup was checked after leaving their
temporary directories. The observer forwarded original HTTP/SSE bytes without
codec injection, task repair, ciphertext inference or output normalization.

Bounds were 180 seconds per case. The initial Code Mode run used a 240-second
total bound; the remaining three cases used 900 seconds. Selected identities
were exactly `cursor-subscription/gpt-5.6-luna-high` and source-catalog-listed
`gpt-6-astra`. Cursor requests actually omitted independent reasoning effort
and sent `parallel_tool_calls:true`; Official requests used `high`.

| Case | Result | Duration | Observed evidence |
| --- | --- | --- | --- |
| Actual Cursor Code Mode | Pass | 19.672s | Native custom exec, distinct Call/Item identity, actual caller fixture read, exact final |
| Cursor completed history after Gateway restart | Pass | 29.354s | Different Gateway and fresh caller processes, original completed Call result replay, exact reversed fixture, no new tools |
| Cursor parent → Official child | Pass after evidence-oracle correction | 46.689s | Single actual child, two custom exec fixture reads, matching same-child target hashes, followup/wait, exact two-line final and fresh-caller restart continuation |
| Official parent → Cursor child | Fail | 54.242s | Native collaboration Calls; Cursor child requests returned HTTP 400 with mixed plaintext/encrypted task parts; no child fixture execution and no correct parent final |

The failed direction did not bypass prevention through nested Code Mode
JavaScript. Its parent emitted native `function_call` items in the
`collaboration` namespace. Spawn and both followups target the same actual
recipient hash. Child directed-message parts were ordered `input_text`, then
`encrypted_content`. The executed harness did not capture the
`encrypted_function_args` field; its presence/absence is unknown after raw
cleanup. The new observer records only absent/empty/nonempty/malformed marker
shape for future diagnosis, never encrypted bytes.

The first reverse-direction oracle counted command text inside a native spawn
assignment as parent execution, and required child-private internal tool Calls
to appear inside the fresh parent's history. Both were attribution errors. The
corrected public oracle accepts only actual exec/exec_command Calls as reads,
and requires the parent's own completed collaboration Calls/results to replay.
Child rollouts independently prove both actual fixture executions. Re-scoring
used only retained sanitized evidence and matching fixture hashes; no vendor
request was repeated. Original checks remain in the JSON, alongside corrected
checks. Exact finals and their zero-width counts/hashes were not changed.

[Code Mode evidence](subscription-codemode-e13e2fd0-2026-10-01.json) and
[V2/restart evidence](subscription-codemode-v2-e13e2fd0-2026-10-01.json) retain
structural declarations, Call/Item/target hashes, exact controlled successful
outputs, failed output hashes and every request status. The earlier real
continuation failure remains in
[the initial diagnostic](subscription-codemode-first-diagnostic-2026-10-01.json).
The failed forward direction keeps Cursor's complete V2 release gate open.
