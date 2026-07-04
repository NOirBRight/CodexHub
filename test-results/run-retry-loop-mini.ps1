$env:CODEX_HOME = 'C:\Users\noirb\.codex'
$results = 'D:\Workstation\CodexHub\test-results\xunfei-glm52-retry-loop-mini.jsonl'
Remove-Item -Force $results -ErrorAction SilentlyContinue

for ($i = 1; $i -le 3; $i++) {
  $started = Get-Date
  $tmpOut = "D:\Workstation\CodexHub\test-results\xunfei-mini-$i.out.txt"
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
    iteration   = $i
    exit_code   = $exit
    duration_ms = $durationMs
    output_tail = $tail
  } | ConvertTo-Json -Compress | Add-Content -Encoding UTF8 $results

  Start-Sleep -Seconds 1
}
"MINI_LOOP_DONE"