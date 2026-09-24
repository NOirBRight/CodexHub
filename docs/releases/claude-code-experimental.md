# Claude Code Gateway client (experimental)

Candidate code SHA: `c97d08897bdae3f56de6e3ce51235150356d8a43`.
Verified Claude Code version: **2.1.280** on Linux. This is candidate evidence,
not a stable release or the release-operator GO in #78. Earlier 2.1.278
compatibility results remain in the [support table](../evidence/claude-code-support-table.md).

## English

- Connect preserves the Claude subscription login and current default. Native
  full model IDs use the subscription; exported Gateway models use their
  configured Provider. Family mappings are optional and affect aliases, not an
  explicitly chosen full native ID.
- To resume an existing conversation with its original native model, use
  `claude --resume --model <original-full-id>`. The Claude settings page can
  copy this command after you enter the original ID. Plain `--resume` uses
  Claude Code's current default or family mapping; CodexHub cannot recover
  the earlier choice from the request.
- New connected native requests appear in Usage Statistics with request,
  input/output, cache-read and cache-write counts when upstream usage is
  available. `count_tokens` remains a local estimate, not billed usage.
- The isolated Linux packaged candidate passed real subscription Haiku through
  Gateway, persisted usage and the rendered Usage page. Creating and explicitly
  resuming an Opus 5.5 session retained `claude-opus-5-5` and recorded both
  requests. Official DeepSeek Flash Messages/Chat and Codex Luna Responses were
  exercised on an earlier candidate. The final code SHA passed Linux and
  Windows language suites and produced Linux and Windows portable builds.
- A full native → external → native interactive switch, automatic compaction,
  every subscription model/version and Windows live subscription use remain
  unverified on this candidate. Non-Claude routes are experimental compatibility,
  not Anthropic-endorsed support.

See [bounded candidate evidence](../evidence/issue-564/README.md) for the exact
model identities, test bounds and unsupported/unverified distinctions.

## 中文

- 连接保留 Claude 订阅登录和当前默认模型。完整的原生模型 ID 走订阅；Gateway
  导出的模型走各自配置的 Provider。family 映射可选，只影响别名，不覆盖显式选择的
  原生完整 ID。
- 恢复旧会话并保留原生模型时，使用
  `claude --resume --model <原始完整模型ID>`。在 Claude 设置中输入原始 ID 后可
  复制命令。单独 `--resume` 会采用 Claude Code 当前的默认值或 family 映射；
  CodexHub 无法从请求推断旧选择。
- 新的已连接原生请求会在用量统计中显示请求数、输入/输出及缓存读写 token，
  前提是上游返回用量。`count_tokens` 仍是本地估算，不是计费用量。
- Linux 隔离候选包已通过真实订阅 Haiku → Gateway → 持久化 → 打包版用量页面；
  创建并显式恢复 Opus 5.5 会话时，两次请求均保留 `claude-opus-5-5` 并记录用量。
  官方 DeepSeek Flash 的 Messages/Chat 和 Codex Luna Responses 在较早候选版验证。
  最终代码 SHA 通过 Linux/Windows 语言测试，并生成两平台 portable 包。
- 原生 → 外部 → 原生的完整交互切换、自动压缩、全部订阅模型/版本，以及
  Windows 真实订阅调用尚未在此候选版验证。非 Claude 路由仍属实验性兼容，
  不代表 Anthropic 官方支持。

具体模型、测试上限及未验证范围见[候选证据](../evidence/issue-564/README.md)。
