"""文件对话范围选择器的框架无关 Web 入站解析。"""

from __future__ import annotations

from collections.abc import Mapping

from app.modules.chat.domain.document_scope import ChatScopeSelector


CHAT_SCOPE_SELECTOR_CONFLICT_ERROR = "architectureId与fileNames不能同时传入"
CHAT_FILE_NAMES_TYPE_ERROR = "fileNames必须为数组"
CHAT_FILE_NAME_ITEM_ERROR = "fileNames中包含无效文件名"


class ChatScopeSelectorValidationError(ValueError):
    """公开文件对话范围选择参数不满足已批准合同。"""


def parse_chat_scope_selector(params: Mapping[str, object]) -> ChatScopeSelector:
    """解析文件对话的 fileNames 范围选择器。

    本函数不读取 Flask ``request``，便于用完整输入矩阵离线验证，也让未来更换 Web
    框架时继续复用同一入站规则。``architectureId`` 已迁移到独立的
    ``/llm/weaponry-chat*`` 合同；旧路由不再把它解释为类别范围。
    """
    if not isinstance(params, Mapping):
        raise TypeError("params must be a mapping")

    if "architectureId" in params:
        raise ChatScopeSelectorValidationError(
            CHAT_SCOPE_SELECTOR_CONFLICT_ERROR
        )

    # 缺少 fileNames 仍走原有类型错误，不反向改变文件对话合同。
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
    "CHAT_FILE_NAME_ITEM_ERROR",
    "CHAT_FILE_NAMES_TYPE_ERROR",
    "CHAT_SCOPE_SELECTOR_CONFLICT_ERROR",
    "ChatScopeSelectorValidationError",
    "parse_chat_scope_selector",
]
