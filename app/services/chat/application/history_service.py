"""Local authoritative history queries for file chat."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.services.chat.domain.models import (
    MESSAGE_COMMITTED,
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_USER,
    ChatMessage,
)
from app.services.chat.persistence.store import ChatPersistenceStore


logger = logging.getLogger(__name__)


class ChatHistoryService:
    """Build API-facing chat history from local committed messages."""

    def __init__(self, store: ChatPersistenceStore) -> None:
        self._store = store

    def list_history(self, chat_id: str) -> list[dict[str, Any]]:
        messages = self._store.messages.list_by_chat(chat_id)
        committed = [
            self._present_message(message)
            for message in messages
            if message.status == MESSAGE_COMMITTED
        ]
        logger.info(
            "读取文件对话历史: chat_id=%s total_messages=%d committed_messages=%d",
            chat_id,
            len(messages),
            len(committed),
        )
        return committed

    def list_title_messages(
        self,
        chat_id: str,
        *,
        limit: int = 12,
        max_content_chars: int = 1000,
    ) -> list[dict[str, str]]:
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        if not isinstance(max_content_chars, int) or max_content_chars < 1:
            raise ValueError("max_content_chars must be a positive integer")

        # 标题生成只消费本地 committed 历史，避免读取 AnythingLLM Thread 中
        # 可能包含的半截回答或供应商侧临时消息。
        history = self.list_history(chat_id)
        title_messages: list[dict[str, str]] = []
        for item in history[-limit:]:
            role = item.get("role")
            content = str(item.get("content") or "").strip()
            if role not in {MESSAGE_ROLE_USER, MESSAGE_ROLE_ASSISTANT} or not content:
                continue
            title_messages.append(
                {
                    "role": str(role),
                    "content": content[:max_content_chars],
                }
            )
        logger.info(
            "构建标题生成历史片段: chat_id=%s history_count=%d title_count=%d limit=%d",
            chat_id,
            len(history),
            len(title_messages),
            limit,
        )
        return title_messages

    @staticmethod
    def _present_message(message: ChatMessage) -> dict[str, Any]:
        item: dict[str, Any] = {
            "role": message.role,
            "content": message.content,
            "timestamp": _to_timestamp_ms(message.created_at),
        }
        if message.role == MESSAGE_ROLE_USER:
            item["files"] = [
                {"name": file.original_name or file.file_name}
                for file in message.files
                if file.original_name or file.file_name
            ]
        return item


def _to_timestamp_ms(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return int(text)
        normalized = text.replace("Z", "+00:00")
        try:
            timestamp = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return int(timestamp.timestamp() * 1000)
    return None


__all__ = ["ChatHistoryService"]
