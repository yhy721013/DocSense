"""Application service for aborting active file-chat streams."""

from __future__ import annotations

from dataclasses import dataclass

from app.services.chat.application.command_service import ChatCommandService
from app.services.chat.domain.events import ChatStreamEvent
from app.services.chat.persistence.store import ChatPersistenceStore


def _required_text(value: str, *, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} cannot be empty")
    return normalized


@dataclass(frozen=True)
class ChatAbortResult:
    """Result returned to `/llm/chat/abort` callers."""

    chat_id: str
    aborted: bool
    msg: str
    run_id: str = ""

    def to_response(self) -> dict[str, object]:
        return {
            "chatId": self.chat_id,
            "aborted": self.aborted,
            "msg": self.msg,
        }


class ChatAbortService:
    """Sets durable abort requests for the current active run of one chat."""

    def __init__(
        self,
        *,
        store: ChatPersistenceStore,
        chat_commands: ChatCommandService,
    ) -> None:
        self._store = store
        self._chat_commands = chat_commands

    def abort_chat(self, *, chat_id: str) -> ChatAbortResult:
        normalized_chat_id = _required_text(chat_id, name="chat_id")
        active_runs = self._store.runs.list_active(normalized_chat_id)
        if not active_runs:
            return ChatAbortResult(
                chat_id=normalized_chat_id,
                aborted=False,
                msg="当前无进行中的流式响应",
            )

        active_run = active_runs[0]
        try:
            requested = self._chat_commands.request_abort(
                run_id=active_run.run_id,
            )
        except ValueError:
            return ChatAbortResult(
                chat_id=normalized_chat_id,
                aborted=False,
                msg="当前无进行中的流式响应",
            )
        return ChatAbortResult(
            chat_id=normalized_chat_id,
            aborted=True,
            msg="已发送中断信号",
            run_id=requested.run_id,
        )

    @staticmethod
    def build_abort_signal(*, chat_id: str) -> ChatStreamEvent:
        return ChatStreamEvent(
            "aborted",
            {"chatId": _required_text(chat_id, name="chat_id")},
        )


__all__ = ["ChatAbortResult", "ChatAbortService"]
