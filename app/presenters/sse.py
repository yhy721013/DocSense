"""SSE Presenter 共享的纯格式化与资源关闭工具。"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping


logger = logging.getLogger(__name__)


def format_sse_event(
    event_type: str,
    data: Mapping[str, Any] | None = None,
) -> str:
    """将一个公开事件格式化为 UTF-8 SSE 文本。"""
    normalized_type = str(event_type or "").strip()
    if not normalized_type:
        raise ValueError("event_type cannot be empty")
    payload = json.dumps(dict(data or {}), ensure_ascii=False)
    return f"event: {normalized_type}\ndata: {payload}\n\n"


def close_sse_resource(resource: Any, *, run_id: str, label: str) -> None:
    """幂等关闭可关闭的事件源，关闭失败只记录脱敏内部日志。"""
    close = getattr(resource, "close", None)
    if not callable(close):
        logger.debug(
            "SSE 事件源无需关闭: run_id=%s resource=%s",
            run_id,
            label,
        )
        return
    try:
        close()
        logger.debug(
            "SSE 事件源已关闭: run_id=%s resource=%s",
            run_id,
            label,
        )
    except Exception:
        logger.exception(
            "SSE 事件源关闭失败: run_id=%s resource=%s",
            run_id,
            label,
        )


__all__ = ["close_sse_resource", "format_sse_event"]
