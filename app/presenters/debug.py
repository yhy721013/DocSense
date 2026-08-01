"""Debug 查询结果到既有内部 JSON 契约的唯一 Presenter。"""

from __future__ import annotations

from typing import Any, Mapping

from app.modules.debug.application.models import (
    CallbackPreviewResult,
    ChatBootstrapResult,
)
from app.modules.debug.ports.callback_history import CallbackRecord


def _callback_record_payload(record: CallbackRecord) -> dict[str, Any]:
    return {
        "id": record.record_id,
        "fileName": record.file_name,
        "modifiedAt": record.modified_at,
        "sizeBytes": record.size_bytes,
    }


def _thaw_json_value(value: Any) -> Any:
    """把 Application 的深层不可变 JSON 快照还原为标准 JSON 容器。"""

    if isinstance(value, Mapping):
        return {key: _thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json_value(item) for item in value]
    return value


def present_callback_preview(result: CallbackPreviewResult) -> dict[str, Any]:
    """逐字段保持 Callback Debug 现有响应，不增加任何调试字段。"""

    return {
        "ok": result.ok,
        "message": result.message,
        "payload": (
            None if result.payload is None else _thaw_json_value(result.payload)
        ),
        "records": [_callback_record_payload(item) for item in result.records],
        "selectedRecord": (
            None
            if result.selected_record is None
            else _callback_record_payload(result.selected_record)
        ),
    }


def present_chat_bootstrap(result: ChatBootstrapResult) -> dict[str, Any]:
    """逐字段保持 Chat Debug 初始化响应，聚合计数只用于日志。"""

    return {
        "ok": result.ok,
        "message": result.message,
        "data": {
            "sessions": [
                {
                    "chatId": item.chat_id,
                    "fileNames": list(item.file_names),
                    "createdAt": item.created_at,
                    "updatedAt": item.updated_at,
                }
                for item in result.sessions
            ],
            "availableFiles": [
                {
                    "fileName": item.file_name,
                    "architectureId": item.architecture_id,
                }
                for item in result.available_files
            ],
        },
    }
