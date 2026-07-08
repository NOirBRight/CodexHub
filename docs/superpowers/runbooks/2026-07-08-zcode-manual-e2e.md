# ZCode Manual CodexHub E2E

## Prompt

Use the selected CodexHub model and reply with exactly:

```text
CODEXHUB_E2E_OK
```

## Pass Criteria

- ZCode sends a request through `http://127.0.0.1:9099`.
- Proxy event log records `client_id=zcode`.
- The selected model returns `CODEXHUB_E2E_OK`.
- ZCode UI does not hang after the model completes.

## Log Check

Run after each manual case:

```powershell
$events = Get-Content C:\Users\noirb\.codex\proxy\codex-proxy-events.jsonl -Tail 500 |
  ForEach-Object { try { $_ | ConvertFrom-Json } catch {} }
$events |
  Where-Object { $_.client_id -eq 'zcode' } |
  Select-Object -Last 20 ts,event,request_id,model,model_requested,model_canonical,upstream,provider_id,inbound_format,upstream_format,status,duration_ms,error,detail |
  ConvertTo-Json -Depth 4
```

## Manual Result Artifact

After the UI run passes, record:

```json
{
  "client": "zcode",
  "status": "passed",
  "prompt": "CODEXHUB_E2E_OK",
  "checked_at": "2026-07-08T00:00:00+08:00",
  "proxy_log_client_id": "zcode",
  "manual_operator": "user-assisted"
}
```
