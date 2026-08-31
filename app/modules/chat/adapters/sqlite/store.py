"""文件对话持久化能力边界及当前 SQLite 单实例适配器。

本模块只表达业务真正需要的持久化能力：原子业务命令、条件更新冲突、唯一
约束冲突、事件账本和将来的事务 outbox。它不会根据环境变量猜测 SQL 方言，
也不会把 SQLite 伪装成可多实例共享的数据库；实际产品选型后应新增适配器和
正式迁移项目，并在容器层替换 ``ChatStore``。
"""

from __future__ import annotations

import logging

from app.modules.chat.adapters.sqlite.event_repository import (
    ChatRunEventRepository,
)
from app.modules.chat.adapters.sqlite.identity_repository import (
    SQLiteConversationIdentityRepository,
)
from app.modules.chat.adapters.sqlite.repositories import (
    ChatCleanupJobRepository,
    ChatDocumentBindingRepository,
    ChatMessageRepository,
    ChatMessageSourceRepository,
    ChatRunInputRepository,
    ChatRunRepository,
    ChatScopeRepository,
    ChatSessionScopeBindingRepository,
    ChatSessionRepository,
    ensure_chat_schema,
)
from app.modules.chat.adapters.sqlite.resource_lease_service import (
    ChatResourceLeaseService,
)
from app.modules.chat.ports.persistence import (
    ChatInfrastructureCapabilityError,
    ChatOutboxMessage,
    ChatOutboxStore,
    ChatPersistenceCapabilities,
    ChatPersistenceError,
    ChatPersistenceStore,
)


logger = logging.getLogger(__name__)


SQLITE_SINGLE_INSTANCE_PERSISTENCE_CAPABILITIES = ChatPersistenceCapabilities(
    supports_single_instance=True,
    supports_shared_instances=False,
    supports_atomic_transactions=True,
    supports_conditional_updates=True,
    supports_unique_constraints=True,
    supports_event_ledger=True,
    supports_transactional_outbox=False,
)


class DisabledChatOutbox:
    """显式拒绝 outbox 操作，防止 SQLite 单实例被误当作可靠队列。"""

    @property
    def enabled(self) -> bool:
        return False

    def enqueue(self, message: ChatOutboxMessage) -> None:
        if not isinstance(message, ChatOutboxMessage):
            raise TypeError("message must be ChatOutboxMessage")
        raise ChatInfrastructureCapabilityError(
            "transactional outbox is not installed for the current chat persistence adapter"
        )


class ChatStore:
    """当前 SQLite 单实例模式的文件对话持久化聚合入口。"""

    capabilities = SQLITE_SINGLE_INSTANCE_PERSISTENCE_CAPABILITIES

    def __init__(self, db_path: str) -> None:
        ensure_chat_schema(db_path)
        # ChatStore 是应用层唯一聚合入口；各仓储共享同一个 db_path，
        # 但每个操作独立获取连接，避免把 SQLite 连接对象跨线程复用。
        self.sessions = ChatSessionRepository(db_path, initialize=False)
        self.identities = SQLiteConversationIdentityRepository(
            db_path,
            initialize=False,
        )
        self.document_bindings = ChatDocumentBindingRepository(
            db_path,
            initialize=False,
        )
        self.runs = ChatRunRepository(db_path, initialize=False)
        self.run_inputs = ChatRunInputRepository(db_path, initialize=False)
        self.scopes = ChatScopeRepository(db_path, initialize=False)
        self.session_scope_bindings = ChatSessionScopeBindingRepository(
            db_path,
            initialize=False,
        )
        self.events = ChatRunEventRepository(db_path, initialize=False)
        self.messages = ChatMessageRepository(db_path, initialize=False)
        self.message_sources = ChatMessageSourceRepository(
            db_path,
            initialize=False,
        )
        self.resource_leases = ChatResourceLeaseService(
            db_path,
            initialize=False,
        )
        self.cleanup_jobs = ChatCleanupJobRepository(
            db_path,
            initialize=False,
        )
        # SQLite 当前没有事务 outbox 或外部 broker；保留显式禁用对象，
        # 使未来代码不能在无可靠投递能力时静默降级为进程内列表。
        self.outbox: ChatOutboxStore = DisabledChatOutbox()
        logger.info(
            "文件对话 SQLite 单实例持久化仓储已初始化: db_path=%s outbox_enabled=%s",
            db_path,
            self.outbox.enabled,
        )


__all__ = [
    "ChatInfrastructureCapabilityError",
    "ChatOutboxMessage",
    "ChatOutboxStore",
    "ChatPersistenceCapabilities",
    "ChatPersistenceError",
    "ChatPersistenceStore",
    "ChatStore",
    "DisabledChatOutbox",
    "SQLITE_SINGLE_INSTANCE_PERSISTENCE_CAPABILITIES",
]
