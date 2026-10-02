# Native V2 source-prevention diagnosis without vendor requests

Two empty-home, no-account actual Codex CLI 0.159.3 loopback diagnostics took
0.860s and 0.922s. Their synthetic servers never contacted a vendor. All private
configuration/caller data was removed and removal checked. These are protocol
observations, not model generation or mixed-provider qualification.

The [actual sanitized native declaration](../../tests/fixtures/collaboration/codex-cli-0.159.3-linux-native-v2.json)
contains six Collaboration methods. Its schema is accepted by the production
V2 classifier: optional `agent_type` omission and normalized required ordering
are already supported. Raw structural comparisons differ for spawn/list/wait,
but this does not make the semantic classifier reject the declaration.

The public `make_messages_portable` and `official_passthrough_request_body`
seams rename its namespace to `codexhub_plaintext_collaboration`, strip message
encryption annotations and record all six permitted handler names. Public
body/SSE inverse seams return native `collaboration` Calls with explicit
`encrypted_function_args:[]`, preserving separate Call and Item identity.
Six public regression tests pin these actual-schema observations.

A synthetic alias spawn response, decoded through the production public
body/SSE inverse, was consumed by actual Codex. Its child made a loopback
request with only an `input_text` task part, without `encrypted_content`.
Exact equality between the controlled literal and that part was false; the
harness did not retain its raw text after cleanup. CLI task wrapping is a
possible explanation, but literal preservation is not established by this
artifact. No ciphertext was interpreted or repaired.

These observations narrow the live failure: the actual native schema is
supported and the public source-prevention/inverse seams can produce a child
without encrypted parts. They do not establish that the live parent reached
those seams or received the inverse metadata. Inspect the real Official
upstream declaration and decoded response/context propagation next. The
[e13e2fd0 Official → Cursor hard-gate failure](subscription-codemode-v2-e13e2fd0-2026-10-01.md)
remains failed; these synthetic observations cannot turn it into success.

[Sanitized diagnostic](subscription-native-v2-source-prevention-2026-10-01.json)
records exact native schemas, accepted classification, public-seam outcomes,
module hashes, child part ordering/address hashes and cleanup results.

The subsequent actual production Gateway → fake loopback Official diagnostic
isolated the missing policy branch. Actual CLI headers identify `codex_exec`,
while desktop passthrough requires `client_id=codex-app`; the unchanged CLI
identity correctly selects `GATEWAY_COMPATIBILITY`. That request path exposed
native Collaboration but never performed source prevention, and the ordinary
Official SSE path returned before inverse decoding. The unmodified base
snapshot `daf4d7b6…` consequently forwarded six native tools upstream and
returned the synthetic ALIAS spawn with no plaintext metadata.

The narrow fix adds the same declaration alias and request context to the
Official compatibility request path and applies its existing inverse before
the Official SSE early return. It leaves the CLI identity, other Official
conversion policies and stream footer unchanged. Six public regression tests
cover the current schema, actual CLI route plan through `execute_exchange`,
Call/Item IDs and real-result history, inverse body/SSE, and non-V2 pass-through.
The older V2 declaration expectation is updated while its native history
identity assertions remain intact; V1 and opaque history tests also pass.

After the fix, another empty-account actual CLI production loopback run took
3.754s. Both fake Official requests contained the six ALIAS handlers. Its
synthetic spawn crossed the real relay as native `collaboration`, with
`encrypted_function_args:[]` on added/done/completed events. Actual Cursor
child `agent_message` contained only `input_text`, with the original controlled
literal present, and received expected HTTP401 because its private HOME had no
Cursor account. The parent continuation also completed (exit 0). The source
snapshot `d5736e99…` includes the uncommitted two-module fix, explicitly marked
dirty relative to base `b89f7c87`; it is not claimed as an exact committed SHA.
All private trees were removed. Vendor request count remained zero. The live
Official → Cursor hard gate still needs a bounded rerun on an integrated SHA.

Targeted verification: 166 tests and 31 subtests passed across the public
regressions, Code Mode, exchange, native history, route planning and
Collaboration boundary suites. Root owns the final core gate and live rerun.
