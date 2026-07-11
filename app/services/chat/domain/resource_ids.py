"""文件对话持久化外部资源租约的稳定标识。"""

from __future__ import annotations

import json
from hashlib import sha256


def _required_text(value: str, *, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} cannot be empty")
    return normalized


def chat_workspace_lease_id(chat_id: str) -> str:
    return f"chat:{_required_text(chat_id, name='chat_id')}:workspace"


def chat_thread_lease_id(chat_id: str) -> str:
    return f"chat:{_required_text(chat_id, name='chat_id')}:thread"


def chat_temporary_thread_lease_id(*, chat_id: str, attempt_id: str) -> str:
    """为一次标题生成临时线程构造可审计的租约 ID。"""
    normalized_chat_id = _required_text(chat_id, name="chat_id")
    normalized_attempt_id = _required_text(attempt_id, name="attempt_id")
    return f"chat:{normalized_chat_id}:temporary_thread:{normalized_attempt_id}"


def chat_scoped_external_ref(*, context_ref: str, resource_ref: str) -> str:
    """为本地租约账本编码归属于上下文的远端引用。

    租约表刻意只保存一个不透明字符串，以保持与任何供应商结构解耦。所有文件对话
    代码都必须使用此辅助函数，而不能临时拼接分隔符文本；这样可使临时线程清理路径
    与普通会话路径保持一致。

    JSON 仅作为内部的自描述封装。不同于 ``"context::resource"`` 约定，它不会保留
    未来供应商可能合法用于任一不透明引用中的字符。
    """
    normalized_context_ref = _required_text(context_ref, name="context_ref")
    normalized_resource_ref = _required_text(resource_ref, name="resource_ref")
    return json.dumps(
        {
            "context_ref": normalized_context_ref,
            "resource_ref": normalized_resource_ref,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def parse_chat_scoped_external_ref(external_ref: str) -> tuple[str, str]:
    """解码由 :func:`chat_scoped_external_ref` 创建的租约引用。

    恢复流程会在调用远端删除前校验内部封装。引用值本身仍对供应商保持不透明，
    可以包含任意文本。
    """
    normalized_external_ref = _required_text(external_ref, name="external_ref")
    try:
        payload = json.loads(normalized_external_ref)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "external_ref is not a scoped chat resource reference"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError("external_ref is not a scoped chat resource reference")
    context_ref = payload.get("context_ref")
    resource_ref = payload.get("resource_ref")
    if not isinstance(context_ref, str) or not isinstance(resource_ref, str):
        raise ValueError("external_ref is not a scoped chat resource reference")
    return (
        _required_text(context_ref, name="context_ref"),
        _required_text(resource_ref, name="resource_ref"),
    )


def chat_document_binding_lease_id(
    *,
    chat_id: str,
    file_name: str,
    document_ref: str = "",
) -> str:
    """为一个业务文件及其不可变文档版本构造租约 ID。"""
    normalized_chat_id = _required_text(chat_id, name="chat_id")
    normalized_file_name = _required_text(file_name, name="file_name")
    normalized_document_ref = str(document_ref or "").strip()
    suffix = ""
    if normalized_document_ref:
        digest = sha256(normalized_document_ref.encode("utf-8")).hexdigest()[:16]
        suffix = f":{digest}"
    return f"chat:{normalized_chat_id}:document_binding:{normalized_file_name}{suffix}"


__all__ = [
    "chat_scoped_external_ref",
    "chat_document_binding_lease_id",
    "chat_temporary_thread_lease_id",
    "chat_thread_lease_id",
    "chat_workspace_lease_id",
    "parse_chat_scoped_external_ref",
]
