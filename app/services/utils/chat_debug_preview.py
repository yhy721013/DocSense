from __future__ import annotations

import logging
from typing import Any

from app.services.chat.domain.chat_id import chat_id_public_value
from app.services.chat.persistence.store import ChatPersistenceStore
from app.services.core.database import DatabaseService


logger = logging.getLogger(__name__)


def load_chat_debug_bootstrap(
    *,
    chat_store: ChatPersistenceStore,
    kb_service: DatabaseService,
) -> dict[str, Any]:
    logger.info("开始读取文件对话调试初始化数据")
    try:
        sessions = []
        active_scope_member_count = 0
        workspace_binding_count = 0
        for item in chat_store.sessions.list_all():
            if item.status == "deleted":
                continue
            try:
                public_chat_id = chat_id_public_value(item.chat_id)
            except ValueError:
                # 调试页同样属于前端调用方，不能把旧格式字符串 chatId 回显给页面。
                # 不兼容历史会话的前提下，跳过该条存量数据而不是让整页初始化失败。
                logger.warning(
                    "调试页跳过非规范 chatId 的存量会话: internal_chat_id=%s",
                    item.chat_id,
                )
                continue
            # `fileNames` 是既有调试接口字段，不能增加新的前后端参数。本次把其
            # 语义校正为 Active Scope，使调试页恢复会话时发送的文件范围与模型
            # 实际范围一致；Workspace bindings 只作为独立计数记录到脱敏日志，
            # 不再冒充当前范围。
            current_scope = chat_store.scopes.get_current_revision(
                item.chat_id
            )
            active_file_names = (
                []
                if current_scope is None
                else [
                    member.file_name
                    for member in current_scope.members
                ]
            )
            if current_scope is None:
                logger.warning(
                    "调试页会话缺少活动范围，按空活动范围展示: chat_id=%s",
                    item.chat_id,
                )
            current_bindings = (
                chat_store.document_bindings.list_current_by_chat(
                    item.chat_id
                )
            )
            active_scope_member_count += len(active_file_names)
            workspace_binding_count += len(current_bindings)
            sessions.append(
                {
                    "chatId": public_chat_id,
                    "fileNames": active_file_names,
                    "createdAt": item.created_at,
                    "updatedAt": item.updated_at,
                }
            )
        available_files = [
            {
                "fileName": item["file_name"],
                "architectureId": item["architecture_id"],
            }
            for item in kb_service.list_document_records()
        ]
    except Exception as exc:
        logger.exception("读取文件对话调试初始化数据失败")
        return {
            "ok": False,
            "message": f"读取失败: {exc}",
            "data": {"sessions": [], "availableFiles": []},
        }

    logger.info(
        "文件对话调试初始化数据读取完成: "
        "session_count=%d active_scope_member_count=%d "
        "workspace_binding_count=%d available_file_count=%d",
        len(sessions),
        active_scope_member_count,
        workspace_binding_count,
        len(available_files),
    )
    return {
        "ok": True,
        "message": "读取成功",
        "data": {
            "sessions": sessions,
            "availableFiles": available_files,
        },
    }
