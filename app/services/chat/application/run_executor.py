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
            logger.info(
                "开始记录文件对话run事件: chat_id=%s run_id=%s file_count=%d",
                request.chat_id,
                request.run_id,
                len(request.file_names),
            )
            self._append_user_pending(
                request=request,
                message_id=user_message_id,
            )
            user_written = True
            logger.debug(
                "用户消息已写入pending: chat_id=%s run_id=%s message_id=%s",
                request.chat_id,
                request.run_id,
                user_message_id,
            )

            event_iterator = iter(events)
            while True:
                if self._abort_requested(request.run_id):
                    terminal_event = "aborted"
                    logger.info(
                        "消费上游事件前检测到中断: chat_id=%s run_id=%s",
                        request.chat_id,
                        request.run_id,
                    )
                    yield self._finish_aborted(
                        request=request,
                        user_message_id=user_message_id,
                        chat_commands=chat_commands,
                    )
                    break
                try:
                    event = next(event_iterator)
                except StopIteration:
                    logger.warning(
                        "文件对话run上游事件流无终态结束: chat_id=%s run_id=%s",
                        request.chat_id,
                        request.run_id,
                    )
                    break
                if not isinstance(event, ChatStreamEvent):
                    raise TypeError("chat stream must yield ChatStreamEvent")
                if self._abort_requested(request.run_id):
                    terminal_event = "aborted"
                    logger.info(
                        "处理上游事件前检测到中断: chat_id=%s run_id=%s upstream_event=%s",
                        request.chat_id,
                        request.run_id,
                        event.event_type,
                    )
                    yield self._finish_aborted(
                        request=request,
                        user_message_id=user_message_id,
                        chat_commands=chat_commands,
                    )
                    break
                if event.event_type == "textChunk":
                    content = event.data.get("content")
                    if isinstance(content, str) and content:
                        assistant_parts.append(content)
                if event.event_type in _TERMINAL_EVENT_TYPES:
                    logger.info(
                        "文件对话run收到终态事件: chat_id=%s run_id=%s event=%s chunks=%d",
                        request.chat_id,
                        request.run_id,
                        event.event_type,
                        len(assistant_parts),
                    )
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
                if self._abort_requested(request.run_id):
                    terminal_event = "aborted"
                    logger.info(
                        "上游无终态结束但检测到中断请求: chat_id=%s run_id=%s",
                        request.chat_id,
                        request.run_id,
                    )
                    yield self._finish_aborted(
                        request=request,
                        user_message_id=user_message_id,
                        chat_commands=chat_commands,
                    )
                else:
                    self._commit_user(user_message_id)
                    logger.warning(
                        "文件对话run因缺失终态标记失败: chat_id=%s run_id=%s",
                        request.chat_id,
                        request.run_id,
                    )
                    chat_commands.fail_chat_run(
                        run_id=request.run_id,
                        error_message="chat stream ended without terminal event",
                    )
        except GeneratorExit:
            if user_written and not terminal_event:
                if self._abort_requested(request.run_id):
                    terminal_event = "aborted"
                    logger.info(
                        "SSE连接关闭时检测到中断请求: chat_id=%s run_id=%s",
                        request.chat_id,
                        request.run_id,
                    )
                    self._finish_aborted(
                        request=request,
                        user_message_id=user_message_id,
                        chat_commands=chat_commands,
                    )
                else:
                    self._commit_user(user_message_id)
                    logger.warning(
                        "SSE连接在终态前关闭: chat_id=%s run_id=%s",
                        request.chat_id,
                        request.run_id,
                    )
                    chat_commands.fail_chat_run(
                        run_id=request.run_id,
                        error_message="chat stream closed before completion",
                    )
            raise
        except Exception as exc:
            if not terminal_event and self._abort_requested(request.run_id):
                terminal_event = "aborted"
                logger.warning(
                    "上游异常后检测到中断请求，按中断收敛: chat_id=%s run_id=%s error=%s",
                    request.chat_id,
                    request.run_id,
                    exc,
                )
                if user_written:
                    yield self._finish_aborted(
                        request=request,
                        user_message_id=user_message_id,
                        chat_commands=chat_commands,
                    )
                else:
                    chat_commands.abort_chat_run(run_id=request.run_id)
                return
            if user_written and not terminal_event:
                self._commit_user(user_message_id)
            if not terminal_event:
                logger.exception(
                    "文件对话run事件记录异常，按失败收敛: chat_id=%s run_id=%s",
                    request.chat_id,
                    request.run_id,
                )
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
        # user 消息先以 pending 写入，只有 run 明确进入 done/error/aborted 等终态
        # 后才提交为 committed。这样可以避免进程崩溃时把未完成轮次误暴露给历史接口。
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
            logger.info(
                "跳过空assistant消息入库: chat_id=%s run_id=%s",
                request.chat_id,
                request.run_id,
            )
            return
        self._store.messages.append(
            message_id=message_id,
            chat_id=request.chat_id,
            run_id=request.run_id,
            role=MESSAGE_ROLE_ASSISTANT,
            content=content,
            status=MESSAGE_COMMITTED,
        )

    def _finish_aborted(
        self,
        *,
        request: ChatRunStreamRequest,
        user_message_id: str,
        chat_commands: ChatCommandService,
    ) -> ChatStreamEvent:
        # 中断时保留 user committed，丢弃已输出但不完整的 assistant 片段；
        # 这是本地历史的权威语义，不依赖 AnythingLLM 是否已经写入远端 Thread。
        self._commit_user(user_message_id)
        chat_commands.abort_chat_run(run_id=request.run_id)
        logger.info(
            "文件对话run按中断完成收敛: chat_id=%s run_id=%s",
            request.chat_id,
            request.run_id,
        )
        return ChatStreamEvent("aborted", {"chatId": request.chat_id})

    def _abort_requested(self, run_id: str) -> bool:
        run = self._store.runs.get(run_id)
        return bool(run and run.abort_requested)

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
