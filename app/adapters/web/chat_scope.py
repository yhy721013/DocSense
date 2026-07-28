"""文件对话范围选择器的框架无关 Web 入站解析。"""

from __future__ import annotations

from collections.abc import Mapping

from app.adapters.web.weaponry_ids import (
    ArchitectureIdValidationError,
    normalize_architecture_id,
)
from app.services.chat.domain.document_scope import ChatScopeSelector


CHAT_ARCHITECTURE_ID_EMPTY_ERROR = "architectureId不能为空"
CHAT_SCOPE_SELECTOR_CONFLICT_ERROR = "architectureId与fileNames不能同时传入"
CHAT_FILE_NAMES_TYPE_ERROR = "fileNames必须为数组"
CHAT_FILE_NAME_ITEM_ERROR = "fileNames中包含无效文件名"


class ChatScopeSelectorValidationError(ValueError):
    """公开文件对话范围选择参数不满足已批准合同。"""


def parse_chat_scope_selector(params: Mapping[str, object]) -> ChatScopeSelector:
    """按字段存在性解析 fileNames/architectureId 严格二选一选择器。

    本函数不读取 Flask ``request``，便于用完整输入矩阵离线验证，也让未来更换 Web
    框架时继续复用同一入站规则。两个字段都缺失时保持既有 fileNames 行为；只有调用方
    明确提交了 ``architectureId`` 且值为 ``null`` 时，才返回 architecture 专用空值错误。
    """
    if not isinstance(params, Mapping):
        raise TypeError("params must be a mapping")

    has_architecture_id = "architectureId" in params
    has_file_names = "fileNames" in params
    if has_architecture_id and has_file_names:
        raise ChatScopeSelectorValidationError(
            CHAT_SCOPE_SELECTOR_CONFLICT_ERROR
        )

    if has_architecture_id:
        raw_architecture_id = params["architectureId"]
        if raw_architecture_id is None:
            raise ChatScopeSelectorValidationError(
                CHAT_ARCHITECTURE_ID_EMPTY_ERROR
            )
        try:
            architecture_id = normalize_architecture_id(
                raw_architecture_id
            ).value
        except ArchitectureIdValidationError as exc:
            raise ChatScopeSelectorValidationError(str(exc)) from exc
        return ChatScopeSelector.for_architecture(architecture_id)

    # 缺少两个字段仍走原 fileNames 类型错误，防止 architecture 能力反向改变旧调用方。
    raw_file_names = params.get("fileNames")
    if not isinstance(raw_file_names, list):
        raise ChatScopeSelectorValidationError(CHAT_FILE_NAMES_TYPE_ERROR)

    normalized_file_names: list[str] = []
    seen: set[str] = set()
    for raw_file_name in raw_file_names:
        if not isinstance(raw_file_name, str) or not raw_file_name.strip():
            raise ChatScopeSelectorValidationError(CHAT_FILE_NAME_ITEM_ERROR)
        file_name = raw_file_name.strip()
        if file_name in seen:
            continue
        seen.add(file_name)
        normalized_file_names.append(file_name)
    return ChatScopeSelector.for_files(normalized_file_names)


__all__ = [
    "CHAT_ARCHITECTURE_ID_EMPTY_ERROR",
    "CHAT_FILE_NAME_ITEM_ERROR",
    "CHAT_FILE_NAMES_TYPE_ERROR",
    "CHAT_SCOPE_SELECTOR_CONFLICT_ERROR",
    "ChatScopeSelectorValidationError",
    "parse_chat_scope_selector",
]
