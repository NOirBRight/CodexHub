"""Compatibility import for the canonical source-runtime preflight."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


_SOURCE_CONTRACT = Path(__file__).resolve().parents[1] / "src-python" / "python_runtime_contract.py"
_SPEC = importlib.util.spec_from_file_location(
    "_codexhub_python_runtime_contract",
    _SOURCE_CONTRACT,
)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"CodexHub Python runtime contract is missing: {_SOURCE_CONTRACT}")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

MINIMUM_PYTHON = _MODULE.MINIMUM_PYTHON
require_python_313 = _MODULE.require_python_313
