# Handoff: Windows 同步做 0.1.9 测试

Branch: `codex/issue-430-dsh-client`  
Train: 0.1.9 Provider Injection + DSH  
Linux host has already run isolated apply/readback and a Linux GUI preflight. Windows still owns the AppX Desktop / ZCode GUI release matrix.

## What landed on this branch

- DSH is injection-first: connect writes `llm-pi-ai.providers.codexhub` + `~/.dsh/.credentials.yaml:CODEXHUB_API_KEY`. Disconnect is surgical detach. `agent-default-model` is never flipped.
- Credential/settings atomic writes keep Unix mode `0600`. DSH mapping activation is surfaced as `provider/model`.
- Pi connect no longer forces `defaultProvider` / `defaultModel` and no longer strips `enabledModels`. Detach only removes managed providers from `models.json`.
- Codex stays named **Codex Desktop / Codex App**. Do not rename the product to ChatGPT in CodexHub UI.
- Codex remains the overlay / stable-`custom`-bucket exception. Linux can now launch the desktop app via the `chatgpt` package (`/usr/bin/chatgpt` → `codex-launcher`).
- Linux packaging is first-class on the same train: AppImage + deb + portable tarball. Scripts: `scripts/build-linux-portable.sh`, `scripts/build-linux-release.sh`.
- Linux GUI preflight: `python3 scripts/e2e_linux_gui_clients.py`.

## What Windows must still prove

Use the existing Windows real-client host and a **new** output root. Do not reuse this Linux HOME or this machine's `~/.dsh`.

### 1. DSH (required for 0.1.9-beta2)

- Install `@deepseek-ai/dsh` at or above pinned `0.1.0-rc.6`. Newer versions may report `drifted`; that is expected, not a connect failure.
- Config: `%USERPROFILE%\.dsh\settings.yaml` and `%USERPROFILE%\.dsh\.credentials.yaml`.
- Connect = inject block + `CODEXHUB_API_KEY` only.
- Disconnect = detach that block + key only.
- Foreign providers and `agent-default-model` must survive.
- Files that were `600` must stay owner-only after connect/disconnect.
- Restart requirement is `none` (hot reload).
- Do not claim “five clients fully supported” until this Windows DSH inject/detach pass exists.

### 2. Pi (beta3 start)

- Apply must keep the user's `defaultProvider` / `defaultModel` / `enabledModels`.
- Foreign providers in `models.json` must survive apply/detach.
- A takeover leftover (`defaultProvider=codexhub-*`) must not be rewritten on apply.

### 3. Codex Desktop + ZCode GUI (Windows matrix)

Keep the current Windows cases. Names stay Codex Desktop / Codex App.

| Case | Client | Route |
|---|---|---|
| `desktop-luna` | Codex Desktop | Official Luna |
| `desktop-ollama-cloud` | Codex Desktop | Ollama Cloud `glm-5.2` |
| `zcode-luna` | ZCode | Official Luna |
| `zcode-ollama-cloud` | ZCode | Ollama Cloud `glm-5.2` |

Launch Desktop through the existing AppX `OpenAI.Codex_*` path. Linux using the `chatgpt` package does **not** retire this gate.

### 4. CLI clients (Windows `-CliOnly` still valid)

Codex CLI, OpenCode, Pi, OMP: Official Luna + Ollama Cloud. OpenCode/OMP/ZCode config semantics are still takeover until #435, except Pi activation which already stopped flipping.

## How to sync

```powershell
git fetch origin
git checkout codex/issue-430-dsh-client
# or rebase this branch onto the current Windows main if your host is behind
```

Linux packaging dry-run (optional on Windows via WSL/pwsh only; do not treat it as the Windows gate):

```bash
./scripts/build-linux-portable.sh --dry-run
python3 scripts/e2e_linux_gui_clients.py --detect-only
```

Windows gate remains:

```powershell
.\scripts\Run-RealClientE2E.ps1   # full GUI matrix when Desktop/ZCode are in scope
.\scripts\Run-RealClientE2E.ps1 -CliOnly
```

## Do not

- Do not rename Codex Desktop to ChatGPT in UI, i18n, or evidence.
- Do not flip DSH or Pi model-selection on Connect.
- Do not use this Linux user profile, `~/.dsh`, or live Gateway key as Windows evidence.
- Do not skip Windows DSH inject/detach because Linux already passed.

## Linux already recorded

- Isolated apply/readback: Codex overlay, ZCode, OpenCode, OMP, DSH.
- DSH live inject/detach against `~/.dsh` with backup/restore.
- Linux GUI preflight report: `/tmp/linux-gui-e2e-full.json` on the Linux host (detect + isolated apply + isolated launch).
