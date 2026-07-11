"""HTTP-facing deletion workflow for file-chat sessions.

This service owns session admission and the frozen delete response semantics.
Actual remote resource deletion lives in ``cleanup_service`` and is invoked by
the replaceable cleanup dispatcher with a durable job ID.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.ports import ChatConversationFactory
from app.services.chat.application.cleanup_dispatcher import (
    ChatCleanupDispatchCapabilities,
    ChatCleanupDispatcher,
    InlineChatCleanupDispatcher,
)
from app.services.chat.application.cleanup_service import (
    ChatCleanupJobExecutionError,
    ChatCleanupJobExecutor,
)
from app.services.chat.application.command_service import ChatCommandService
from app.services.chat.domain.models import (
    CLEANUP_JOB_SUCCEEDED,
    CLEANUP_REASON_DELETE_CHAT,
    LEASE_CLEANUP_FAILED,
    LEASE_CLOSED,
    LEASE_PLANNED,
    SESSION_DELETED,
    SESSION_ERROR,
    ChatResourceLease,
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
    """Raised after cleanup failure has been persisted for retry and audit."""

    def __init__(
        self,
        *,
        chat_id: str,
        failed_leases: tuple[ChatResourceLease, ...],
    ) -> None:
        self.chat_id = chat_id
        self.failed_leases = failed_leases
        # Remote resource references are operational/audit data rather than
        # public API content.  Keep the frozen error payload stable and avoid
        # leaking supplier-side identifiers through the HTTP error message.
        super().__init__("对话资源清理失败")


class ChatDeleteBusyError(RuntimeError):
    """Raised when delete cannot enter its exclusive session state."""

    def __init__(self, *, chat_id: str, reason: str) -> None:
        self.chat_id = chat_id
        self.reason = reason
        super().__init__(reason)


class ChatDeleteService:
    """Accept a delete request and synchronously observe its durable cleanup.

    The current public endpoint promises a completed delete only after remote
    cleanup succeeds.  Therefore the single-instance composition must install
    a dispatcher with ``supports_synchronous_completion=True``.  A future
    asynchronous scheduler can reuse the cleanup executor, but cannot silently
    replace this endpoint's semantics without an explicitly designed API change.
    """

    def __init__(
        self,
        *,
        store: ChatPersistenceStore,
        chat_commands: ChatCommandService,
        conversation_factory: ChatConversationFactory,
        cleanup_dispatcher: ChatCleanupDispatcher | None = None,
        cleanup_executor: ChatCleanupJobExecutor | None = None,
    ) -> None:
        if not isinstance(store, ChatPersistenceStore):
            raise TypeError("store must implement ChatPersistenceStore")
        if not isinstance(chat_commands, ChatCommandService):
            raise TypeError("chat_commands must be ChatCommandService")
        if not isinstance(conversation_factory, ChatConversationFactory):
            raise TypeError(
                "conversation_factory must implement ChatConversationFactory"
            )
        if cleanup_executor is not None and not isinstance(
            cleanup_executor,
            ChatCleanupJobExecutor,
        ):
            raise TypeError("cleanup_executor must be ChatCleanupJobExecutor")

        self._store = store
        self._chat_commands = chat_commands
        self._conversation_factory = conversation_factory
        self._cleanup_executor = cleanup_executor or ChatCleanupJobExecutor(
            store=store,
            conversation_factory=conversation_factory,
        )
        self._cleanup_dispatcher = cleanup_dispatcher or InlineChatCleanupDispatcher(
            execute=self._cleanup_executor.execute_cleanup_job,
        )
        if not isinstance(self._cleanup_dispatcher, ChatCleanupDispatcher):
            raise TypeError("cleanup_dispatcher must implement ChatCleanupDispatcher")
        if not self._cleanup_dispatcher.capabilities.supports_synchronous_completion:
            raise ValueError(
                "the existing delete endpoint requires synchronous cleanup completion"
            )

    @property
    def cleanup_dispatcher_capabilities(self) -> ChatCleanupDispatchCapabilities:
        """Expose actual dispatch behaviour for composition-root validation."""
        return self._cleanup_dispatcher.capabilities

    def delete_chat(self, *, chat_id: str) -> ChatDeleteResult:
        normalized_chat_id = _required_text(chat_id, name="chat_id")
        logger.info("收到文件对话删除指令: chat_id=%s", normalized_chat_id)

        session = self._store.sessions.get(normalized_chat_id)
        if session is None:
            raise ChatDeleteNotFoundError(normalized_chat_id)
        if session.status == SESSION_DELETED:
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

        # The durable job is recorded before any remote side effect.  The
        # inline adapter invokes the same job executor that a future worker
        # will use, keeping the current synchronous API path queue-shaped.
        cleanup_job = self._store.cleanup_jobs.enqueue(
            chat_id=normalized_chat_id,
            reason=CLEANUP_REASON_DELETE_CHAT,
        )
        try:
            completed_job = self._cleanup_dispatcher.dispatch(job=cleanup_job)
        except ChatCleanupJobExecutionError as exc:
            self._mark_session_cleanup_error(
                chat_id=normalized_chat_id,
                error_message=exc.reason,
            )
            raise ChatDeleteCleanupError(
                chat_id=normalized_chat_id,
                failed_leases=self._failed_leases(normalized_chat_id),
            ) from exc
        except Exception as exc:
            # Dispatch failures are also persisted as lease failures.  The
            # original exception remains the cause for server-side diagnosis,
            # while the route retains its stable cleanup-error response.
            self._mark_session_cleanup_error(
                chat_id=normalized_chat_id,
                error_message=str(exc) or exc.__class__.__name__,
            )
            raise ChatDeleteCleanupError(
                chat_id=normalized_chat_id,
                failed_leases=self._failed_leases(normalized_chat_id),
            ) from exc

        if completed_job.status != CLEANUP_JOB_SUCCEEDED:
            self._mark_session_cleanup_error(
                chat_id=normalized_chat_id,
                error_message=(
                    completed_job.error_message
                    or "cleanup dispatcher did not complete the job"
                ),
            )
            raise ChatDeleteCleanupError(
                chat_id=normalized_chat_id,
                failed_leases=self._failed_leases(normalized_chat_id),
            )

        failed = self._failed_leases(normalized_chat_id)
        if failed:
            self._store.sessions.set_status(
                chat_id=normalized_chat_id,
                status=SESSION_ERROR,
            )
            raise ChatDeleteCleanupError(
                chat_id=normalized_chat_id,
                failed_leases=failed,
            )

        self._store.sessions.set_status(
            chat_id=normalized_chat_id,
            status=SESSION_DELETED,
        )
        logger.info("文件对话删除完成: chat_id=%s", normalized_chat_id)
        return ChatDeleteResult(
            chat_id=normalized_chat_id,
            deleted=True,
            msg="对话已删除",
        )

    def _mark_session_cleanup_error(self, *, chat_id: str, error_message: str) -> None:
        """Keep every unresolved local lease visible after a failed dispatch."""
        for lease in self._store.resource_leases.list_by_chat(
            chat_id,
            include_closed=False,
        ):
            if lease.status == LEASE_CLOSED:
                continue
            if lease.status == LEASE_PLANNED:
                self._store.resource_leases.mark_cleanup_pending(lease.lease_id)
            if lease.status != LEASE_CLEANUP_FAILED:
                self._store.resource_leases.record_cleanup_failure(
                    lease_id=lease.lease_id,
                    error_message=error_message,
                )
        self._store.sessions.set_status(
            chat_id=chat_id,
            status=SESSION_ERROR,
        )

    def _failed_leases(self, chat_id: str) -> tuple[ChatResourceLease, ...]:
        return tuple(
            lease
            for lease in self._store.resource_leases.list_by_chat(chat_id)
            if lease.status == LEASE_CLEANUP_FAILED
        )


__all__ = [
    "ChatDeleteBusyError",
    "ChatDeleteCleanupError",
    "ChatDeleteNotFoundError",
    "ChatDeleteResult",
    "ChatDeleteService",
]
