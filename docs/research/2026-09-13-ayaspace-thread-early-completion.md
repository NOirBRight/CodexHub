# AYASpace 持续停止：xAI 原生搜索与客户端 view_image 名称冲突

对象：01a07e5b-377c-73d0-a066-128816e481fc（优化整体软件架构）。核查日期：2026-09-13，时间为北京时间。

## 已确认结论

**本例不是工具数量本身过多，也不是模型上下文到顶，而是 xAI 原生 web_search 与客户端 view_image 同时声明时的名称交互问题。** 真实 API 对照中，这个组合导致只有 reasoning 或计划性正文的完成响应，缺少客户端工具调用；只把客户端工具名改成 codex_view_image，保留搜索、历史、指令和其他工具，就恢复调用。

已将复现从约 424 KB、284 个工具缩小到 **963 字节、2 个工具、约 1,672 input token**。最小失败请求及成功请求只差 /tools/0/name，分别为 view_image 和 codex_view_image。三组请求均表现为前者空完成、后者产生工具调用。这推翻了排查中“减少工具后恢复，因此可能是数量/上下文负载”的初步解释。

[xAI Web Search 官方文档](https://docs.x.ai/developers/tools/web-search) 明确说明网页搜索的 image understanding 使用服务端 view_image 工具。文档没有声明该名字是保留名，也没有记载这里的故障；**同名组合产生故障及改名恢复来自本次真实请求证据**。最简单的 web_search 声明已能触发问题，无须显式设置 enable_image_understanding。目前不能观察 xAI 内部究竟在生成、工具选择还是服务端分发阶段处理错了这个名字。

## 真实 E2E 对照

共完成 37 次有界真实 xAI 请求，用于基线、反向验证、工具集合二分和最小化。所有客户端工具调用只捕获、不执行；没有向原线程发消息、操作真机、修改运行配置或认证文件，也没有重启服务。

| 输入条件 | 工具数量 | 是否有客户端工具调用 | 次数 |
| --- | ---: | --- | ---: |
| 重建历史 + 完整目录，含 web_search、view_image | 284 | 否；reasoning-only 或只说下一步 | 3/3 |
| 相同历史，仅 exec_command、write_stdin、view_image | 3 | 是，view_image | 3/3 |
| 上一组仅加回 web_search | 4 | 否，reasoning-only | 3/3 |
| 完整目录仅移除 web_search | 283 | 是，view_image | 3/3 |
| 完整目录只改 view_image 名称，保留搜索 | 284 | 是，codex_view_image | 3/3 |
| 4 工具目录只改 view_image 名称，保留搜索 | 4 | 是，codex_view_image | 3/3 |
| 全新合成提示，只有 view_image + 原生搜索 | 2 | 否，reasoning-only | 3/3 |
| 上一组只改 view_image 名称 | 2 | 是，codex_view_image | 3/3 |

完整目录的改名对照唯一修改为 /tools/8/name；最小对照唯一修改为 /tools/0/name。模型、推理强度、用户输入、系统指令、历史、工具描述与参数 schema 均不变。

删除 reasoning 历史、删除助手计划性消息、加强最后一条提示、设置 tool_choice=required 或删除最大的单个工具 schema，均未使完整目录恢复调用；这些是单次定位样本，不是对所有场景的结论。二分后失败持续跟随包含原生 web_search 的分组，仅加回搜索即可复现，才完成变量缩小。

改成短历史和更具体的用户输入时，完整目录能产生其他工具调用。因此这不是“有搜索就会使全部函数失效”的泛化故障。移除搜索后仍有 109,414 input token 却能调用工具，而最小失败仅约 1,672 input token，也不支持上下文大小是主因。

## Gateway 是否丢失工具调用

基线加载当前运行包 /tmp/.mount_CodexHGGKBjG/usr/lib/CodexHub/src-python/，经过真实 Gateway HTTP 请求处理路径。隔离 localhost 转发器捕获准备后的请求，使用已有 xAI 会话请求 https://api.x.ai/v1/responses，记录原始 SSE，再交给同一个 Gateway 请求的响应适配流程。

上游和下游均有 39 个 JSON SSE 事件，**解码后逐项完全相同**，只有 reasoning 和 response.completed，没有 function_call。因此这次不是 Gateway 在响应转换阶段丢掉了模型已生成的调用。其余改名/移除搜索等对照直接请求 xAI，不经过 Gateway 响应转换。

取证转发器先收集完整 SSE 再交给 Gateway，验证的是事件语义和适配，不模拟生产逐事件到达延迟。请求有 socket 超时、外部进程时间上限和 2 MiB 响应上限，不允许自动无界重试。

## 为何没有逐步披露

这条 Grok 路径实际使用完整目录：

- 原请求 a1786e003f76 的 Route Plan 是 tool_surface_strategy=eager、tool_protocol=responses_structured。
- 原始日志记录每次 292 个上游函数工具。
- 当前模型目录中 xai/grok-4.6 的 supports_search_tool=false。
- 捕获目录和重建请求没有 defer_loading 标记，出站没有 tool_search 查询入口。
- config/providers.toml 的 xAI 没有覆盖默认策略；src-python/providers_config.py 默认 eager，src-python/catalog_sync.py 的外部模型模板默认 supports_search_tool=false。

这与逐步披露的预期不一致，是需要单独处理的工具暴露策略问题。但它不是“两个工具也停止”的主因，不能以开启逐步披露替代同名问题修复。

## 原线程和上次修复

原始文件：/home/noirbright/.codex/sessions/2026/09/08/rollout-2026-09-08T08-11-42-01a07e5b-377c-73d0-a066-128816e481fc.jsonl

| 时间 | 请求 | 原始行为 |
| --- | --- | --- |
| 16:05:23–16:05:43 | fc3793429fac | 准备看截图，零工具调用，task_complete（8537 行） |
| 16:05:56–16:06:13 | 4d57c6b7f4dd | exec_command 成功，needs_follow_up=true，客户端继续请求 |
| 16:06:14–16:06:23 | 60ade9ddd8a4 | 只有 reasoning，无正文或新工具，空 task_complete（8556 行） |
| 16:24:08–16:24:16 | a1786e003f76 | 准备看截图，零工具调用，task_complete（8568 行） |

全部请求 HTTP 200；结束点均有 needs_follow_up=false、token_limit_reached=false、full_context_window_limit_reached=false。8568 行全部可解析，索引游标位于 81,900,039 字节的文件末尾，无历史索引卡住。

15:44 的提交 a6434f8f5796882de7d0cdb166d03292c11582b5 允许已经开始输出思考摘要的响应正常转发 response.completed。它解决了反复重连，却没有处理上游缺少客户端工具调用的问题。tests/test_xai_empty_completed_stream_e2e.py 验证流正常闭合，不能代替持续执行验收。

## 脱敏证据与验证边界

- [逐次结果与请求/响应指纹](../evidence/ayaspace-view-image-collision/summary.json)
- [最小失败请求](../evidence/ayaspace-view-image-collision/minimal-collision-request.json)
- [仅改名称的成功请求](../evidence/ayaspace-view-image-collision/minimal-renamed-request.json)

两个最小请求是全新合成任务，不含原线程正文、截图或凭据；文件路径是合成工具参数。验收条件是响应包含相应客户端 function_call，不要求合成文件存在，也不执行调用。HTTP 200 或只有完成事件不算成功。

真实执行使用仓库启动器和外部 timeout。临时脚本为 /tmp/codex-thread-01a07e5b-diagnosis-20260913/live_probe.py，支持 tiny_collision、tiny_renamed、full_renamed_image 等场景及 --repeat 2。私有捕获位于同目录 private/，权限受限，没有提交到仓库。临时目录及 AppImage 挂载路径可能在重启后失效；仓库中的合成请求和摘要不依赖它们。

原请求体未保留，重建使用最后一次压缩后历史、截至第 8560 行的记录及当前工具目录（284 个，当时 292 个），不能称为原请求的字节级重放。最终的两工具合成复现消除了对该差异的依赖。

先前定向检查 tests/test_xai_empty_completed_stream_e2e.py 和 tests/test_stream_output_classification.py 共 15 项通过。本轮验证 JSON 有效、唯一改名差异、37 次摘要及关键三次对照统计，并检查 git diff --check。未改生产代码，没有运行完整语言套件，也没有宣称原线程或真机验收已经修复完成。

## 修复方向

保留原生 web_search，为发往 xAI 的客户端 view_image 使用不冲突的内部别名，返回 Codex 前还原工具名。映射还需覆盖后续历史中的调用名称和调用结果关联；应沿用请求级工具注册表与已有双向映射边界，不能只修改声明或全局禁用搜索。

后续已完成正式 Gateway 双向映射、历史回放回归，以及客户端逐步披露默认值和 E2E，见 [实现与验证](2026-09-13-client-tool-discovery-default.md)。**尚未部署生产或续跑原任务**。其他服务端工具名称是否也冲突需独立证据。

OpenAI Docs 的 [Troubleshooting](https://developers.openai.com/codex/app/troubleshooting/) 提供日志位置和卡住状态的排查入口；本例最终归因来自真实 xAI 对照与最小化结果。
