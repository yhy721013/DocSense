"""文件对话本地权威数据的持久化适配器。"""

from app.modules.chat.adapters.sqlite.repositories import (
    ChatCleanupJobRepository,
    ChatDocumentBindingRepository,
    ChatMessageRepository,
    ChatMessageSourceRepository,
    ChatRunRepository,
    ChatScopeRepository,
    ChatSessionScopeBindingRepository,
    ChatSessionRepository,
    chat_scope_revision_id_for_run,
    ensure_chat_schema,
)
from app.modules.chat.adapters.sqlite.event_repository import (
    ChatRunEventRepository,
    ChatRunEventStore,
)
from app.modules.chat.adapters.sqlite.identity_repository import (
    DEFAULT_CONVERSATION_ADMISSION_SECONDS,
    SQLiteConversationIdentityRepository,
)
from app.modules.chat.adapters.sqlite.resource_lease_service import (
    ChatResourceLeaseService,
)
from app.modules.chat.adapters.sqlite.store import (
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
    "DEFAULT_CONVERSATION_ADMISSION_SECONDS",
    "ChatMessageRepository",
    "ChatMessageSourceRepository",
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
    "SQLiteConversationIdentityRepository",
    "chat_scope_revision_id_for_run",
    "ensure_chat_schema",
]
