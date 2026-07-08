"""File-chat application persistence services."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.services.chat.models import (
    LEASE_ACTIVE,
    LEASE_CLEANUP_FAILED,
    LEASE_CLEANUP_PENDING,
    LEASE_CLOSED,
    LEASE_OPEN_STATUSES,
    LEASE_PLANNED,
    LEASE_STATUSES,
    MESSAGE_COMMITTED,
    MESSAGE_DISCARDED,
    MESSAGE_PENDING,
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_USER,
    MESSAGE_ROLES,
    MESSAGE_STATUSES,
    RESOURCE_DOCUMENT_BINDING,
    RESOURCE_THREAD,
    RESOURCE_TYPES,
    RESOURCE_WORKSPACE,
    RUN_ABORTED,
    RUN_ACCEPTED,
    RUN_ACTIVE_STATUSES,
    RUN_FAILED,
    RUN_RUNNING,
    RUN_STATUSES,
    RUN_SUCCEEDED,
    RUN_TERMINAL_STATUSES,
    SESSION_ACTIVE,
    SESSION_DELETED,
    SESSION_DELETING,
    SESSION_ERROR,
    SESSION_STATUSES,
    ChatDocument,
    ChatMessage,
    ChatMessageFile,
    ChatResourceLease,
    ChatRun,
    ChatSession,
)
from app.services.chat.repositories import (
    ChatDocumentRepository,
    ChatMessageRepository,
    ChatRunRepository,
    ChatSessionRepository,
    ensure_chat_schema,
)
from app.services.chat.resource_lease_service import ChatResourceLeaseService


@runtime_checkable
class ChatPersistenceStore(Protocol):
    """Persistence capabilities required by chat application services."""

    db_path: str
    sessions: ChatSessionRepository
    documents: ChatDocumentRepository
    runs: ChatRunRepository
    messages: ChatMessageRepository
    resource_leases: ChatResourceLeaseService


class ChatStore:
    """Aggregate access point for the stage-3 chat persistence repositories."""

    def __init__(self, db_path: str) -> None:
        ensure_chat_schema(db_path)
        self.db_path = db_path
        self.sessions = ChatSessionRepository(db_path, initialize=False)
        self.documents = ChatDocumentRepository(db_path, initialize=False)
        self.runs = ChatRunRepository(db_path, initialize=False)
        self.messages = ChatMessageRepository(db_path, initialize=False)
        self.resource_leases = ChatResourceLeaseService(
            db_path,
            initialize=False,
        )


__all__ = [
    "ChatDocument",
    "ChatDocumentRepository",
    "ChatMessage",
    "ChatMessageFile",
    "ChatMessageRepository",
    "ChatResourceLease",
    "ChatResourceLeaseService",
    "ChatRun",
    "ChatRunRepository",
    "ChatSession",
    "ChatSessionRepository",
    "ChatPersistenceStore",
    "ChatStore",
    "LEASE_ACTIVE",
    "LEASE_CLEANUP_FAILED",
    "LEASE_CLEANUP_PENDING",
    "LEASE_CLOSED",
    "LEASE_OPEN_STATUSES",
    "LEASE_PLANNED",
    "LEASE_STATUSES",
    "MESSAGE_COMMITTED",
    "MESSAGE_DISCARDED",
    "MESSAGE_PENDING",
    "MESSAGE_ROLE_ASSISTANT",
    "MESSAGE_ROLE_USER",
    "MESSAGE_ROLES",
    "MESSAGE_STATUSES",
    "RESOURCE_DOCUMENT_BINDING",
    "RESOURCE_THREAD",
    "RESOURCE_TYPES",
    "RESOURCE_WORKSPACE",
    "RUN_ABORTED",
    "RUN_ACCEPTED",
    "RUN_ACTIVE_STATUSES",
    "RUN_FAILED",
    "RUN_RUNNING",
    "RUN_STATUSES",
    "RUN_SUCCEEDED",
    "RUN_TERMINAL_STATUSES",
    "SESSION_ACTIVE",
    "SESSION_DELETED",
    "SESSION_DELETING",
    "SESSION_ERROR",
    "SESSION_STATUSES",
    "ensure_chat_schema",
]
