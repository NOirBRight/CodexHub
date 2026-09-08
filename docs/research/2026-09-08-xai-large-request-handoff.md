# Handoff：Grok 长会话请求断开、超时与客户端重试

## 接手任务

调查并修复任务 `01a07e5b-377c-73d0-a066-128816e481fc`（“优化整体软件架构”）切换 `xai/grok-4.6` 后推进极慢的问题。先建立有界、可重复的复现，再区分上游断线、Gateway 传输阶段与客户端外层重试责任。用户自行安排外部 agent；本文件不代表已派发。

目标任务 cwd 为 `/home/noirbright/Workstation/AYASpace 2`，最后业务请求是验证完整刮削、合集及模拟器启动。2026-09-08 19:33:46 北京时间从 Astra low 切换到 Grok xhigh。最后 commentary 约在 19:40，但之后仍有工具调用及 HTTP 请求，不能描述成完全停止。20:48:50 至 21:12:47 两次工具完成相隔约 24 分钟。

## 已确认的日志证据

统计截止 2026-09-08 21:26:42 北京时间（13:26:42Z），目标任务时间范围内有 104 个独立 request_id：72 个 200、11 个 500、11 个 502、9 个 504、1 个 pending。时间边界可能包含切换时正在完成的请求，进一步分析应以每条请求的路由字段确认模型。

最后完成 usage：input_tokens=212889、output_tokens=321、reasoning_output_tokens=111、cached_input_tokens=212224，model_context_window=475000。最后请求体 6,392,034 bytes，历史包含截图。

错误包括 HTTP 500 `Internal Server Error`、`URLError` 包裹 `BrokenPipeError: [Errno 32] Broken pipe`、`RemoteDisconnected: Remote end closed connection without response`、偶发 `SSLEOFError: [SSL: UNEXPECTED_EOF_WHILE_READING]`，以及 `gateway_pre_response_budget_exhausted`。长失败常耗时 188–321 秒；路由为 `https://api.x.ai/v1/responses`，request timeout 为 300 秒，配置的 open/relay attempts 为 5。

相同 body_sha256 会以新 request_id 再次提交，单组观察到最多 5 次。关联的 `upstream_retry_suppressed` 事件包含 retryable=false、retry_forbidden=true、retry_safety_class=unknown、failure_phase=unknown、attempt=1、max_attempts=5、failure_class=quick_transient。对应危险重试已被 Gateway 内层抑制，随后客户端仍重新提交请求。必须分别分析内外两层，不能把所有重试归因于 Gateway 循环。

19:30–21:35 的对照窗口按真实 UUID window_id 排除明显测试记录后，official 为 592 请求（585 个 200、3 个 502、1 个 499、1 个 504、2 pending）；xai 为 126 请求（82 个 200、14 个 502、8 个 400、11 个 500、10 个 504、1 pending）。xAI 的 400 包含另一个任务的加密 spawn 复现，不属于目标任务的长请求故障。Gateway `/health` 持续 200，其他官方任务正常。

初步按 payload 分组：1–4 MB 的 40 请求全部 200；超过 4 MB 的 59 请求为 24 个 200、11 个 500、10 个 504、13 个 502、1 pending。该初步分组含少量测试/复现数据，且没有控制模型、时间与图片数量，只能说明相关性，不能据此认定 4 MB 硬限制。

## 证据位置与读取注意

- 脱敏请求摘要：`docs/evidence/conversation-xai-timeouts/request-summary.json`。
- 会话：`/home/noirbright/.codex/sessions/2026/09/08/rollout-2026-09-08T08-11-42-01a07e5b-377c-73d0-a066-128816e481fc.jsonl`。
- Gateway 事件：`/home/noirbright/.codex/proxy/codex-proxy-events.jsonl`（调查时约 316 MB）。
- 文本日志：`/home/noirbright/.codex/proxy/codex-proxy.log`（约 34 MB）。

按 window_id、request_id 和 UTC 时间关联；部分 retry 事件只有 request_id。日志混有 `_FakeHTTPError`、`unknown/not-a-model` 与 fixture window_id，必须过滤。文本日志最近匹配的 ERROR/Traceback 有 8 月旧记录，不能算作当天故障。只输出结构、错误与统计；不要导出正文、截图、凭据或密文。

## 代码线索与待验证假设

`src-python/gateway_transport.py` 的 `transport_failure_phase()` 将泛化 OSError/URLError 归为 tcp_connect，BrokenPipe/RemoteDisconnected 因而可能出现误导性的建连阶段标签。`retry_safety_failure_phase()` 更保守：HTTPError 为 response_headers、gaierror 为 dns、ConnectionRefused 为 tcp_connect，普通 BrokenPipe/RemoteDisconnected 为 unknown 并抑制重试。不要依据前者标签直接断定建连失败。

建议按单变量构造有界合成测试：分别改变体积、图片数量、历史长度，记录上传开始/完成与首响应头边界；观察失败是否随体积而非 token 数增长。随后区分 xAI 服务、代理/网络上传断开，以及 Desktop 对 502/504 的重新提交行为。若补传输阶段事件，必须同时验证其诊断准确性与重试安全分类，不应将 unknown 改成可重试来掩盖问题。

尚未进行控制变量复现，尚未确认断线根因。这一调查没有修改传输/重试实现，没有修复目标任务，没有更换其模型、删除或压缩历史、停止任务、重启或部署 Gateway。

## 与 V2 工作的边界

早先 `encrypted_agent_message_unavailable` 属于官方父代理向第三方子代理交接密文的问题，已有独立的明文 namespace 适配和真实 spawn/followup E2E；与这里的大请求断线不是同一错误。相关说明在 `docs/research/2026-09-08-portable-collaboration-v2.md`，证据在 `docs/evidence/portable-collaboration-v2/`。

原 agent 继续处理 Luna、5.5 与第三方模型的 V2 默认值、详情页设置和列表标注；外部 agent 聚焦本文件的长请求/重试问题。共享 checkout 位于 `/home/noirbright/Workstation/CodexHub`，分支 `codex/gateway-prompt-cache`，存在大量已有未提交改动（含 gateway_transport.py）。开始前保存并审阅当前 diff，避免覆盖；不要将整个工作区 diff 视为自己的修复。

## 验收与约束

1. 提供能捕获原症状的有界复现及脱敏前后证据，明确根因或仍缺少的证据。
2. 验证传输阶段准确性、危险重试抑制以及客户端外层重试的最终行为；成功应包含真实工具调用继续推进，不能仅以 HTTP 200 为依据。
3. 根据 `docs/agents/verification-policy.md` 运行受影响的 targeted/full checks。Python 使用 `./scripts/codexhub-python.sh`，禁止环境中的裸 Python/pytest。
4. 不用缩短超时、删除历史或切换模型冒充根治；未经明确授权不改变目标任务或重启生产 Gateway。不得暴露凭据、绕过质量门或主动向主分支提交/推送。

## 接手进展（2026-09-08 晚）

未改目标任务，未重启或部署生产 Gateway。`gateway_transport.py` 上原有的 OpenCode Go session header 改动保留。

### 根因分层

同一条约 6.39 MB 的请求体在 8 次独立 `request_id` 后得到 HTTP 200（失败累计等待约 1368 秒），因此**不是硬体积上限**。目标任务窗口内 `<4 MB` 请求全部 200；失败全部落在 `>4 MB`。成功请求中位耗时约 24 秒，失败中位约 145 秒。

三层责任：

1. **xAI / 网络**：大 POST 在写 body 或等响应头时被对端断开（`BrokenPipe` / `RemoteDisconnected`）、返回 HTTP 500，或一直不回包直到 Gateway 180–300 秒预算（`gateway_pre_response_budget_exhausted` → 504）。官方对照窗口几乎全 200。
2. **Gateway 内层**：第三方 POST 的危险重试已被抑制（`retry_forbidden=true`）。旧诊断把 `BrokenPipe`/`RemoteDisconnected` 标成 `tcp_connect`，容易误判为建连失败；重试安全路径把它们当成 `unknown`，结论碰巧仍是「不要内层重试」。
3. **客户端外层**：Gateway 返回 502/504/500 后，Codex 用新 `request_id` 重提相同 body。这才是 20:48–21:12 约 24 分钟空窗的来源。内层没有循环这 8 次。

把 `unknown` 改成可重试会在写后重复提交，不能用来「加快」失败请求。

### 有界复现

本地 HTTP 环回（不打 xAI、不读会话正文）：

- 读完 body 后不回包 → `RemoteDisconnected`，阶段 `response_headers`，内层不重试。
- 读完请求头即断开、客户端仍在写 8 MB body → `BrokenPipe`，阶段 `request_write`，内层不重试。
- 200 且响应含 `function_call`（`exec_command`），避免「只有 HTTP 200」。

测试：`tests/test_standard_transport_disconnect.py`、`tests/test_gateway_transport.py`。脱敏统计：`docs/evidence/conversation-xai-timeouts/analysis.json`。

未做对真实 `api.x.ai` 的控制变量探针（体积 / 图片数 / 历史长度），因此不能证明 xAI 侧是负载、中间设备还是 TLS 半开连接。

### Gateway 修复（未部署）

第三方 **STANDARD** 路径（xAI、OpenCode Go、Kimi、Volcengine、MiniMax、Command Code、Ollama Cloud，以及任意自定义 Provider）不再每请求 `build_opener`。它与官方共用 [`gateway_http_pool.py`](../../src-python/gateway_http_pool.py) 的 urllib3 连接类、空闲淘汰、15 秒建连拆分和写预算，但使用独立的 `STANDARD_HTTP_POOLS`，按 `(proxy, origin)` 分键，**不**写入 `OFFICIAL_HTTP_POOLS`。第三方因此也认 `HTTP_PROXY`/`HTTPS_PROXY`（与官方同一套 `official_proxy_url`）。`BrokenPipeError` → `request_write`，`RemoteDisconnected` → `response_headers`，urllib3 吞掉写中 `EPIPE` 后再 `getresponse` 时仍保留 `request_write`。重试类为 `suppressed_post_write`，**不会**变成 `safe_prewrite`。测试里 patch 过的 `urlopen` 仍走旧入口。OpenCode Go 的 `x-opencode-session` 绑定在 [`opencode_go_session.py`](../../src-python/opencode_go_session.py)，由 `bind_route_plan_operational_authentication` 在调用时读取；这是既有路由亲和能力，不要为了「清理 pooling 范围」删掉。

这改善传输形状（复用热连接、丢掉空闲死套接字、诊断可见 `new`/`reused`）。它**不能**让 xAI 更常接受 6 MB 请求，也不能关掉 Desktop 对外层 502/504 的重提。生产 Gateway 仍跑旧代码，要生效需另一次授权重启/部署。

### 15 秒建连与官方 Codex 对照

15 秒**不是上传超时**。它只封顶 TCP+TLS 握手；写 body 和等响应头仍用 300 秒 request timeout / 180 秒 pre-response budget。本事故失败耗时 21–321 秒，说明已经连上，加长 15 秒不会让 6 MB 更稳。

Codex CLI（`openai/codex` `codex-rs`）：

- `HttpClientBuilder::connect_timeout` 注释写明只限制 connection establishment。API 默认是 `None`。仓库里显式 5 秒只给 LM Studio，不是 Responses 主路径。
- 流式 Responses 在响应头到达前**没有**总超时（GitHub issue 41985）；`stream_idle_timeout` 只在 SSE 开始之后生效。Gateway 的 180 秒预算比官方客户端更严，不是更松。
- zstd 只在 `enable_request_compression && ChatGPT/codex-backend && provider.is_openai()` 时启用。自定义 `model_providers`（含 CodexHub 注入的 `Codex Proxy`）走 `Compression::None`。上游 issue 41662 也写了这一点。
- 官方后端靠 zstd 减小 **WAN** 上传。对本任务抽样 `request_start`：`content_encoding=null`、`content_decoded=false`、`content_length≈6.38MB`、上游 `body_bytes≈6.39MB`。App→Gateway→xAI 全程未压缩。
- 官方 auto-compact 按 token（本仓库 overlay 为窗口 90%）。本次 `input_tokens=212889` / `model_context_window=475000`，约 45%，不会触发 compact。字节大是截图/base64，不是 token 触顶。

因此「让 xAI 更稳吃下 6 MB」不能靠加长握手，也不能指望 Codex 对第三方自动压缩。STANDARD 连接池已落地（全部第三方，不是 xAI 特例）：对齐官方可靠性，不减小 body。仍可选：在转发时压缩/降采样历史图片以减小 **byte** 而不是删会话。不要在未证实 xAI 接受 `Content-Encoding` 时盲加 gzip/zstd。

### 验证

`./scripts/codexhub-python.sh -m pytest -q` 针对：

- `tests/test_gateway_transport.py`
- `tests/test_standard_http_pool.py`
- `tests/test_standard_transport_disconnect.py`
- `tests/test_proxy_event_logging.py`
- `tests/test_diagnostic_recorder_gateway.py`
- `tests/test_transport_failure_analyzer.py`
- `tests/test_gateway_exchange.py`
- `tests/test_chat_completions_gateway.py`
- `tests/test_image_generation_gateway.py`
- `tests/test_websocket_transport.py`
- `tests/test_gateway_characterization.py`
- `tests/test_module_boundaries.py`
- `tests/test_entry_discipline.py`

全部通过。随后 `./scripts/codexhub-python.sh -m pytest -q --ignore=tests/test_real_client_e2e.py` 的 Python 核心套件也通过（2208 passed / 170 skipped）。
