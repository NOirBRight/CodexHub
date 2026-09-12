"""Strict JSON primitives shared by adapted wire envelopes and arguments."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import math
from typing import Any


class AmbiguousJSONError(ValueError):
    """A duplicate key or non-finite number makes wire identity ambiguous."""


def strict_json_loads(value: str | bytes, *, exact_numbers: bool = False) -> Any:
    """Reject JSON extensions, optionally retaining exact decimal numbers.

    Syntax errors retain JSONDecodeError so existing non-JSON passthrough
    policies can stay separate from rejection of ambiguous JSON objects.
    No source values are included in controlled error messages.
    """
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, child in pairs:
            if key in result:
                raise AmbiguousJSONError("duplicate_json_key")
            result[key] = child
        return result

    def number(text: str) -> float | Decimal:
        try:
            parsed = Decimal(text) if exact_numbers else float(text)
        except (ValueError, InvalidOperation):
            raise AmbiguousJSONError("invalid_json_number") from None
        finite = parsed.is_finite() if isinstance(parsed, Decimal) else math.isfinite(parsed)
        if not finite:
            raise AmbiguousJSONError("non_finite_json_number")
        return parsed

    def constant(_text: str) -> None:
        raise AmbiguousJSONError("non_finite_json_number")

    try:
        return json.loads(value, object_pairs_hook=unique_object, parse_float=number, parse_constant=constant)
    except (json.JSONDecodeError, AmbiguousJSONError):
        raise
    except (ValueError, RecursionError):
        raise AmbiguousJSONError("json_numeric_or_nesting_limit") from None
