"""Durable resource-cleanup execution for file-chat workflows.

The module owns *how* a persisted cleanup job is executed.  HTTP-facing
services only decide when a job should be created and how its result maps to
their existing response contract.  This separation is important because a
future scheduler can call :meth:`ChatCleanupJobExecutor.execute_cleanup_job`
with the same durable ``job_id`` without recreating request-local closures.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.ports import (
    ChatConversationFactory,
    ChatConversationNotFoundError,
    ChatOperationResult,
    ChatPortError,
    ChatSessionRefs,
)
from app.services.chat.domain.models import (
    CLEANUP_JOB_RUNNING,
    CLEANUP_JOB_SUCCEEDED,
    CLEANUP_REASON_DELETE_CHAT,
    CLEANUP_REASON_TEMPORARY_THREAD,
    LEASE_CLOSED,
    LEASE_PLANNED,
    RESOURCE_DOCUMENT_BINDING,
    RESOURCE_THREAD,
    RESOURCE_WORKSPACE,
    ChatCleanupJob,
    ChatResourceLease,
    ChatSession,
)
from app.services.chat.domain.resource_ids import (
    chat_scoped_external_ref,
    chat_thread_lease_id,
    chat_workspace_lease_id,
    parse_chat_scoped_external_ref,
)
from app.services.chat.persistence.store import ChatPersistenceStore


logger = logging.getLogger(__name__)

DEFAULT_CLEANUP_RETRY_BASE_SECONDS = 30
DEFAULT_CLEANUP_RETRY_MAX_SECONDS = 15 * 60


class ChatCleanupJobExecutionError(RuntimeError):
    """Raised after a cleanup attempt has been durably marked as failed."""

    def __init__(self, *, job: ChatCleanupJob, reason: str) -> None:
        self.job = job
        self.reason = str(reason or "cleanup job failed").strip()
        super().__init__(self.reason)


class ChatCleanupJobExecutor:
    """Execute persisted cleanup jobs through the supplier-neutral Chat Port.

    The executor deliberately has no HTTP request, SSE stream, callback or
    in-memory task state.  Its input is only ``job_id``; all resource identity
    is reloaded from the authoritative local store before a remote operation.
    """

    def __init__(
        self,
        *,
        store: ChatPersistenceStore,
        conversation_factory: ChatConversationFactory,
        retry_base_seconds: int = DEFAULT_CLEANUP_RETRY_BASE_SECONDS,
        retry_max_seconds: int = DEFAULT_CLEANUP_RETRY_MAX_SECONDS,
    ) -> None:
        if not isinstance(store, ChatPersistenceStore):
            raise TypeError("store must implement ChatPersistenceStore")
        if not isinstance(conversation_factory, ChatConversationFactory):
            raise TypeError(
                "conversation_factory must implement ChatConversationFactory"
            )
        if (
            isinstance(retry_base_seconds, bool)
            or not isinstance(retry_base_seconds, int)
            or retry_base_seconds < 1
        ):
            raise ValueError("retry_base_seconds must be a positive integer")
        if (
            isinstance(retry_max_seconds, bool)
            or not isinstance(retry_max_seconds, int)
            or retry_max_seconds < retry_base_seconds
        ):
            raise ValueError(
                "retry_max_seconds must be an integer no smaller than retry_base_seconds"
            )
        self._store = store
        self._conversation_factory = conversation_factory
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds

    def execute_cleanup_job(self, *, job_id: str) -> ChatCleanupJob:
        """Claim and execute exactly one durable cleanup job.

        The claim transition makes concurrent schedulers safe at the job level.
        A failed remote operation is recorded before this method raises, so a
        caller never has to infer whether retry evidence was written.
        """
        job = self._store.cleanup_jobs.claim(job_id=job_id)
        if job.status == CLEANUP_JOB_SUCCEEDED:
            return job

        try:
            if job.reason == CLEANUP_REASON_DELETE_CHAT:
                self._cleanup_deleted_chat(job)
            elif job.reason == CLEANUP_REASON_TEMPORARY_THREAD:
                self._cleanup_temporary_thread(job)
            else:  # Defensive guard for data written by a future application version.
                raise ValueError(f"unsupported cleanup job reason: {job.reason}")
        except Exception as exc:
            failed_job = self._mark_failed(job=job, error=exc)
            logger.warning(
                "文件对话清理任务失败并已保留重试记录: job_id=%s chat_id=%s reason=%s attempt=%d error=%s",
                failed_job.job_id,
                failed_job.chat_id,
                failed_job.reason,
                failed_job.attempt_count,
                failed_job.error_message,
            )
            raise ChatCleanupJobExecutionError(
                job=failed_job,
                reason=failed_job.error_message,
            ) from exc

        completed = self._store.cleanup_jobs.mark_succeeded(job_id=job.job_id)
        logger.info(
            "文件对话清理任务完成: job_id=%s chat_id=%s reason=%s attempt=%d",
            completed.job_id,
            completed.chat_id,
            completed.reason,
            completed.attempt_count,
        )
        return completed

    def execute_ready_cleanup_jobs(self, *, limit: int = 100) -> tuple[ChatCleanupJob, ...]:
        """Run a bounded snapshot of ready jobs for a future maintenance loop.

        The current inline dispatcher invokes a specific newly-created job and
        does not call this method automatically.  Keeping this maintenance
        entry point bounded prevents a future scheduler from monopolising a
        worker when historical failures accumulate.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        results: list[ChatCleanupJob] = []
        for ready_job in self._store.cleanup_jobs.list_ready()[:limit]:
            try:
                results.append(self.execute_cleanup_job(job_id=ready_job.job_id))
            except ChatCleanupJobExecutionError as exc:
                # Continue with independent jobs.  The failed job itself has
                # already been transitioned to ``failed`` with a retry time.
                results.append(exc.job)
        return tuple(results)

    def _cleanup_deleted_chat(self, job: ChatCleanupJob) -> None:
        session = self._store.sessions.get(job.chat_id)
        if session is None:
            raise ValueError("chat session does not exist for cleanup job")
        self._ensure_session_reference_leases(session)

        if not session.workspace_ref:
            unresolved = self._open_leases(job.chat_id)
            if unresolved:
                raise ValueError("remote resource reference was not persisted")
            return

        with self._conversation_factory.create() as conversation:
            if session.thread_ref:
                self._delete_main_conversation(
                    chat_id=job.chat_id,
                    conversation=conversation,
                    session=ChatSessionRefs(
                        context_ref=session.workspace_ref,
                        conversation_ref=session.thread_ref,
                    ),
                )
            self._delete_context(
                chat_id=job.chat_id,
                conversation=conversation,
                context_ref=session.workspace_ref,
            )

        failed = self._failed_leases(job.chat_id)
        if failed:
            raise RuntimeError("remote resource cleanup failed")

    def _cleanup_temporary_thread(self, job: ChatCleanupJob) -> None:
        if not job.lease_id:
            raise ValueError("temporary-thread cleanup job must contain lease_id")
        lease = self._store.resource_leases.get(job.lease_id)
        if lease is None:
            raise ValueError("temporary-thread resource lease does not exist")
        if lease.chat_id != job.chat_id:
            raise ValueError("temporary-thread resource lease belongs to another chat")
        if lease.resource_type != RESOURCE_THREAD:
            raise ValueError("temporary-thread cleanup job targets a non-thread lease")
        if lease.status == LEASE_CLOSED:
            return
        if not lease.external_ref:
            # No remote reference was ever obtained.  Closing a planned lease
            # is safe; a non-planned lease without an identity is evidence we
            # cannot repair automatically and must remain visible as failure.
            if lease.status == LEASE_PLANNED:
                self._store.resource_leases.mark_closed(lease.lease_id)
                return
            raise ValueError("temporary-thread resource lease has no external reference")

        context_ref, conversation_ref = parse_chat_scoped_external_ref(
            lease.external_ref
        )
        self._mark_cleanup_pending_if_open(lease.lease_id)
        with self._conversation_factory.create() as conversation:
            error = self._delete_conversation_operation(
                conversation=conversation,
                session=ChatSessionRefs(
                    context_ref=context_ref,
                    conversation_ref=conversation_ref,
                ),
            )
        if error:
            self._store.resource_leases.record_cleanup_failure(
                lease_id=lease.lease_id,
                error_message=error,
            )
            raise RuntimeError(error)
        self._store.resource_leases.mark_closed(lease.lease_id)

    def _ensure_session_reference_leases(self, session: ChatSession) -> None:
        """Backfill deterministic session leases before the delete side effect.

        New runs create these leases before remote resources.  The defensive
        check keeps cleanup self-contained: the worker can operate from the
        authoritative session and lease records without consulting any legacy
        table or request-local state.
        """
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
                external_ref=chat_scoped_external_ref(
                    context_ref=session.workspace_ref,
                    resource_ref=session.thread_ref,
                ),
            )

    def _delete_main_conversation(
        self,
        *,
        chat_id: str,
        conversation,
        session: ChatSessionRefs,
    ) -> None:
        lease_id = chat_thread_lease_id(chat_id)
        self._mark_cleanup_pending_if_open(lease_id)
        error = self._delete_conversation_operation(
            conversation=conversation,
            session=session,
        )
        if error:
            self._store.resource_leases.record_cleanup_failure(
                lease_id=lease_id,
                error_message=error,
            )
            logger.warning(
                "文件对话主线程删除失败，等待 workspace 删除或后续补偿: chat_id=%s error=%s",
                chat_id,
                error,
            )
            return
        self._store.resource_leases.mark_closed(lease_id)

    def _delete_context(self, *, chat_id: str, conversation, context_ref: str) -> None:
        workspace_lease_id = chat_workspace_lease_id(chat_id)
        self._mark_cleanup_pending_if_open(workspace_lease_id)
        for lease in self._document_binding_leases(chat_id):
            self._mark_cleanup_pending_if_open(lease.lease_id)

        error = self._delete_context_operation(
            conversation=conversation,
            context_ref=context_ref,
        )
        if error:
            # A workspace owns every child resource.  Once its deletion fails,
            # all currently open leases describe an unresolved remote state and
            # must remain retryable instead of only marking document bindings.
            for lease in self._open_leases(chat_id):
                self._record_cleanup_failure_if_open(
                    lease_id=lease.lease_id,
                    error_message=error,
                )
            logger.warning(
                "文件对话 workspace 删除失败，已保留全部开放租约: chat_id=%s error=%s",
                chat_id,
                error,
            )
            return

        # Context deletion is authoritative for all child threads and document
        # bindings, including historical document revisions and temporary title
        # threads created after the main conversation was opened.
        for lease in self._store.resource_leases.list_by_chat(chat_id):
            if lease.status != LEASE_CLOSED:
                self._store.resource_leases.mark_closed(lease.lease_id)

    @staticmethod
    def _delete_conversation_operation(*, conversation, session: ChatSessionRefs) -> str:
        try:
            result = conversation.delete_conversation(session)
        except ChatConversationNotFoundError:
            return ""
        except ChatPortError as exc:
            return str(exc) or exc.__class__.__name__
        return ChatCleanupJobExecutor._operation_error(result)

    @staticmethod
    def _delete_context_operation(*, conversation, context_ref: str) -> str:
        try:
            result = conversation.delete_context(context_ref)
        except ChatConversationNotFoundError:
            return ""
        except ChatPortError as exc:
            return str(exc) or exc.__class__.__name__
        return ChatCleanupJobExecutor._operation_error(result)

    @staticmethod
    def _operation_error(result: ChatOperationResult) -> str:
        if not isinstance(result, ChatOperationResult):
            raise TypeError("cleanup operation must return ChatOperationResult")
        if result.success:
            return ""
        return result.error_message

    def _mark_failed(self, *, job: ChatCleanupJob, error: Exception) -> ChatCleanupJob:
        current = self._store.cleanup_jobs.get(job.job_id)
        if current is None:
            raise ValueError("cleanup job disappeared during execution") from error
        if current.status != CLEANUP_JOB_RUNNING:
            return current
        return self._store.cleanup_jobs.mark_failed(
            job_id=job.job_id,
            error_message=str(error) or error.__class__.__name__,
            next_attempt_at=self._next_retry_at(attempt_count=current.attempt_count),
        )

    def _next_retry_at(self, *, attempt_count: int) -> str:
        # Exponential delay protects a future maintenance worker from a tight
        # remote-failure loop.  A new explicit delete/title request can still
        # re-enqueue a failed job immediately through the repository.
        exponent = max(0, int(attempt_count) - 1)
        delay = min(
            self._retry_base_seconds * (2**exponent),
            self._retry_max_seconds,
        )
        return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()

    def _mark_cleanup_pending_if_open(self, lease_id: str) -> None:
        lease = self._store.resource_leases.get(lease_id)
        if lease is not None and lease.status != LEASE_CLOSED:
            self._store.resource_leases.mark_cleanup_pending(lease_id)

    def _record_cleanup_failure_if_open(self, *, lease_id: str, error_message: str) -> None:
        lease = self._store.resource_leases.get(lease_id)
        if lease is None or lease.status == LEASE_CLOSED:
            return
        if lease.status == LEASE_PLANNED:
            self._store.resource_leases.mark_cleanup_pending(lease_id)
        self._store.resource_leases.record_cleanup_failure(
            lease_id=lease_id,
            error_message=error_message,
        )

    def _open_leases(self, chat_id: str) -> tuple[ChatResourceLease, ...]:
        return self._store.resource_leases.list_by_chat(
            chat_id,
            include_closed=False,
        )

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
            if lease.status != LEASE_CLOSED and lease.error_message
        )


__all__ = [
    "ChatCleanupJobExecutionError",
    "ChatCleanupJobExecutor",
    "DEFAULT_CLEANUP_RETRY_BASE_SECONDS",
    "DEFAULT_CLEANUP_RETRY_MAX_SECONDS",
]
