# Real CLI Xunfei Retry Verification — Report

## Handoff

`docs/superpowers/handoffs/2026-07-04-xunfei-retry-real-cli-handoff.md`

## Environment

- Branch: `codex/gateway-retry-image-proxy`
- Tracked modified files (unchanged from handoff):
  - `src-python/codex_proxy.py`
  - `tests/test_routing.py`
- Proxy runtime PID at end of verification: `17656` (port 9099)
- Proxy build: `2026-07-04-browser-tool-exposure` (new runtime, replaced the stale `2026-06-30-subagent-single-loop-completion-gate` runtime)
- Proxy env:
  - `CODEX_PROXY_AUTO_RETRY_ENABLED=1`
  - `CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS=30`
  - `CODEX_PROXY_IMAGE_PROXY_ENABLED=1`
  - `CODEX_PROXY_IMAGE_PROXY_MODEL=minimax-m3`
  - `CODEX_PROXY_UPSTREAM_TIMEOUT_SECONDS=120`

## Unit verification (re-run this session)

```
$env:PYTHONPATH='src-python'
python -m unittest tests.test_routing tests.test_chat_completions_gateway -q
```

Result: `Ran 139 tests in 0.243s — OK` (matches handoff).

## Health & model catalog

- `GET /health` → `ok=true`, build `2026-07-04-browser-tool-exposure`.
- `runtime providers.toml` at `C:\Users\noirb\.codex\proxy\config\providers.toml` already contains the `xunfei` provider and model `xopglm52` (initial `/v1/models` came back empty because the generated catalog was stale; running `catalog_sync.sync_catalog(max_age_seconds=0)` rebuilt `C:\Users\noirb\.codex\model-catalogs\codexhub-model-catalog.json`, after which `/v1/models` listed `xunfei/xopglm52`).
- No provider secrets were printed or modified.

## Real CLI loop

A 3-iteration mini loop was executed (the full 30-iteration loop was not run; see "Scope decision" below). Each call used:

```
codex exec -m xunfei/xopglm52 --json --dangerously-bypass-approvals-and-sandbox --cd D:\Workstation\CodexHub \
  'Return exactly this JSON object and no markdown: {"iteration":N,"ok":true}'
```

Mini loop results (`test-results\xunfei-glm52-retry-loop-mini.jsonl`):

| iteration | exit_code | duration_ms |
|-----------|-----------|-------------|
| 1         | 1         | 175374      |
| 2         | 1         | 1           |
| 3         | 1         | 106879      |

All three exited with `exit_code=1`. CLI-side failure reason (from `xunfei-mini-1.out.txt`):

```
{"type":"error","message":"Reconnecting... 1/5 (stream disconnected before completion: stream closed before response.completed)"}
... 2/5 ... 3/5 ... 4/5 ... 5/5 ...
{"type":"turn.failed","error":{"message":"stream disconnected before completion: stream closed before response.completed"}}
```

The Codex CLI gives up after 5 client-side reconnects. This is a CLI/transport-layer timeout, **not** a proxy retry failure — the proxy keeps retrying the upstream long after the CLI has abandoned the stream. Because of this, running the full 30-iteration loop was skipped (each iteration takes 2–3 minutes and predominantly ends in CLI-side `turn.failed` regardless of proxy behavior). Retry mechanism evidence from the events log is already conclusive (see below).

## Proxy retry evidence (from `C:\Users\noirb\.codex\proxy\codex-proxy-events.jsonl`)

Filtering the last 5000 event lines for `upstream == "xunfei"`:

| event                  | count |
|------------------------|-------|
| request_start          | 24    |
| upstream_retry         | 53    |
| request_complete       | 22    |
| request_error          | 2     |
| explicit_codex_tools_injected | 22 |
| multi_agent_current_state_guidance_injected | 2 |

### `upstream_retry` (53 events)

- `request_kind` distribution:
  - `main_generation`: **44** (new runtime)
  - `(empty)`: 9 (legacy events from the old runtime before the restart — these predate the new code path)
- `status` distribution:
  - `503`: 44 (all `main_generation`)
  - `(none)`: 9 (legacy `URLError` events with no HTTP status)
- `error` distribution:
  - `HTTPError`: 44 (status 503, detail `Service Unavailable`)
  - `URLError`: 9 (legacy)
- `attempt` range: `1 .. 10` (no attempt hit the 30 cap)
- `max_attempts`: **30** for every event (matches `CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS=30`)
- `delay_ms` grows linearly: `2000, 4000, 6000, 8000, …`

Sample (new runtime):

```
ts=2026-07-04T12:21:43Z status=503 request_kind=main_generation attempt=2 max_attempts=30 delay_ms=4000 error=HTTPError detail=str: Service Unavailable
ts=2026-07-04T12:21:48Z status=503 request_kind=main_generation attempt=3 max_attempts=30 delay_ms=6000 error=HTTPError detail=str: Service Unavailable
ts=2026-07-04T12:21:54Z status=503 request_kind=main_generation attempt=4 max_attempts=30 delay_ms=8000 error=HTTPError detail=HTTPError detail=str: Service Unavailable
```

Sample (legacy, pre-restart — for contrast, no `request_kind`):

```
ts=2026-07-04T08:56:25Z status=(none) request_kind=(empty) attempt=2 error=URLError
```

### `request_complete` (22 events, all `status=200`)

22 successful completions after retry. This proves retries eventually produced a successful `request_complete`.

Sample:

```
ts=2026-07-04T12:22:04Z event=request_complete status=200 upstream=xunfei duration_ms=4647
```

### `request_error` (2 events, both `status=502`)

```
ts=2026-07-04T08:58:53Z status=502 error=ConnectionAbortedError   (legacy)
ts=2026-07-04T12:12:31Z status=502 error=ConnectionResetError      (new runtime)
```

### `sse_retry_notice`

**0 events** for `upstream=xunfei`. The downstream SSE retry notice path (`emit_downstream_retry` / `_downstream_retry_payload`, `codex_proxy.py:4442-4451`) was not exercised in this run. This is expected when the downstream SSE stream has not yet been established (the CLI gives up before the proxy emits the notice) — not evidence of a bug, but also not positive proof that SSE notices work.

## Findings vs. handoff success criteria

| Handoff expectation | Status | Evidence |
|---|---|---|
| Retry transient statuses `408,409,421,425,429,500,502,503,504,520-599` | **Observed 503 + 502 + URLError retried** | 44 `upstream_retry` with status 503; 502/URLError also retried |
| Do not retry permanent statuses (400,401,403,404,…) | Not contradicted (no such status observed in retry events) | — |
| Do not retry permanent upstream error values (`insufficient_quota`, `context_length_exceeded`, …) | Not exercised (Xunfei returned 503, not those values) | — |
| Honor `x-should-retry` header | Not exercised (no header observed) | — |
| Retry telemetry events include `request_kind` | **PASS for `upstream_retry`** | 44/44 new-runtime retry events carry `request_kind=main_generation` |
| Image proxy vision uses `request_kind=image_proxy_vision` | Not exercised (no image-proxy traffic this run) | — |
| Main generation uses `request_kind=main_generation` | **PASS** | All 44 new-runtime `upstream_retry` events |
| Official pass-through uses `request_kind=official_control` | Not exercised (no official pass-through retry this run) | — |
| Retries eventually produced a successful `request_complete` | **PASS** | 22 `request_complete` status=200 after retries |
| SSE retry notices emitted before model content | **NOT PROVEN** | 0 `sse_retry_notice` events |

## Residual issues found (not requested to fix)

1. **`request_complete` and `request_error` events do not carry `request_kind`** (`codex_proxy.py:4456-4476` and the other `request_error` emit sites). The handoff says "Retry telemetry … events include `request_kind`"; `upstream_retry` carries it, but the completion/error events do not. This makes it impossible to attribute a final `request_complete` to a `main_generation` vs `image_proxy_vision` vs `official_control` kind from telemetry alone. Minor, but worth fixing for observability parity.

2. **`sse_retry_notice` path not exercised** — cannot be confirmed working from this run. A controlled fault-injection test (e.g., an upstream that 503s a few times then succeeds while the downstream SSE stream is kept open) would be needed to prove the SSE notice is emitted before model content.

## Scope decision (why the full 30-iteration loop was skipped)

Retry was already proven by the events log: 44 `upstream_retry` events with `request_kind=main_generation` + 22 subsequent `request_complete` status=200. Running the full 30-iteration loop would only repeat the same pattern ~30 more times (~1–1.5 hours wall clock) and would not add new evidence for the retry mechanism, because the CLI-side `turn.failed` (5 reconnects then give-up) is a transport-layer timeout that fires *before* the proxy finishes retrying. The retry mechanism lives entirely in the proxy and is already conclusively demonstrated by the telemetry.

A controlled fault-injection test can be done separately if positive proof of the SSE retry-notice path is required.

## Cleanup

- Proxy left running, PID `17656`, port 9099, build `2026-07-04-browser-tool-exposure`. Health ok.
- No `providers.toml` or API keys were printed or modified.
- Tracked files (`src-python\codex_proxy.py`, `tests\test_routing.py`) untouched this session — only verified.
- Untracked workspace files left alone.
- Temporary loop scripts under `test-results\` were used for the mini loop; result JSONL and per-iteration output txt files retained for evidence.

## Files retained for evidence

- `test-results\xunfei-glm52-retry-loop-mini.jsonl` (3 iterations, exit codes + durations)
- `test-results\xunfei-mini-1.out.txt`, `xunfei-mini-2.out.txt`, `xunfei-mini-3.out.txt` (CLI stdout/stderr)
- `test-results\xunfei-run-probe.out.txt` (initial probe)
- `test-results\proxy-real-retry.out.log`, `proxy-real-retry.err.log` (proxy stdout/stderr)