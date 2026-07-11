"""Persistence adapters for file chat local authority data."""

from app.services.chat.persistence.repositories import (
    ChatDocumentRepository,
    ChatMessageRepository,
    ChatRunRepository,
    ChatSessionRepository,
    ensure_chat_schema,
)
from app.services.chat.persistence.event_repository import (
    ChatRunEventRepository,
    ChatRunEventStore,
)
from app.services.chat.persistence.resource_lease_service import (
    ChatResourceLeaseService,
)
from app.services.chat.persistence.store import (
    ChatInfrastructureCapabilityError,
    ChatOutboxMessage,
    ChatOutboxStore,
    ChatPersistenceCapabilities,
    ChatPersistenceConflictError,
    ChatPersistenceError,
    ChatPersistenceStore,
    ChatStore,
    ChatUniqueConstraintViolation,
    ChatUnitOfWork,
    DisabledChatOutbox,
    SQLITE_SINGLE_INSTANCE_PERSISTENCE_CAPABILITIES,
    SQLiteChatUnitOfWork,
)

__all__ = [
    "ChatDocumentRepository",
    "ChatInfrastructureCapabilityError",
    "ChatMessageRepository",
    "ChatOutboxMessage",
    "ChatOutboxStore",
    "ChatPersistenceCapabilities",
    "ChatPersistenceConflictError",
    "ChatPersistenceError",
    "ChatPersistenceStore",
    "ChatResourceLeaseService",
    "ChatRunEventRepository",
    "ChatRunEventStore",
    "ChatRunRepository",
    "ChatSessionRepository",
    "ChatStore",
    "ChatUniqueConstraintViolation",
    "ChatUnitOfWork",
    "DisabledChatOutbox",
    "SQLITE_SINGLE_INSTANCE_PERSISTENCE_CAPABILITIES",
    "SQLiteChatUnitOfWork",
    "ensure_chat_schema",
]
