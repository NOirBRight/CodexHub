# Claude Code same-conversation model switching

## Scope and reproduction

Continue the model-switch failure reported on 2026-09-26: native Opus succeeds,
but selecting a Codex model returns `unsupported_for_chat_conversion: safeguards`.
The same conversion boundary serves third-party Chat Completions and Responses
models; this is not a Codex-specific exception. Work is classified **strict**.

The observed-shape fixture in `tests/test_anthropic_messages_ir.py` reproduced
`Adapted` on native Anthropic and `NotForwardable ('safeguards',)` on Responses
before the change. It now converts on all three protocols. Fixture values are
synthetic; no operator conversation, classifier context or credentials are saved.

## Primary-source evidence

Inspected the installed Claude Code **2.1.283** executable, SHA256
`1859583ce32920595c61ef868bee52e1b1594f7486db209935e01f1e5e804ae2`.
These observations come from its embedded JavaScript, not a claim that the
server-side classifier was independently verified:

- Request assembly conditionally emits
  `safeguards: [{type: "dangerous_tool_use", classifier_context: context}]`
  when the server classifier is enabled and Auto Mode context is available.
  The context builder returns an object, including a version and contextual
  information. Its contents must not be logged or forwarded to another provider.
- The matching beta is `dangerous-tool-use-2026-09-03`.
- The response parser reads `safeguard_results`, with states including
  `available`, `unsupported`, and `unavailable`.
- Client tool-permission handling contains the explicit fallback message:
  "a completed response carried no classification result (no safeguard_results);
  assuming something on the path to the API dropped it, so auto mode classifies
  locally for the rest of this session".
- A separate third-party-deployment branch stops sending context and classifies
  locally after the platform supplies no result. Recognized HTTP 400 rejection
  has different fail-closed behavior; it must not be confused with that fallback.

The older v2.1.278 wire observation established the same envelope but explicitly
left enforcement semantics unknown. The old code's citation to ADR-0013 was
incorrect: that ADR concerns default subagents. ADR-0014 governs declared
best-effort conversion.

## Adaptation policy

Native Anthropic retains the original body and semantic headers. Converted
routes accept only a list of the known classifier-context envelopes (or an empty
list). They omit that context under
`claude_server_classifier_context_omitted_for_non_anthropic`, and the transport
removes its matching beta. Diagnostics explicitly state that no equivalent
Anthropic server classifier ran. The Gateway never fabricates a classifier
result or a permission decision; Claude Code retains its permission handling.
Unknown types, extra envelope controls, and malformed context fail explicitly.

Tool results followed by user text/images become ordered Chat tool messages
followed by user content; call identities remain intact. Invalid ordering with
text before a tool result remains rejected.

Plain-text reasoning deltas on converted responses no longer abort an otherwise
valid answer/tool stream. Like the existing JSON response adaptation, unsigned
reasoning is omitted with a named diagnostic; it is not presented as an answer
or fabricated Anthropic-signed thinking. Models that require their exact thinking
history on the next tool roundtrip still require separate qualification. This
change does not claim every provider-specific capability is portable.

Claude Code also sends a non-streaming model-check request before changing to a
custom model. If an upstream supplies SSE for that request, Gateway reconstructs
JSON first. The old relay then parsed those JSON bytes again using the original
SSE Content-Type (and the wrong envelope for buffered Chat). The relay now passes
the reconstructed JSON and its actual protocol to the Messages adapter. Both
Responses and Chat versions were reproduced as HTTP 400 before the fix.

## Verification

`tests/test_claude_model_switch_http.py` runs the production HTTP Gateway against
loopback upstreams. One growing history crosses native Anthropic, official Codex
Responses, third-party Chat, third-party Responses, then native Anthropic again.
It checks the actual upstream model, preserved text/tool results, matching call
IDs, and credential separation. It also exercises reasoning SSE through the
production relay. This is deterministic HTTP evidence, not live-provider proof.

The IR/exchange tests cover declared adaptations, malformed/unknown classifier
envelopes, native byte preservation, images, and reasoning streams. Live-provider
and packaged-release qualification must be recorded separately; prior package
results do not prove this source delta.

### Real CLI, loopback upstreams

Claude Code 2.1.283 passed four turns in one running process using its stream-JSON
`set_model` control: native Opus → Gateway Codex → Gateway third-party Chat →
native Opus. It retained the same session ID and the first-turn marker in every
later upstream request. The two custom-model selections each made one successful
validation request followed by one generation; the native turns each made one
generation. All six requests went through the production Gateway to synthetic
loopback upstreams. This verifies client switching and history, not actual model
availability or quality.

The run used a temporary HOME/config/work directory, synthetic credentials, no
enabled tools, 128 requested output tokens, and 40-second per-message waits.
External proxy traffic was directed to an unavailable loopback endpoint. The
operator's Claude settings, credentials, sessions and installed Gateway were not
changed. The first attempt exposed the buffered-SSE defect described above; after
the fix both model-check controls and all four generation results succeeded.
