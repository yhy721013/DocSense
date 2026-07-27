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
        current_binding_count = 0
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
            # 会话文件选择只读取当前 binding heads；历史附件由页面随后调用
            # `/llm/chat/history` 从本地 committed 消息读取。两者都不依赖调用方
            # 本次传入的空数组，也不读取 AnythingLLM Thread 历史。
            current_bindings = (
                chat_store.document_bindings.list_current_by_chat(
                    item.chat_id
                )
            )
            current_binding_count += len(current_bindings)
            sessions.append(
                {
                    "chatId": public_chat_id,
                    "fileNames": [
                        document.file_name
                        for document in current_bindings
                    ],
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
        "session_count=%d current_binding_count=%d available_file_count=%d",
        len(sessions),
        current_binding_count,
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
