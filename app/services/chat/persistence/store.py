"""Aggregate persistence store for file chat."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.services.chat.persistence.repositories import (
    ChatDocumentRepository,
    ChatMessageRepository,
    ChatRunRepository,
    ChatSessionRepository,
    ensure_chat_schema,
)
from app.services.chat.persistence.resource_lease_service import (
    ChatResourceLeaseService,
)


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
    """Aggregate access point for chat persistence repositories."""

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


__all__ = ["ChatPersistenceStore", "ChatStore"]
