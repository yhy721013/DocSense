"""Durable deletion workflow for file-chat sessions."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.ports import (
    ChatConversationFactory,
    ChatConversationNotFoundError,
    ChatOperationResult,
    ChatPortError,
    ChatSessionRefs,
)
from app.services.chat.domain.models import (
    LEASE_CLEANUP_FAILED,
    LEASE_CLOSED,
    LEASE_PLANNED,
    RESOURCE_DOCUMENT_BINDING,
    RESOURCE_THREAD,
    RESOURCE_WORKSPACE,
    SESSION_DELETED,
    SESSION_DELETING,
    SESSION_ERROR,
    ChatResourceLease,
    ChatSession,
)
from app.services.chat.domain.resource_ids import (
    chat_thread_lease_id,
    chat_workspace_lease_id,
)
from app.services.chat.application.command_service import ChatCommandService
from app.services.chat.application.cleanup_dispatcher import (
    ChatCleanupDispatchCapabilities,
    ChatCleanupDispatcher,
    ChatCleanupTask,
    InlineChatCleanupDispatcher,
)
from app.services.chat.locking.lock_service import (
    ChatSessionDeleteBusyError,
    ChatSessionUnavailableError,
)
from app.services.chat.persistence.store import ChatPersistenceStore


logger = logging.getLogger(__name__)


def _required_text(value: str, *, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} cannot be empty")
    return normalized


@dataclass(frozen=True)
class ChatDeleteResult:
    """API-facing result for a successful idempotent delete request."""

    chat_id: str
    deleted: bool
    msg: str

    def to_response(self) -> dict[str, object]:
        return {
            "chatId": self.chat_id,
            "deleted": self.deleted,
            "msg": self.msg,
        }


class ChatDeleteNotFoundError(ValueError):
    """Raised when the local authoritative session does not exist."""

    def __init__(self, chat_id: str) -> None:
        self.chat_id = chat_id
        super().__init__("对话不存在")


class ChatDeleteCleanupError(RuntimeError):
    """Raised after cleanup failure has been persisted for retry/audit."""

    def __init__(
        self,
        *,
        chat_id: str,
        failed_leases: tuple[ChatResourceLease, ...],
    ) -> None:
        self.chat_id = chat_id
        self.failed_leases = failed_leases
        failed_refs = ",".join(lease.external_ref for lease in failed_leases)
        message = "对话资源清理失败"
        if failed_refs:
            message = f"{message}: {failed_refs}"
        super().__init__(message)


class ChatDeleteBusyError(RuntimeError):
    """Raised when delete cannot enter its exclusive session state."""

    def __init__(self, *, chat_id: str, reason: str) -> None:
        self.chat_id = chat_id
        self.reason = reason
        super().__init__(reason)


class ChatDeleteService:
    """Delete chat resources with a persistent recovery trail.

    The service treats the database as the source of truth. Remote cleanup is
    attempted through the supplier-neutral Chat Port, while every external
    resource reference is represented by a durable lease before deletion starts.
    """

    def __init__(
        self,
        *,
        store: ChatPersistenceStore,
        chat_commands: ChatCommandService,
        conversation_factory: ChatConversationFactory,
        cleanup_dispatcher: ChatCleanupDispatcher | None = None,
    ) -> None:
        self._store = store
        self._chat_commands = chat_commands
        self._conversation_factory = conversation_factory
        self._cleanup_dispatcher = cleanup_dispatcher or InlineChatCleanupDispatcher()
        if not isinstance(self._cleanup_dispatcher, ChatCleanupDispatcher):
            raise TypeError("cleanup_dispatcher must implement ChatCleanupDispatcher")

    @property
    def cleanup_dispatcher_capabilities(self) -> ChatCleanupDispatchCapabilities:
        """向容器暴露清理调度能力，避免把同步实现误用于集群模式。"""
        return self._cleanup_dispatcher.capabilities

    def delete_chat(self, *, chat_id: str) -> ChatDeleteResult:
        normalized_chat_id = _required_text(chat_id, name="chat_id")
        logger.info("收到文件对话删除指令: chat_id=%s", normalized_chat_id)

        session = self._store.sessions.get(normalized_chat_id)
        if session is None:
            raise ChatDeleteNotFoundError(normalized_chat_id)
        if session.status == SESSION_DELETED:
            logger.info(
                "文件对话已处于deleted状态，删除请求幂等返回: chat_id=%s",
                normalized_chat_id,
            )
            return ChatDeleteResult(
                chat_id=normalized_chat_id,
                deleted=True,
                msg="对话已删除",
            )

        try:
            self._chat_commands.begin_chat_deletion(chat_id=normalized_chat_id)
        except ChatSessionDeleteBusyError as exc:
            raise ChatDeleteBusyError(
                chat_id=normalized_chat_id,
                reason=exc.reason,
            ) from exc
        except ChatSessionUnavailableError as exc:
            raise ChatDeleteBusyError(
                chat_id=normalized_chat_id,
                reason=str(exc),
            ) from exc
        session = self._store.sessions.get(normalized_chat_id)
        if session is None:
            raise ChatDeleteNotFoundError(normalized_chat_id)

        self._ensure_reference_leases(session)
        if not session.workspace_ref:
            open_leases = self._store.resource_leases.list_by_chat(
                normalized_chat_id,
                include_closed=False,
            )
            if open_leases:
                self._record_all_open_cleanup_failed(
                    normalized_chat_id,
                    error_message="remote resource reference was not persisted",
                )
                self._store.sessions.set_status(
                    chat_id=normalized_chat_id,
                    status=SESSION_ERROR,
                )
                raise ChatDeleteCleanupError(
                    chat_id=normalized_chat_id,
                    failed_leases=self._failed_leases(normalized_chat_id),
                )
            self._mark_deleted(normalized_chat_id)
            logger.info(
                "文件对话无远端上下文引用，直接标记deleted: chat_id=%s",
                normalized_chat_id,
            )
            return ChatDeleteResult(
                chat_id=normalized_chat_id,
                deleted=True,
                msg="对话已删除",
            )

        logger.info(
            "文件对话进入deleting状态: chat_id=%s workspace_ref=%s thread_ref=%s",
            normalized_chat_id,
            session.workspace_ref,
            session.thread_ref,
        )

        try:
            # 无论是调用方发起的重复删除，还是未来调度器触发的补偿，远端
            # 清理都必须经过同一个 dispatcher 边界，不能退回为人工扫描状态表。
            self._cleanup_dispatcher.dispatch(
                task=ChatCleanupTask(
                    chat_id=normalized_chat_id,
                    reason="delete_chat",
                ),
                execute=lambda: self._cleanup_remote_resources(
                    chat_id=normalized_chat_id,
                    session=session,
                ),
            )
        except Exception as exc:
            self._record_all_open_cleanup_failed(
                normalized_chat_id,
                error_message=str(exc) or exc.__class__.__name__,
            )
            self._store.sessions.set_status(
                chat_id=normalized_chat_id,
                status=SESSION_ERROR,
            )
            failed = self._failed_leases(normalized_chat_id)
            logger.exception(
                "文件对话删除执行异常，已保留cleanup_failed租约: chat_id=%s failed_count=%d",
                normalized_chat_id,
                len(failed),
            )
            raise ChatDeleteCleanupError(
                chat_id=normalized_chat_id,
                failed_leases=failed,
            ) from exc

        failed = self._failed_leases(normalized_chat_id)
        if failed:
            self._store.sessions.set_status(
                chat_id=normalized_chat_id,
                status=SESSION_ERROR,
            )
            logger.warning(
                "文件对话删除未完成，已保留cleanup_failed租约: chat_id=%s failed_count=%d",
                normalized_chat_id,
                len(failed),
            )
            raise ChatDeleteCleanupError(
                chat_id=normalized_chat_id,
                failed_leases=failed,
            )

        self._mark_deleted(normalized_chat_id)
        return ChatDeleteResult(
            chat_id=normalized_chat_id,
            deleted=True,
            msg="对话已删除",
        )

    def _cleanup_remote_resources(
        self,
        *,
        chat_id: str,
        session: ChatSession,
    ) -> None:
        """执行一次远端资源删除，供同步或未来 worker dispatcher 调用。

        函数本身不决定 HTTP 返回值；所有失败都会回到 ``delete_chat`` 的统一
        状态收敛逻辑，确保租约先被标记为 ``cleanup_failed`` 再对外报告失败。
        """
        with self._conversation_factory.create() as port:
            if session.thread_ref:
                self._delete_conversation(
                    chat_id=chat_id,
                    port=port,
                    session=ChatSessionRefs(
                        context_ref=session.workspace_ref,
                        conversation_ref=session.thread_ref,
                    ),
                )
            self._delete_context(
                chat_id=chat_id,
                port=port,
                context_ref=session.workspace_ref,
            )

    def _ensure_reference_leases(self, session: ChatSession) -> None:
        if session.workspace_ref:
            self._store.resource_leases.ensure_active(
                lease_id=chat_workspace_lease_id(session.chat_id),
                chat_id=session.chat_id,
                resource_type=RESOURCE_WORKSPACE,
                external_ref=session.workspace_ref,
            )
        if session.thread_ref:
            self._store.resource_leases.ensure_active(
                lease_id=chat_thread_lease_id(session.chat_id),
                chat_id=session.chat_id,
                resource_type=RESOURCE_THREAD,
                external_ref=f"{session.workspace_ref}::{session.thread_ref}",
            )

    def _delete_conversation(
        self,
        *,
        chat_id: str,
        port,
        session: ChatSessionRefs,
    ) -> None:
        lease_id = chat_thread_lease_id(chat_id)
        self._mark_pending_if_open(lease_id)
        error = self._execute_delete_operation(
            lambda: port.delete_conversation(session),
            not_found_message="conversation already absent",
        )
        if error:
            self._store.resource_leases.record_cleanup_failure(
                lease_id=lease_id,
                error_message=error,
            )
            logger.warning(
                "文件对话thread删除失败，等待context删除或后续补偿: chat_id=%s error=%s",
                chat_id,
                error,
            )
            return
        self._store.resource_leases.mark_closed(lease_id)
        logger.info("文件对话thread删除完成: chat_id=%s", chat_id)

    def _delete_context(self, *, chat_id: str, port, context_ref: str) -> None:
        workspace_lease_id = chat_workspace_lease_id(chat_id)
        self._mark_pending_if_open(workspace_lease_id)
        for lease in self._document_binding_leases(chat_id):
            self._mark_pending_if_open(lease.lease_id)

        error = self._execute_delete_operation(
            lambda: port.delete_context(context_ref),
            not_found_message="context already absent",
        )
        if error:
            self._store.resource_leases.record_cleanup_failure(
                lease_id=workspace_lease_id,
                error_message=error,
            )
            for lease in self._document_binding_leases(chat_id):
                if lease.status != LEASE_CLOSED:
                    self._store.resource_leases.record_cleanup_failure(
                        lease_id=lease.lease_id,
                        error_message=error,
                    )
            logger.warning(
                "文件对话workspace删除失败，保留补偿租约: chat_id=%s error=%s",
                chat_id,
                error,
            )
            return

        # Deleting the context/workspace removes contained thread and document
        # bindings as well. Close dependent leases even if a narrower delete
        # failed earlier in this same attempt.
        self._store.resource_leases.mark_closed(workspace_lease_id)
        thread_lease = self._store.resource_leases.get(chat_thread_lease_id(chat_id))
        if thread_lease is not None and thread_lease.status != LEASE_CLOSED:
            self._store.resource_leases.mark_closed(thread_lease.lease_id)
        for lease in self._document_binding_leases(chat_id):
            if lease.status != LEASE_CLOSED:
                self._store.resource_leases.mark_closed(lease.lease_id)
        logger.info("文件对话workspace删除完成: chat_id=%s", chat_id)

    @staticmethod
    def _execute_delete_operation(operation, *, not_found_message: str) -> str:
        try:
            result = operation()
        except ChatConversationNotFoundError:
            logger.info("文件对话远端资源已不存在，按幂等成功处理: %s", not_found_message)
            return ""
        except ChatPortError as exc:
            return str(exc) or exc.__class__.__name__
        if not isinstance(result, ChatOperationResult):
            raise TypeError("delete operation must return ChatOperationResult")
        if result.success:
            return ""
        return result.error_message

    def _mark_pending_if_open(self, lease_id: str) -> ChatResourceLease | None:
        lease = self._store.resource_leases.get(lease_id)
        if lease is None or lease.status == LEASE_CLOSED:
            return lease
        return self._store.resource_leases.mark_cleanup_pending(lease_id)

    def _document_binding_leases(self, chat_id: str) -> tuple[ChatResourceLease, ...]:
        return tuple(
            lease
            for lease in self._store.resource_leases.list_by_chat(chat_id)
            if lease.resource_type == RESOURCE_DOCUMENT_BINDING
        )

    def _failed_leases(self, chat_id: str) -> tuple[ChatResourceLease, ...]:
        return tuple(
            lease
            for lease in self._store.resource_leases.list_by_chat(chat_id)
            if lease.status == LEASE_CLEANUP_FAILED
        )

    def _record_all_open_cleanup_failed(
        self,
        chat_id: str,
        *,
        error_message: str,
    ) -> None:
        for lease in self._store.resource_leases.list_by_chat(
            chat_id,
            include_closed=False,
        ):
            if lease.status == LEASE_CLOSED:
                continue
            if lease.status != LEASE_CLEANUP_FAILED:
                if lease.status == LEASE_PLANNED:
                    self._store.resource_leases.mark_cleanup_pending(lease.lease_id)
                self._store.resource_leases.record_cleanup_failure(
                    lease_id=lease.lease_id,
                    error_message=error_message,
                )

    def _mark_deleted(self, chat_id: str) -> None:
        self._store.sessions.set_status(chat_id=chat_id, status=SESSION_DELETED)
        logger.info("文件对话删除状态机完成: chat_id=%s status=deleted", chat_id)



__all__ = [
    "ChatDeleteBusyError",
    "ChatDeleteCleanupError",
    "ChatDeleteNotFoundError",
    "ChatDeleteResult",
    "ChatDeleteService",
]
