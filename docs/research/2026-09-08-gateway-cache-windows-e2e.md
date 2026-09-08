> 历史验收记录：下文结果仅适用于文中注明的 2026-09-08 候选与诊断运行；其中“当前”“未修复”等表述保留当时语境。2026-09-09 的修复与验收以新的收尾报告为准。

# Gateway 缓存修改的 Windows E2E 验证

**最新结果：OMP 会话头修复后，正式 Windows CLI E2E 8/8 通过；详见文末修复记录。**

日期：2026-09-08（Asia/Shanghai）。本轮由用户明确要求在 Windows 上开展 E2E，范围承接工具别名、上游缓存 key 遥测、端点缓存控制和缓存写入用量两批修改。

## 候选与环境

- Windows 测试主机为既有 专用 Windows 测试主机，系统 build 为 `10.0.26200.0`。
- 代码从 Linux 工作区 `fb664ed727bdf454c972528423e476bc7a268f95` 加当前工作区改动冻结为独立、签名测试快照 `9445702e7bca52d457d9ceac6eea1dc6f95e94f4`。快照包含原本已存在的工作区改动，因此不将所有候选行为归因于缓存修改。
- 远端隔离 checkout 为 `D:\Workstation\CodexHub-cache-e2e-9445702e`；706 个文件逐项 SHA-256 校验与导出清单一致。两端原工作区均保留。
- 测试使用仓库 `scripts/codexhub-python.cmd`，已验证开发运行时为 Python 3.13.11、pytest 9.1.1。构建工具为 Node 25.7.0、npm 11.10.1、Cargo 1.97.1、Tauri CLI 2.11.4。
- Windows 无人值守检查全部由仓库 `run-with-windows-watchdog.py` 限时；回归测试上限 1,200 秒，synthetic E2E 上限 3,600 秒，portable 构建上限 5,400 秒。
- 真实客户端使用专用 E2E 登录和 OpenCode Go 凭据的独立副本，以及本轮新生成的本地 Gateway key。没有读取日常客户端会话，没有启动 Desktop/ZCode GUI。

## 已完成的验证（首次运行，后续完整结果见文末）

Windows 缓存及相关协议回归：**438 passed、2 skipped、13 subtests passed（18.24s）**。覆盖 `test_gateway_prompt_cache.py`、exchange、runtime tool compatibility、事件日志、Chat Completions 和第三方 reasoning 请求边界。跳过项为未开启的可选 live probe。

完整 Windows synthetic E2E：**151 passed、1 failed（1,951.59s）**。失败项为 `test_candidate_bootstrap_does_not_discover_or_reuse_ambient_host_state`，具体原因见后文。

前端依赖按锁文件安装完成。使用 PowerShell 7 的 Windows Debug portable 构建成功，版本为 `0.2.1`，归档 SHA-256 为 `2667fa3920d9e9614387b11d78d8ae857dff8f12826682df44a49b0cccd57ec5`。真实 CLI E2E 在候选启动阶段失败，八组计划用例均未执行（case_count = 0），因此本轮 Windows E2E 未通过。75 个打包 Python 源文件已逐项与冻结快照校验一致。

## 构建时发现的问题

Windows PowerShell 5.1 经 Python watchdog 启动后，`Prepare-PythonRuntime.ps1` 调用 `Get-FileHash` 失败，导致 portable 构建尚未进入正常完成状态。

最小对照中，直接启动 PowerShell 5.1 执行 `Get-Command Get-FileHash -ErrorAction Stop` 成功，经同一仓库 launcher/watchdog 启动则失败。进一步实际执行文件哈希，继承路径时得到 `CommandNotFoundException`，可用 Utility 模块列表先出现 PowerShell 7 的模块，再出现 Windows PowerShell 系统模块；在该 5.1 子进程内将 `PSModulePath` 限定为 `%WINDIR%\System32\WindowsPowerShell\v1.0\Modules` 后，同一文件哈希计算成功。

因此已确认这个命令失败与跨版本模块路径继承有关。仅在外层删除 `PSModulePath` 不足以使完整 5.1 构建通过；不能把那次最小探针通过描述成完整打包修复。没有修改系统或仓库的模块设置，完整候选继续使用脚本支持的 PowerShell 7 构建，保留两次 5.1 构建失败记录。

这是 Windows 打包/验收路径上发现的问题；目前没有证据将其归因于本轮 Gateway 缓存代码修改。

## Synthetic E2E 的隔离失败

唯一失败场景中，模拟客户端的 12 项内部用例全部通过，`summary.json` 的 `failure_classification` 为 `none`，但调用者的模拟 `CODEX_HOME` 被创建了 `proxy` 子目录，违反“不读取或写入调用者会话目录”的验收约束。

定位到 [Run-RealClientE2E.ps1](../../scripts/Run-RealClientE2E.ps1) 的 `Invoke-XaiGrokToolsPreflight`：它仅取消 `CODEXHUB_E2E_XAI`，随后直接 `Start-Process`，未为此预检子进程建立其他客户端所使用的隔离环境。预检调用 [e2e_xai_grok_tools.py](../../scripts/e2e_xai_grok_tools.py)，其兼容性入口会触发 Gateway 事件记录。

进一步在全新临时目录中仅运行该离线预检，不启动任何客户端或模型请求，就能复现：脚本退出码为 0，调用者目录却新增 `proxy/codex-proxy-events.jsonl`。因此这不是只能通过整套 E2E 偶然观察到的超时问题。建议让该预检使用现有的隔离子进程环境构造入口，并保留当前失败测试作为回归约束。

本轮真实 CLI 验收显式将外层 `CODEX_HOME`、runtime home 和 Codex target home 指向全新输出下的专用预检目录，限制该副作用的落点。这是本次运行环境的防护措施，没有修复仓库脚本，也不能据此将完整 synthetic gate 标为通过。


## 真实 CLI E2E 启动失败与诊断边界

四个原生客户端版本预检通过：Codex CLI 0.153.4、OpenCode 1.18.21、Pi 0.80.6、OMP 17.0.3。正式运行在 5,541 ms 后报告 `candidate_gateway_bootstrap_failed`；真实模型用例执行数为 **0**。启动诊断中的进程、监听和健康状态均为 false；这些是 bootstrap 错误分支写入的状态，不能据此单独推断 Python 根本未启动。

随后在同一个失败运行的隔离 candidate 目录，通过独立子进程探针重放候选的 `refresh-models`，18.43 秒后以退出码 0 完成。探针未匹配到 `codex_desktop_restart_required` 等错误信号。因此“被正在运行的 Codex Desktop 阻挡”没有得到验证，不能作为已确认根因。独立重放也不等价于 runner 完整路径重试成功；正式运行的启动失败根因仍未确定。

当前 runner 将非特定错误归并为通用 bootstrap 分类，汇总未保留可用于定位的底层退出码和脱敏错误码。建议补齐该诊断，再在全新隔离输出目录重跑正式八组 CLI 用例。不得复用已经执行诊断的目录冒充新一轮验收，也不应通过自动关闭用户 Desktop 来绕过检查。

## 结论与证据

本轮没有修改业务代码。确认的问题是 xAI 离线预检污染调用者目录，以及 PowerShell 5.1 构建时跨版本模块路径继承导致命令加载失败；此外真实 E2E 存在尚未定位的启动失败。现有证据不能证明这些问题由缓存修改引入，也不能证明真实上游缓存命中率或 token 费用已经改善。

- [结构化验证汇总](../evidence/gateway-cache-windows/2026-09-08/verification.json)
- [真实 CLI 运行汇总](../evidence/gateway-cache-windows/2026-09-08/summary.json)
- [首次启动失败诊断](../evidence/gateway-cache-windows/2026-09-08/candidate-startup.json)
- [独立重放脱敏结果](../evidence/gateway-cache-windows/2026-09-08/bootstrap-probe.sanitized.json)

完整回归日志保留在 Windows 的 `D:\Workstation\codexhub-real-client-e2e\cache-9445702e-checks`；真实 CLI 运行目录为同级 `cache-9445702e-live-r1`。仓库仅收录脱敏汇总，不收录专用认证文件或原始模型输出。


## 追加启动专项调查

沿用冻结候选和实际 runner 函数，完成以下对照。全部是 bootstrap 诊断，不是完整 E2E 验收：

| 对照 | 退出码 | 耗时 | 脱敏证据 |
| --- | --- | --- | --- |
| PowerShell 7，原 Invoke-IsolatedProcess，已有候选目录 | 0 | 13.809 秒 | [结果](../evidence/gateway-cache-windows/2026-09-08/runner-bootstrap-probe.sanitized.json) |
| PowerShell 7，原初始化/进程函数，全新无模型缓存目录 | 0 | 13.766 秒 | [结果](../evidence/gateway-cache-windows/2026-09-08/runner-bootstrap-cold-probe.sanitized.json) |
| PowerShell 5.1，原初始化/进程函数，全新无模型缓存目录 | 0 | 13.917 秒 | [结果](../evidence/gateway-cache-windows/2026-09-08/runner-bootstrap-cold-ps5.sanitized.json) |
| 完整 supervisor、5.1 worker、预检、嵌套 Job Object，全新输出 | 0 | 13.472 秒 | [结果](../evidence/gateway-cache-windows/2026-09-08/bootstrap-attempt-1.sanitized.json) |

前三组从冻结脚本提取原函数执行；第四组使用临时脚本副本，仅增加 bootstrap 返回值的脱敏记录，并在 bootstrap 成功后以专用标记 `candidate_diagnostic_bootstrap_completed` 主动停止，未执行模型用例。该临时 runner 已删除，业务代码和正式 runner 均未修改。所有组 stderr 为空，无超时。认证副本与首次运行输入逐项文件哈希相同，未发生认证文件刷新；全新目录未拷贝模型缓存。

专项调查将失败阶段确定为 `Invoke-CandidateOfficialBootstrap` 调用的 `CodexHub.exe refresh-models`，早于 Gateway 服务启动与模型用例。现有诊断时间与代码路径排除了前置 auth 文件复制校验分支，但首次底层退出码和 stdout/stderr 没有被 runner 保存，无法从通用分类逆推出原因。

本轮未复现首次失败。对照表明同一候选、认证、冷启动状态和完整 runner 链路目前均能成功；不能据此证明首次是网络瞬断，也不能追认 Desktop 阻挡。建议为正式 runner 的 bootstrap 失败分支保存数值退出码、超时标志和白名单错误分类，以便再次失败时区分 Desktop 协调、认证、官方目录获取及本地启动错误；不应保存未经脱敏的完整输出。


## 正式完整重跑：八组已执行，七组通过

用户要求跑通执行链路后，使用原始冻结 runner、同一候选 SHA 和同一 portable，在全新 `cache-9445702e-live-r2` 输出目录执行完整 CLI-only E2E。没有复制旧模型缓存、修改正式 runner、替换客户端或放宽断言。此次官方目录刷新和 Gateway 启动均成功，八组全部执行；此前的启动失败未复现。完整结果为 **7 passed、1 failed**，不是全绿。

| 客户端 | 官方 Luna | OpenCode Go Muse Spark |
| --- | --- | --- |
| Codex CLI | 通过 | 通过 |
| OpenCode | 通过 | 通过 |
| Pi | 通过 | 通过 |
| OMP | 通过 | HTTP 400，MissingSessionID |

七组成功用例均完成一次只读工具调用、哨兵响应校验和流式终止校验；HTTP 200，无协议 fallback、错误事件、重复终止或重连。失败用例 OMP → OpenCode Go 在首次请求返回 HTTP 400，未产生工具调用。整体观测到 15 个请求开始和 15 个请求完成事件。

### 已确认的失败原因

OMP 原生客户端在隔离目录自动保存了 HTTP 400 的错误记录。只提取并脱敏其错误响应后，明确得到：

> Error from provider (Console Go): Request is missing x-opencode-session and cannot be routed efficiently. (type=MissingSessionID)

该请求含 `prompt_cache_key`，但没有满足上游的会话头要求；不是用例超时，也没有证据表明此次 400 由工具 schema 导致。当前 Gateway 通用上游头构造会转发来路头，但没有 `x-opencode-session` 的补齐策略。后续修复应针对 OpenCode Go 的实际端点，从稳定的客户端会话标识建立上游路由会话头，保留客户端已提供值；不能每个请求生成新值或给所有会话共用固定值，否则可能进一步破坏缓存亲和性。这次未修改业务代码，问题仍待修复。

### 真实缓存观测

合并 `request_complete` 与异步 `usage_observed` 后，OpenCode Go 三个成功客户端的后续请求均报告了缓存输入 token：

| 客户端 | 后续请求输入 token | 其中缓存输入 token |
| --- | ---: | ---: |
| Codex CLI | 11,120 | 10,865 |
| OpenCode | 7,795 | 7,537 |
| Pi | 1,244 | 1,137 |

八个官方请求报告的缓存输入 token 均为 0；这一轮短测试不足以区分上游缓存策略、路由或预热条件，不能据此断言 Gateway 破坏官方缓存。三方成功请求没有报告 cache-write 字段，保留未知语义。全部 15 个请求均记录 `prompt_cache_key_state=present`；这只证明 key 存在，不单独证明其跨请求稳定性或费用收益。本轮无修改前同条件基线，不能计算节省比例。

- [本轮正式汇总](../evidence/gateway-cache-windows/2026-09-08/live-r2/summary.json)
- [脱敏错误和最终用量](../evidence/gateway-cache-windows/2026-09-08/live-r2/analysis.sanitized.json)
- [OMP MissingSessionID 证据](../evidence/gateway-cache-windows/2026-09-08/live-r2/omp-error.sanitized.json)


## OMP 修复完成：正式 CLI E2E 8/8 通过

修复候选为独立验证仓库的签名提交 `b27449e051812342ea53a918ceb99c715112dac3`，基于前述冻结候选，仅增加两份生产 Python 文件的修改和一个回归测试文件。用户工作区保留源码改动，没有推送或替换用户生产安装。

`gateway_handler_impl.py` 将已有 caller `prompt_cache_key` 交给路由头绑定；`gateway_transport.py` 在冻结各次上游尝试的请求头时，对 `https://opencode.ai/zen/go/v1/` 下默认 HTTPS 端口的端点补齐 `x-opencode-session`。已有会话头优先保留，缺失时先取客户端会话头，再取 cache key。派生值以供应商认证值为 HMAC 密钥，按账号隔离且随会话稳定，不明文暴露原始 cache key。没有会话标识时不凭空创建随机或全局共享 ID；其他端点不新增该头。请求正文和 prompt cache key 均未修改。

先运行新测试确认缺少路由头的失败，再实现修复。14 项回归覆盖同会话稳定、不同会话/账号隔离、显式头的大小写保留、原生会话头优先、Unicode/控制字符安全派生、相似域名/非 HTTPS/非默认端口/其他路径排除，以及缺少标识时不生成随机值。针对性四套测试 85 passed。

完整检查：

- Linux Python core：**2182 passed、169 skipped、263 subtests passed**，66.03 秒。
- Windows Python core：**2315 passed、36 skipped、263 subtests passed**，238.03 秒。
- Windows Debug portable 重新构建成功，两份修复源码与打包文件逐项 SHA-256 校验一致。
- 正式 Windows CLI-only E2E：**8 passed、0 failed**。Codex CLI、OpenCode、Pi、OMP 的官方与 OpenCode Go 路径全部通过，未修改 runner 或放宽断言。
- `git diff --check`、证据 JSON/链接/八项断言一致性验证通过；report-only quality report 已执行。

此前 OMP → OpenCode Go 的 `MissingSessionID` 已消失：本次耗时 11.143 秒，HTTP 200，一次只读工具调用，两个 Gateway 请求均正常完成，无错误、回退、重连或重试。其后续请求记录 3,848 个输入 token，其中 **3,697 个为缓存输入 token**。这同时验证了修复后的真实工具闭环和一次缓存命中；不作为长期命中率或费用节省比例。

本次 Codex CLI 官方后续请求也观察到 7,936 个缓存输入 token，因此上一轮“官方请求均为零”不能解释成确定的 Gateway 缓存破坏。仍没有同条件费用对照实验。

构建环境另有一次辅助进程清理：编译和归档已成功，但本次 Visual C++ 的 `vctip.exe` 留在看门狗 Job Object 中，使看门狗等待。核对其 PID、名称和精确创建时间后，只终止该构建辅助进程，看门狗正常返回；未停止用户 Desktop 或生产 Gateway。未修改看门狗脚本。

历史 synthetic E2E 的 xAI 预检隔离问题不在本修复中；本轮未重跑或声称该套 synthetic gate 已变绿。

- [修复后正式汇总](../evidence/gateway-cache-windows/2026-09-08/omp-fix/summary.json)
- [修复后脱敏缓存遥测](../evidence/gateway-cache-windows/2026-09-08/omp-fix/analysis.sanitized.json)
- [回归测试](../../tests/test_opencode_session_headers.py)
