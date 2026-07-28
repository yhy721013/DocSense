"""文件对话运行生命周期的应用命令服务。"""

from __future__ import annotations

import logging

from app.services.chat.domain.document_candidates import (
    ChatDocumentSelectionCandidates,
)
from app.services.chat.domain.events import ChatStreamEvent
from app.services.chat.domain.models import ChatRun
from app.services.chat.locking.lease import (
    ChatRunCoordinator,
    ChatRunLease,
    ChatRunLeaseCapabilities,
)


logger = logging.getLogger(__name__)


class ChatCommandService:
    """协调持久化文件对话运行的生命周期操作。"""

    def __init__(self, run_coordinator: ChatRunCoordinator) -> None:
        """注入 run 协调能力，而不是绑定某个数据库锁实现。

        容器当前传入 SQLite 单实例协调器；未来共享持久化适配器只需实现
        ``ChatRunCoordinator``，不需要修改路由、流式 Presenter 或 Chat Port。
        """
        if not isinstance(run_coordinator, ChatRunCoordinator):
            raise TypeError("run_coordinator must implement ChatRunCoordinator")
        self._run_coordinator = run_coordinator

    @property
    def lease_capabilities(self) -> ChatRunLeaseCapabilities:
        """暴露底层协调器的真实租约能力，供容器执行部署门禁。"""
        return self._run_coordinator.lease_capabilities

    def start_chat_run(
        self,
        *,
        chat_id: str,
        user_message: str | None = None,
        user_files: tuple[tuple[str, str], ...] = (),
        input_documents: tuple[tuple[str, str, str, str], ...] = (),
        document_candidates: ChatDocumentSelectionCandidates | None = None,
        max_files_per_request: int | None = None,
    ) -> ChatRun:
        if document_candidates is not None and not isinstance(
            document_candidates,
            ChatDocumentSelectionCandidates,
        ):
            raise TypeError(
                "document_candidates must be ChatDocumentSelectionCandidates "
                "or None"
            )
        if document_candidates is not None and (
            user_files or input_documents
        ):
            raise ValueError(
                "document_candidates cannot be combined with legacy "
                "document tuples"
            )
        explicit_count = (
            len(document_candidates.explicit_documents)
            if document_candidates is not None
            else len(input_documents)
        )
        default_count = (
            len(document_candidates.new_session_default_documents)
            if document_candidates is not None
            else 0
        )
        logger.info(
            "准备启动文件对话运行: chat_id=%s message_chars=%d "
            "explicit_candidate_count=%d default_candidate_count=%d",
            chat_id,
            len(str(user_message or "")),
            explicit_count,
            default_count,
        )
        run = self._run_coordinator.try_acquire_chat_run(
            chat_id=chat_id,
            user_message=user_message,
            user_files=user_files,
            input_documents=input_documents,
            document_candidates=document_candidates,
            max_files_per_request=max_files_per_request,
        )
        logger.info(
            "文件对话运行已受理: chat_id=%s run_id=%s status=%s "
            "has_owner_instance=%s",
            run.chat_id,
            run.run_id,
            run.status,
            bool(run.owner_instance_id),
        )
        return run

    def complete_chat_run(self, *, run_id: str) -> ChatRun:
        run = self._run_coordinator.complete_run(run_id)
        logger.info(
            "文件对话运行已成功完成: chat_id=%s run_id=%s status=%s",
            run.chat_id,
            run.run_id,
            run.status,
        )
        return run

    def fail_chat_run(self, *, run_id: str, error_message: str) -> ChatRun:
        run = self._run_coordinator.fail_run(
            run_id,
            error_message=error_message,
        )
        logger.warning(
            "文件对话运行已标记失败: chat_id=%s run_id=%s error_chars=%d",
            run.chat_id,
            run.run_id,
            len(run.error_message),
        )
        return run

    def discard_unstarted_chat_run(
        self,
        *,
        run_id: str,
        error_message: str,
    ) -> ChatRun:
        """丢弃已受理但从未进入执行器的请求。"""
        run = self._run_coordinator.discard_unstarted_run(
            run_id=run_id,
            error_message=error_message,
        )
        logger.info(
            "未启动的文件对话运行已收敛: chat_id=%s run_id=%s status=%s",
            run.chat_id,
            run.run_id,
            run.status,
        )
        return run

    def abort_chat_run(self, *, run_id: str) -> ChatRun:
        run = self._run_coordinator.abort_run(run_id)
        logger.info(
            "文件对话运行已标记中断: chat_id=%s run_id=%s abort_requested=%s",
            run.chat_id,
            run.run_id,
            run.abort_requested,
        )
        return run

    def heartbeat_chat_run(
        self,
        *,
        run_id: str,
        execution_lease: ChatRunLease | None = None,
    ) -> ChatRun:
        """刷新运行心跳；有 lease 时始终走未来可 fencing 的稳定入口。"""
        if execution_lease is None:
            self._require_lease_or_allow_single_instance_compatibility()
            run = self._run_coordinator.heartbeat_run(run_id)
        else:
            self._require_matching_lease(run_id=run_id, lease=execution_lease)
            run = self._run_coordinator.heartbeat_execution_lease(
                lease=execution_lease,
            )
        logger.debug(
            "文件对话运行心跳已刷新: chat_id=%s run_id=%s status=%s",
            run.chat_id,
            run.run_id,
            run.status,
        )
        return run

    def request_abort(self, *, run_id: str) -> ChatRun:
        run = self._run_coordinator.request_abort(run_id)
        logger.info(
            "文件对话运行收到中断请求: chat_id=%s run_id=%s abort_requested=%s",
            run.chat_id,
            run.run_id,
            run.abort_requested,
        )
        return run

    def begin_chat_deletion(self, *, chat_id: str) -> None:
        """在删除流程接触资源前，原子阻止新的运行进入会话。"""
        logger.info("开始请求文件对话删除准入: chat_id=%s", chat_id)
        self._run_coordinator.begin_chat_deletion(chat_id=chat_id)
        logger.info("文件对话删除准入已完成，会话已阻止新运行: chat_id=%s", chat_id)

    def issue_execution_lease(self, *, run_id: str) -> ChatRunLease:
        """为已受理 run 创建内部执行所有权证明，不向 HTTP/SSE 暴露该信息。"""
        logger.debug("开始领取文件对话运行执行权: run_id=%s", run_id)
        lease = self._run_coordinator.issue_execution_lease(run_id=run_id)
        logger.info(
            "文件对话运行执行权已领取: chat_id=%s run_id=%s has_owner_instance=%s",
            lease.chat_id,
            lease.run_id,
            bool(lease.owner_instance_id),
        )
        return lease

    def validate_execution_lease(self, *, lease: ChatRunLease) -> ChatRun:
        """在执行开始前验证 worker 仍持有目标 run 的运行权。"""
        run = self._run_coordinator.validate_execution_lease(lease=lease)
        logger.debug(
            "文件对话执行租约校验通过: chat_id=%s run_id=%s status=%s",
            run.chat_id,
            run.run_id,
            run.status,
        )
        return run

    def complete_chat_run_with_messages(
        self,
        *,
        run_id: str,
        user_message_id: str,
        assistant_message_id: str,
        assistant_content: str,
        terminal_event: ChatStreamEvent | None = None,
        execution_lease: ChatRunLease | None = None,
    ) -> ChatRun:
        if execution_lease is None:
            self._require_lease_or_allow_single_instance_compatibility()
            run = self._run_coordinator.complete_run_with_messages(
                run_id=run_id,
                user_message_id=user_message_id,
                assistant_message_id=assistant_message_id,
                assistant_content=assistant_content,
                terminal_event=terminal_event,
            )
        else:
            self._require_matching_lease(run_id=run_id, lease=execution_lease)
            run = self._run_coordinator.complete_run_with_execution_lease(
                lease=execution_lease,
                user_message_id=user_message_id,
                assistant_message_id=assistant_message_id,
                assistant_content=assistant_content,
                terminal_event=terminal_event,
            )
        logger.info(
            "文件对话运行已提交成功终态和消息: chat_id=%s run_id=%s assistant_chars=%d",
            run.chat_id,
            run.run_id,
            len(assistant_content),
        )
        return run

    def fail_chat_run_with_user(
        self,
        *,
        run_id: str,
        user_message_id: str,
        error_message: str,
        terminal_event: ChatStreamEvent | None = None,
        execution_lease: ChatRunLease | None = None,
    ) -> ChatRun:
        if execution_lease is None:
            self._require_lease_or_allow_single_instance_compatibility()
            run = self._run_coordinator.fail_run_with_user(
                run_id=run_id,
                user_message_id=user_message_id,
                error_message=error_message,
                terminal_event=terminal_event,
            )
        else:
            self._require_matching_lease(run_id=run_id, lease=execution_lease)
            run = self._run_coordinator.fail_run_with_execution_lease(
                lease=execution_lease,
                user_message_id=user_message_id,
                error_message=error_message,
                terminal_event=terminal_event,
            )
        logger.warning(
            "文件对话运行已提交失败终态: chat_id=%s run_id=%s error_chars=%d",
            run.chat_id,
            run.run_id,
            len(str(error_message or "")),
        )
        return run

    def abort_chat_run_with_user(
        self,
        *,
        run_id: str,
        user_message_id: str,
        terminal_event: ChatStreamEvent | None = None,
        execution_lease: ChatRunLease | None = None,
    ) -> ChatRun:
        if execution_lease is None:
            self._require_lease_or_allow_single_instance_compatibility()
            run = self._run_coordinator.abort_run_with_user(
                run_id=run_id,
                user_message_id=user_message_id,
                terminal_event=terminal_event,
            )
        else:
            self._require_matching_lease(run_id=run_id, lease=execution_lease)
            run = self._run_coordinator.abort_run_with_execution_lease(
                lease=execution_lease,
                user_message_id=user_message_id,
                terminal_event=terminal_event,
            )
        logger.info(
            "文件对话运行已提交中断终态: chat_id=%s run_id=%s",
            run.chat_id,
            run.run_id,
        )
        return run

    def expire_stale_chat_runs(self, *, chat_id: str) -> tuple[ChatRun, ...]:
        expired_runs = self._run_coordinator.expire_stale_runs_for_chat(
            chat_id=chat_id,
        )
        if expired_runs:
            logger.warning(
                "文件对话过期运行已在命令层收敛: chat_id=%s run_ids=%s",
                chat_id,
                ",".join(run.run_id for run in expired_runs),
            )
        return expired_runs

    def _require_lease_or_allow_single_instance_compatibility(self) -> None:
        """禁止未来 fencing 适配器沿用当前无租约的兼容调用。"""
        if self.lease_capabilities.requires_execution_lease_for_mutations:
            raise RuntimeError(
                "the configured chat run coordinator requires an execution lease"
            )

    @staticmethod
    def _require_matching_lease(*, run_id: str, lease: ChatRunLease) -> None:
        """在调用适配器前拒绝 run ID 与内部执行租约不一致的编程错误。"""
        if not isinstance(lease, ChatRunLease):
            raise TypeError("execution_lease must be ChatRunLease")
        if lease.run_id != run_id:
            raise ValueError("execution_lease does not belong to run_id")


__all__ = ["ChatCommandService"]
