"""Public failure and stream contract for official CLI subscription backends.

Backends receive the canonical Chat Completions payload after Gateway tool and
protocol adaptation. They emit ordinary chunk dictionaries; the exchange owns
SSE encoding, downstream conversion, and cancellation when its reader closes.
"""
from __future__ import annotations

from pathlib import Path
import sys


class BackendError(Exception):
    """A bounded, credential-free failure safe to expose to a caller."""

    def __init__(self, code: str, message: str, status: int = 502) -> None:
        self.code = code
        self.status = status
        self.message = message
        super().__init__(message)


def load_http2_dependencies() -> None:
    """Load the pinned pure Python wheels in embedded and host runtimes alike."""
    vendor = Path(__file__).resolve().parent / "vendor"
    for wheel in (
        "h2-4.3.0-py3-none-any.whl",
        "hpack-4.1.0-py3-none-any.whl",
        "hyperframe-6.1.0-py3-none-any.whl",
    ):
        path = vendor / wheel
        if not path.is_file():
            raise BackendError("backend-unavailable", "Cursor transport dependencies are missing.", 503)
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
