# Codex 连接缺陷排查

## 修复进展（后续授权实现）

已在分支 `fix/codex-connected-model-catalog` 实现修复，尚未提交或替换已安装版本。下方原始诊断记录保留，用于解释修复前行为。

- 软件的显式连接操作和对应配置预览传递 `--use-managed-catalog`，选择当前生成的 CodexHub 目录。原目录文件不修改，原目录引用保存在现有恢复备份中；断开恢复。后台重发布及未显式选择托管目录的 Python 调用仍采用保守策略。
- 重复连接保留恢复基线；连接期间用户修改目录后再次连接，会把该修改保存在恢复基线中。跨 Stable/Beta 接管恢复也有回归覆盖。
- 含单引号或控制字符的目录使用合法 TOML 双引号字符串编码。现有单引号测试改为标准解析器验证，而非断言错误输出字符串。
- 沿用连接操作的 Toast 和 Codex 重启提示，没有增加自动重启客户端行为。

验证：新增 `tests/test_codex_catalog_connection.py`，修复前 5 failed / 3 passed，最终相关集合 173 passed / 64 subtests passed。新增用例后最终 Python 核心全套为 **2697 passed, 177 skipped, 267 subtests passed**；`verify-linux.sh` 的 Rust **715 passed / 4 ignored**、Clippy、前端构建、真实窗口物理输入 E2E 均通过。前端契约三组共 **121 passed**。Python 最终集合因后续特殊字符补测重新执行一次，结果见 `/tmp/codexhub-catalog-fix-python-final.log`。

`git diff --check` 通过；report-only quality gates 已运行，解析错误为 0，其他报告项保持非阻断性质。未跑 Windows、真实模型付费链路或安装包发布验证。新增的内部 CLI 参数要求修复后的 Rust 与 Python 一起交付，不能只更新其中一侧。临时验证日志位于 `/tmp/codexhub-catalog-fix-verification.log` 和 `/tmp/codexhub-catalog-fix-ui.log`。

范围/验收自查：变更遵循现有配置 overlay 入口，新增参数仅用于显式连接；未引入持久化格式或 Tauri 公共命令参数变化，未改写用户目录文件。备份、断开、用户连接期间修改和跨渠道恢复均通过实际 Python 配置路径测试。按 strict 类完成涉及的 Python/Rust 验证，未向外部 Issue 或 PR 发布内容。

## 原始诊断

日期：2026-09-13。源码基线：`c230b23`（0.2.8）。接手会话：`01a09b19-9dfd-7e62-8aa7-d4d1af45c9a9`。

原会话修正了本机目录引用，没有修复软件。本轮仅审查 Codex 连接、目录选择和断开恢复链路；未修改产品实现、用户配置，未启动或重启客户端，未调用真实模型。

## 确认的问题

### 1. 目录冲突未报告，连接成功不代表外部模型可见（P1）

前置条件：已有 `model_catalog_json` 指向非 CodexHub 托管目录，该目录缺少 Gateway 外部模型。

实际结果：`apply_overlay` 成功写入 Gateway 路由，但继续引用原目录；外部模型仍不可见。原目录文件不存在时也返回成功，留下不可读取的目录引用。临时目录实测以上两种情况均成立。

原因：`src-python/config_overlay.py:184` 的 `_overlay_catalog_value` 无条件保留非托管目录，不检查内容或文件存在性，也不输出冲突结果；调用方 `apply_overlay`（同文件 `906`）继续正常发布。`src-tauri/src/config.rs:1068` 将成功执行映射为切换成功。`frontend/src/components/GatewayClientCard.tsx:294` 按 `route_mode` 判定 connected，没有模型目录可见性维度。

这不是“保留用户目录”本身有错：ADR-0004 要求保护用户设置。缺陷是冲突没有被表达，用户无法从软件内完成可理解、可恢复的处理。强制覆盖所有自定义目录不是合适修复。

建议验收：连接前检测目录冲突和缺失；明确提供使用 CodexHub 目录的选择并保存恢复依据；选择保留自定义目录时说明外部模型可能不可见；断开恢复原路径且保留连接期间用户的新修改。目录变更后明确告知需要重新启动 Codex。至少覆盖首次连接、重复连接和断开。

### 2. 路径含单引号时写出非法 TOML（P1，条件触发）

前置条件：合法配置中的目录路径包含 `'`，例如 `team's-models.json`；或者生成的托管目录位于含 `'` 的父目录。

实际结果：连接函数返回成功，但标准 TOML 解析器拒绝写出的配置：`TOMLDecodeError: Expected newline or end of document after a statement`。客户端读取该文件时会遇到配置解析错误；本轮没有在真实 GUI 上验证最终错误界面。

原因：`src-python/config_overlay.py:54` 的 `toml_literal` 使用 `value.replace("'", "''")`。TOML 单引号字符串不支持这种转义。`build_overlay`（同文件 `795`）用该函数写目录，`apply_overlay`（同文件 `918`）落盘前没有完整 TOML 解析检查。

建议验收：用符合 TOML 语法的序列化生成字符串；用标准解析器验证写出结果及值的往返一致性，覆盖单引号、双引号、反斜杠、`#` 和非 ASCII 路径。不能只断言输出包含某段文本。

## 已执行的验证

```bash
./scripts/codexhub-python.sh -m pytest -q tests/test_config_overlay.py tests/test_catalog_sync.py
```

结果：`161 passed, 64 subtests passed in 0.47s`。

现有 `tests/test_config_overlay.py:568` 附近的用例锁定保留自定义目录的策略，但未验证外部模型可见性或冲突反馈；`794` 附近的单引号用例只断言错误转义后的字符串存在，未交给标准 TOML 解析器。这解释了测试通过却仍存在缺陷。

隔离对照实验：

| 场景 | apply 结果 | 配置/目录结果 | 断开精确恢复原配置 |
|---|---|---|---|
| 无原目录 | 成功 | 外部模型可见 | 是 |
| 原自定义目录无外部模型 | 成功 | 外部模型缺失 | 是 |
| 原自定义目录文件不存在 | 成功 | 引用文件仍缺失 | 是 |
| 自定义目录路径含单引号 | 成功 | 非法 TOML | 是 |
| 托管目录路径含单引号 | 成功 | 非法 TOML | 是 |

以下最小回归用例已提取至 `/tmp/test_codex_catalog_audit_oDENJm.py` 并通过仓库启动器执行，结果为 `2 failed in 0.24s`：分别是外部模型缺失断言失败与 `TOMLDecodeError`。临时脚本执行后清理，可由下方代码重新生成。第一个用例允许实现明确拒绝冲突，或正常返回并保证外部模型可见；不规定强制覆盖用户目录。

```python
import json
import tomllib

import pytest

from config_overlay import apply_overlay


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    for key, suffix in (
        ("CODEX_HOME", "home"),
        ("CODEXHUB_RUNTIME_HOME", "runtime"),
        ("CODEXHUB_ROLLBACK_PROVENANCE_DIR", "provenance"),
    ):
        monkeypatch.setenv(key, str(tmp_path / suffix))


def test_catalog_conflict_is_reported_or_external_model_is_visible(tmp_path):
    old = tmp_path / "old.json"
    managed = tmp_path / "managed.json"
    config = tmp_path / "config.toml"
    old.write_text('{"models": []}')
    managed.write_text('{"models": [{"slug": "external/test-model"}]}')
    config.write_text("model_catalog_json = " + json.dumps(str(old)) + "\n")
    try:
        apply_overlay(config, tmp_path / "backup.toml", managed,
                      "http://127.0.0.1:19099")
    except ValueError as error:
        assert "catalog" in str(error).lower()
        return
    selected = tomllib.loads(config.read_text())["model_catalog_json"]
    models = json.loads(type(old)(selected).read_text())["models"]
    assert "external/test-model" in [model["slug"] for model in models]


def test_apostrophe_catalog_remains_valid_toml(tmp_path):
    catalog = tmp_path / "team's-models.json"
    catalog.write_text('{"models": []}')
    config = tmp_path / "config.toml"
    config.write_text("model_catalog_json = " + json.dumps(str(catalog)) + "\n")
    tomllib.loads(config.read_text())  # Input is valid.
    apply_overlay(config, tmp_path / "backup.toml", None,
                  "http://127.0.0.1:19099")
    parsed = tomllib.loads(config.read_text())
    assert parsed["model_catalog_json"] == str(catalog)
```

把上述 Python 块保存至临时 `test_codex_catalog_audit.py`，使用仓库启动器运行：

```bash
./scripts/codexhub-python.sh -m pytest -q /tmp/test_codex_catalog_audit.py
```

## 范围与后续

没有对全软件作无缺陷保证，未执行 Rust/前端全套或真实客户端 E2E。Rust 隔离读回函数 `readback_codex_config_isolated`（`src-tauri/src/config.rs:1356`）仅检查 owner、provider、wire_api，不验证完整 TOML 和目录内容；这是源码确认的检查缺口，本轮未独立执行该 Rust 路径，不另计一个已复现产品 bug。

本报告是只读诊断产物。后续实现涉及持久化及 Python/Rust/前端状态契约，应按 verification-policy 的 strict 类执行，不能用此次 161 项测试替代修复后的完整验证。
