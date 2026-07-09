import os
from pathlib import Path

import pytest

from atomic_io import atomic_write_text


def test_atomic_write_text_replaces_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text("old", encoding="utf-8")

    atomic_write_text(target, "new", encoding="utf-8")

    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_write_text_keeps_existing_file_when_replace_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "providers.toml"
    target.write_text("old", encoding="utf-8")

    def fail_replace(source: str | bytes | os.PathLike[str] | os.PathLike[bytes], destination: str | bytes | os.PathLike[str] | os.PathLike[bytes]) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        atomic_write_text(target, "new", encoding="utf-8")

    assert target.read_text(encoding="utf-8") == "old"
