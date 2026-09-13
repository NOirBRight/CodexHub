# xAI Chat Completions 端点与 view_image 冲突核查

2026-09-13。用户要求确认 xAI 是否只有 Responses，以及 Chat 端点是否存在相同工具问题。此轮只做官方文档读取与有界真实 API 对照，未修改生产配置或源码。

## 结论

xAI 同时提供 `/v1/responses` 和 `/v1/chat/completions`。官方将后者标为 legacy，Grok 4.6 的 Chat 调用示例仍存在，真实请求也返回 200。CodexHub 内置 xAI Preset 的 `available_upstream_formats=["responses"]` 是当前产品配置范围，不能等同于 xAI 没有 Chat 端点。

本轮没有在 Chat 端点复现 Responses 的同名组合故障。使用与原最小复现相同的本地图片请求和函数 schema，并设 `reasoning_effort=xhigh`：

| Chat 请求 | 实际结果 |
| --- | --- |
| 仅客户端函数 `view_image` | 3/3 HTTP 200，生成 `view_image`，finish_reason=tool_calls，SSE 正常结束 |
| 客户端函数 `view_image` + 客户端函数 `web_search` | 3/3 同上 |
| 客户端函数 `view_image` + Responses 原生 `{type:web_search}` | HTTP 422；未知工具类型，不进入生成 |
| 客户端函数 `view_image` + 旧 `search_parameters={mode:auto}` | HTTP 410；Live Search 已废弃 |
| 客户端函数 `view_image` + `{type:live_search,sources:[{type:web}]}` | HTTP 410；Live Search 已废弃 |

422 原文：`unknown variant web_search, expected function or live_search`。缺少 sources 的初步 live_search 探针先返回 422；补齐 sources 后确认是 410，不能把前一个 schema 错误误认成端点不支持。

410 原文：`Live search is deprecated. Please switch to the Agent Tools API: https://docs.x.ai/docs/guides/tools/overview`。

因此，当前可用的 Chat 工具路径是普通客户端函数调用；无法在此端点启用导致本次冲突的 Responses 原生 `web_search`。两个普通函数名称相同于这两个标识并不会在本轮对照中触发故障。无需据此将现有 xAI 名称适配无条件扩展到 Chat；“本次未复现”不代表对 Chat 任意工具组合的无缺陷保证。

## 文档交叉核对

- [Chat Completions（Legacy）](https://docs.x.ai/developers/model-capabilities/legacy/chat-completions)：提供 Grok 4.6 和 `/v1/chat/completions` 示例。
- [Responses 与 Chat 比较](https://docs.x.ai/developers/model-capabilities/text/comparison)：Responses 提供原生 agentic tools，Chat 列为 function calling only。
- [Chat REST 参考 Markdown](https://docs.x.ai/developers/rest-api-reference/inference/chat-completions.md)：仍列出 `search_parameters`，但真实端点已返回 410，不能只凭残留字段声明支持旧搜索。
- [Web Search](https://docs.x.ai/developers/tools/web-search)：说明原生搜索及服务端 `view_image`。xAI SDK 的 `client.chat.create` 方法名不等于 HTTP `/v1/chat/completions`。

## 证据与边界

[脱敏摘要及请求指纹](../evidence/client-tool-discovery/xai-chat/summary.json) 保留最终 9 条确认记录。相邻的 `*-request.json` 全是合成请求，无原线程历史、用户截图或凭据。图片路径为合成参数，所有函数调用只捕获、没有执行。普通函数对照均设 xhigh；原生 web_search 的 422 参数校验探针未指定推理强度。

临时复现脚本 `/tmp/codexhub-xai-chat-probe/probe.py` 使用仓库 Python 启动器运行，读取已配置 xAI 会话但不打印请求头，每次有超时与响应大小上限。探索阶段记录可能被同名最终对照覆盖，仓库摘要仅代表保留下来的确认样本，不声称收录全部探索调用。

本轮没有更改 xAI Preset 的协议选项，也没有部署或重启 Gateway。
