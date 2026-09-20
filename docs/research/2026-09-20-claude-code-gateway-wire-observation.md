# Claude Code Gateway wire observation (Issue #74 evidence gate)

Date: 2026-09-20
Status: **scoped PARTIAL** — no GO. Live upstream evidence is still missing.

This note records what an *installed* Claude Code CLI actually sends to an
isolated loopback Anthropic-Messages gateway, and separates that from what the
official documentation states. It is evidence for [#74][i74]; it authorizes no
production route.

## 1. Pins (never interchangeable)

| Pin | Value | What it is |
| --- | --- | --- |
| Observed CLI | `2.1.278 (Claude Code)` | Local install, `/home/noirbright/.local/share/claude/versions/2.1.278`; every "observed" claim below is bound to it |
| Documented source pin | `v2.1.207` | Public pin used by prior campaign notes; **not** observed here |
| Historical local evidence | `2.1.201` | Prior scoped-PARTIAL spike (PR #100); not re-used as evidence |
| Documented Facts | `code.claude.com/docs` fetched 2026-09-20 | Current public docs, no version stamp of their own |

No historical version was installed, downgraded, or equated with this pin.

## 2. Isolation used for the observation

The run is reproducible with
`scripts/claude_messages_loopback_harness.py run` (see §7). Every guard below is
part of the harness, not a manual step:

- **Environment scrubbed** (`env` replaced, not extended): no inherited
  `ANTHROPIC_*`, `CLAUDE_CODE_USE_*`, proxy, or credential variable survives.
  Only the synthetic token is set.
- **Own home and config**: fresh `HOME` and `CLAUDE_CONFIG_DIR` under the run
  output directory; `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` so no real memory file is
  read; `DISABLE_AUTOUPDATER=1` so the global install is never touched.
- **Loopback only**: base URL `http://127.0.0.1:<port>`; `HTTP(S)_PROXY` also
  point at the same loopback recorder, so any egress attempt is answered `502`
  and recorded as `egress_guard`.
- **Connect-level verification**: the CLI runs under
  `strace -f -e trace=connect`; the harness fails the run if any `connect()`
  target is not `127.0.0.1`/AF_UNIX/AF_NETLINK.
- **Pre-dispatch bounds**: `Admission` counts every would-be upstream request
  (messages, count_tokens, discovery, probe) and refuses N+1 *before* writing a
  response; every body's `max_tokens` is checked against the budget first.
  Refusals are recorded, and logs are audit-only, never the enforcement.
- Result: `egress_attempts: 0`, `non_loopback: []`, synthetic credentials only.

## 3. Observed on the pinned CLI (loopback, synthetic credentials)

### Endpoints and startup traffic

- Inference: `POST /v1/messages?beta=true` — path *and* query variant, matching
  the documented "match on the path, not the full URL".
- Discovery, only with `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`:
  `GET /v1/models?limit=1000`, sent **before** the first inference request.
  Without the variable no discovery request is sent.
- Discovery cache written to `$CLAUDE_CONFIG_DIR/cache/gateway-models.json` as
  `{"baseUrl", "fetchedAt", "models":[...]}`. The cache keeps *every* returned
  entry, including `vendor/not-claude`, so the `claude`/`anthropic` substring
  filter is applied when the picker is built, not when the response is stored.
- No `HEAD /api/hello` probe and no `POST /v1/messages/count_tokens` request were
  observed on this non-interactive path.

### Request headers actually sent

`Authorization: Bearer <synthetic>` on both discovery and inference;
`anthropic-version: 2023-06-01`; `anthropic-beta` as one comma-separated open
set (10 values observed, e.g. `claude-code-20250219`,
`interleaved-thinking-2025-05-14`, `context-management-2025-06-27`,
`effort-2025-11-24`); `x-claude-code-session-id`; `x-app: cli`; the
`x-stainless-*` client-metadata family; `anthropic-dangerous-direct-browser-access`.
No `x-claude-code-agent-id` / `-parent-agent-id` in a single-session print run.

### Credential carrier (all three configurations observed)

| Environment | Discovery | Inference |
| --- | --- | --- |
| `ANTHROPIC_AUTH_TOKEN` only | `Authorization: Bearer` | `Authorization: Bearer` |
| `ANTHROPIC_API_KEY` only | `x-api-key` | `x-api-key` |
| both | both headers | both headers |

Recommendation: the Gateway accepts `Authorization: Bearer` as the primary
carrier and treats a *conflicting* `x-api-key`/helper configuration as a
reported connection conflict. Observed dual-header behavior means "both present
with the same value" must not be treated as an error by itself. Fail closed with
an actionable error when no acceptable carrier is present; never fall back to an
ambient or saved credential.

### Request body shape (first turn)

Top-level keys: `context_management`, `max_tokens` (32000), `messages`,
`metadata` (`{user_id}`), `model`, `output_config` (`{effort: "high"}`),
`safeguards`, `stream: true`, `system`, `thinking` (`{type: "adaptive",
display: ...}`), `tools` (20 client tools).

- `system` is an ordered **array** of text blocks; the first carries no
  `cache_control`, later ones do. Block order and `cache_control` must survive.
- `messages` mixes roles: `user` (multi-block text) and a mid-conversation
  `role: "system"` entry. No `tool_choice` was sent.
- `safeguards` is a **list** of classifier-context blocks
  (`{"type": "dangerous_tool_use", "classifier_context": ...}`) present on the
  first request of a turn and absent from the follow-up. Its enforcement
  semantics are not established, so the prototype treats it as fail-closed.
- `metadata.user_id` is client telemetry, not model input.
- No field outside this observed set appeared, so the prototype reports an empty
  `unmodelled_fields` tuple for real traffic — the open-set hook exists but is
  not yet exercised by the pinned client.

### Tool lifecycle (isolated loopback, synthetic tool)

Two-message lifecycle observed with stable call identity: turn 1 is answered
with a synthetic `tool_use`; the CLI executes the tool and turn 2 carries
`assistant[tool_use]` + `user[tool_result]` with the same opaque id
(sha256-fingerprint match in `observation.json`), plus the full prior history.
`stop_reason: tool_use` and `end_turn` are both echoed back correctly.

### Client behavior worth recording

- An unknown model id (`claude-synthetic-1`) is accepted: the CLI logs
  `[claude-code:unrecognized_model]` on stderr and proceeds. `--model` and
  `ANTHROPIC_MODEL` are not validated against a built-in list on a custom base
  URL.
- The CLI prints a client-side notice when the gateway does not implement the
  auto-mode classifier billing contract; it does not break inference.

## 4. Documented facts (primary sources, current docs)

Short list only; the full official-doc survey is owned by a separate
preparation stream, so this note cites rather than duplicates it.

- [Gateway compatibility guide](https://code.claude.com/docs/en/llm-gateway-protocol):
  Anthropic-Messages-format endpoints `/v1/messages` and optional
  `/v1/messages/count_tokens`; `HEAD /api/hello` warm-up probe; discovery
  request `GET /v1/models?limit=1000` with a 3 s default timeout and
  redirect-as-failure; discovery keeps ids containing `claude`/`anthropic`
  (prefix-only before v2.1.223); results cached to the config dir and refreshed
  each startup; `anthropic-version`/`anthropic-beta` must be forwarded verbatim;
  body/header capability pairs must travel together; error bodies must be
  forwarded unmodified for capability-rejection recovery; 300 s byte watchdog on
  `ANTHROPIC_BASE_URL` streams counts SSE pings.
- [Model configuration](https://code.claude.com/docs/en/model-config): role
  variables `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU,FABLE}_MODEL`,
  `CLAUDE_CODE_SUBAGENT_MODEL`, single-entry `ANTHROPIC_CUSTOM_MODEL_OPTION`;
  `ANTHROPIC_SMALL_FAST_MODEL` is deprecated in favor of
  `ANTHROPIC_DEFAULT_HAIKU_MODEL`.
- [Connect to an LLM gateway](https://code.claude.com/docs/en/llm-gateway-connect):
  `ANTHROPIC_AUTH_TOKEN` → `Authorization: Bearer`, `ANTHROPIC_API_KEY` →
  `x-api-key`, `apiKeyHelper` → both, with a gateway credential outranking a
  saved login.
- [Environment variables](https://code.claude.com/docs/en/env-vars):
  `DISABLE_AUTOUPDATER`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`,
  `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY`, `CLAUDE_CODE_DISABLE_CLAUDE_MDS`,
  `CLAUDE_CODE_GATEWAY_MODEL_DISCOVERY_TIMEOUT_MS` (v2.1.269+).

Role mapping, refresh TTL, and ID-prefix behavior were **not** re-verified
against the pinned CLI here (discovery was observed, the picker was not driven);
those remain #77 work.

## 5. Representation seam status

`src-python/anthropic_messages_prototype.py` re-establishes the ADR-0001 /
ADR-0014 seam as an isolated prototype (no production import, no route):

- native path: byte-exact pass-through of the inbound body; the Gateway-owned
  rewrites (model identity, credential injection, transport) are named in
  `NATIVE_GATEWAY_REWRITES` rather than applied here;
- converted path: Anthropic → Chat in the prototype, then the **existing**
  `protocol_translation.chat_completions_request_to_responses_body` seam for
  Responses — no third translator;
- every non-equivalent field returns a named policy; unknown top-level fields,
  unsupported block types, and explicitly requested safety controls
  (`safeguards`) return a non-forwardable result naming the field;
- `output_config.effort` is *mapped* (`reasoning_effort`) so it reaches
  Responses as `reasoning.effort` instead of being dropped on the Chat leg;
- credential/header and prompt-bearing representations keep values out of
  `repr`, so diagnostics cannot leak them.

## 6. Compatibility classification so far

| Capability | Status | Evidence |
| --- | --- | --- |
| Text request/response, SSE incremental | preserved on native path; converted shape tested | observed loopback text stream |
| Multi-turn history + tool call/result identity | preserved | observed two-turn loopback with matching id fingerprint |
| Discovery `/v1/models` | request/response/cache shape observed; picker behavior not driven | this note §3 |
| `cache_control` on system/tools | native preserved; converted = declared adaptation | prototype tests |
| Adaptive thinking, `output_config.effort`, `context_management`, `metadata` | effort mapped; others named adaptation / fail-closed | prototype tests |
| `safeguards` classifier context | **non-forwardable** (no proven equivalent) | prototype tests |
| Images, prompt caching, compaction/resume, subagents, count_tokens, cancellation, retry, betas pairing | **not established** | requires live upstream |
| Three upstream protocols (native Anthropic, Responses, Chat) end to end | **not established** | requires approved live resources |

## 7. Reproduction

```bash
./scripts/codexhub-python.sh scripts/claude_messages_loopback_harness.py self-check --out /tmp/t74-selfcheck
./scripts/codexhub-python.sh scripts/claude_messages_loopback_harness.py run \
  --out /tmp/t74-run --claude-bin "$(command -v claude)" \
  --scenario tool --enable-discovery --max-output-tokens 32768 --timeout 120
./scripts/codexhub-python.sh -m pytest tests/test_anthropic_messages_prototype.py -q
```

`run` refuses `--allow-network`: live upstream use is not authorized for #74.
Raw artifacts stay outside the repository (`observation.json`, `requests.jsonl`);
only content-free structure is recorded.

## 8. Decision

**Scoped PARTIAL.** The native-format wire contract, credential carrier,
discovery request/response/cache shape, and the tool/result lifecycle are
established against the pinned CLI on loopback. Nothing here proves behavior
against a real upstream, so:

- no production `/v1/messages` route is authorized;
- #75/#77/#76/#78 stay blocked;
- remaining gates for GO: live native-Anthropic, Responses, and Chat
  Completions evidence (text, incremental SSE, tool round trip, cancellation,
  error/retry, usage), plus the capability matrix rows marked above.

[i74]: https://github.com/NOirBRight/CodexHub/issues/74
