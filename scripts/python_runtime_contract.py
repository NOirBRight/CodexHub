"""Shared preflight for Python scripts that can be launched directly."""

from __future__ import annotations

import sys
from pathlib import Path


MINIMUM_PYTHON = (3, 13)


def require_python_313(entrypoint: str | Path) -> None:
    """Stop a direct script invocation before it imports 3.13-only modules."""

    if sys.version_info[:2] < MINIMUM_PYTHON:
        raise RuntimeError(
            "CodexHub requires Python 3.13 or newer. "
            f"Run .\\scripts\\codexhub-python.cmd {Path(entrypoint).as_posix()} ..."
        )
