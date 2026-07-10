"""SSE presentation helpers for file-chat streams."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Iterable, Iterator, Mapping

from app.services.chat import ChatStreamEvent


logger = logging.getLogger(__name__)
_TERMINAL_EVENT_TYPES = frozenset({"aborted", "done", "error"})


def format_sse_event(event_type: str, data: Mapping[str, Any] | None = None) -> str:
    """Format one domain stream event as a Server-Sent Events payload."""
    normalized_type = str(event_type or "").strip()
    if not normalized_type:
        raise ValueError("event_type cannot be empty")
    payload = json.dumps(dict(data or {}), ensure_ascii=False)
    return f"event: {normalized_type}\ndata: {payload}\n\n"


def present_chat_stream(events: Iterable[ChatStreamEvent]) -> Iterator[str]:
    """Convert supplier-neutral chat events to SSE payloads."""
    for event in events:
        if not isinstance(event, ChatStreamEvent):
            raise TypeError("chat stream must yield ChatStreamEvent")
        logger.debug("展示文件对话SSE事件: event_type=%s", event.event_type)
        yield format_sse_event(event.event_type, event.data)


def close_chat_stream_resource(
    resource: Any,
    *,
    run_id: str,
    label: str,
) -> None:
    close = getattr(resource, "close", None)
    if not callable(close):
        logger.debug(
            "文件对话流资源无需关闭: run_id=%s resource=%s",
            run_id,
            label,
        )
        return
    try:
        close()
        logger.debug(
            "文件对话流资源已关闭: run_id=%s resource=%s",
            run_id,
            label,
        )
    except Exception:
        logger.exception(
            "failed to close chat stream resource: run_id=%s resource=%s",
            run_id,
            label,
        )


def finalize_chat_run_stream(
    *,
    stream: Iterable[ChatStreamEvent],
    run_id: str,
    on_close: Callable[[], None] | None = None,
) -> Iterator[str]:
    try:
        for event in stream:
            if not isinstance(event, ChatStreamEvent):
                raise TypeError("chat stream must yield ChatStreamEvent")
            is_terminal = event.event_type in _TERMINAL_EVENT_TYPES
            # Presenter 只负责协议转换和资源关闭，run 状态已经由 application
            # 层的 ChatRunEventRecorder 收敛，避免展示层与业务层双写状态。
            logger.debug(
                "准备发送文件对话SSE事件: run_id=%s event_type=%s terminal=%s",
                run_id,
                event.event_type,
                is_terminal,
            )
            yield format_sse_event(event.event_type, event.data)
            if is_terminal:
                logger.info(
                    "文件对话SSE流收到终态事件并准备关闭: run_id=%s event_type=%s",
                    run_id,
                    event.event_type,
                )
                break
    finally:
        close_chat_stream_resource(stream, run_id=run_id, label="stream")
        if on_close is not None:
            try:
                on_close()
                logger.debug("文件对话客户端关闭回调已完成: run_id=%s", run_id)
            except Exception:
                logger.exception(
                    "failed to close chat client: run_id=%s",
                    run_id,
                )


__all__ = [
    "close_chat_stream_resource",
    "finalize_chat_run_stream",
    "format_sse_event",
    "present_chat_stream",
]
