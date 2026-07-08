"""Application commands for file-chat runs."""

from __future__ import annotations

from app.services.chat.lock_service import ChatRunLockService
from app.services.chat.models import ChatRun


class ChatCommandService:
    """Coordinates durable chat-run lifecycle operations."""

    def __init__(self, lock_service: ChatRunLockService) -> None:
        self._lock_service = lock_service

    def start_chat_run(self, *, chat_id: str) -> ChatRun:
        return self._lock_service.try_acquire_chat_run(chat_id=chat_id)

    def complete_chat_run(self, *, run_id: str) -> ChatRun:
        return self._lock_service.complete_run(run_id)

    def fail_chat_run(self, *, run_id: str, error_message: str) -> ChatRun:
        return self._lock_service.fail_run(
            run_id,
            error_message=error_message,
        )

    def heartbeat_chat_run(self, *, run_id: str) -> ChatRun:
        return self._lock_service.heartbeat_run(run_id)

    def request_abort(self, *, run_id: str) -> ChatRun:
        return self._lock_service.request_abort(run_id)


__all__ = ["ChatCommandService"]
