"""Fail early with an actionable message when pytest uses the wrong Python."""

from __future__ import annotations

import sys


if sys.version_info < (3, 13):
    raise RuntimeError(
        "CodexHub requires Python 3.13 or newer. "
        "Run .\\scripts\\codexhub-python.cmd -m pytest ... or set "
        "CODEXHUB_PYTHON to a Python 3.13+ executable."
    )
