# Claude Code external-client support: Messages gateway evidence

Date: 2026-07-12
Scope: Issue #74 research only. This note records the official Claude Code / Anthropic Messages contract relevant to a downstream Claude Code client of CodexHub. It does not add, imply, or authorize a production route, client auto-configuration, or a non-Claude upstream configuration.

## Evidence boundary and source pins

All protocol claims below are from primary Anthropic / Claude Code documentation or Anthropic's official release repository, accessed on 2026-07-12. The JSON and SSE fragments in this note are schema-only sanitized fixtures: they use invented identifiers and placeholder text and are not captures from a user session.

| Source | Pinned / accessed evidence | What it establishes |
| --- | --- | --- |
| [Claude Code gateway protocol reference](https://code.claude.com/docs/en/llm-gateway-protocol) | Accessed 2026-07-12 | Gateway endpoints, streaming requirement, forwarding rules, Claude Code headers, open-list policy, beta/body pairing, retry/error forwarding, discovery. |
| [Other LLM gateways overview](https://code.claude.com/docs/en/llm-gateway) | Accessed 2026-07-12 | Anthropic does not support routing Claude Code to non-Claude models through a gateway. |
| [Claude Code v2.1.207 release](https://github.com/anthropics/claude-code/releases/tag/v2.1.207) and [pinned changelog](https://github.com/anthropics/claude-code/blob/v2.1.207/CHANGELOG.md) | v2.1.207, tag commit d4d8fbbb333c627d8fe2c1c583a5ccc26fdb1aed, published 2026-07-11 | Public CLI release pin for a repeatable smoke plan. |
| [Using the Messages API](https://platform.claude.com/docs/en/build-with-claude/working-with-messages) | Accessed 2026-07-12 | Response shape and stateless full-history behavior. |
| [Streaming messages](https://platform.claude.com/docs/en/build-with-claude/streaming) and [API versions](https://platform.claude.com/docs/en/api/versioning) | Accessed 2026-07-12 | Named SSE event flow, deltas, errors, unknown events, and version guarantees. |
| [Handle tool calls](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls) | Accessed 2026-07-12 | Client-tool use/result lifecycle and ordering constraints. |
| [Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching), [Vision](https://platform.claude.com/docs/en/build-with-claude/vision), and [Token counting](https://platform.claude.com/docs/en/build-with-claude/token-counting) | Accessed 2026-07-12 | Cache, image, and count-token input/usage implications. |
| [Errors](https://platform.claude.com/docs/en/api/errors) | Accessed 2026-07-12 | HTTP/SSE error envelope and retry facts. |
| [OpenAI Responses create reference](https://developers.openai.com/api/reference/resources/responses/methods/create), [OpenAI function calling guide](https://developers.openai.com/api/docs/guides/function-calling), and [OpenAI Responses streaming guide](https://developers.openai.com/api/docs/guides/streaming-responses) | Accessed 2026-07-12 | Official OpenAI request, function-call, and stream shapes used only for the deterministic Responses/Chat adapter fixtures. |

### CLI and platform pin

Verified locally without sending an API request:

| Item | Observation |
| --- | --- |
| Installed Claude Code | 2.1.201 |
| Runtime | Node.js v25.7.0 |
| Platform | Windows NT 10.0.26200.0 |

The local executable is not the public-release pin above. A real smoke must record the exact executable version it actually uses; it may use the installed 2.1.201 only if that version is recorded in its fixture metadata, or upgrade to the pinned public v2.1.207 and record that change. This note makes no claim that either version completed a real gateway request.

## Verified Claude Code gateway contract

### Endpoint selection and startup traffic

With **ANTHROPIC_BASE_URL**, Claude Code speaks the Anthropic Messages format to:

- POST /v1/messages
- POST /v1/messages/count_tokens (optional)

The official protocol reference says inference currently targets /v1/messages?beta=true. Routing therefore must match the path rather than require an exact query string. A gateway may also see a best-effort HEAD / connectivity probe. Model discovery is optional and, when enabled, requests GET /v1/models?limit=1000 with a three-second timeout; it is not required for the narrow translator spike.

Claude Code consumes inference responses as they arrive. Buffering a full result before relaying it stalls the client, so a compatibility layer must emit a live Anthropic SSE stream rather than an OpenAI-style terminal-only response.

The official support boundary matters: Anthropic documents gateway support for the formats it describes, but explicitly does **not** support routing Claude Code to non-Claude models through a gateway. Therefore, any CodexHub-to-OpenAI Responses or Chat Completions work is a scoped translation experiment only, not an officially supported Claude Code deployment.

### Headers: verified facts and required policy

Header names are case-insensitive on the wire. The protocol reference differentiates fields to forward unchanged from fields a gateway may consume. It also says headers and request body fields are open lists: new Anthropic and Claude Code fields can arrive in later CLI releases.

| Header / class | Verified behavior | Spike handling policy |
| --- | --- | --- |
| **anthropic-version** | Required by the Messages API; the protocol reference currently identifies 2023-06-01. Forward unchanged to an Anthropic-format upstream. | Preserve the raw value in the intermediate request. Never substitute an OpenAI version for it. |
| **anthropic-beta** | Comma-separated capability values. Forward verbatim; do not allowlist individual values. It can include an OAuth capability needed when Claude Code uses a claude.ai login, and stripping it can yield 401. | Treat as an open list. Preserve it for an Anthropic-compatible upstream. For a non-Anthropic upstream, an unmappable beta/body pair must receive an explicit unsupported result, never a silent drop. |
| **anthropic-workspace-id** | Forward when the upstream is Claude Platform on AWS. | Out of scope for the Responses/Chat experiment; retain as an explicit unsupported/unknown field rather than discard it. |
| **Authorization** and **x-api-key** | Gateway credentials may appear in one or both. Claude Code maps ANTHROPIC_AUTH_TOKEN to bearer Authorization, ANTHROPIC_API_KEY to x-api-key, and an apiKeyHelper can use both. | Consume at the Gateway boundary. Redact values from every trace and do not reuse a downstream gateway credential as an upstream provider credential. |
| **x-claude-code-session-id** | Unique Claude Code session identifier. | Accept as opaque correlation metadata; pseudonymize it in fixtures and logs. |
| **x-claude-code-agent-id** | Present on requests from a spawned subagent. | Accept as opaque agent-attribution metadata; do not treat it as a human or device identity. |
| **x-claude-code-parent-agent-id** | Present for nested agents. | Retain relation metadata only when a later adapter can represent it; otherwise report it as unsupported, not dropped. |
| Future **x-claude-code-*** and **anthropic-*** headers; custom headers | The protocol explicitly warns that new headers and body fields can be introduced. | Admit them into a structured open header collection. Anthropic-format paths forward the required Anthropic fields unchanged. A non-Anthropic adapter must either map a known semantic or return a precise unsupported outcome; it must not silently strip the field. |

The documented Claude Code agent IDs identify an agent, not a person or device. That distinction is a privacy and telemetry requirement for CodexHub as well.

### Request and response shape

The Messages API is stateless: each request carries the full conversation history. The normal successful Message object includes an identifier, type, assistant role, ordered content blocks, model, stop reason, optional stop sequence, and usage. A top-level system prompt is supported; newer Messages capabilities can add fields, so the following is deliberately not a closed schema.

~~~json
{
  "model": "fixture-model",
  "max_tokens": 128,
  "stream": true,
  "system": [
    {
      "type": "text",
      "text": "[sanitized-system]"
    }
  ],
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "[sanitized-user-turn]"
        }
      ]
    }
  ]
}
~~~

~~~json
{
  "id": "msg_fixture_01",
  "type": "message",
  "role": "assistant",
  "content": [
    {
      "type": "text",
      "text": "[sanitized-assistant-output]"
    }
  ],
  "model": "fixture-model",
  "stop_reason": "end_turn",
  "stop_sequence": null,
  "usage": {
    "input_tokens": 0,
    "output_tokens": 0
  }
}
~~~

The zero token values above are placeholders, not reported usage. A translator must not manufacture token counts when an upstream does not provide a reliable mapping.

### SSE contract

For stream: true, the documented named-event order is:

1. message_start, with a Message whose content is empty;
2. zero or more content blocks, each with content_block_start, one or more content_block_delta events, and content_block_stop;
3. one or more message_delta events;
4. message_stop.

Ping events may occur anywhere. The usage values in message_delta are cumulative. An error event can occur after an HTTP 200 response, and new event types may be added; a translator must therefore retain or explicitly surface unknown events instead of assuming a finite event enum.

~~~text
event: message_start
data: {"type":"message_start","message":{"id":"msg_fixture_01","type":"message","role":"assistant","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":0,"output_tokens":0}}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"[sanitized-delta]"}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":0}}

event: message_stop
data: {"type":"message_stop"}
~~~

Tool input arrives as partial JSON through input_json_delta events. Accumulate those deltas and validate the resulting object only when the corresponding content block stops. Thinking streams have thinking_delta and a signature_delta before the block closes; a converter must not invent a signature it cannot preserve.

### Tool use and result follow-up

For a client tool, a successful assistant turn ends with stop_reason: tool_use and one or more tool_use blocks. Each block supplies an opaque unique id, name, and input object. The subsequent request must include the assistant tool call in history followed immediately by a user message whose tool_result block references that exact id. Tool results must come first in the user content array; a tool result can carry content or is_error: true.

~~~json
{
  "role": "assistant",
  "content": [
    {
      "type": "tool_use",
      "id": "toolu_fixture_01",
      "name": "read_fixture",
      "input": {
        "path": "[sanitized-path]"
      }
    }
  ]
}
~~~

~~~json
{
  "role": "user",
  "content": [
    {
      "type": "tool_result",
      "tool_use_id": "toolu_fixture_01",
      "content": "[sanitized-tool-result]"
    }
  ]
}
~~~

This is not an OpenAI tool role. The official tool guide specifically notes that Messages embeds tool use in assistant/user content blocks. A later Responses or Chat adapter must keep a one-to-one internal mapping while returning the original Messages tool ID to Claude Code. Changing, losing, or reordering that ID breaks the follow-up lifecycle.

### Errors, retries, and cancellation

Documented non-streaming errors are JSON with a top-level type, an error object containing type and message, and a request_id. The official error guide lists 400, 401, 402, 403, 404, 409, 413, 429, 500, 504, and 529 classes. SSE can instead emit:

~~~text
event: error
data: {"type":"error","error":{"type":"overloaded_error","message":"[sanitized-message]"}}
~~~

The gateway protocol states that Claude Code retries some rejections and disables a rejected capability for the rest of a conversation. Its retry logic matches upstream error wording; a gateway that wraps the error in a new envelope can break recovery even if it preserves the status code. The prototype must retain a stable Anthropic error shape and avoid misleading remapping of retryable failure semantics.

**Cancellation is unknown at the wire level.** The public gateway contract defines no cancellation endpoint and does not specify what Ctrl+C, Escape, a client disconnect, or a watchdog abort becomes over HTTP. Do not infer an abort event or synthesize message_stop. A real sanitized CLI smoke is the only acceptable evidence for downstream disconnect detection and upstream cancellation propagation.

### Usage and token counting

Usage is observable in the successful Message response and cumulatively during message_delta. It can include input/output totals and cache-related fields. Map only values the upstream actually reports; retain their provenance and make absent values explicit.

The token-count endpoint accepts the same structured inputs as a message request, including system prompts, tools, images, and documents, and returns input_tokens. It is optional for a Claude Code gateway: when absent, Claude Code estimates context usage locally. A non-Anthropic upstream must therefore not return a guessed Anthropic count as though it were authoritative.

## Compatibility classification before live proof

These are conservative translator classifications, not claims that the Issue #74 prototype has already implemented or smoke-tested them. “Preserved” means only an Anthropic-compatible upstream can receive the feature unchanged. “Adapted” means a translator must maintain explicit semantic/ID/event mappings. “Explicitly unsupported” means the minimal non-Anthropic adapter should reject or omit the optional endpoint with a documented result, not silently erase data.

| Capability | Classification for a Messages-to-Responses/Chat experiment | Evidence / safe behavior |
| --- | --- | --- |
| Text request and stateless multi-turn history | Adapted | Preserve ordered history and content blocks; Responses/Chat role models differ. Live history equivalence remains to be tested. |
| Anthropic-compatible Messages pass-through | Preserved | Forward required Anthropic headers and body fields unchanged; do not reshape the system array. |
| Text SSE | Adapted | Emit the named Messages SSE lifecycle incrementally; no buffered terminal-only response. |
| Usage | Adapted for base input/output counts; cache/reasoning details explicitly unsupported | Map only reported base counts and validate a redundant total when supplied. Reject cache/reasoning/provider-detail usage until a tested semantic mapping exists; do not collapse it into input/output totals. |
| Client tool call and tool-result follow-up | Adapted | Maintain opaque tool IDs and strict follow-up ordering; validate a real read-file lifecycle. |
| Server tools and server-side result blocks | Unknown | The public Messages protocol has behaviors not demonstrated by the initial client-tool fixture. |
| Images | Unknown | Messages accepts image blocks and several source types; map only when the selected upstream/provider advertises a verified equivalent, otherwise return a precise unsupported result. |
| Thinking / adaptive reasoning / thinking signatures | Explicitly unsupported in the minimal non-Anthropic adapter | Claude Code can send adaptive thinking and Messages streams signed thinking blocks. Do not convert it to fabricated text or a fabricated signature. A future adapter needs an explicit, tested capability contract. |
| Prompt caching and attribution block | Unknown | Cache control affects ordered tools/system/messages content. With a custom base URL, preserve the system array; the protocol notes attribution-header stability from Claude Code v2.1.181. No OpenAI cache-equivalence claim is justified. |
| Context compaction and resume | Unknown | Context-management beta/body pairing is documented, but the exact 2.1.201 / 2.1.207 CLI wire trace for compact/resume is not in this note. Do not conflate a local resumed transcript with server context management. |
| Subagent attribution | Adapted for documented headers; unknown for full lifecycle | Accept session/agent/parent-agent metadata as opaque and sanitized. Actual spawned/nested request traces remain a smoke gate. |
| Beta fields and future body fields | Explicitly unsupported per field until mapped | Treat header/body pairs as open; preserve or reject together. Never silently drop just the header or just the body field. |
| Error and retry behavior | Adapted | Preserve an Anthropic-shaped error and the relevant upstream wording; test a deterministic upstream failure and an SSE error. |
| Cancellation | Unknown | Capture downstream close and upstream abort behavior with a real CLI; no documented cancel endpoint exists. |
| Count tokens | Explicitly unsupported when endpoint is absent | Omitting the optional endpoint is a documented degradation to CLI local estimation. Do not fabricate input_tokens. |

## Sanitized capture plan

The required live evidence can be collected without exposing a credential or user content:

1. Record CLI version, Node version, OS, adapter commit, and fixture schema version before the run.
2. Use a temporary, isolated Claude Code configuration and a local loopback test gateway. Supply only a non-secret fixture credential accepted by that gateway; never load, print, commit, or proxy a real API key, OAuth token, cookie, or auth file.
3. Replace every message/system/tool-result text value with a fixture label. Replace filesystem paths, URLs, hostnames, session IDs, agent IDs, tool IDs, request IDs, and model identifiers with stable typed placeholders where the exact value is not required for an ID-equality assertion.
4. Record header names and only safe values such as anthropic-version. Redact Authorization, x-api-key, cookies, custom header values, and any OAuth-bearing beta value. Record the beta policy as an open comma-separated list rather than committing a sensitive or version-fragile observed list.
5. Capture structural request JSON, normal response JSON, raw named SSE event order, cumulative usage shape, a deterministic upstream failure, and a client-abort attempt after streaming starts. Keep synthetic fixtures visibly separate from real sanitized trace metadata.
6. Exercise a text stream, a read-file client tool followed by the matching tool_result, and at least one additional history turn. Prove tool ID equality using placeholder IDs only.
7. Run the same isolated translator through the official Codex/OpenAI route and one Chat Completions Provider. Classify any divergence by field/event rather than masking it with fallback text.

## Local loopback CLI evidence

The committed, sanitized structural trace is
`tests/fixtures/claude_messages_real_cli_smoke.json`. It was captured with the
locally installed Claude Code **2.1.201** rather than the public **2.1.207**
source pin. The harness used only a temporary loopback server and a throwaway
fixture credential; it did not contact an Anthropic, OpenAI, or third-party
provider and did not load an existing credential or configuration.

- **Text streaming:** Claude Code made `POST /v1/messages`, consumed a synthetic
  Responses-to-Messages SSE stream, returned the expected fixture marker, and
  exited successfully. The strict input translator deliberately rejected the
  observed request as non-forwardable because it contained `thinking`,
  `output_config.effort`, cache-control-bearing system/message blocks, and
  open-set Anthropic headers. This is successful client/SSE evidence, not a
  claim that the complete request can safely route to a non-Anthropic upstream.
- **Tool lifecycle:** the first request received a synthetic `Read` tool call;
  Claude Code issued a second Messages request whose first user content block
  was a `tool_result` with the exact fixture tool ID, then consumed the final
  response successfully. The trace retains only the boolean ID-equality/order
  proof and redacted structural fields. The same strict input blockers remained
  present on both turns.
- **Cancellation:** after named SSE output began, the harness forcibly ended the
  local CLI process and observed the loopback connection close before the next
  write. This is an attempted downstream-disconnect observation only; it does
  not establish Ctrl+C/Escape behavior or upstream abort semantics.
- **Error/usage shapes:** deterministic unit fixtures cover a 529-to-
  `overloaded_error` SSE translation, cumulative usage fields, and the named
  Messages SSE event order. They are not live upstream error or billing proof.

The in-memory test suite also exercises the same translator seam with a
Responses-shaped upstream fixture (the official Codex/OpenAI protocol shape)
and a Chat Completions-shaped provider fixture. Neither is a real provider call;
the run is protocol-shape evidence only.

## Research conclusion and remaining gates

The official contract is sufficiently specific to build an isolated in-memory translator prototype and fixtures: endpoint selection, mandatory incremental SSE, open header/body behavior, tool-result ID ordering, error shapes, usage, and optional token counting are documented. The real loopback evidence proves the current CLI's basic downstream Messages/SSE and tool-result behavior, but it also proves that a strict non-Anthropic translator sees current default fields with no safe mapping yet.

The decision is **scoped PARTIAL**, not GO. Anthropic does not officially support Claude Code routed to non-Claude models, and current Claude Code defaults require explicit capability decisions for adaptive thinking, effort output configuration, cache-control-bearing blocks, Anthropic beta/header pairs, and count tokens. No production `/v1/messages` route, client auto-configuration, or handler integration is warranted.

Before any production route consideration, a follow-up must select and test an explicit policy for every rejected field, verify the chosen policy against a real official Codex/OpenAI route and a real Chat Completions Provider with approved credentials, prove error/retry behavior without wrapping upstream wording, and repeat the trace against the pinned/upgraded Claude Code version. The canonical representation/version-policy ADR added by this Spike applies only to that later implementation seam. This Issue remains strictly downstream Claude Code Messages compatibility and does not cover ACP AgentProvider work.
