"""面向 HTTP 的文件对话会话删除工作流。

本服务负责会话准入与已冻结的删除响应语义。实际的远端资源删除由
``cleanup_service`` 负责，并通过可替换的清理调度器使用持久化任务 ID 调用。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.modules.chat.ports import ChatConversationFactory
from app.modules.chat.application.cleanup_dispatcher import (
    ChatCleanupDispatchCapabilities,
    ChatCleanupDispatcher,
    InlineChatCleanupDispatcher,
)
from app.modules.chat.application.cleanup_service import (
    ChatCleanupJobExecutionError,
    ChatCleanupJobExecutor,
)
from app.modules.chat.application.command_service import ChatCommandService
from app.modules.chat.domain.identity import ConversationIdentity, FileChatIdentity
from app.modules.chat.domain.models import (
    CLEANUP_JOB_SUCCEEDED,
    CLEANUP_REASON_DELETE_CHAT,
    LEASE_CLEANUP_FAILED,
    LEASE_CLOSED,
    LEASE_PLANNED,
    SESSION_DELETED,
    SESSION_ERROR,
    ChatResourceLease,
)
from app.modules.chat.ports.coordination import (
    ChatSessionDeleteBusyError,
    ChatSessionUnavailableError,
)
from app.modules.chat.ports.persistence import ChatPersistenceStore


logger = logging.getLogger(__name__)


def _required_text(value: str, *, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} cannot be empty")
    return normalized


@dataclass(frozen=True)
class ChatDeleteResult:
    """成功删除的供应商与公开接口无关结果。"""

    identity: ConversationIdentity
    deleted: bool
    msg: str

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ConversationIdentity):
            raise TypeError("identity must implement ConversationIdentity")


class ChatDeleteNotFoundError(ValueError):
    """本地权威会话不存在时抛出。"""

    def __init__(self, conversation_id: str) -> None:
        self.conversation_id = conversation_id
        super().__init__("对话不存在")


class ChatDeleteCleanupError(RuntimeError):
    """清理失败已持久化以供重试和审计后抛出。"""

    def __init__(
        self,
        *,
        conversation_id: str,
        failed_leases: tuple[ChatResourceLease, ...],
    ) -> None:
        self.conversation_id = conversation_id
        self.failed_leases = failed_leases
        # 远端资源引用属于运维和审计数据，而非公开 API 内容。应保持已冻结的错误载荷稳定，
        # 并避免通过 HTTP 错误消息泄露供应商侧标识。
        super().__init__("对话资源清理失败")


class ChatDeleteBusyError(RuntimeError):
    """删除操作无法进入会话独占状态时抛出。"""

    def __init__(self, *, conversation_id: str, reason: str) -> None:
        self.conversation_id = conversation_id
        self.reason = reason
        super().__init__(reason)


class ChatDeleteService:
    """受理删除请求，并同步等待其持久化清理结果。

    当前公开接口只会在远端清理成功后承诺删除完成。因此单实例组合必须注入
    ``supports_synchronous_completion=True`` 的调度器。未来异步调度器可以复用清理
    执行器，但不得在未明确设计 API 变更的情况下悄然替换本接口的语义。
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
        """暴露实际调度行为，供组合根校验。"""
        return self._cleanup_dispatcher.capabilities

    def delete_chat(self, *, identity: ConversationIdentity) -> ChatDeleteResult:
        if not isinstance(identity, ConversationIdentity):
            raise TypeError("identity must implement ConversationIdentity")
        # File chatId 是永久墓碑，因此重复删除仍可解析到已删除世代并保持既有幂等
        # 语义；Weaponry 删除成功后会释放复合身份，重复删除必须视为不存在。
        resolution = (
            self._store.identities.resolve_any(identity)
            if isinstance(identity, FileChatIdentity)
            else self._store.identities.resolve_active(identity)
        )
        if resolution is None:
            logger.warning(
                "对话删除失败：公开身份不存在: identity_kind=%s",
                identity.identity_kind,
            )
            raise ChatDeleteNotFoundError(identity.identity_key)
        normalized_conversation_id = resolution.conversation_id
        logger.info("收到文件对话删除指令: conversation_id=%s", normalized_conversation_id)

        session = self._store.sessions.get(normalized_conversation_id)
        if session is None:
            logger.warning("文件对话删除失败：本地会话不存在: conversation_id=%s", normalized_conversation_id)
            raise ChatDeleteNotFoundError(normalized_conversation_id)
        if session.status == SESSION_DELETED:
            logger.info("文件对话已处于删除完成状态，无需重复清理: conversation_id=%s", normalized_conversation_id)
            return ChatDeleteResult(
                identity=identity,
                deleted=True,
                msg="对话已删除",
            )

        try:
            self._chat_commands.begin_chat_deletion(conversation_id=normalized_conversation_id)
        except ChatSessionDeleteBusyError as exc:
            logger.warning(
                "文件对话删除被拒绝：会话正被其他操作占用: conversation_id=%s reason=%s",
                normalized_conversation_id,
                exc.reason,
            )
            raise ChatDeleteBusyError(
                conversation_id=normalized_conversation_id,
                reason=exc.reason,
            ) from exc
        except ChatSessionUnavailableError as exc:
            logger.warning(
                "文件对话删除被拒绝：会话状态不可删除: conversation_id=%s",
                normalized_conversation_id,
            )
            raise ChatDeleteBusyError(
                conversation_id=normalized_conversation_id,
                reason=str(exc),
            ) from exc

        logger.info("文件对话已进入删除中状态，开始创建清理任务: conversation_id=%s", normalized_conversation_id)

        # 在任何远端副作用发生前先记录持久化任务。内联适配器调用的执行器与未来工作进程
        # 使用的执行器相同，因此当前同步 API 路径仍保持队列化形态。
        cleanup_job = self._store.cleanup_jobs.enqueue(
            conversation_id=normalized_conversation_id,
            reason=CLEANUP_REASON_DELETE_CHAT,
        )
        logger.info(
            "文件对话删除清理任务已创建或复用: conversation_id=%s job_id=%s status=%s",
            normalized_conversation_id,
            cleanup_job.job_id,
            cleanup_job.status,
        )
        try:
            completed_job = self._cleanup_dispatcher.dispatch(job=cleanup_job)
        except ChatCleanupJobExecutionError as exc:
            logger.warning(
                "文件对话删除清理执行失败，已保留重试记录: conversation_id=%s job_id=%s",
                normalized_conversation_id,
                exc.job.job_id,
            )
            self._mark_session_cleanup_error(
                conversation_id=normalized_conversation_id,
                error_message=exc.reason,
            )
            raise ChatDeleteCleanupError(
                conversation_id=normalized_conversation_id,
                failed_leases=self._failed_leases(normalized_conversation_id),
            ) from exc
        except Exception as exc:
            # 调度失败也会被持久化为租约失败。原始异常保留为服务端诊断原因，而路由仍
            # 返回稳定的清理错误响应。
            logger.exception(
                "文件对话删除清理调度发生异常: conversation_id=%s job_id=%s",
                normalized_conversation_id,
                cleanup_job.job_id,
            )
            self._mark_session_cleanup_error(
                conversation_id=normalized_conversation_id,
                error_message=str(exc) or exc.__class__.__name__,
            )
            raise ChatDeleteCleanupError(
                conversation_id=normalized_conversation_id,
                failed_leases=self._failed_leases(normalized_conversation_id),
            ) from exc

        if completed_job.status != CLEANUP_JOB_SUCCEEDED:
            logger.warning(
                "文件对话删除清理未完成，已保留失败状态: conversation_id=%s job_id=%s status=%s",
                normalized_conversation_id,
                completed_job.job_id,
                completed_job.status,
            )
            self._mark_session_cleanup_error(
                conversation_id=normalized_conversation_id,
                error_message=(
                    completed_job.error_message
                    or "cleanup dispatcher did not complete the job"
                ),
            )
            raise ChatDeleteCleanupError(
                conversation_id=normalized_conversation_id,
                failed_leases=self._failed_leases(normalized_conversation_id),
            )

        failed = self._failed_leases(normalized_conversation_id)
        if failed:
            logger.warning(
                "文件对话删除清理后仍存在失败租约: conversation_id=%s failed_lease_count=%d",
                normalized_conversation_id,
                len(failed),
            )
            self._store.sessions.set_status(
                conversation_id=normalized_conversation_id,
                status=SESSION_ERROR,
            )
            raise ChatDeleteCleanupError(
                conversation_id=normalized_conversation_id,
                failed_leases=failed,
            )

        # 本地正文清除、最小审计事实、删除终态和 Weaponry 身份释放必须在同一
        # SQLite 事务完成。任何一步失败都不能向调用方承诺删除成功。
        self._store.identities.finalize_completed_delete(
            normalized_conversation_id
        )
        logger.info("文件对话删除完成: conversation_id=%s", normalized_conversation_id)
        return ChatDeleteResult(
            identity=identity,
            deleted=True,
            msg="对话已删除",
        )

    def _mark_session_cleanup_error(self, *, conversation_id: str, error_message: str) -> None:
        """调度失败后保持每一条未解决的本地租约可见。"""
        marked_lease_count = 0
        for lease in self._store.resource_leases.list_by_chat(
            conversation_id,
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
                marked_lease_count += 1
        self._store.sessions.set_status(
            conversation_id=conversation_id,
            status=SESSION_ERROR,
        )
        logger.info(
            "文件对话删除失败状态已持久化: conversation_id=%s marked_lease_count=%d error_chars=%d",
            conversation_id,
            marked_lease_count,
            len(str(error_message or "")),
        )

    def _failed_leases(self, conversation_id: str) -> tuple[ChatResourceLease, ...]:
        return tuple(
            lease
            for lease in self._store.resource_leases.list_by_chat(conversation_id)
            if lease.status == LEASE_CLEANUP_FAILED
        )


__all__ = [
    "ChatDeleteBusyError",
    "ChatDeleteCleanupError",
    "ChatDeleteNotFoundError",
    "ChatDeleteResult",
    "ChatDeleteService",
]
