from __future__ import annotations

import logging
from typing import Any

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
        for item in chat_store.sessions.list_all():
            if item.status == "deleted":
                continue
            sessions.append(
                {
                    "chatId": item.chat_id,
                    "fileNames": [
                        document.file_name
                        for document in chat_store.document_bindings.list_current_by_chat(
                            item.chat_id
                        )
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
        "文件对话调试初始化数据读取完成: session_count=%d available_file_count=%d",
        len(sessions),
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
