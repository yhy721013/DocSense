"""Application commands for file-chat runs."""

from __future__ import annotations

import logging

from app.services.chat.domain.models import ChatRun
from app.services.chat.locking.lock_service import ChatRunLockService


logger = logging.getLogger(__name__)


class ChatCommandService:
    """Coordinates durable chat-run lifecycle operations."""

    def __init__(self, lock_service: ChatRunLockService) -> None:
        self._lock_service = lock_service

    def start_chat_run(self, *, chat_id: str) -> ChatRun:
        logger.info("准备启动文件对话run: chat_id=%s", chat_id)
        run = self._lock_service.try_acquire_chat_run(chat_id=chat_id)
        logger.info(
            "文件对话run已启动: chat_id=%s run_id=%s status=%s owner=%s",
            run.chat_id,
            run.run_id,
            run.status,
            run.owner_instance_id,
        )
        return run

    def complete_chat_run(self, *, run_id: str) -> ChatRun:
        run = self._lock_service.complete_run(run_id)
        logger.info(
            "文件对话run已成功完成: chat_id=%s run_id=%s status=%s",
            run.chat_id,
            run.run_id,
            run.status,
        )
        return run

    def fail_chat_run(self, *, run_id: str, error_message: str) -> ChatRun:
        run = self._lock_service.fail_run(
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
        run = self._lock_service.abort_run(run_id)
        logger.info(
            "文件对话run已标记中断: chat_id=%s run_id=%s abort_requested=%s",
            run.chat_id,
            run.run_id,
            run.abort_requested,
        )
        return run

    def heartbeat_chat_run(self, *, run_id: str) -> ChatRun:
        run = self._lock_service.heartbeat_run(run_id)
        logger.debug(
            "文件对话run心跳已刷新: chat_id=%s run_id=%s status=%s",
            run.chat_id,
            run.run_id,
            run.status,
        )
        return run

    def request_abort(self, *, run_id: str) -> ChatRun:
        run = self._lock_service.request_abort(run_id)
        logger.info(
            "文件对话run收到中断请求: chat_id=%s run_id=%s abort_requested=%s",
            run.chat_id,
            run.run_id,
            run.abort_requested,
        )
        return run


__all__ = ["ChatCommandService"]
