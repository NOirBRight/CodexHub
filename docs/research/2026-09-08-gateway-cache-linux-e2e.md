> 历史验收记录：下文结果仅适用于文中注明的 2026-09-08 候选与诊断运行；其中“当前”“未修复”等表述保留当时语境。2026-09-09 的修复与验收以[新的收尾报告](../evidence/review-followup/2026-09-09/README.md)为准。

# 本机 Linux CLI E2E 验证

日期：2026-09-08。用户要求在本机补做 E2E，重点确认 OMP 会话头修复。

## 结果

**原始 Linux E2E 入口未通过；隔离生产 Gateway 子进程的诊断矩阵 8/8 通过。不能将诊断结果描述成原始 Linux gate 全绿。**

测试前按仓库入口自构建 Linux Debug 候选，使用当前工作区（包含已有未提交改动）。测试期间记录的跟踪源码哈希未变化。输入来自本机配置和认证的临时独立副本，复用官方模型目录以避免重启日常 Desktop。未替换生产安装，未修改业务代码或正式 runner。

本机客户端：Codex CLI 0.153.4、OpenCode 1.18.29、Pi 0.85.1、OMP 18.1.14，与 Windows 版本不同。

## 原始入口发现的生命周期问题

执行仓库 `scripts/e2e_linux_cli_clients.py`，构建成功，但原生 `codexhub start` 报告 running 后，runner 的健康检查失败，真实用例数为 0。对应 Gateway PID 后续已不存在。

当前 `src-tauri/src/proxy.rs` 的 Linux `configure_detached` 设置了 `PR_SET_PDEATHSIG(SIGTERM)`，而 `codexhub start` 是执行后立即退出的 CLI 父进程。这一生命周期约束与 runner 的后台启动方式冲突。

随后用私有 D-Bus/Xvfb 中持续运行的真实 CodexHub app 承载 Gateway：最初健康检查通过，但 Gateway 随后仍退出；客户端出现无法连接错误，Gateway 遥测没有模型请求。该诊断运行被主动中断并清理，未作为通过证据。Linux 父死亡信号还与创建线程生命周期有关，持续 app 场景的确切退出触发点仍需进一步验证，不能仅凭源码断言已经定位到线程退出。

## CLI 请求链路隔离验证

为区分生命周期故障和请求兼容性，使用临时诊断驱动仅替换 start/stop：由驱动直接持有当前生产 `src-python/codex_proxy.py` 子进程，结束时清理；保持同一自构建原生候选负责 managed apply/readback，复用仓库八个客户端启动命令和模型选择。

| 客户端 | 官方 Luna | OpenCode Go Muse Spark |
| --- | --- | --- |
| Codex CLI | 通过 | 通过 |
| OpenCode | 通过 | 通过 |
| Pi | 通过 | 通过 |
| OMP | 通过 | 通过 |

全部八组完成配置 apply、readback，并以退出码 0 返回预期 sentinel。本轮 Linux runner 的 live 判据是退出码与 sentinel，不等同于 Windows runner 对精确工具次数、流终止和回退计数的完整校验；没有将二者混为一套验收。

该对照证明 OMP 修复在本机请求链路上也能工作。要使原始 Linux E2E 真正通过，下一步应修复原生 Gateway 生命周期：区分短生命周期 CLI 的后台启动与应用持有的 Gateway，验证应用退出清理及创建线程退出场景，而不是取消用例断言或忽略健康检查。

## 证据与保护

[脱敏验证结果](../evidence/gateway-cache-linux/2026-09-08/verification.json) 记录候选二进制哈希、修复源码哈希、客户端版本和八组判据。原始报告含客户端输出和配置物化详情，仅保留在权限受限的 `/tmp/codexhub-linux-omp-e2e`，未写入仓库。中断后的客户端进程已检查，最终诊断 Gateway 由驱动 finally 清理。
