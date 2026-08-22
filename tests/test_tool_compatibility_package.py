from __future__ import annotations

from pathlib import Path

import runtime_tool_compatibility
import tool_compatibility


ROOT = Path(__file__).resolve().parents[1]
COLLAB_V1 = ROOT / "src-python" / "tool_compatibility" / "collab_v1.py"


def test_collab_v1_source_does_not_import_v2() -> None:
    source = COLLAB_V1.read_text(encoding="utf-8")
    assert "collab_v2" not in source


def test_public_names_importable_from_runtime_and_package() -> None:
    assert runtime_tool_compatibility.__all__ == tool_compatibility.__all__
    for name in tool_compatibility.__all__:
        assert hasattr(runtime_tool_compatibility, name)
        assert hasattr(tool_compatibility, name)
        assert getattr(runtime_tool_compatibility, name) is getattr(tool_compatibility, name)
