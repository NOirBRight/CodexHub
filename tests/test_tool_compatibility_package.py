from __future__ import annotations

from pathlib import Path

import runtime_tool_compatibility
import tool_compatibility


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src-python" / "tool_compatibility"
COLLAB_V1 = PACKAGE / "collab_v1.py"
COLLAB_V2 = PACKAGE / "collab_v2.py"
MAX_PACKAGE_FILE_LINES = 2100


def test_collab_v1_source_does_not_import_v2() -> None:
    source = COLLAB_V1.read_text(encoding="utf-8")
    assert "collab_v2" not in source
    assert "from .collab_v2" not in source
    assert "import collab_v2" not in source


def test_collab_v2_source_does_not_import_v1() -> None:
    source = COLLAB_V2.read_text(encoding="utf-8")
    assert "collab_v1" not in source
    assert "from .collab_v1" not in source
    assert "import collab_v1" not in source


def test_v1_repair_implementation_lives_in_collab_v1() -> None:
    source = COLLAB_V1.read_text(encoding="utf-8")
    assert "is_legacy_flattened_spawn" in source
    assert "matches_flattened_native_identity" in source
    assert "V1_FLAT_PREFIX" in source


def test_v2_adaptation_implementation_lives_in_collab_v2() -> None:
    source = COLLAB_V2.read_text(encoding="utf-8")
    assert "agent_message" in source
    assert "_validate_collaboration_v2_call_item" in source
    assert "encrypted_function_args" in source


def test_package_modules_stay_within_size_bound() -> None:
    oversized = []
    for path in sorted(PACKAGE.glob("*.py")):
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if lines > MAX_PACKAGE_FILE_LINES:
            oversized.append(f"{path.name}:{lines}")
    assert oversized == []


def test_public_names_importable_from_runtime_and_package() -> None:
    assert runtime_tool_compatibility.__all__ == tool_compatibility.__all__
    for name in tool_compatibility.__all__:
        assert hasattr(runtime_tool_compatibility, name)
        assert hasattr(tool_compatibility, name)
        assert getattr(runtime_tool_compatibility, name) is getattr(tool_compatibility, name)
