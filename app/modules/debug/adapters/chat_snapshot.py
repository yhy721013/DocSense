"""本地 Chat 与知识库数据的 Debug 只读 Adapter。"""

from __future__ import annotations

import logging

from app.modules.debug.ports.chat_snapshot import (
    ChatAvailableFile,
    ChatDebugSession,
    ChatDebugSnapshot,
)
from app.services.chat.domain.chat_id import chat_id_public_value
from app.services.chat.persistence.store import ChatPersistenceStore
from app.services.core.database import DatabaseService


logger = logging.getLogger(__name__)


class LocalChatDebugSnapshotReadAdapter:
    """读取本地持久化快照；不发起 AnythingLLM 或其他网络请求。"""

    def __init__(
        self,
        *,
        chat_store: ChatPersistenceStore,
        kb_service: DatabaseService,
    ) -> None:
        self._chat_store = chat_store
        self._kb_service = kb_service

    def read_snapshot(self) -> ChatDebugSnapshot:
        sessions: list[ChatDebugSession] = []
        active_scope_member_count = 0
        workspace_binding_count = 0

        for item in self._chat_store.sessions.list_all():
            if item.status == "deleted":
                continue
            try:
                public_chat_id = chat_id_public_value(item.chat_id)
            except ValueError:
                # 不把非规范内部 chatId 回显给浏览器；日志也不记录其原值。
                logger.warning("调试快照跳过非规范 chatId 的存量会话")
                continue

            current_scope = self._chat_store.scopes.get_current_revision(item.chat_id)
            active_file_names = (
                ()
                if current_scope is None
                else tuple(member.file_name for member in current_scope.members)
            )
            if current_scope is None:
                logger.warning(
                    "调试快照会话缺少活动范围，按空活动范围展示: chat_id=%s",
                    public_chat_id,
                )
            binding_count = len(
                self._chat_store.document_bindings.list_current_by_chat(item.chat_id)
            )
            active_scope_member_count += len(active_file_names)
            workspace_binding_count += binding_count
            sessions.append(
                ChatDebugSession(
                    chat_id=public_chat_id,
                    file_names=active_file_names,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                )
            )

        available_files = tuple(
            ChatAvailableFile(
                file_name=item["file_name"],
                architecture_id=item["architecture_id"],
            )
            for item in self._kb_service.list_document_records()
        )
        return ChatDebugSnapshot(
            sessions=tuple(sessions),
            available_files=available_files,
            active_scope_member_count=active_scope_member_count,
            workspace_binding_count=workspace_binding_count,
        )
