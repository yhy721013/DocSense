"""Execution boundary for one file-chat run."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.services.chat.application.command_service import ChatCommandService
from app.services.chat.domain.events import ChatStreamEvent
from app.services.chat.domain.models import (
    MESSAGE_COMMITTED,
    MESSAGE_PENDING,
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_USER,
)
from app.services.chat.persistence.store import ChatPersistenceStore


logger = logging.getLogger(__name__)

_TERMINAL_EVENT_TYPES = frozenset({"aborted", "done", "error"})


def _required_text(value: str, *, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} cannot be empty")
    return normalized


def _text_tuple(values: Sequence[str], *, name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{name} must be a sequence of strings")
    normalized: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, str):
            raise TypeError(f"{name}[{index}] must be str")
        item = str(value or "").strip()
        if not item:
            raise ValueError(f"{name}[{index}] cannot be empty")
        normalized.append(item)
    return tuple(normalized)


@dataclass(frozen=True)
class ChatRunStreamRequest:
    """Application input required to execute one file-chat stream."""

    run_id: str
    chat_id: str
    message: str
    file_names: tuple[str, ...] = ()
    file_original_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "run_id",
            _required_text(self.run_id, name="run_id"),
        )
        object.__setattr__(
            self,
            "chat_id",
            _required_text(self.chat_id, name="chat_id"),
        )
        object.__setattr__(
            self,
            "file_names",
            _text_tuple(self.file_names, name="file_names"),
        )
        object.__setattr__(
            self,
            "file_original_names",
            _text_tuple(self.file_original_names, name="file_original_names"),
        )
        object.__setattr__(
            self,
            "message",
            _required_text(self.message, name="message"),
        )
        if len(self.file_names) != len(self.file_original_names):
            raise ValueError(
                "file_names and file_original_names must have the same length"
            )


@runtime_checkable
class ChatRunExecutor(Protocol):
    """Executes a chat run and yields supplier-neutral stream events."""

    def stream_chat_run(
        self,
        request: ChatRunStreamRequest,
    ) -> Iterable[ChatStreamEvent]:
        """Execute one accepted/running run without producing presentation text."""
        ...


class ChatRunEventRecorder:
    """Persist local authoritative messages while preserving stream events."""

    def __init__(self, store: ChatPersistenceStore) -> None:
        self._store = store

    def record(
        self,
        *,
        request: ChatRunStreamRequest,
        events: Iterable[ChatStreamEvent],
        chat_commands: ChatCommandService,
    ) -> Iterator[ChatStreamEvent]:
        user_message_id = self._message_id(request.run_id, MESSAGE_ROLE_USER)
        assistant_message_id = self._message_id(
            request.run_id,
            MESSAGE_ROLE_ASSISTANT,
        )
        user_written = False
        terminal_event = ""
        assistant_parts: list[str] = []
        try:
            self._append_user_pending(
                request=request,
                message_id=user_message_id,
            )
            user_written = True

            for event in events:
                if not isinstance(event, ChatStreamEvent):
                    raise TypeError("chat stream must yield ChatStreamEvent")
                if event.event_type == "textChunk":
                    content = event.data.get("content")
                    if isinstance(content, str) and content:
                        assistant_parts.append(content)
                if event.event_type in _TERMINAL_EVENT_TYPES:
                    if event.event_type == "done":
                        self._commit_user(user_message_id)
                        self._append_assistant_committed(
                            request=request,
                            message_id=assistant_message_id,
                            content="".join(assistant_parts),
                        )
                        chat_commands.complete_chat_run(run_id=request.run_id)
                    elif event.event_type == "aborted":
                        self._commit_user(user_message_id)
                        chat_commands.abort_chat_run(run_id=request.run_id)
                    else:
                        self._commit_user(user_message_id)
                        chat_commands.fail_chat_run(
                            run_id=request.run_id,
                            error_message="chat stream emitted error event",
                        )
                    terminal_event = event.event_type
                else:
                    chat_commands.heartbeat_chat_run(run_id=request.run_id)
                yield event
                if terminal_event:
                    break

            if not terminal_event:
                self._commit_user(user_message_id)
                chat_commands.fail_chat_run(
                    run_id=request.run_id,
                    error_message="chat stream ended without terminal event",
                )
        except GeneratorExit:
            if user_written and not terminal_event:
                self._commit_user(user_message_id)
                chat_commands.fail_chat_run(
                    run_id=request.run_id,
                    error_message="chat stream closed before completion",
                )
            raise
        except Exception as exc:
            if user_written and not terminal_event:
                self._commit_user(user_message_id)
            if not terminal_event:
                chat_commands.fail_chat_run(
                    run_id=request.run_id,
                    error_message=str(exc) or exc.__class__.__name__,
                )
            raise
        finally:
            self._close_events(events=events, run_id=request.run_id)

    def _append_user_pending(
        self,
        *,
        request: ChatRunStreamRequest,
        message_id: str,
    ) -> None:
        self._store.messages.append(
            message_id=message_id,
            chat_id=request.chat_id,
            run_id=request.run_id,
            role=MESSAGE_ROLE_USER,
            content=request.message,
            status=MESSAGE_PENDING,
            files=tuple(zip(request.file_names, request.file_original_names)),
        )

    def _commit_user(self, message_id: str) -> None:
        self._store.messages.set_status(
            message_id=message_id,
            status=MESSAGE_COMMITTED,
        )

    def _append_assistant_committed(
        self,
        *,
        request: ChatRunStreamRequest,
        message_id: str,
        content: str,
    ) -> None:
        if not content:
            return
        self._store.messages.append(
            message_id=message_id,
            chat_id=request.chat_id,
            run_id=request.run_id,
            role=MESSAGE_ROLE_ASSISTANT,
            content=content,
            status=MESSAGE_COMMITTED,
        )

    @staticmethod
    def _message_id(run_id: str, role: str) -> str:
        return f"{run_id}:{role}"

    @staticmethod
    def _close_events(*, events: Iterable[ChatStreamEvent], run_id: str) -> None:
        close = getattr(events, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception:
            logger.exception("failed to close chat event stream: run_id=%s", run_id)


def record_chat_run_events(
    *,
    request: ChatRunStreamRequest,
    events: Iterable[ChatStreamEvent],
    store: ChatPersistenceStore,
    chat_commands: ChatCommandService,
) -> Iterator[ChatStreamEvent]:
    return ChatRunEventRecorder(store).record(
        request=request,
        events=events,
        chat_commands=chat_commands,
    )


__all__ = [
    "ChatRunEventRecorder",
    "ChatRunExecutor",
    "ChatRunStreamRequest",
    "record_chat_run_events",
]
