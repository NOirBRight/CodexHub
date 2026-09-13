# 协作历史兼容修复与目录连接接手

基线：`c230b23e9fbb0c4dd2cd8609b99ad4867ee4d941`。隔离分支：`codex/collaboration-catalog-recovery`。

## 运行证据与原因

2026-09-13（北京时间）本机 Gateway 日志和对应 rollout 显示：

- 22:51:55，父任务 `01a09b2c-1006-7060-97aa-094c5306c2f7` 成功创建 `/root/standards_review_round1`，下一次请求被 Gateway 以 400 `namespace_child_set_invalid` 拒绝。
- 子任务 `01a09b40-df6a-7871-b6b9-7d34f96fe0b4` 仍继续执行，直到 22:57:08 成功调用 `collaboration.send_message` 后，同样在下一次请求报错。子任务自己的结束事件确认了截图红色状态对应的失败。
- 因此父任务失败时的 `Stopped` 不能代表子任务当时已停止。Desktop 的状态展示属于客户端；本修复不引入 Gateway 代理调度或取消行为。

最小复现调用真实 `gateway_compat.compatible_request_body`：完整原生声明没有历史调用时成功；加入原生 `spawn_agent` 或 `send_message` 调用和结果后，被只认识平铺 Chat 工具的转换器误判为缺少声明。相同遗漏影响 V1 和其他命名空间的同名工具。

## 实现与相邻检查

- 已有原生声明的请求不再进入 Chat V2 六工具转换器；子任务 handoff 和已带命名空间的历史原样保留。当前声明仍为平铺 Chat 六工具时照常展开，不根据历史推断是否需要转换；原有声明、版本和历史校验继续执行。
- Chat 转换器只处理其拥有的平铺工具身份，保留其他命名空间的调用，避免按子工具名字误判。
- 原生协作调用与其他原生工具共存时，请求侧不再错误拒绝加密参数；Responses 响应侧保留加密标记。Chat 响应空标记可移除后转换，非空加密参数明确拒绝，避免按普通消息传递。
- 对缺失/重复子工具、丢失 namespace 的原生调用仍拒绝；测试覆盖父调用、子任务省略声明的续接、V1/V2、同名工具、加密字段、完整 Chat 输出转换。
- 私密哨兵测试确认，拒绝请求中的私密内容不进入兼容错误响应或记录的诊断事件。这是协作兼容链路的检查，不是全软件无泄露保证。

## 接手范围

接续原工作区的四个已修改文件、目录回归测试和原始诊断报告，未覆盖原工作区或纳入其余两份 E2E 研究文件。目录连接修复见 [原始诊断与实现记录](2026-09-13-codex-connection-bug-audit.md)：显式连接选用托管目录，保留恢复基线，断开恢复；路径含单引号/控制字符时生成合法 TOML。

复审新增修正：连接期间删除或清空目录设置，在断开及再次连接后仍保留该修改；整个配置文件缺失则继续从备份恢复，避免把丢文件误当成删除单个设置。文件缺失后再次连接也保留原备份，不用空配置覆盖唯一的恢复基线。

## 验证与交付边界

按 `strict` 验证。首次候选 `fe22852`：Python 核心 2718 passed / 177 skipped / 267 subtests passed；Rust 715 passed / 4 ignored；Clippy、前端构建、Linux 真实窗口物理输入 E2E 通过；前端契约 121 passed。报告式质量门解析错误 0。

审查和修复循环中，各项均先补充失败回归再修改：Chat 响应边界（`cb90a71`）、平铺声明带原生历史（`dfcae77`）、其他命名空间响应逆转换（`07f9a7c`）、目录删除恢复（`311d5fd`）、缺文件重连保留备份（`fd8565f`）。最终源码候选 `fd8565f88a15ce77b4bfb43d6cb64c875ea8e805` 的 Python 核心 **2729 passed / 177 skipped / 267 subtests passed**；目录删除修复后的 Rust 配置复查 **48 passed**。Rust 源码和前端未在首次全套后修改；首次全套/Clippy/构建/真实窗口结果继续适用。最终报告式质量门解析错误 0，`git diff --check` 通过。

日志：`/tmp/codexhub-recovery-verify-linux.log`、`/tmp/codexhub-recovery-ui.log`、`/tmp/codexhub-recovery-python-fd8565f.log`、`/tmp/codexhub-recovery-rust-config-final.log`、`/tmp/codexhub-recovery-quality-final.log`。仅保存隔离测试输出，未复制运行期敏感请求正文。

## Standards

规范轴发现并关闭两项：按历史协议误跳过平铺 Chat 声明展开；配置丢失后重连覆盖恢复备份。最终复核 `fd8565f`，288 项聚焦测试及 13 subtests 通过，未发现新增规范或泄露问题。此前对其他命名空间同名工具的泛化疑虑已撤回：完整 `namespace + name` 是不同身份，原生工具不属于 Chat 别名逆映射。

## Spec

需求轴发现并关闭两项：Responses 加密标记保留影响 Chat 转换；连接期间删除/清空目录后被恢复逻辑覆盖。最终复核至 `fd8565f`，177 项聚焦测试及 13 subtests 通过，未见剩余可操作问题。

双轴最终剩余问题：Standards 0，Spec 0。此结论限于本次修复范围，不代表全仓库无缺陷。

未替换本机安装版本、重启 Gateway、修改用户配置或继续失败的旧任务；未运行 Windows 或真实上游模型付费 E2E。源码验证通过不能等同于已运行版本生效。部署时需一起交付 Rust/Python 目录参数变更并重启 CodexHub Gateway；目录连接变更后按现有提示重启 Codex。
