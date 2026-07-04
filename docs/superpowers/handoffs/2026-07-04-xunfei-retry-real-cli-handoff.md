# Handoff: Real CLI Xunfei Retry Verification

## Current state

The retry/image-proxy implementation is in progress on branch `codex/gateway-retry-image-proxy`.

Modified tracked files:

- `src-python/codex_proxy.py`
- `tests/test_routing.py`

Do not touch unrelated untracked files in the workspace.

Unit verification already passed before this handoff:

```powershell
$env:PYTHONPATH='src-python'
python -m unittest tests.test_routing tests.test_chat_completions_gateway -q
```

Result: `139 tests OK`.

`git diff --check -- src-python\codex_proxy.py tests\test_routing.py` had no whitespace errors beyond existing CRLF/LF warnings.

## Why this handoff exists

The current Codex conversation itself may be using the local CodexHub proxy, so this agent should not own restarting the proxy. I accidentally already sent:

```powershell
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:9099/shutdown'
```

If the proxy is now down, restart it from this worktree. If it is still running, verify whether it is the old runtime before replacing it.

## What changed in code

Retry now distinguishes request kinds:

- `main_generation`
- `image_proxy_vision`
- `official_control`

Expected retry behavior in `src-python/codex_proxy.py`:

- Retry transient statuses: `408, 409, 421, 425, 429, 500, 502, 503, 504`, and `520-599`.
- Do not retry permanent statuses: `400, 401, 403, 404, 405, 406, 407, 410, 411, 412, 413, 414, 415, 416, 417, 418, 422, 426, 428, 431, 451, 501, 505`.
- Do not retry permanent upstream error values such as `insufficient_quota`, `context_length_exceeded`, `invalid_image`, `invalid_request_error`, and `unsupported_*`.
- Honor upstream `x-should-retry: true/false`.
- Retry telemetry and downstream retry SSE events include `request_kind`.
- Image proxy vision subrequests use `request_kind=image_proxy_vision`.
- Main generation requests use `request_kind=main_generation`.
- Official pass-through/control requests use `request_kind=official_control`.

## Next action

Restart the proxy outside this Codex session, then run a real long-loop test through the real Codex CLI using Xunfei GLM-5.2:

```powershell
cd D:\Workstation\CodexHub

$env:PYTHONPATH='D:\Workstation\CodexHub\src-python'
$env:CODEX_HOME='C:\Users\noirb\.codex'
$env:CODEX_PROXY_UPSTREAM_TIMEOUT_SECONDS='120'
$env:CODEX_PROXY_AUTO_RETRY_ENABLED='1'
$env:CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS='30'
$env:CODEX_PROXY_IMAGE_PROXY_ENABLED='1'
$env:CODEX_PROXY_IMAGE_PROXY_MODEL='minimax-m3'

$out = 'D:\Workstation\CodexHub\test-results\proxy-real-retry.out.log'
$err = 'D:\Workstation\CodexHub\test-results\proxy-real-retry.err.log'
New-Item -ItemType Directory -Force 'D:\Workstation\CodexHub\test-results' | Out-Null

$p = Start-Process -FilePath python `
  -ArgumentList @('D:\Workstation\CodexHub\src-python\codex_proxy.py','--host','127.0.0.1','--port','9099') `
  -WorkingDirectory 'D:\Workstation\CodexHub\src-python' `
  -RedirectStandardOutput $out `
  -RedirectStandardError $err `
  -WindowStyle Hidden `
  -PassThru

$p.Id
```

Confirm health:

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:9099/health' -TimeoutSec 5 | ConvertTo-Json -Depth 5
```

Confirm the model exists without printing provider secrets:

```powershell
(Invoke-RestMethod -Uri 'http://127.0.0.1:9099/v1/models' -TimeoutSec 10).data |
  Where-Object { $_.id -eq 'xunfei/xopglm52' } |
  Select-Object id, name, provider, model
```

Run the real CLI loop. Keep prompts harmless and deterministic:

```powershell
$results = 'D:\Workstation\CodexHub\test-results\xunfei-glm52-retry-loop.jsonl'
Remove-Item -Force $results -ErrorAction SilentlyContinue

for ($i = 1; $i -le 30; $i++) {
  $started = Get-Date
  $tmpOut = "D:\Workstation\CodexHub\test-results\xunfei-run-$i.out.txt"
  $tmpErr = "D:\Workstation\CodexHub\test-results\xunfei-run-$i.err.txt"
  $prompt = "Return exactly this JSON object and no markdown: {`"iteration`":$i,`"ok`":true}"

  & codex exec `
    -m xunfei/xopglm52 `
    --json `
    --dangerously-bypass-approvals-and-sandbox `
    --cd D:\Workstation\CodexHub `
    $prompt *> $tmpOut

  $exit = $LASTEXITCODE
  $durationMs = [int](((Get-Date) - $started).TotalMilliseconds)
  $tail = if (Test-Path $tmpOut) { (Get-Content $tmpOut -Tail 20 -Raw) } else { '' }

  [pscustomobject]@{
    iteration = $i
    exit_code = $exit
    duration_ms = $durationMs
    output_tail = $tail
  } | ConvertTo-Json -Compress | Add-Content -Encoding UTF8 $results

  Start-Sleep -Seconds 1
}
```

## Evidence to collect

Inspect recent proxy events:

```powershell
$eventsPath = 'C:\Users\noirb\.codex\proxy\codex-proxy-events.jsonl'
$since = (Get-Date).AddHours(-2)

$events = Get-Content $eventsPath -Tail 5000 |
  ForEach-Object {
    try { $_ | ConvertFrom-Json } catch { $null }
  } |
  Where-Object {
    $_ -and (
      $_.model -eq 'xunfei/xopglm52' -or
      $_.upstream -eq 'xunfei' -or
      $_.request_kind -eq 'main_generation'
    )
  }

$events |
  Where-Object { $_.event -in @('upstream_retry','request_complete','request_error','sse_retry_notice') } |
  Select-Object event, request_kind, provider, upstream, model, status, error, attempt, max_attempts, delay_ms |
  Format-Table -AutoSize
```

Summarize:

- CLI success count and failure count.
- Number of `upstream_retry` events.
- Retry statuses/errors observed.
- Whether retries were `request_kind=main_generation`.
- Whether any retry eventually produced a successful `request_complete`.
- Whether SSE retry notices were emitted before model content.

If no natural retry occurs, report that plainly. Do not claim retry was proven by the loop. A controlled fault-injection test can be done separately if needed.

## Do not

- Do not print `providers.toml` or any API keys.
- Do not modify real provider secrets.
- Do not revert unrelated files.
- Do not leave two proxies fighting on port `9099`.
- Do not treat a passing loop with zero retry events as proof that retry worked in production.

## Cleanup

After verification, either:

- leave the restarted proxy running if the app needs it, and report the PID; or
- stop only the PID started by this handoff.

Do not stop an unrelated proxy process unless the user explicitly asks.
