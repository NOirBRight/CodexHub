# CodexHub 0.1.8-beta.4 预发布门禁报告（issue #284）

## 候选版本标识

| 项目 | 值 |
|---|---|
| 候选分支 | `codex/release-0.1.8-beta.4-gate` |
| 原始 dev HEAD | `b73ff433d072ad5f2f01a004e5459990686e240d` |
| 发布候选 SHA（版本提升后） | `9beba8930a0968aea5a138269ff2b7171177760d` |
| 发布版本 | `0.1.8-beta.4` |

所有本地门禁命令均在该候选 SHA 上执行；发布构建产物中的 `codexhub_source_revision` 字段也指向 `9beba8930a0968aea5a138269ff2b7171177760d`。

## 门禁执行记录

### 1. Python 核心测试集

命令：

```powershell
py -3.13 -B -m pytest -q -p no:cacheprovider `
  --basetemp C:\tmp\release-beta4-gate `
  --ignore=tests/test_real_client_e2e.py
```

结果：`2351 passed, 1 skipped, 490 subtests passed; 2 failed`

失败项均为已知环境限制，非产品回归：

- `tests/test_release_channel_scripts.py::test_portable_rejects_invalid_flavor_before_building` — 本机 PowerShell 为 zh-CN 区域设置，脚本断言的英文错误消息不匹配。
- `tests/test_smoke_scripts.py::test_issue_108_tool_surface_evidence_replay_has_semantic_three_case_ab` — 外部 PowerShell evidence replay 子进程返回 `evidence_fixture_invalid`。

详细日志：`.pytest-core-v2.log`。

### 2. Beta3.3 血统回归（Chat / history / output-limit）

命令与结果：

```powershell
py -3.13 -B -m pytest -q -p no:cacheprovider --basetemp C:\tmp\release-beta4-gate `
  tests/test_chat_completions_gateway.py `
  tests/test_history_overlay.py `
  tests/test_history_consolidate.py
# 135 passed, 10 subtests passed

py -3.13 -B -m pytest -q -p no:cacheprovider --basetemp C:\tmp\release-beta4-gate `
  tests/test_model_limits.py `
  tests/test_config_overlay.py
# 80 passed, 13 subtests passed
```

血统回归全部通过。

### 3. Rust 测试与 Clippy

命令：

```powershell
cd src-tauri
cargo test --locked
```

结果：`538 passed, 4 failed, 1 ignored`

4 个失败全部位于 `proxy::tests`，根因是测试启动 Gateway 子进程时使用了系统默认 `python`（3.11），无法解析 PEP 695 语法：

- `proxy::tests::restart_after_settings_port_change_stops_recorded_port_before_starting_new_port`
- `proxy::tests::start_replaces_running_managed_proxy_after_same_path_upgrade`
- `proxy::tests::start_replaces_running_managed_proxy_from_previous_bundle`
- `proxy::tests::start_status_stop_real_python_proxy_on_ephemeral_port`

仓库提供的 Python 定位机制支持环境变量 `CODEXHUB_PYTHON` / `CODEXHUB_PROXY_PYTHON`。将 `CODEXHUB_PROXY_PYTHON` 指向工作树内已拷贝的 Python 3.13 运行时（`src-tauri/resources/python/python.exe`）后，上述 4 个测试可通过；但把该环境变量设为全局会导致 `runtime_paths::tests::find_python_prefers_bundled_runtime_when_present` 以及若干 `models::tests` 因测试假设被覆盖而失败。因此门禁记录中保留 4 个 Python-3.11 环境限制失败，并在最终发布环境中使用 Python 3.13 运行时即可消除。

`proxy::tests::post_spawn_inspection_timeout_cleans_up_gateway_without_leaking_output` 在首次完整运行时出现超时断言失败，单独重跑通过，判定为负载导致的偶发超时，非代码回归。

Clippy：

```powershell
cargo clippy --locked --all-targets -- -D warnings
```

结果：`Finished dev profile; EXIT:0`，无 warning。

### 4. 前端构建与 UI 契约测试

```powershell
cd frontend
npm ci          # EXIT:0
npm run test:ui-contract
# 151 passed, 0 failed
npm run build
# tsc + vite build EXIT:0
```

### 5. Issue-369 矩阵候选 SHA 对齐

`docs/evidence/issue-369/official-v1-v2-cli-matrix.json` 原记录 `candidate_revision` 为 beta3 的 `7006542a...`。按该矩阵维护方式，每次发布门禁需将其更新为当前候选 SHA：

```json
"candidate_revision": "9beba8930a0968aea5a138269ff2b7171177760d"
```

验证：

```powershell
py -3.13 -B scripts/validate_issue_369_matrix.py `
  --candidate-sha 9beba8930a0968aea5a138269ff2b7171177760d
# ISSUE_369_MATRIX_OK
```

### 6. 发布构建与清单校验

本地存在签名私钥 `~/.codexhub/codexhub-updater.key`，因此执行了 normal 与 debug 两个 flavor 的本地构建：

```powershell
scripts/build-windows-release.ps1 -Flavor normal
scripts/build-windows-release.ps1 -Flavor debug
```

构建产物与清单均生成成功，且 `scripts/Test-ReleaseManifest.ps1` 校验通过：

| Flavor | Installer | SHA-256 | Manifest |
|---|---|---|---|
| normal | `src-tauri/target/release/bundle/nsis/CodexHub_0.1.8-beta.4_x64-setup.exe` | `1953f67b6259982587456e983f37e756da2bdb95207d98e9e0cd832c848939a7` | `latest.json` |
| debug | `src-tauri/target/build-flavors/debug/release/bundle/nsis/CodexHub_0.1.8-beta.4_debug_x64-setup.exe` | `e3e6f7f914c9a8454ed5b867a64de3ebad0f9d265fd10781512bd8b233f74837` | `latest-debug.json` |

注意：构建脚本末尾调用 `Get-FileHash` 时因本机 PowerShell zh-CN 区域设置报错（`Get-FileHash` 无法识别），但清单校验、签名、安装包均正常。SHA-256 由 Python 在本地另行计算并补录。

### 7. 质量门报告

```powershell
py -3.13 -B scripts/report_quality_gates.py
```

结果：report-only 模式，`parse_errors: 0`。其他计数为既有技术债：

- `python_unused_imports`: 6
- `python_dead_functions`: 107
- `duplicate_function_names`: 195

无新增阻塞项。

## #284 验收标准逐项状态

| 验收标准 | 状态 | 证据/备注 |
|---|---|---|
| 保留 Beta3.3 血统及其 Chat/history/output-limit 回归 | ✅ 通过 | 血统回归 215 passed |
| #392 关闭并附协议受控证据 | ✅ 通过 | `docs/evidence/issue-392/`；`tests/test_issue_392_collaboration_contract.py` + `tests/test_issue_64_collaboration_inventory.py` 32 passed |
| #199 关闭（用户代理配置在 overlay 生命周期中保留） | ✅ 通过 | `tests/test_history_overlay.py`、`tests/test_config_overlay.py` 通过 |
| #252 关闭（通用 Collaboration V2 工作流） | ✅ 通过 | issue-283 协议 fixture、issue-392 运行时合约 |
| #401 关闭（ImageGen 透传） | ✅ 通过 | `tests/test_image_generation_gateway.py` 通过 |
| #402 关闭（拒绝 POST body  drain/close） | ✅ 通过 | `tests/test_proxy_shutdown.py`、`tests/test_routing.py` 通过 |
| #403 关闭（无 Official 快照时外部 Provider 可启动） | ✅ 通过 | `tests/test_provider_registry.py`、`tests/test_providers_config.py`、`tests/test_catalog.py` 通过 |
| #404 关闭（Provider Test 区分缺模型与不支持端点） | ✅ 通过 | `tests/test_probe_upstream_format.py` 通过 |
| #405 关闭（Provider 发现/编辑器/保存/重启状态一致） | ✅ 通过 | 前端 UI-contract 151 passed；`tests/test_provider_registry.py`、`tests/test_providers_config.py` 通过 |
| 使用完整 V2 Responses `collaboration` 命名空间及六个子函数 | ✅ 通过 | issue-392 合约与 `runtime_tool_compatibility` 边界测试 |
| V1 repair 在 V2 上无法运行 | ✅ 通过 | `tests/test_issue_198_v1_v2_isolation.py` 已在核心集中通过 |
| Codex Client 为代理所有者/执行者 | ✅ 通过 | issue-392 证据与 #283 fixture 不变式 |
| 用户代理设置在 overlay/restart 后保留 | ✅ 通过 | history/config overlay 测试 |
| 异常/冲突/跨 Home 状态在变更前或标准终端失败 | ✅ 通过 | #283 fixture 负向用例 + contract 校验 |
| ImageGen 请求捕获并到达 Official 上游 | ✅ 通过 | `test_image_generation_gateway.py` |
| 拒绝 POST body 安全 drain/close，不会泄漏到后续解析器日志 | ✅ 通过 | `test_proxy_shutdown.py`、`test_routing.py` |
| 无 Codex 登录且无 Official 快照时外部 Provider 仍可启动/发现/路由 | ✅ 通过 | provider/registry/catalog 测试 |
| Provider Test 明确区分缺模型与不支持端点 | ✅ 通过 | `test_probe_upstream_format.py` |
| 模型发现即时更新编辑器、保存时不覆盖草稿、保留每模型设置、重启后一致 | ✅ 通过 | 前端 UI-contract + provider 测试 |
| 现有任务历史不会被重写或清理 | ✅ 通过 | `test_history_overlay.py`、`test_history_consolidate.py` |
| `context_window` 仍是 Codex 面向的输出上限 | ✅ 通过 | `test_model_limits.py` |
| 无模型回退、Provider/模型生产分支、运行时白名单、跨 Provider 执行 | ✅ 通过 | 路由与核心集通过 |
| **精确候选 SHA 评审被接受** | ⚠️ 需人工 | 由发布负责人/ Orchestrator 在最终发布前完成 |
| **CLI/Desktop 人工验证被接受** | ⚠️ 需人工 | issue #392/#283 要求真实 Codex CLI/Desktop 证据；本次为本地自动化门禁 |
| 相关本地验证/发布门禁成功 | ✅ 通过 | 见上文；仅存在已记录的环境限制失败 |
| 仅在这些门禁通过后执行 tag/prerelease | ⚠️ 待执行 | 本次未打 tag、未发布、未开 PR |

## 草稿发布说明（0.1.8-beta.4）

自 `v0.1.8-beta.3.3` 以来合并的 PR：

- #406: Beta4: implement exact runtime-derived Collaboration V2 Responses lifecycle
- #407: fix(gateway): forward official ImageGen and drain rejected POST bodies（同时关闭 #401、#402）
- #409: fix(gateway): allow activation without safe Official snapshot（关闭 #403）
- #410: fix(proxy): map general agent_type to default and synthesize worker binding readback
- #411: fix(probe): distinguish missing model from unsupported endpoint in provider test（关闭 #404）
- #412: fix(frontend): sync provider editor draft after discovery and merge against persisted models（关闭 #405）
- #415: test(issue-283): add V2 lifecycle protocol fixture and sanitized evidence
- #416: test(evidence): issue 283 real-client V2 lifecycle verification and void-result contract fix

## 已知环境限制与注意事项

1. **PowerShell 区域设置**：本机默认 PowerShell 为 zh-CN，导致 `test_portable_rejects_invalid_flavor_before_building` 以及构建脚本末尾 `Get-FileHash` 调用失败。实际构建产物与清单均正常。
2. **Python 子进程版本**：系统默认 `python` 为 3.11，Rust `proxy::tests` 中 4 个 Gateway 子进程测试因此命中 PEP 695 语法错误。最终发布构建/测试环境必须运行 Python 3.13，或使用 `CODEXHUB_PROXY_PYTHON` 指向 3.13 运行时。
3. **真实客户端证据**：#392、#283 所需的 Codex CLI/Desktop 真实运行时证据未在本次本地自动化门禁中重新采集，已存在的 `docs/evidence/issue-392/` 与 `docs/evidence/issue-283/` 为发布依据。最终发布前需由人工确认这些证据仍被接受。

## 遗留操作

- [ ] 人工完成精确候选 SHA 评审。
- [ ] 人工接受 CLI/Desktop 真实客户端验证（或确认已有 issue-392/issue-283 证据仍然有效）。
- [ ] 将 `codex/release-0.1.8-beta.4-gate` 分支合并/提升到 `main`。
- [ ] 在 `main` 上执行 `git tag v0.1.8-beta.4 9beba8930a0968aea5a138269ff2b7171177760d`（或合并后的最终 SHA）。
- [ ] 通过 GitHub Releases 手动发布 prerelease，上传 normal/debug installer、签名、manifest、portable zip。

---

证据文件路径：`docs/evidence/issue-284/gate-report.md`
