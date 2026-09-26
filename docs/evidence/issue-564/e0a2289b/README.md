# Packaged coexistence qualification, 2026-09-26

**Not a release approval.** The owner requested completing this branch's
verification, then combining it with the other session's fixes after they
stabilize. No publication, installation or merge is authorized by these results.
No failed or unverified gate is waived.

## Identity and scope

The tested Linux and Windows debug portable packages both contain product SHA
`e0a2289b61c2f6d454667e60871fb4bc13152aa4`, with version string `0.2.25`.
The installed `0.2.26` and the other session's subsequent fixes are separate.
The browser locator and Windows upstream-proxy harness delta is
`0c1c28a1bf32feadcc37e1ecb091edc8bd0ddc2b`; it does not relabel these packages.

| Artifact | SHA256 |
| --- | --- |
| Linux debug portable archive | `f5b00393d251aa1ac3975574416b9356021c09262d956b809b9b59ee6a6b4fde` |
| Windows debug portable archive | `29652e38199b33d1209e8ba99ba5bd0a86c685d2e554f790bfac31b885b5cf8c` |

| CLI | Linux | Windows |
| --- | --- | --- |
| Codex | 0.157.1 | 0.156.1 |
| OpenCode | 1.18.32 | 1.18.32 |
| Pi | 0.87.1 | 0.80.6 |
| OMP | 18.3.2 | 17.0.3 |
| Claude Code | 2.1.283 | 2.1.282 |

Windows' global npm Codex wrapper disappeared during an external update.
The original 0.156.1 binary was still available and was copied to the dedicated
lab before qualification. This campaign did not upgrade the clients.

## Results

| Gate | Result | Evidence / exact attribution |
| --- | --- | --- |
| Linux four CLI clients × two providers | Passed 8/8 | [d1a2ac4e baseline](linux-cli-d1a2ac4e.json); not relabeled as e0a2289b |
| Linux independent Chat | Passed 2/2 | [85f53d35](linux-chat-85f53d35.json); Official Luna and DeepSeek Flash |
| Windows independent Chat | Passed 2/2 | [e0a2289b, explicit test proxy](windows-chat.json) |
| Windows four CLI clients × two providers | Blocked before cases | e0a2289b bootstrap rejects the hidden helper in the current Desktop AppX manifest; fix b5eea871 requires a fresh package |
| Linux native Haiku → persistence → rendered Usage | Passed | [JSON](linux-native-haiku-ui.json), [packaged screenshot](linux-native-haiku-ui.png) |
| Native Opus 5.5 explicit-model resume | Passed on both OSes | [Linux](linux-opus-resume.json), [Windows](windows-opus-resume.json) |
| Windows native Haiku usage persistence | Passed | [JSON](windows-native-haiku.json); Windows rendered UI not tested |
| DeepSeek Messages/Chat and Luna Responses usage | Passed within recorded rows | [Linux](linux-provider-usage.json), [Windows DeepSeek](windows-deepseek.json), [Windows Luna](windows-luna.json) |
| Same-session native/external/native, tools, compact, resume | Passed on Linux | [20 requests](linux-switch-compact-resume.json) |
| Max-effort same-session switching across protocols | Passed on Linux | [16 requests](linux-switch-max.json) |
| Family mapping, upstream error and CLI cancellation | Passed on Linux | [8 requests](linux-mapping-error-cancel.json) |
| Combined settings UI and real CLI loopback roundtrip | Passed on Linux | Real e0a2289b binary; span-label locator delta in 0c1c28a1; synthetic upstream only |

The main same-session sequence is explicit `claude-opus-5-5` → `gpt-6-luna`
Responses → official `deepseek-flash` Chat → DeepSeek Messages → native Opus,
then manual `/compact`, post-compaction recall and
`--resume <session-id> --model claude-opus-5-5`. Every generation turn checks a
fresh Read tool result, matching tool IDs and the original session marker.
The max-effort run also covers DeepSeek Responses. Requested max effort is
recorded; this does not assert identical reasoning behavior across providers.

The separate edge run maps the Haiku family to DeepSeek while retaining explicit
native Opus selection. A deliberately invalid synthetic bearer produces an
executed native upstream 401. Terminating the real Claude CLI after its first
text delta produces Gateway completion 499. These are expected error outcomes,
not successful generation. Sixteen tool/recall turns across the three runs
passed; the combined request count is 44, including the expected 401 and 499.

Native cache reuse is observed, not inferred: the initial Opus turn reports
2,356 cache-write tokens, and its tool follow-up reports 2,356 cache-read tokens.
Explicit resume has its own persisted cache-read evidence. The rendered Haiku
row records input 29,493, output 84, cache-read 19,436 and cache-write 10,047.

## Local checks

- e0a2289b Linux Python core: 3,421 passed, 189 skipped, 283 subtests passed.
- e0a2289b Windows Python core: 3,546 passed, 64 skipped, 283 subtests passed.
- Windows full synthetic real-client contract: 153 passed on d1a2ac4e;
  five affected selections passed on e0a2289b. The full 0c1c28a1 rerun is pending.
- 0c1c28a1 explicit proxy tests: six passed on Windows, covering supervisor
  forwarding, candidate-only scope, rejection of non-loopback/credential URLs,
  and exclusion of ambient proxy settings.
- The browser settings regression passes preview, connect, edit, readback,
  disconnect and real Claude loopback generation. It uses synthetic data.
- The e0a2289b protocol delta and 0c1c28a1 harness delta each completed
  Standards/Spec review with no hard findings. Prior Rust, clippy, frontend and
  physical-pointer baselines remain incremental evidence at their original SHA.

## Isolation and limitations

The full Windows CLI gate also exposed a product defect: current Codex Desktop
26.917.6896.0 has both a visible application and a hidden command-runner helper.
The old resolver rejected both as ambiguous FullTrustApplication entries.
Reviewed fix `b5eea871` ignores manifest-hidden helpers while still requiring a
unique visible desktop; Windows behavioral fixtures and real manifest read-only
resolution pass. A new candidate is required; the e0a2289b gate is not waived.

Windows direct egress returned 403 for Official and native Anthropic requests.
The passing runs explicitly used an SSH reverse loopback tunnel to the Linux
host's existing HTTP proxy. TLS remains end-to-end to the real upstreams;
responses are not stubbed. The CLI matrix's proxy parameter applies only to
candidate Gateway/bootstrap processes; the four clients retain their isolated
Gateway connection settings. DeepSeek direct egress also passed independently.
The original expired Windows test login failed isolated refresh with
`401 / refresh_token_reused`; the owner completed a new dedicated device login.
No operator credentials were replaced.

All live runs use isolated homes/configuration/history/runtime/ports. Claude
snapshots contain access tokens only, and source fingerprints are checked.
The frozen [switch harness](codexhub-packaged-switch.py) and
[edge harness](codexhub-packaged-edges.py) require explicit candidate, source,
CLI and credential input paths. Their exact hashes are in each report.
They bound overall time to 900 seconds, waits to 120 seconds, requests below
32, and requested output to 2,048 tokens per request; observed complete usage
above that bound fails. Missing usage, including an interrupted response, is
not replaced by a fabricated token count. JSON retains counts, route identity
and status, never model text, tool-file contents or tokens.

Inherited/pinned subagents, too-long recovery, automatic compaction on these
current CLI versions, every native subscription version, and Windows rendered
Usage UI are **unverified**. Earlier automatic-compaction evidence remains
historical. This result set does not turn any such row into a pass or establish
universal third-party compatibility. The final combined product requires its
own required qualification before publishing matching Linux/Windows assets.

## 中文说明

本轮完成本分支验证，不发布、不安装、不合并。测试包对应 `e0a2289b`，
测试脚本增量对应 `0c1c28a1`；旧版本通过项保留原始 SHA，不冒充新包结果。
原生与第三方同会话切换、工具调用、手动压缩、显式恢复、缓存读写、
上游 401 与真实 CLI 取消 499 已取得 Linux 实测证据。Windows 的独立 Chat、
原生 Haiku/Opus 恢复及第三方用量入库已通过；八项 CLI 门禁在启动前发现新版 Desktop manifest 的隐藏 helper 兼容性问题，
已修复并通过定向测试，需重建候选包后继续。
Windows 通过项明确使用测试进程专用代理，未改系统网络配置或运行中的 Gateway。
表中未验证能力继续保持未验证，不作豁免。等待另一会话修改稳定后再合并，
并对最终共同候选验证、构建和发布。
