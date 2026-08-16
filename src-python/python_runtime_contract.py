"""Shared preflight for Python code that can be launched directly.

This module intentionally uses syntax supported by the oldest interpreter we
need to reject.  It must be importable by Python 3.11 before any production
module using the repository's newer syntax is loaded.
"""

from __future__ import annotations

import sys
from pathlib import Path


MINIMUM_PYTHON = (3, 13)


def require_python_313(entrypoint: str | Path) -> None:
    """Stop a direct script invocation before it imports newer syntax."""

    if sys.version_info[:2] < MINIMUM_PYTHON:
        raise RuntimeError(
            "CodexHub requires Python 3.13 or newer. "
            f"Run .\\scripts\\codexhub-python.cmd {Path(entrypoint).as_posix()} ..."
        )
