"""文件对话工作流的持久化资源清理执行器。

本模块负责“如何”执行已持久化的清理任务。面向 HTTP 的服务只负责决定何时创建任务，
以及如何将执行结果映射到既有响应契约。这样的分层使未来调度器只需使用同一个持久化
``job_id`` 调用 :meth:`ChatCleanupJobExecutor.execute_cleanup_job`，无需重建请求级闭包。
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
    """清理尝试已被持久化标记为失败后抛出。"""

    def __init__(self, *, job: ChatCleanupJob, reason: str) -> None:
        self.job = job
        self.reason = str(reason or "cleanup job failed").strip()
        super().__init__(self.reason)


class ChatCleanupJobExecutor:
    """通过供应商无关的 Chat Port 执行持久化清理任务。

    执行器刻意不持有 HTTP 请求、SSE 流、回调或内存任务状态。它只接收 ``job_id``；
    每次调用远端操作前，都会从本地权威存储重新加载完整的资源身份。
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
        """领取并执行恰好一条持久化清理任务。

        领取状态迁移保证多个调度器在任务层面的互斥。远端操作失败时，会先写入失败
        记录再抛出异常，因此调用方无需猜测重试依据是否已落库。
        """
        job = self._store.cleanup_jobs.claim(job_id=job_id)
        if job.status == CLEANUP_JOB_SUCCEEDED:
            logger.debug(
                "文件对话清理任务已完成，跳过重复执行: job_id=%s chat_id=%s",
                job.job_id,
                job.chat_id,
            )
            return job

        logger.info(
            "开始执行文件对话清理任务: job_id=%s chat_id=%s reason=%s attempt=%d",
            job.job_id,
            job.chat_id,
            job.reason,
            job.attempt_count,
        )

        try:
            if job.reason == CLEANUP_REASON_DELETE_CHAT:
                logger.info(
                    "执行会话删除后的远端资源清理: job_id=%s chat_id=%s",
                    job.job_id,
                    job.chat_id,
                )
                self._cleanup_deleted_chat(job)
            elif job.reason == CLEANUP_REASON_TEMPORARY_THREAD:
                logger.info(
                    "执行临时标题线程清理: job_id=%s chat_id=%s has_lease=%s",
                    job.job_id,
                    job.chat_id,
                    bool(job.lease_id),
                )
                self._cleanup_temporary_thread(job)
            else:  # 防御性保护：拒绝未来应用版本写入的未知任务原因。
                raise ValueError(f"unsupported cleanup job reason: {job.reason}")
        except Exception as exc:
            failed_job = self._mark_failed(job=job, error=exc)
            logger.warning(
                "文件对话清理任务失败并已保留重试记录: job_id=%s chat_id=%s reason=%s attempt=%d error_type=%s",
                failed_job.job_id,
                failed_job.chat_id,
                failed_job.reason,
                failed_job.attempt_count,
                exc.__class__.__name__,
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
        """执行数量受限的就绪任务快照，供未来维护循环调用。

        当前内联调度器只执行刚创建的指定任务，不会自动调用本方法。限制单次处理
        数量，可避免未来调度器在历史失败任务积压时长期占用一个工作进程。
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        ready_jobs = self._store.cleanup_jobs.list_ready()[:limit]
        logger.info(
            "开始执行就绪文件对话清理任务快照: selected_job_count=%d limit=%d",
            len(ready_jobs),
            limit,
        )
        results: list[ChatCleanupJob] = []
        for ready_job in ready_jobs:
            try:
                results.append(self.execute_cleanup_job(job_id=ready_job.job_id))
            except ChatCleanupJobExecutionError as exc:
                # 继续处理互不依赖的任务。失败任务自身已转为 ``failed`` 状态，并写入
                # 下次重试时间。
                results.append(exc.job)
        logger.info(
            "就绪文件对话清理任务快照执行结束: processed_job_count=%d succeeded_job_count=%d failed_job_count=%d",
            len(results),
            sum(job.status == CLEANUP_JOB_SUCCEEDED for job in results),
            sum(job.status != CLEANUP_JOB_SUCCEEDED for job in results),
        )
        return tuple(results)

    def _cleanup_deleted_chat(self, job: ChatCleanupJob) -> None:
        session = self._store.sessions.get(job.chat_id)
        if session is None:
            raise ValueError("chat session does not exist for cleanup job")
        logger.debug(
            "加载待删除会话的资源租约: chat_id=%s has_workspace=%s has_thread=%s",
            job.chat_id,
            bool(session.workspace_ref),
            bool(session.thread_ref),
        )
        self._ensure_session_reference_leases(session)

        if not session.workspace_ref:
            unresolved = self._open_leases(job.chat_id)
            if unresolved:
                logger.warning(
                    "删除会话清理无法继续：本地仍有未关闭租约但缺少工作区引用: chat_id=%s open_lease_count=%d",
                    job.chat_id,
                    len(unresolved),
                )
                raise ValueError("remote resource reference was not persisted")
            logger.info("删除会话清理无需调用远端资源: chat_id=%s", job.chat_id)
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
            logger.warning(
                "删除会话清理完成后仍有失败租约: chat_id=%s failed_lease_count=%d",
                job.chat_id,
                len(failed),
            )
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
            logger.debug(
                "临时标题线程租约已关闭，无需重复清理: job_id=%s lease_id=%s",
                job.job_id,
                lease.lease_id,
            )
            return
        if not lease.external_ref:
            # 从未获得远端引用。关闭 planned 租约是安全的；非 planned 租约缺少身份
            # 表明无法自动修复，必须以失败状态保留该问题。
            if lease.status == LEASE_PLANNED:
                self._store.resource_leases.mark_closed(lease.lease_id)
                logger.info(
                    "临时标题线程未创建远端资源，已关闭计划租约: job_id=%s lease_id=%s",
                    job.job_id,
                    lease.lease_id,
                )
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
        logger.info(
            "临时标题线程远端资源清理完成: job_id=%s lease_id=%s",
            job.job_id,
            lease.lease_id,
        )

    def _ensure_session_reference_leases(self, session: ChatSession) -> None:
        """在删除远端资源前补齐会话的确定性租约。

        新运行会在创建远端资源前创建这些租约。该防御性检查使清理任务保持自包含：
        工作进程只依赖权威的会话和租约记录，不查询任何旧表或请求级状态。
        """
        logger.debug(
            "确保删除会话的资源租约完整: chat_id=%s has_workspace=%s has_thread=%s",
            session.chat_id,
            bool(session.workspace_ref),
            bool(session.thread_ref),
        )
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
                "文件对话主线程删除失败，等待工作区删除或后续补偿: chat_id=%s error_chars=%d",
                chat_id,
                len(error),
            )
            return
        self._store.resource_leases.mark_closed(lease_id)
        logger.info("文件对话主线程远端资源删除完成: chat_id=%s", chat_id)

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
            # 工作区拥有全部子资源。一旦其删除失败，所有仍打开的租约都表示未解决的
            # 远端状态，必须保持可重试，而不能只标记文档绑定。
            for lease in self._open_leases(chat_id):
                self._record_cleanup_failure_if_open(
                    lease_id=lease.lease_id,
                    error_message=error,
                )
            logger.warning(
                "文件对话工作区删除失败，已保留全部开放租约: chat_id=%s error_chars=%d",
                chat_id,
                len(error),
            )
            return

        # 上下文删除对所有子线程和文档绑定具有权威性，包括历史文档版本以及主对话
        # 创建后生成的临时标题线程。
        for lease in self._store.resource_leases.list_by_chat(chat_id):
            if lease.status != LEASE_CLOSED:
                self._store.resource_leases.mark_closed(lease.lease_id)
        logger.info("文件对话工作区远端资源删除完成，已关闭本地关联租约: chat_id=%s", chat_id)

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
        next_attempt_at = self._next_retry_at(attempt_count=current.attempt_count)
        failed = self._store.cleanup_jobs.mark_failed(
            job_id=job.job_id,
            error_message=str(error) or error.__class__.__name__,
            next_attempt_at=next_attempt_at,
        )
        logger.info(
            "文件对话清理任务已安排重试: job_id=%s chat_id=%s next_attempt_at=%s",
            failed.job_id,
            failed.chat_id,
            failed.next_attempt_at,
        )
        return failed

    def _next_retry_at(self, *, attempt_count: int) -> str:
        # 指数退避可避免未来维护工作进程在远端持续失败时形成紧密循环。新的显式删除
        # 或标题请求仍可通过仓储立即重新入队失败任务。
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
