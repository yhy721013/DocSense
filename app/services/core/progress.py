from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict


_MIN_PROGRESS = Decimal("0")
_MAX_PROGRESS = Decimal("1")
_PROGRESS_QUANT = Decimal("0.0001")


def normalize_progress(value: Any) -> float:
    """Return a bounded, JSON-friendly task progress value.

    Public progress is a ratio in ``[0.0, 1.0]``. Rounding at shared IO
    boundaries prevents binary float artifacts from leaking to clients.
    """
    try:
        progress = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        progress = _MIN_PROGRESS

    if not progress.is_finite():
        progress = _MIN_PROGRESS

    if progress < _MIN_PROGRESS:
        progress = _MIN_PROGRESS
    elif progress > _MAX_PROGRESS:
        progress = _MAX_PROGRESS

    return float(progress.quantize(_PROGRESS_QUANT, rounding=ROUND_HALF_UP))


def normalize_progress_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, dict) or "progress" not in data:
        return payload

    normalized_payload = dict(payload)
    normalized_data = dict(data)
    normalized_data["progress"] = normalize_progress(normalized_data["progress"])
    normalized_payload["data"] = normalized_data
    return normalized_payload
