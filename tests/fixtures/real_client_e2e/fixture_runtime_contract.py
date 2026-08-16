"""Runtime boundary for E2E fixture scripts.

Fixture command files are expected to pass the repository-selected Python
path through ``CODEXHUB_E2E_PYTHON``.  Keep this guard importable by a copied
fixture as well as by the checked-in fixture tree, and fail before fixture
code can mutate state under an ambient interpreter.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys


MINIMUM_PYTHON = (3, 13)


def require_python_313(entrypoint: str | Path) -> None:
    if sys.version_info[:2] < MINIMUM_PYTHON:
        raise RuntimeError(
            "CodexHub requires Python 3.13 or newer. "
            f"Run .\\scripts\\codexhub-python.cmd {Path(entrypoint).as_posix()} ..."
        )

    expected = next(
        (
            value
            for name in (
                "CODEXHUB_E2E_PYTHON",
                "CODEXHUB_PYTHON",
                "CODEXHUB_PROXY_PYTHON",
            )
            if (value := os.environ.get(name))
        ),
        None,
    )
    if not expected:
        raise RuntimeError(
            "CodexHub E2E fixture requires an explicit CODEXHUB_E2E_PYTHON "
            "binding; run it through run-fixture-python.cmd."
        )

    actual_path = Path(sys.executable).resolve()
    expected_path = Path(expected).resolve()
    if actual_path != expected_path:
        raise RuntimeError(
            "CodexHub E2E fixture interpreter does not match the bound "
            f"Python runtime: {actual_path} != {expected_path}"
        )
