"""文件对话本地权威数据的持久化适配器。"""

from app.services.chat.persistence.repositories import (
    ChatCleanupJobRepository,
    ChatDocumentBindingRepository,
    ChatMessageRepository,
    ChatRunRepository,
    ChatScopeRepository,
    ChatSessionScopeBindingRepository,
    ChatSessionRepository,
    chat_scope_revision_id_for_run,
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
    ChatPersistenceError,
    ChatPersistenceStore,
    ChatStore,
    DisabledChatOutbox,
    SQLITE_SINGLE_INSTANCE_PERSISTENCE_CAPABILITIES,
)

__all__ = [
    "ChatCleanupJobRepository",
    "ChatDocumentBindingRepository",
    "ChatInfrastructureCapabilityError",
    "ChatMessageRepository",
    "ChatOutboxMessage",
    "ChatOutboxStore",
    "ChatPersistenceCapabilities",
    "ChatPersistenceError",
    "ChatPersistenceStore",
    "ChatResourceLeaseService",
    "ChatRunEventRepository",
    "ChatRunEventStore",
    "ChatRunRepository",
    "ChatScopeRepository",
    "ChatSessionScopeBindingRepository",
    "ChatSessionRepository",
    "ChatStore",
    "DisabledChatOutbox",
    "SQLITE_SINGLE_INSTANCE_PERSISTENCE_CAPABILITIES",
    "chat_scope_revision_id_for_run",
    "ensure_chat_schema",
]
