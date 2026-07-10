"""Stable domain models for the file-chat persistence layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional


SESSION_ACTIVE = "active"
SESSION_DELETING = "deleting"
SESSION_DELETED = "deleted"
SESSION_ERROR = "error"
SESSION_STATUSES = frozenset(
    {SESSION_ACTIVE, SESSION_DELETING, SESSION_DELETED, SESSION_ERROR}
)

RUN_ACCEPTED = "accepted"
RUN_RUNNING = "running"
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"
RUN_ABORTED = "aborted"
RUN_STATUSES = frozenset(
    {RUN_ACCEPTED, RUN_RUNNING, RUN_SUCCEEDED, RUN_FAILED, RUN_ABORTED}
)
RUN_ACTIVE_STATUSES = frozenset({RUN_ACCEPTED, RUN_RUNNING})
RUN_TERMINAL_STATUSES = frozenset({RUN_SUCCEEDED, RUN_FAILED, RUN_ABORTED})

MESSAGE_ROLE_USER = "user"
MESSAGE_ROLE_ASSISTANT = "assistant"
MESSAGE_ROLES = frozenset({MESSAGE_ROLE_USER, MESSAGE_ROLE_ASSISTANT})

MESSAGE_PENDING = "pending"
MESSAGE_COMMITTED = "committed"
MESSAGE_DISCARDED = "discarded"
MESSAGE_STATUSES = frozenset(
    {MESSAGE_PENDING, MESSAGE_COMMITTED, MESSAGE_DISCARDED}
)

RESOURCE_WORKSPACE = "workspace"
RESOURCE_THREAD = "thread"
RESOURCE_DOCUMENT_BINDING = "document_binding"
RESOURCE_TYPES = frozenset(
    {RESOURCE_WORKSPACE, RESOURCE_THREAD, RESOURCE_DOCUMENT_BINDING}
)

LEASE_PLANNED = "planned"
LEASE_ACTIVE = "active"
LEASE_CLEANUP_PENDING = "cleanup_pending"
LEASE_CLOSED = "closed"
LEASE_CLEANUP_FAILED = "cleanup_failed"
LEASE_STATUSES = frozenset(
    {
        LEASE_PLANNED,
        LEASE_ACTIVE,
        LEASE_CLEANUP_PENDING,
        LEASE_CLOSED,
        LEASE_CLEANUP_FAILED,
    }
)
LEASE_OPEN_STATUSES = frozenset(
    {LEASE_PLANNED, LEASE_ACTIVE, LEASE_CLEANUP_PENDING, LEASE_CLEANUP_FAILED}
)


@dataclass(frozen=True)
class ChatSession:
    """Local authoritative session metadata for a file-chat conversation."""

    chat_id: str
    workspace_ref: str
    thread_ref: str
    status: str
    created_at: str
    updated_at: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class ChatDocument:
    """Document bound to a chat session."""

    chat_id: str
    file_name: str
    original_name: str
    document_ref: str
    external_location: str
    added_by_run_id: str
    created_at: str


@dataclass(frozen=True)
class ChatRun:
    """One execution attempt for `/llm/chat`."""

    run_id: str
    chat_id: str
    request_id: str
    status: str
    abort_requested: bool
    owner_instance_id: str
    heartbeat_at: Optional[str]
    error_message: str
    created_at: str
    started_at: Optional[str]
    completed_at: Optional[str]
    updated_at: str


@dataclass(frozen=True)
class ChatRunInputFile:
    """Immutable document identity captured when a run is accepted."""

    file_name: str
    original_name: str
    document_ref: str
    external_location: str


@dataclass(frozen=True)
class ChatRunInput:
    """Queue-safe message and document snapshot for one accepted run."""

    run_id: str
    message: str
    files: tuple[ChatRunInputFile, ...]
    created_at: str


@dataclass(frozen=True)
class ChatMessageFile:
    """Business file linked to one user message."""

    message_id: str
    file_name: str
    original_name: str


@dataclass(frozen=True)
class ChatMessage:
    """Locally persisted chat message."""

    message_id: str
    chat_id: str
    run_id: str
    role: str
    content: str
    status: str
    sequence_no: int
    created_at: str
    files: tuple[ChatMessageFile, ...] = ()


@dataclass(frozen=True)
class ChatResourceLease:
    """Persistent external-resource lease for chat cleanup and recovery."""

    lease_id: str
    chat_id: str
    run_id: str
    resource_type: str
    external_ref: str
    status: str
    error_message: str
    created_at: str
    updated_at: str
