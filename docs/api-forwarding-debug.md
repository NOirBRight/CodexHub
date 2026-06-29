# API Request Forwarding Debug Note

## Context

CodexHub proxies API requests between Codex App and upstream providers (OpenAI, Ollama Cloud, Volcengine, MiniMax.cn).
Request-level errors are an expected part of debugging during development and production use.

## Error: "A parameter specified in the request is not valid"

- **Request ID:** 021782705891055399eaa44f2e314002ba9c0fca0e9df6e29274c
- **When:** 2026-06-29, during CodexHub planning session
- **Symptom:** API returned an error indicating a parameter in the request was not valid
- **Likely cause:** The request was forwarded through the local proxy (codex_proxy.py on port 9099) and the upstream provider rejected a parameter. This could be:
  1. A `reasoning` parameter that the upstream model does not support
  2. A `max_output_tokens` value exceeding the model limit
  3. An `encrypted_content` field leaking through to a third-party provider
  4. A model alias that did not resolve correctly to the upstream model ID
- **Resolution:** Transient during this session; did not block planning work. But this class of error needs proper debugging tooling in CodexHub.

## Future debugging requirements for CodexHub

1. **Per-request logging**: Log every proxied request with model, upstream, status, duration, and error detail.
2. **Request/response capture**: Optional verbose mode that captures full request and response bodies for debugging (off by default for privacy).
3. **Parameter validation**: Before forwarding, validate that request parameters (reasoning effort, max_output_tokens, etc.) are compatible with the target upstream model.
4. **Error surface in UI**: Show recent proxy errors in the ProxyStatusBar or a dedicated debug panel.
5. **Event log**: The existing `codex-proxy-events.jsonl` log already records request_start/request_error/request_complete events. CodexHub should surface this in the UI.

## Existing infrastructure

- `codex_proxy.py` already logs: request_start, request_complete, request_error, client_write_failed, sse_reasoning_summary
- Error details are captured in `safe_upstream_error_detail()` with API key redaction
- The `codex-proxy-events.jsonl` file is the primary debug artifact
- The `codex-proxy.log` file has text-level logging

## Action items

- [ ] Add parameter compatibility checking before forwarding requests
- [ ] Surface recent errors in CodexHub UI debug panel
- [ ] Document common error patterns and their causes in user-facing help
