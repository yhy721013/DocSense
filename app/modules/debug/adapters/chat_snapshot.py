"""本地 Chat 与知识库数据的 Debug 只读 Adapter。"""

from __future__ import annotations

import logging

from app.modules.debug.ports.chat_snapshot import (
    ChatAvailableFile,
    ChatDebugSession,
    ChatDebugSnapshot,
)
from app.modules.chat.domain.chat_id import chat_id_public_value
from app.modules.chat.domain.identity import IDENTITY_KIND_FILE
from app.modules.chat.adapters.sqlite.store import ChatPersistenceStore
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
            resolution = self._chat_store.identities.get_by_conversation_id(
                item.conversation_id
            )
            if (
                resolution is None
                or resolution.binding.identity_kind != IDENTITY_KIND_FILE
            ):
                # 既有 Debug 合同只展示文件对话，不能把 Weaponry 的可信业务
                # userId/architectureId 塞进 chatId 字段或输出到日志。
                continue
            public_chat_id = chat_id_public_value(resolution.binding.chat_id)

            current_scope = self._chat_store.scopes.get_current_revision(
                item.conversation_id
            )
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
                self._chat_store.document_bindings.list_current_by_chat(
                    item.conversation_id
                )
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
