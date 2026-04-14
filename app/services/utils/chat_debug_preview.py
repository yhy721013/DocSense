from __future__ import annotations

from typing import Any

from app.services.core.database import ChatDatabaseService, DatabaseService


def load_chat_debug_bootstrap(
    *,
    chat_db: ChatDatabaseService,
    kb_service: DatabaseService,
) -> dict[str, Any]:
    try:
        sessions = [
            {
                "chatId": item["chat_id"],
                "fileNames": item["file_names"],
                "createdAt": item["created_at"],
                "updatedAt": item["updated_at"],
            }
            for item in chat_db.list_chats()
        ]
        available_files = [
            {
                "fileName": item["file_name"],
                "architectureId": item["architecture_id"],
            }
            for item in kb_service.list_document_records()
        ]
    except Exception as exc:
        return {
            "ok": False,
            "message": f"读取失败: {exc}",
            "data": {"sessions": [], "availableFiles": []},
        }

    return {
        "ok": True,
        "message": "读取成功",
        "data": {
            "sessions": sessions,
            "availableFiles": available_files,
        },
    }
