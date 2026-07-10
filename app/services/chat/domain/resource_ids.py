"""Stable identifiers for durable file-chat external-resource leases."""

from __future__ import annotations

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


def chat_document_binding_lease_id(
    *,
    chat_id: str,
    file_name: str,
    document_ref: str = "",
) -> str:
    """Build a lease ID for one business file and immutable document revision."""
    normalized_chat_id = _required_text(chat_id, name="chat_id")
    normalized_file_name = _required_text(file_name, name="file_name")
    normalized_document_ref = str(document_ref or "").strip()
    suffix = ""
    if normalized_document_ref:
        digest = sha256(normalized_document_ref.encode("utf-8")).hexdigest()[:16]
        suffix = f":{digest}"
    return f"chat:{normalized_chat_id}:document_binding:{normalized_file_name}{suffix}"


__all__ = [
    "chat_document_binding_lease_id",
    "chat_thread_lease_id",
    "chat_workspace_lease_id",
]
