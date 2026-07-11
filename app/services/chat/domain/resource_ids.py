"""Stable identifiers for durable file-chat external-resource leases."""

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
    """Build an auditable lease ID for one title-generation temporary thread."""
    normalized_chat_id = _required_text(chat_id, name="chat_id")
    normalized_attempt_id = _required_text(attempt_id, name="attempt_id")
    return f"chat:{normalized_chat_id}:temporary_thread:{normalized_attempt_id}"


def chat_scoped_external_ref(*, context_ref: str, resource_ref: str) -> str:
    """Encode a context-owned remote reference for the local lease ledger.

    The lease table intentionally stores one opaque string so it remains
    independent from any supplier schema.  All file-chat code must use this
    helper instead of assembling delimiter-separated text ad hoc; that keeps
    the temporary-thread cleanup path and the normal session path consistent.

    JSON is used solely as an internal, self-describing envelope.  In contrast
    to a ``"context::resource"`` convention, it does not reserve characters
    that a future supplier may legitimately use in either opaque reference.
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
    """Decode a lease reference created by :func:`chat_scoped_external_ref`.

    Recovery validates the internal envelope before a remote delete call.  The
    values themselves remain supplier-opaque and can contain arbitrary text.
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
    "chat_scoped_external_ref",
    "chat_document_binding_lease_id",
    "chat_temporary_thread_lease_id",
    "chat_thread_lease_id",
    "chat_workspace_lease_id",
    "parse_chat_scoped_external_ref",
]
