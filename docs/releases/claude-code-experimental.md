# Claude Code Gateway client (experimental)

Candidate code SHA: `fae658a2c523ec8431ba0959bf4da22756903bb3`.
Verified Claude Code versions: **2.1.280** for subscription, picker, switching
and compaction; **2.1.281** for the live external-provider routes on Linux. This is candidate evidence,
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
  requests. Official DeepSeek Flash Messages/Chat, balance and Codex Luna
  Responses with max effort passed on this candidate. The combined settings flow
  passed in an isolated browser with a synthetic saved Claude login. The
  candidate code SHA passed Linux and Windows language suites and produced
  Linux and Windows portable builds.
- A full native → external → native switch in one Claude Code 2.1.280 terminal
  process passed on this code SHA. A separate bounded session
  recorded an automatic `compact_boundary`, complete persisted usage for
  five native requests and marker recall after compaction. Manual `/compact`
  also retained native Haiku, persisted summary usage and recalled the marker.
  Too-long recovery, inherited/pinned subagent selection, live tool/error/
  cancellation paths, real cache-hit reuse, every subscription model/version
  and Windows live subscription use remain unverified.
  Non-Claude routes are experimental compatibility, not Anthropic-endorsed
  support.

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
  官方 DeepSeek Flash 的 Messages/Chat、余额查询及 Codex Luna Responses max effort
  也在此候选版验证。组合设置流程通过隔离浏览器和合成 Claude 登录验证。
  最终代码 SHA 通过 Linux/Windows 语言测试，并生成两平台 portable 包。
- 后续在同一代码 SHA 上，Claude Code 2.1.280 的单进程原生 → 外部 → 原生
  交互切换已通过。另一组有界会话记录了自动 `compact_boundary`、五次原生请求
  的完整持久化用量及压缩后的标记回忆。手动 `/compact` 也保留原生 Haiku、记录摘要
  用量并找回标记。超长恢复、继承/固定子代理模型、真实工具/错误/取消路径、缓存复用命中、
  全部订阅模型/版本和 Windows 真实订阅调用仍未验证。非 Claude 路由仍属实验性
  兼容，不代表 Anthropic 官方支持。

具体模型、测试上限及未验证范围见[候选证据](../evidence/issue-564/README.md)。
