"""Turning arbitrary Python values into small, JSON-safe data.

Nothing here may raise: a broken ``__repr__`` must never turn into a second
error while reporting the first.
"""

from __future__ import annotations

import datetime
import decimal
import enum
import uuid
from typing import Any

MAX_DEPTH = 5
MAX_ITEMS = 50
MAX_STRING = 1024
MAX_REPR = 256


def safe_repr(value: Any, limit: int = MAX_REPR) -> str:
    try:
        text = repr(value)
    except Exception:  # noqa: BLE001 - user __repr__ can do anything
        try:
            text = f"<{type(value).__name__} object (repr failed)>"
        except Exception:  # noqa: BLE001
            text = "<unrepresentable object>"
    return truncate(text, limit)


def truncate(text: str, limit: int = MAX_STRING) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def to_json(value: Any, depth: int = MAX_DEPTH, string_limit: int = MAX_STRING, max_items: int = MAX_ITEMS) -> Any:
    """A JSON-serializable copy of value, bounded in depth, length and size."""
    try:
        if value is None or isinstance(value, (bool, int, float)):
            if isinstance(value, float) and value != value:  # NaN
                return "NaN"
            return value
        if isinstance(value, str):
            return truncate(value, string_limit)
        if isinstance(value, bytes):
            return truncate(value.decode("utf-8", "replace"), string_limit)
        if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
            return value.isoformat()
        if isinstance(value, (decimal.Decimal, uuid.UUID)):
            return str(value)
        if isinstance(value, enum.Enum):
            return to_json(value.value, depth, string_limit, max_items)
        if depth <= 0:
            return safe_repr(value)
        if isinstance(value, dict):
            out = {}
            for i, (k, v) in enumerate(value.items()):
                if i >= max_items:
                    out["…"] = f"{len(value) - max_items} more"
                    break
                out[truncate(str(k), 200)] = to_json(v, depth - 1, string_limit, max_items)
            return out
        if isinstance(value, (list, tuple, set, frozenset)):
            items = list(value)
            out_list = [to_json(v, depth - 1, string_limit, max_items) for v in items[:max_items]]
            if len(items) > max_items:
                out_list.append(f"… {len(items) - max_items} more")
            return out_list
        return safe_repr(value)
    except Exception:  # noqa: BLE001
        return "<unserializable>"
