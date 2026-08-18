# Windows DSH 证据怎么给我（0.1.9-beta.1）

把下面这份填好，贴到 PR 评论，或另开 issue 评论并 `@` 我。不要把 `CODEXHUB_API_KEY`、用户 API key、token 贴出来。

## 最短交法

1. 新开一个 PowerShell，不要复用 Linux 这台机器的配置。
2. 备份 `%USERPROFILE%\.dsh\settings.yaml` 和 `%USERPROFILE%\.dsh\.credentials.yaml`。
3. 在 CodexHub Gateway 页对 DSH 做一次 Connect，再 Disconnect。
4. 把下面模板填进 PR。

```md
## Windows DSH inject/detach

- Host:
- CodexHub build: 0.1.9-beta.1 / commit:
- dsh version (`dsh --version`):
- Connect: ok / fail
- Disconnect: ok / fail
- agent-default-model unchanged: yes / no (before → after, no secrets)
- foreign providers kept (names only):
- settings.yaml / .credentials.yaml mode after connect (icacls or `Get-Acl` 摘要，不要贴 key):
- restart_required shown in UI:
- screenshot: Gateway DSH card Connected / Official (no secrets visible)

Notes:
```

## 更好（可选）

把一份**打码**的 `settings.yaml` 片段贴上：只保留 `llm-pi-ai.providers` 的 key 名、`codexhub.api` / `baseURL` / `apiKeyEnv`，删掉一切 credential 值。

Connect 后应能看到 `codexhub` + `apiKeyEnv: CODEXHUB_API_KEY`。  
Disconnect 后 `codexhub` 和 `CODEXHUB_API_KEY` 应消失，其它 provider / activation 还在。

## 不要发

- `CODEXHUB_API_KEY` 的值
- Kimi / Ollama / 任何上游 key
- 完整 `.credentials.yaml`
