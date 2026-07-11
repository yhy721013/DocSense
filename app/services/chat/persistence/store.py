"""文件对话持久化能力边界及当前 SQLite 单实例适配器。

本模块只表达业务真正需要的持久化能力：原子业务命令、条件更新冲突、唯一
约束冲突、事件账本和将来的事务 outbox。它不会根据环境变量猜测 SQL 方言，
也不会把 SQLite 伪装成可多实例共享的数据库；实际产品选型后应新增适配器和
正式迁移项目，并在容器层替换 ``ChatStore``。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from app.services.chat.persistence.event_repository import (
    ChatRunEventRepository,
    ChatRunEventStore,
)
from app.services.chat.persistence.repositories import (
    ChatCleanupJobRepository,
    ChatDocumentBindingRepository,
    ChatMessageRepository,
    ChatRunInputRepository,
    ChatRunRepository,
    ChatSessionRepository,
    ensure_chat_schema,
)
from app.services.chat.persistence.resource_lease_service import (
    ChatResourceLeaseService,
)


logger = logging.getLogger(__name__)


class ChatPersistenceError(RuntimeError):
    """文件对话持久化适配器的稳定基础异常类型。"""


class ChatInfrastructureCapabilityError(ChatPersistenceError):
    """当前适配器未提供调用方所要求的基础设施能力。"""


@dataclass(frozen=True)
class ChatPersistenceCapabilities:
    """持久化适配器的可验证能力声明。

    能力均以正向语义命名，避免一个支持多实例的未来适配器因为“不再是 only”
    而被错误拒绝。``supports_transactional_outbox`` 为假时，业务不得声称已经
    拥有可靠投递能力。
    """

    supports_single_instance: bool
    supports_shared_instances: bool
    supports_atomic_transactions: bool
    supports_conditional_updates: bool
    supports_unique_constraints: bool
    supports_event_ledger: bool
    supports_transactional_outbox: bool


SQLITE_SINGLE_INSTANCE_PERSISTENCE_CAPABILITIES = ChatPersistenceCapabilities(
    supports_single_instance=True,
    supports_shared_instances=False,
    supports_atomic_transactions=True,
    supports_conditional_updates=True,
    supports_unique_constraints=True,
    supports_event_ledger=True,
    supports_transactional_outbox=False,
)


@dataclass(frozen=True)
class ChatOutboxMessage:
    """未来可靠投递使用的内部 outbox 记录，不属于 HTTP/SSE 协议。"""

    message_id: str
    topic: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        message_id = str(self.message_id or "").strip()
        topic = str(self.topic or "").strip()
        if not message_id:
            raise ValueError("message_id cannot be empty")
        if not topic:
            raise ValueError("topic cannot be empty")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a mapping")
        object.__setattr__(self, "message_id", message_id)
        object.__setattr__(self, "topic", topic)
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@runtime_checkable
class ChatOutboxStore(Protocol):
    """事务 outbox 的产品无关能力协议。

    当前实现故意不可用；只有在正式持久化产品、迁移和调度器均完成验证后，
    才能安装一个 ``supports_transactional_outbox=True`` 的实现。
    """

    @property
    def enabled(self) -> bool:
        """返回 outbox 是否可安全用于持久化投递。"""
        ...

    def enqueue(self, message: ChatOutboxMessage) -> None:
        """在持久化适配器的原子业务命令内登记待投递的内部消息。"""
        ...


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


@runtime_checkable
class ChatPersistenceStore(Protocol):
    """文件对话应用服务依赖的持久化能力集合。"""

    capabilities: ChatPersistenceCapabilities
    sessions: ChatSessionRepository
    document_bindings: ChatDocumentBindingRepository
    runs: ChatRunRepository
    run_inputs: ChatRunInputRepository
    events: ChatRunEventStore
    messages: ChatMessageRepository
    resource_leases: ChatResourceLeaseService
    cleanup_jobs: ChatCleanupJobRepository
    outbox: ChatOutboxStore


class ChatStore:
    """当前 SQLite 单实例模式的文件对话持久化聚合入口。"""

    capabilities = SQLITE_SINGLE_INSTANCE_PERSISTENCE_CAPABILITIES

    def __init__(self, db_path: str) -> None:
        ensure_chat_schema(db_path)
        # ChatStore 是应用层唯一聚合入口；各仓储共享同一个 db_path，
        # 但每个操作独立获取连接，避免把 SQLite 连接对象跨线程复用。
        self.sessions = ChatSessionRepository(db_path, initialize=False)
        self.document_bindings = ChatDocumentBindingRepository(
            db_path,
            initialize=False,
        )
        self.runs = ChatRunRepository(db_path, initialize=False)
        self.run_inputs = ChatRunInputRepository(db_path, initialize=False)
        self.events = ChatRunEventRepository(db_path, initialize=False)
        self.messages = ChatMessageRepository(db_path, initialize=False)
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
            "文件对话SQLite单实例持久化仓储已初始化: db_path=%s outbox_enabled=%s",
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
