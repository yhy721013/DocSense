"""SSE presentation helpers for file-chat streams."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Iterable, Iterator, Mapping

from app.services.chat import ChatCommandService, ChatStreamEvent


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
        yield format_sse_event(event.event_type, event.data)


def mark_chat_run_failed(
    *,
    chat_commands: ChatCommandService,
    run_id: str,
    error_message: str,
) -> None:
    try:
        chat_commands.fail_chat_run(
            run_id=run_id,
            error_message=error_message,
        )
    except Exception:
        logger.exception("failed to mark chat run failed: run_id=%s", run_id)


def close_chat_stream_resource(
    resource: Any,
    *,
    run_id: str,
    label: str,
) -> None:
    close = getattr(resource, "close", None)
    if not callable(close):
        return
    try:
        close()
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
            yield format_sse_event(event.event_type, event.data)
            if is_terminal:
                break
    finally:
        close_chat_stream_resource(stream, run_id=run_id, label="stream")
        if on_close is not None:
            try:
                on_close()
            except Exception:
                logger.exception(
                    "failed to close chat client: run_id=%s",
                    run_id,
                )


__all__ = [
    "close_chat_stream_resource",
    "finalize_chat_run_stream",
    "format_sse_event",
    "mark_chat_run_failed",
    "present_chat_stream",
]
