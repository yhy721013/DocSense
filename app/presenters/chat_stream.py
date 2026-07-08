"""SSE presentation helpers for file-chat streams."""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Iterator

from app.services.chat import ChatCommandService


logger = logging.getLogger(__name__)


def is_sse_event(payload: str, event_name: str) -> bool:
    prefix = f"event: {event_name}"
    return any(line.strip() == prefix for line in payload.splitlines())


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


def mark_chat_run_succeeded(
    *,
    chat_commands: ChatCommandService,
    run_id: str,
) -> None:
    try:
        chat_commands.complete_chat_run(run_id=run_id)
    except Exception:
        logger.exception("failed to mark chat run succeeded: run_id=%s", run_id)


def touch_chat_run(
    *,
    chat_commands: ChatCommandService,
    run_id: str,
) -> None:
    try:
        chat_commands.heartbeat_chat_run(run_id=run_id)
    except Exception:
        logger.exception("failed to heartbeat chat run: run_id=%s", run_id)


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
    stream: Iterable[str],
    chat_commands: ChatCommandService,
    run_id: str,
    on_close: Callable[[], None] | None = None,
) -> Iterator[str]:
    terminal_event = ""
    try:
        for payload in stream:
            if is_sse_event(payload, "error"):
                terminal_event = "error"
            elif is_sse_event(payload, "done"):
                terminal_event = "done"
            touch_chat_run(chat_commands=chat_commands, run_id=run_id)
            yield payload
        if terminal_event == "error":
            mark_chat_run_failed(
                chat_commands=chat_commands,
                run_id=run_id,
                error_message="chat stream emitted error event",
            )
        elif terminal_event == "done":
            mark_chat_run_succeeded(
                chat_commands=chat_commands,
                run_id=run_id,
            )
        else:
            mark_chat_run_failed(
                chat_commands=chat_commands,
                run_id=run_id,
                error_message="chat stream ended without terminal event",
            )
    except GeneratorExit:
        if terminal_event == "done":
            mark_chat_run_succeeded(
                chat_commands=chat_commands,
                run_id=run_id,
            )
        elif terminal_event == "error":
            mark_chat_run_failed(
                chat_commands=chat_commands,
                run_id=run_id,
                error_message="chat stream emitted error event before close",
            )
        else:
            mark_chat_run_failed(
                chat_commands=chat_commands,
                run_id=run_id,
                error_message="chat stream closed before completion",
            )
        raise
    except Exception as exc:
        mark_chat_run_failed(
            chat_commands=chat_commands,
            run_id=run_id,
            error_message=str(exc) or exc.__class__.__name__,
        )
        raise
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
    "is_sse_event",
    "mark_chat_run_failed",
    "mark_chat_run_succeeded",
    "touch_chat_run",
]
