"""Live lookup of sibling compatibility symbols.

Cross-module calls go through this module so tests can patch the owning file.
"""

from __future__ import annotations

from . import lookup


def __getattr__(name: str):
    return lookup(name)
