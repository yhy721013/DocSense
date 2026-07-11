"""Execution boundary for one file-chat run."""

from __future__ import annotations

import logging
from time import monotonic
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from threading import BoundedSemaphore
from typing import Protocol, runtime_checkable

from app.ports import ChatConversationFactory, ChatDocumentRef, ChatResourceError, ChatSessionRefs
from app.services.chat.application.command_service import ChatCommandService
from app.services.chat.application.document_resolver import (
    ChatDocumentResolver,
    ResolvedChatDocument,
)
from app.services.chat.domain.resource_ids import (
    chat_document_binding_lease_id,
    chat_thread_lease_id,
    chat_workspace_lease_id,
)
from app.services.chat.domain.events import ChatStreamEvent
from app.services.chat.domain.models import (
    MESSAGE_PENDING,
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_USER,
    RESOURCE_DOCUMENT_BINDING,
    RESOURCE_THREAD,
    RESOURCE_WORKSPACE,
    SESSION_ACTIVE,
)
from app.services.chat.locking.lease import ChatRunLease
from app.services.chat.persistence.store import ChatPersistenceStore
from app.services.core.settings import (
    CHAT_MAX_FILES_PER_REQUEST,
    CHAT_MAX_CONCURRENT_STREAMS,
    CHAT_MAX_MESSAGE_CHARS,
    CHAT_MAX_OUTPUT_CHARS,
)


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
    documents: tuple["ChatRunDocumentSnapshot", ...] = ()

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
        documents = tuple(self.documents)
        if any(not isinstance(item, ChatRunDocumentSnapshot) for item in documents):
            raise TypeError("documents must contain ChatRunDocumentSnapshot")
        if documents and tuple(item.file_name for item in documents) != self.file_names:
            raise ValueError("documents must match file_names in the same order")
        if documents and tuple(item.original_name for item in documents) != self.file_original_names:
            raise ValueError("documents must match file_original_names in the same order")
        object.__setattr__(self, "documents", documents)


@dataclass(frozen=True)
class ChatRunDocumentSnapshot:
    """Immutable document identity used by one synchronous chat execution."""

    file_name: str
    original_name: str
    document: ChatDocumentRef

    def __post_init__(self) -> None:
        object.__setattr__(self, "file_name", _required_text(self.file_name, name="file_name"))
        object.__setattr__(
            self,
            "original_name",
            _required_text(self.original_name, name="original_name"),
        )
        if not isinstance(self.document, ChatDocumentRef):
            raise TypeError("document must be ChatDocumentRef")


@dataclass(frozen=True)
class PreparedChatRun:
    """Accepted run and immutable execution input handed to the stream layer."""

    request: ChatRunStreamRequest
    execution_lease: ChatRunLease | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request, ChatRunStreamRequest):
            raise TypeError("request must be ChatRunStreamRequest")
        if self.execution_lease is not None:
            if not isinstance(self.execution_lease, ChatRunLease):
                raise TypeError("execution_lease must be ChatRunLease or None")
            if self.execution_lease.run_id != self.request.run_id:
                raise ValueError("execution_lease does not match request.run_id")
            if self.execution_lease.chat_id != self.request.chat_id:
                raise ValueError("execution_lease does not match request.chat_id")

    @property
    def run_id(self) -> str:
        return self.request.run_id


@runtime_checkable
class ChatRunExecutor(Protocol):
    """Executes a chat run and yields supplier-neutral stream events."""

    def stream_chat_run(
        self,
        request: ChatRunStreamRequest,
    ) -> Iterable[ChatStreamEvent]:
        """Execute one accepted/running run without producing presentation text."""
        ...


class SynchronousChatRunExecutor:
    """Single-instance executor that owns the new supplier-neutral chat path."""

    def __init__(
        self,
        *,
        store: ChatPersistenceStore,
        chat_commands: ChatCommandService,
        conversation_factory: ChatConversationFactory,
        document_resolver: ChatDocumentResolver,
        max_files_per_request: int = CHAT_MAX_FILES_PER_REQUEST,
        max_message_chars: int = CHAT_MAX_MESSAGE_CHARS,
        max_output_chars: int = CHAT_MAX_OUTPUT_CHARS,
        max_concurrent_streams: int = CHAT_MAX_CONCURRENT_STREAMS,
    ) -> None:
        if not isinstance(store, ChatPersistenceStore):
            raise TypeError("store must implement ChatPersistenceStore")
        if not isinstance(chat_commands, ChatCommandService):
            raise TypeError("chat_commands must be ChatCommandService")
        if not isinstance(conversation_factory, ChatConversationFactory):
            raise TypeError("conversation_factory must implement ChatConversationFactory")
        if not isinstance(document_resolver, ChatDocumentResolver):
            raise TypeError("document_resolver must implement ChatDocumentResolver")
        for name, value in (
            ("max_files_per_request", max_files_per_request),
            ("max_message_chars", max_message_chars),
            ("max_output_chars", max_output_chars),
            ("max_concurrent_streams", max_concurrent_streams),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self._store = store
        self._chat_commands = chat_commands
        self._conversation_factory = conversation_factory
        self._document_resolver = document_resolver
        self._max_files_per_request = max_files_per_request
        self._max_message_chars = max_message_chars
        self._max_output_chars = max_output_chars
        self._max_concurrent_streams = max_concurrent_streams
        self._stream_slots = BoundedSemaphore(max_concurrent_streams)

    @property
    def max_concurrent_streams(self) -> int:
        """Return the explicit single-process stream capacity."""
        return self._max_concurrent_streams

    def try_acquire_stream_slot(self) -> bool:
        """Reserve one synchronous stream slot without blocking a web worker."""
        return self._stream_slots.acquire(blocking=False)

    def release_stream_slot(self) -> None:
        """Release a slot reserved by the route once its SSE iterable closes."""
        self._stream_slots.release()

    def prepare_chat_run(
        self,
        *,
        chat_id: str,
        message: str,
        file_names: Sequence[str],
    ) -> PreparedChatRun:
        """Resolve immutable inputs and atomically accept a new single-chat run."""
        normalized_chat_id = _required_text(chat_id, name="chat_id")
        normalized_message = _required_text(message, name="message")
        normalized_file_names = _text_tuple(file_names, name="file_names")
        if len(normalized_file_names) > self._max_files_per_request:
            raise ValueError("fileNames exceeds the configured chat file limit")
        if len(normalized_message) > self._max_message_chars:
            raise ValueError("message exceeds the configured chat message limit")
        resolved = self._document_resolver.resolve_many(normalized_file_names)
        snapshots = tuple(self._snapshot(document) for document in resolved)
        if tuple(item.file_name for item in snapshots) != normalized_file_names:
            raise ValueError("document resolver returned unexpected file order")
        run = self._chat_commands.start_chat_run(
            chat_id=normalized_chat_id,
            user_message=normalized_message,
            user_files=tuple(
                (item.file_name, item.original_name) for item in snapshots
            ),
            input_documents=tuple(
                (
                    item.file_name,
                    item.original_name,
                    item.document.document_ref,
                    item.document.external_location,
                )
                for item in snapshots
            ),
        )
        request = ChatRunStreamRequest(
            run_id=run.run_id,
            chat_id=normalized_chat_id,
            message=normalized_message,
            file_names=tuple(item.file_name for item in snapshots),
            file_original_names=tuple(item.original_name for item in snapshots),
            documents=snapshots,
        )
        # 受理成功后立即签发内部执行租约。该租约不会进入 HTTP/SSE，而是由
        # dispatcher 传到 recorder，使未来 worker 可以在同一入口使用 fencing。
        # 若未来协调器在领取阶段失败，必须收敛已创建的 run，不能遗留 409 锁。
        try:
            execution_lease = self._chat_commands.issue_execution_lease(
                run_id=run.run_id,
            )
        except Exception as exc:
            logger.exception(
                "文件对话run签发执行租约失败，收敛已受理run: chat_id=%s run_id=%s",
                normalized_chat_id,
                run.run_id,
            )
            try:
                self._chat_commands.fail_chat_run_with_user(
                    run_id=run.run_id,
                    user_message_id=f"{run.run_id}:user",
                    error_message=str(exc) or exc.__class__.__name__,
                )
            except Exception:
                logger.exception(
                    "文件对话run执行租约失败后的终态收敛失败: chat_id=%s run_id=%s",
                    normalized_chat_id,
                    run.run_id,
                )
            raise
        return PreparedChatRun(
            request=request,
            execution_lease=execution_lease,
        )

    def stream_chat_run(
        self,
        request: ChatRunStreamRequest,
    ) -> Iterator[ChatStreamEvent]:
        """Create/reuse resources, bind new documents, and emit supplier-neutral events."""
        try:
            session = self._store.sessions.get(request.chat_id)
            if session is None or session.status != SESSION_ACTIVE:
                raise ChatResourceError("当前对话不可用于执行")
            documents = request.documents or tuple(
                self._snapshot(item)
                for item in self._document_resolver.resolve_many(request.file_names)
            )
            with self._conversation_factory.create() as conversation:
                refs, is_new_chat = self._open_or_reuse_conversation(
                    request=request,
                    session=session,
                    conversation=conversation,
                )
                active_documents = self._attach_new_documents(
                    request=request,
                    refs=refs,
                    documents=documents,
                    conversation=conversation,
                )
                yield ChatStreamEvent(
                    "chatInfo",
                    {"chatId": request.chat_id, "isNewChat": is_new_chat},
                )
                output_chars = 0
                for chunk in conversation.stream_message(
                    refs,
                    request.message,
                    document_refs=active_documents,
                ):
                    output_chars += len(chunk.content)
                    if output_chars > self._max_output_chars:
                        raise ChatResourceError(
                            "chat output exceeds the configured output limit"
                        )
                    yield ChatStreamEvent("textChunk", {"content": chunk.content})
                yield ChatStreamEvent("done", {"chatId": request.chat_id})
        except GeneratorExit:
            raise
        except Exception:
            logger.exception(
                "文件对话新主链路执行异常: chat_id=%s run_id=%s",
                request.chat_id,
                request.run_id,
            )
            yield ChatStreamEvent("error", {"error": "大模型服务响应异常"})

    def _open_or_reuse_conversation(
        self,
        *,
        request: ChatRunStreamRequest,
        session,
        conversation,
    ) -> tuple[ChatSessionRefs, bool]:
        if session.workspace_ref and session.thread_ref:
            return (
                ChatSessionRefs(
                    context_ref=session.workspace_ref,
                    conversation_ref=session.thread_ref,
                ),
                False,
            )
        workspace_lease_id = chat_workspace_lease_id(request.chat_id)
        thread_lease_id = chat_thread_lease_id(request.chat_id)
        self._store.resource_leases.begin(
            lease_id=workspace_lease_id,
            chat_id=request.chat_id,
            resource_type=RESOURCE_WORKSPACE,
            run_id=request.run_id,
        )
        self._store.resource_leases.begin(
            lease_id=thread_lease_id,
            chat_id=request.chat_id,
            resource_type=RESOURCE_THREAD,
            run_id=request.run_id,
        )
        try:
            refs = conversation.open_conversation(
                context_name=f"chat-{request.chat_id}",
                conversation_name=f"thread-{request.chat_id}",
            )
        except ChatResourceError as exc:
            self._record_uncompensated_workspace_reference(
                request=request,
                workspace_lease_id=workspace_lease_id,
                resource_refs=exc.resource_refs,
            )
            self._close_planned_lease(thread_lease_id)
            if not exc.resource_refs:
                self._close_planned_lease(workspace_lease_id)
            raise
        # Save remote references before changing the session. If the following
        # session update fails, cleanup can still discover both resources.
        self._store.resource_leases.activate(
            lease_id=workspace_lease_id,
            external_ref=refs.context_ref,
        )
        self._store.resource_leases.activate(
            lease_id=thread_lease_id,
            external_ref=f"{refs.context_ref}::{refs.conversation_ref}",
        )
        self._store.sessions.update_refs(
            chat_id=request.chat_id,
            workspace_ref=refs.context_ref,
            thread_ref=refs.conversation_ref,
        )
        return refs, True

    def _record_uncompensated_workspace_reference(
        self,
        *,
        request: ChatRunStreamRequest,
        workspace_lease_id: str,
        resource_refs: Sequence[str],
    ) -> None:
        """Persist a recoverable workspace reference reported by the adapter."""
        if not resource_refs:
            return
        workspace_ref = resource_refs[0]
        try:
            lease = self._store.resource_leases.begin(
                lease_id=workspace_lease_id,
                chat_id=request.chat_id,
                resource_type=RESOURCE_WORKSPACE,
                run_id=request.run_id,
                external_ref=workspace_ref,
            )
            if lease.status == "planned":
                self._store.resource_leases.activate(
                    lease_id=workspace_lease_id,
                    external_ref=workspace_ref,
                )
        except Exception:
            logger.exception(
                "failed to persist uncompensated workspace reference: chat_id=%s run_id=%s",
                request.chat_id,
                request.run_id,
            )

    def _close_planned_lease(self, lease_id: str) -> None:
        lease = self._store.resource_leases.get(lease_id)
        if lease is not None and lease.status == "planned":
            self._store.resource_leases.mark_closed(lease_id)

    def _attach_new_documents(
        self,
        *,
        request: ChatRunStreamRequest,
        refs: ChatSessionRefs,
        documents: Sequence[ChatRunDocumentSnapshot],
        conversation,
    ) -> tuple[str, ...]:
        known = {
            item.file_name: item
            for item in self._store.documents.list_by_chat(request.chat_id)
        }
        new_documents = [
            item
            for item in documents
            if (
                item.file_name not in known
                or known[item.file_name].document_ref != item.document.document_ref
                or known[item.file_name].external_location
                != item.document.external_location
            )
        ]
        for item in new_documents:
            self._store.resource_leases.begin(
                lease_id=chat_document_binding_lease_id(
                    chat_id=request.chat_id,
                    file_name=item.file_name,
                    document_ref=item.document.document_ref,
                ),
                chat_id=request.chat_id,
                resource_type=RESOURCE_DOCUMENT_BINDING,
                run_id=request.run_id,
                external_ref=f"{refs.context_ref}::{item.document.external_location}",
            )
        attached_by_location: dict[str, ChatDocumentRef] = {}
        if new_documents:
            attached = conversation.attach_documents(
                refs,
                [item.document for item in new_documents],
            )
            attached_by_location = {
                item.external_location: item for item in attached if item.external_location
            }
            for item in new_documents:
                attached_document = attached_by_location.get(
                    item.document.external_location,
                    item.document,
                )
                stored_document = self._store.documents.add(
                    chat_id=request.chat_id,
                    file_name=item.file_name,
                    original_name=item.original_name,
                    document_ref=attached_document.document_ref,
                    external_location=attached_document.external_location,
                    added_by_run_id=request.run_id,
                )
                self._store.resource_leases.activate(
                    lease_id=chat_document_binding_lease_id(
                        chat_id=request.chat_id,
                        file_name=item.file_name,
                        document_ref=item.document.document_ref,
                    ),
                    external_ref=(
                        f"{refs.context_ref}::{attached_document.external_location}"
                    ),
                )
                known[item.file_name] = stored_document
        selected: list[str] = []
        for item in documents:
            stored = known.get(item.file_name)
            document_ref = stored.document_ref if stored and stored.document_ref else item.document.document_ref
            selected.append(document_ref)
        return tuple(selected)

    @staticmethod
    def _snapshot(document: ResolvedChatDocument) -> ChatRunDocumentSnapshot:
        return ChatRunDocumentSnapshot(
            file_name=document.file_name,
            original_name=document.original_name,
            document=document.document,
        )


class ChatRunEventRecorder:
    """Persist local authoritative messages while preserving stream events."""

    def __init__(
        self,
        store: ChatPersistenceStore,
        *,
        heartbeat_interval_seconds: float = 10.0,
    ) -> None:
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")
        self._store = store
        self._heartbeat_interval_seconds = heartbeat_interval_seconds

    def record(
        self,
        *,
        request: ChatRunStreamRequest,
        events: Iterable[ChatStreamEvent],
        chat_commands: ChatCommandService,
        execution_lease: ChatRunLease | None = None,
    ) -> Iterator[ChatStreamEvent]:
        user_message_id = self._message_id(request.run_id, MESSAGE_ROLE_USER)
        assistant_message_id = self._message_id(
            request.run_id,
            MESSAGE_ROLE_ASSISTANT,
        )
        user_written = False
        terminal_event = ""
        assistant_parts: list[str] = []
        last_heartbeat_at: float | None = None
        try:
            if execution_lease is not None:
                # 在访问外部会话资源之前校验运行权。当前 SQLite 仅作单实例
                # 校验；未来协调器会在这里校验实际 token/fencing 信息。
                chat_commands.validate_execution_lease(lease=execution_lease)
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
                        execution_lease=execution_lease,
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
                        execution_lease=execution_lease,
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
                        chat_commands.complete_chat_run_with_messages(
                            run_id=request.run_id,
                            user_message_id=user_message_id,
                            assistant_message_id=assistant_message_id,
                            assistant_content="".join(assistant_parts),
                            terminal_event=event,
                            **self._execution_lease_kwargs(execution_lease),
                        )
                    elif event.event_type == "aborted":
                        chat_commands.abort_chat_run_with_user(
                            run_id=request.run_id,
                            user_message_id=user_message_id,
                            terminal_event=event,
                            **self._execution_lease_kwargs(execution_lease),
                        )
                    else:
                        chat_commands.fail_chat_run_with_user(
                            run_id=request.run_id,
                            user_message_id=user_message_id,
                            error_message="chat stream emitted error event",
                            terminal_event=event,
                            **self._execution_lease_kwargs(execution_lease),
                        )
                    terminal_event = event.event_type
                else:
                    self._store.events.append(
                        run_id=request.run_id,
                        event=event,
                    )
                    last_heartbeat_at = self._heartbeat_if_due(
                        run_id=request.run_id,
                        chat_commands=chat_commands,
                        last_heartbeat_at=last_heartbeat_at,
                        execution_lease=execution_lease,
                    )
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
                        execution_lease=execution_lease,
                    )
                else:
                    logger.warning(
                        "文件对话run因缺失终态标记失败: chat_id=%s run_id=%s",
                        request.chat_id,
                        request.run_id,
                    )
                    self._finish_failed(
                        request=request,
                        run_id=request.run_id,
                        user_message_id=user_message_id,
                        error_message="chat stream ended without terminal event",
                        chat_commands=chat_commands,
                        execution_lease=execution_lease,
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
                        execution_lease=execution_lease,
                    )
                else:
                    logger.warning(
                        "SSE连接在终态前关闭: chat_id=%s run_id=%s",
                        request.chat_id,
                        request.run_id,
                    )
                    self._finish_failed(
                        request=request,
                        run_id=request.run_id,
                        user_message_id=user_message_id,
                        error_message="chat stream closed before completion",
                        chat_commands=chat_commands,
                        execution_lease=execution_lease,
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
                        execution_lease=execution_lease,
                    )
                else:
                    chat_commands.abort_chat_run(run_id=request.run_id)
                return
            if not terminal_event:
                logger.exception(
                    "文件对话run事件记录异常，按失败收敛: chat_id=%s run_id=%s",
                    request.chat_id,
                    request.run_id,
                )
                if user_written:
                    self._finish_failed(
                        request=request,
                        run_id=request.run_id,
                        user_message_id=user_message_id,
                        error_message=str(exc) or exc.__class__.__name__,
                        chat_commands=chat_commands,
                        execution_lease=execution_lease,
                    )
                else:
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
        execution_lease: ChatRunLease | None,
    ) -> ChatStreamEvent:
        # 中断时保留 user committed，丢弃已输出但不完整的 assistant 片段；
        # 这是本地历史的权威语义，不依赖 AnythingLLM 是否已经写入远端 Thread。
        event = ChatStreamEvent("aborted", {"chatId": request.chat_id})
        chat_commands.abort_chat_run_with_user(
            run_id=request.run_id,
            user_message_id=user_message_id,
            terminal_event=event,
            **self._execution_lease_kwargs(execution_lease),
        )
        logger.info(
            "文件对话run按中断完成收敛: chat_id=%s run_id=%s",
            request.chat_id,
            request.run_id,
        )
        return event

    @staticmethod
    def _failure_event(*, error_message: str) -> ChatStreamEvent:
        return ChatStreamEvent("error", {"error": error_message})

    def _finish_failed(
        self,
        *,
        request: ChatRunStreamRequest,
        run_id: str,
        user_message_id: str,
        error_message: str,
        chat_commands: ChatCommandService,
        execution_lease: ChatRunLease | None,
    ) -> None:
        """Persist a non-presented terminal error while preserving run cleanup.

        A client-side disconnect has no final SSE frame in the frozen protocol.
        The internal ledger still needs a terminal record, but a ledger failure
        must not leave the active run locked forever.
        """
        terminal_event = self._failure_event(error_message=error_message)
        try:
            chat_commands.fail_chat_run_with_user(
                run_id=run_id,
                user_message_id=user_message_id,
                error_message=error_message,
                terminal_event=terminal_event,
                **self._execution_lease_kwargs(execution_lease),
            )
        except Exception:
            logger.exception(
                "终态事件写入失败，降级收敛文件对话run: chat_id=%s run_id=%s",
                request.chat_id,
                run_id,
            )
            chat_commands.fail_chat_run_with_user(
                run_id=run_id,
                user_message_id=user_message_id,
                error_message=error_message,
                **self._execution_lease_kwargs(execution_lease),
            )

    def _abort_requested(self, run_id: str) -> bool:
        run = self._store.runs.get(run_id)
        return bool(run and run.abort_requested)

    def _heartbeat_if_due(
        self,
        *,
        run_id: str,
        chat_commands: ChatCommandService,
        last_heartbeat_at: float | None,
        execution_lease: ChatRunLease | None,
    ) -> float:
        now = monotonic()
        if (
            last_heartbeat_at is not None
            and now - last_heartbeat_at < self._heartbeat_interval_seconds
        ):
            return last_heartbeat_at
        chat_commands.heartbeat_chat_run(
            run_id=run_id,
            **self._execution_lease_kwargs(execution_lease),
        )
        return now

    @staticmethod
    def _execution_lease_kwargs(
        execution_lease: ChatRunLease | None,
    ) -> dict[str, ChatRunLease]:
        """仅在有内部执行租约时把它传给命令层，兼容已有离线调用。"""
        if execution_lease is None:
            return {}
        return {"execution_lease": execution_lease}

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
    execution_lease: ChatRunLease | None = None,
) -> Iterator[ChatStreamEvent]:
    return ChatRunEventRecorder(store).record(
        request=request,
        events=events,
        chat_commands=chat_commands,
        execution_lease=execution_lease,
    )


__all__ = [
    "ChatRunEventRecorder",
    "ChatRunExecutor",
    "ChatRunStreamRequest",
    "record_chat_run_events",
]
