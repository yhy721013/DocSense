"""Application commands for file-chat runs."""

from __future__ import annotations

import logging

from app.services.chat.domain.events import ChatStreamEvent
from app.services.chat.domain.models import ChatRun
from app.services.chat.locking.lease import (
    ChatRunCoordinator,
    ChatRunLease,
    ChatRunLeaseCapabilities,
)


logger = logging.getLogger(__name__)


class ChatCommandService:
    """Coordinates durable chat-run lifecycle operations."""

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
    ) -> ChatRun:
        logger.info("准备启动文件对话run: chat_id=%s", chat_id)
        run = self._run_coordinator.try_acquire_chat_run(
            chat_id=chat_id,
            user_message=user_message,
            user_files=user_files,
            input_documents=input_documents,
        )
        logger.info(
            "文件对话run已启动: chat_id=%s run_id=%s status=%s owner=%s",
            run.chat_id,
            run.run_id,
            run.status,
            run.owner_instance_id,
        )
        return run

    def complete_chat_run(self, *, run_id: str) -> ChatRun:
        run = self._run_coordinator.complete_run(run_id)
        logger.info(
            "文件对话run已成功完成: chat_id=%s run_id=%s status=%s",
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
            "文件对话run已标记失败: chat_id=%s run_id=%s error=%s",
            run.chat_id,
            run.run_id,
            run.error_message,
        )
        return run

    def abort_chat_run(self, *, run_id: str) -> ChatRun:
        run = self._run_coordinator.abort_run(run_id)
        logger.info(
            "文件对话run已标记中断: chat_id=%s run_id=%s abort_requested=%s",
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
            "文件对话run心跳已刷新: chat_id=%s run_id=%s status=%s",
            run.chat_id,
            run.run_id,
            run.status,
        )
        return run

    def request_abort(self, *, run_id: str) -> ChatRun:
        run = self._run_coordinator.request_abort(run_id)
        logger.info(
            "文件对话run收到中断请求: chat_id=%s run_id=%s abort_requested=%s",
            run.chat_id,
            run.run_id,
            run.abort_requested,
        )
        return run

    def begin_chat_deletion(self, *, chat_id: str) -> None:
        """Atomically stop new runs before the delete workflow touches resources."""
        self._run_coordinator.begin_chat_deletion(chat_id=chat_id)

    def issue_execution_lease(self, *, run_id: str) -> ChatRunLease:
        """为已受理 run 创建内部执行所有权证明，不向 HTTP/SSE 暴露该信息。"""
        return self._run_coordinator.issue_execution_lease(run_id=run_id)

    def validate_execution_lease(self, *, lease: ChatRunLease) -> ChatRun:
        """在执行开始前验证 worker 仍持有目标 run 的运行权。"""
        return self._run_coordinator.validate_execution_lease(lease=lease)

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
            return self._run_coordinator.complete_run_with_messages(
                run_id=run_id,
                user_message_id=user_message_id,
                assistant_message_id=assistant_message_id,
                assistant_content=assistant_content,
                terminal_event=terminal_event,
            )
        self._require_matching_lease(run_id=run_id, lease=execution_lease)
        return self._run_coordinator.complete_run_with_execution_lease(
            lease=execution_lease,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            assistant_content=assistant_content,
            terminal_event=terminal_event,
        )

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
            return self._run_coordinator.fail_run_with_user(
                run_id=run_id,
                user_message_id=user_message_id,
                error_message=error_message,
                terminal_event=terminal_event,
            )
        self._require_matching_lease(run_id=run_id, lease=execution_lease)
        return self._run_coordinator.fail_run_with_execution_lease(
            lease=execution_lease,
            user_message_id=user_message_id,
            error_message=error_message,
            terminal_event=terminal_event,
        )

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
            return self._run_coordinator.abort_run_with_user(
                run_id=run_id,
                user_message_id=user_message_id,
                terminal_event=terminal_event,
            )
        self._require_matching_lease(run_id=run_id, lease=execution_lease)
        return self._run_coordinator.abort_run_with_execution_lease(
            lease=execution_lease,
            user_message_id=user_message_id,
            terminal_event=terminal_event,
        )

    def expire_stale_chat_runs(self, *, chat_id: str) -> tuple[ChatRun, ...]:
        expired_runs = self._run_coordinator.expire_stale_runs_for_chat(
            chat_id=chat_id,
        )
        if expired_runs:
            logger.warning(
                "文件对话过期run已在命令层收敛: chat_id=%s run_ids=%s",
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
