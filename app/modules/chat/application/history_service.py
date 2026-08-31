"""文件对话本地权威历史的查询服务。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.modules.chat.domain.models import (
    MESSAGE_COMMITTED,
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_USER,
    SESSION_DELETED,
    ChatMessage,
    ChatMessageSourceChunk,
)
from app.modules.chat.domain.identity import ConversationIdentity
from app.modules.chat.application.policy import chat_policy_for
from app.modules.chat.ports.persistence import ChatPersistenceStore


logger = logging.getLogger(__name__)


class ChatHistoryService:
    """根据本地已提交消息构造面向接口的对话历史。"""

    def __init__(self, store: ChatPersistenceStore) -> None:
        self._store = store

    def list_history(
        self,
        identity: ConversationIdentity,
    ) -> list[dict[str, Any]]:
        """按公开身份读取历史；不存在或已删除时保持公开合同的空数组语义。"""

        if not isinstance(identity, ConversationIdentity):
            raise TypeError("identity must implement ConversationIdentity")
        resolution = self._store.identities.resolve_any(identity)
        if resolution is None:
            logger.info(
                "对话历史为空：公开身份尚未创建 Conversation: identity_kind=%s",
                identity.identity_kind,
            )
            return []
        return self._list_history_by_conversation_id(
            resolution.conversation_id,
            identity_kind=resolution.binding.identity_kind,
        )

    def _list_history_by_conversation_id(
        self,
        conversation_id: str,
        *,
        identity_kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """供同模块标题用例复用的内部聚合查询，不属于公开身份边界。"""

        session = self._store.sessions.get(conversation_id)
        if session is not None and session.status == SESSION_DELETED:
            logger.info(
                "文件对话已删除，历史接口返回空列表: conversation_id=%s",
                conversation_id,
            )
            return []
        if identity_kind is None:
            resolution = self._store.identities.get_by_conversation_id(
                conversation_id
            )
            if resolution is None:
                raise ValueError("conversation identity does not exist")
            identity_kind = resolution.binding.identity_kind
        policy = chat_policy_for(identity_kind)
        messages = self._store.messages.list_by_chat(conversation_id)
        chunks_by_message: dict[str, list[ChatMessageSourceChunk]] = {}
        if policy.expose_source_chunks:
            for chunk in self._store.message_sources.list_by_conversation(
                conversation_id
            ):
                chunks_by_message.setdefault(chunk.message_id, []).append(chunk)
        committed = [
            self._present_message(
                message,
                identity_kind=identity_kind,
                source_chunks=tuple(chunks_by_message.get(message.message_id, ())),
            )
            for message in messages
            if message.status == MESSAGE_COMMITTED
        ]
        logger.info(
            "读取文件对话历史: conversation_id=%s total_messages=%d committed_messages=%d",
            conversation_id,
            len(messages),
            len(committed),
        )
        return committed

    def list_title_messages(
        self,
        conversation_id: str,
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
        history = self._list_history_by_conversation_id(conversation_id)
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
            "构建标题生成历史片段: conversation_id=%s history_count=%d title_count=%d limit=%d",
            conversation_id,
            len(history),
            len(title_messages),
            limit,
        )
        return title_messages

    @staticmethod
    def _present_message(
        message: ChatMessage,
        *,
        identity_kind: str,
        source_chunks: tuple[ChatMessageSourceChunk, ...] = (),
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "role": message.role,
            "content": message.content,
            "timestamp": _to_timestamp_ms(message.created_at),
        }
        policy = chat_policy_for(identity_kind)
        if message.role == MESSAGE_ROLE_USER and policy.expose_user_file_selection:
            if message.architecture_id is not None:
                # architecture 模式只公开不可变类别 ID，禁止把内部冻结成员展开回前端。
                item["architectureId"] = message.architecture_id
            else:
                item["files"] = [
                    {"name": file.original_name or file.file_name}
                    for file in message.files
                    if file.original_name or file.file_name
                ]
        elif message.role == MESSAGE_ROLE_ASSISTANT and policy.expose_source_chunks:
            item["chunks"] = [
                {
                    "content": chunk.content,
                    "fileName": chunk.file_name,
                    "originalFileName": chunk.original_file_name,
                }
                for chunk in source_chunks
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
