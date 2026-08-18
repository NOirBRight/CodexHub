# 方案：Linux 上人手测 Codex App + ZCode GUI

本机安装包已编出（本地签名，不能当生产 updater）：

- `src-tauri/target/release/bundle/appimage/CodexHub_0.1.8-beta.4.2_amd64.AppImage`
- `src-tauri/target/release/bundle/deb/CodexHub_0.1.8-beta.4.2_amd64.deb`

四家 CLI 已用解包后的 AppImage 二进制跑过 apply/readback。Codex / OpenCode / OMP 的 live sentinel（`gpt-5.6-luna`）通过。Pi 0.79.3 低于门槛 0.80.6，live 400，apply/readback 仍通过。

下面两家 GUI 需要你在桌面里点完。

产品名继续叫 **Codex Desktop / Codex App**。本机安装包是 `chatgpt`，启动器是 `/usr/bin/chatgpt` → `codex-launcher`。ZCode 是 `/opt/ZCode/zcode`。

## 开始前

1. 确认 CodexHub Gateway 在 `127.0.0.1:9099` 已 running。
2. 不要用 `zcode --version`，会拉起完整桌面端并可能触发升级。版本用 `dpkg -s chatgpt` / `dpkg -s zcode`。
3. 建议先关掉已打开的 ChatGPT / ZCode，避免 Electron 单实例接到旧进程。
4. 先跑检测：

```bash
python3 scripts/e2e_linux_gui_clients.py --detect-only
```

本机已接受门槛：Codex Desktop ≥ `26.715.8383`，ZCode ≥ `3.3.6`。

## Codex App（Desktop）

目标：Official Luna + 一条第三方（Ollama Cloud / GLM-5.2，若已启用）。

1. CodexHub → Providers：把 Codex 连到 Hub（`switchMode` / Connect）。名字保持 Codex。
2. 打开 Codex App：Gateway 页点「打开 Codex App」，或终端执行 `chatgpt`。
3. 在 App 里选模型：
   - Official：`gpt-5.6-luna`
   - 第三方：Gateway 目录里已启用的 `ollama-cloud/glm-5.2` 或当前等价模型
4. 每个模型各开一个工作目录，放一个 `sentinel.txt`，内容一行可读。
5. 提示词明确写：只读 `./sentinel.txt`，把内容原样说出来，然后结束。只允许一次成功的只读 read。
6. 记录：
   - 模型 id
   - 是否走 `http://127.0.0.1:9099`
   - 是否只读了一次 sentinel
   - 是否需要重启 App（overlay 变更后通常要重启）
7. 不要改 `model_provider` 离开 `custom` 桶。Direct vs Hub 只体现在桶内 `base_url`。

## ZCode GUI

1. CodexHub Gateway 页把 ZCode 拨到 Connected（当前仍是 takeover 语义，直到 #435）。
2. 启动 `/opt/ZCode/zcode`（不要 `zcode --version`）。
3. 确认当前 bot/模型是 CodexHub 投影出来的 Official Luna 或 Ollama Cloud GLM。
4. 同样：独立目录 + `sentinel.txt` + 只读一次。
5. 若弹出升级/安装，**取消**。不要让它 `pkexec` 装新版本。
6. 记录版本（`dpkg -s zcode`）、模型、是否经 Gateway、sentinel 是否成功。

## 你交回的证据

每个 GUI case 三样即可：

- 截图：模型选择 + 回复里出现 sentinel 原文
- 一句话：`desktop-luna` / `desktop-third-party` / `zcode-luna` / `zcode-third-party` 过/不过
- 若失败：Gateway 日志时间点 + 客户端报错原文

不必改 Windows 矩阵名字。Windows AppX 门仍然独立。

## Linux GUI 结果（本机）

| Case | 结果 | 备注 |
|---|---|---|
| desktop-luna | 过 | Official `gpt-5.6-luna` |
| zcode-luna | 过 | Read 工具带 0 起算行号，不当失败 |
| zcode-third | 过 | `codexhub-ollama-cloud` 必须投影成 Responses |
| desktop-third | 过 | Codex App Ollama Cloud `glm-5.2` |

Windows 真机 E2E 已由 Windows 主机报过。

