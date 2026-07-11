"""文件对话持久化能力边界及当前 SQLite 单实例适配器。

本模块只表达业务真正需要的持久化能力：事务工作单元、条件更新冲突、唯一
约束冲突、事件账本和将来的事务 outbox。它不会根据环境变量猜测 SQL 方言，
也不会把 SQLite 伪装成可多实例共享的数据库；实际产品选型后应新增适配器和
正式迁移项目，并在容器层替换 ``ChatStore``。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

import logging

from app.services.chat.persistence.event_repository import (
    ChatRunEventRepository,
    ChatRunEventStore,
)
from app.services.chat.persistence.repositories import (
    ChatDocumentRepository,
    ChatMessageRepository,
    ChatRunInputRepository,
    ChatRunRepository,
    ChatSessionRepository,
    _connect,
    ensure_chat_schema,
)
from app.services.chat.persistence.resource_lease_service import (
    ChatResourceLeaseService,
)


logger = logging.getLogger(__name__)


class ChatPersistenceError(RuntimeError):
    """文件对话持久化适配器的稳定基础异常类型。"""


class ChatPersistenceConflictError(ChatPersistenceError):
    """条件状态更新、版本比较或领取条件未命中时抛出。"""


class ChatUniqueConstraintViolation(ChatPersistenceConflictError):
    """稳定表达唯一性冲突，不向应用层泄漏具体数据库异常。"""


class ChatInfrastructureCapabilityError(ChatPersistenceError):
    """当前适配器未提供调用方所要求的基础设施能力。"""


@dataclass(frozen=True)
class ChatPersistenceCapabilities:
    """持久化适配器的可验证能力声明。

    ``single_instance_only`` 是部署门禁而不是性能建议。只要其为真，运行时就
    不得把同一个数据文件暴露给多个应用副本。``transactional_outbox`` 为假时，
    业务不得声称已经拥有可靠投递能力。
    """

    single_instance_only: bool
    transactional_unit_of_work: bool
    conditional_updates: bool
    unique_constraints: bool
    event_ledger: bool
    transactional_outbox: bool


SQLITE_SINGLE_INSTANCE_PERSISTENCE_CAPABILITIES = ChatPersistenceCapabilities(
    single_instance_only=True,
    transactional_unit_of_work=True,
    conditional_updates=True,
    unique_constraints=True,
    event_ledger=True,
    transactional_outbox=False,
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
    才能安装一个 ``transactional_outbox=True`` 的实现。
    """

    @property
    def enabled(self) -> bool:
        """返回 outbox 是否可安全用于持久化投递。"""
        ...

    def enqueue(self, message: ChatOutboxMessage) -> None:
        """在当前工作单元内登记待投递的内部消息。"""
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
class ChatUnitOfWork(Protocol):
    """不泄漏 SQL 方言的事务工作单元生命周期协议。"""

    @property
    def active(self) -> bool:
        """返回事务是否仍可用于当前持久化适配器内部操作。"""
        ...

    def __enter__(self) -> "ChatUnitOfWork":
        """进入由适配器管理的事务范围。"""
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> bool | None:
        """根据上下文退出原因提交或回滚事务。"""
        ...

    def commit(self) -> None:
        """提交当前工作单元。"""
        ...

    def rollback(self) -> None:
        """回滚当前工作单元。"""
        ...


class SQLiteChatUnitOfWork(AbstractContextManager["SQLiteChatUnitOfWork"]):
    """SQLite 事务工作单元的适配器内部实现。

    它不向应用服务暴露 ``sqlite3.Connection`` 或通用 SQL API，避免尚未选型时
    把 SQLite 方言变成业务依赖。现有 SQLite 仓储仍保留各自的原子操作；阶段
    13 选型后应让共享持久化适配器把同一业务动作映射到正式工作单元。
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = str(db_path)
        self._connection: sqlite3.Connection | None = None
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def __enter__(self) -> "SQLiteChatUnitOfWork":
        if self._active:
            raise ChatPersistenceError("SQLite unit of work is already active")
        connection = _connect(self._db_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
        except Exception:
            connection.close()
            raise
        self._connection = connection
        self._active = True
        return self

    def commit(self) -> None:
        connection = self._require_active_connection()
        try:
            connection.commit()
        except sqlite3.IntegrityError as exc:
            self._close_after_finish()
            raise self._translate_sqlite_error(exc) from exc
        except sqlite3.DatabaseError as exc:
            self._close_after_finish()
            raise ChatPersistenceError("chat persistence transaction commit failed") from exc
        self._close_after_finish()

    def rollback(self) -> None:
        connection = self._require_active_connection()
        try:
            connection.rollback()
        except sqlite3.DatabaseError as exc:
            raise ChatPersistenceError("chat persistence transaction rollback failed") from exc
        finally:
            self._close_after_finish()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> bool:
        if not self._active:
            return False
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False

    def _require_active_connection(self) -> sqlite3.Connection:
        if not self._active or self._connection is None:
            raise ChatPersistenceError("SQLite unit of work is not active")
        return self._connection

    def _close_after_finish(self) -> None:
        connection = self._connection
        self._connection = None
        self._active = False
        if connection is not None:
            connection.close()

    @staticmethod
    def _translate_sqlite_error(exc: sqlite3.IntegrityError) -> ChatPersistenceError:
        message = str(exc).lower()
        if "unique constraint" in message or "primary key" in message:
            return ChatUniqueConstraintViolation("chat persistence unique constraint violated")
        return ChatPersistenceConflictError("chat persistence integrity constraint violated")


@runtime_checkable
class ChatPersistenceStore(Protocol):
    """文件对话应用服务依赖的持久化能力集合。"""

    db_path: str
    capabilities: ChatPersistenceCapabilities
    sessions: ChatSessionRepository
    documents: ChatDocumentRepository
    runs: ChatRunRepository
    run_inputs: ChatRunInputRepository
    events: ChatRunEventStore
    messages: ChatMessageRepository
    resource_leases: ChatResourceLeaseService
    outbox: ChatOutboxStore

    def open_unit_of_work(self) -> ChatUnitOfWork:
        """创建一个由持久化适配器负责实现的事务工作单元。"""
        ...


class ChatStore:
    """当前 SQLite 单实例模式的文件对话持久化聚合入口。"""

    capabilities = SQLITE_SINGLE_INSTANCE_PERSISTENCE_CAPABILITIES

    def __init__(self, db_path: str) -> None:
        ensure_chat_schema(db_path)
        self.db_path = db_path
        # ChatStore 是应用层唯一聚合入口；各仓储共享同一个 db_path，
        # 但每个操作独立获取连接，避免把 SQLite 连接对象跨线程复用。
        self.sessions = ChatSessionRepository(db_path, initialize=False)
        self.documents = ChatDocumentRepository(db_path, initialize=False)
        self.runs = ChatRunRepository(db_path, initialize=False)
        self.run_inputs = ChatRunInputRepository(db_path, initialize=False)
        self.events = ChatRunEventRepository(db_path, initialize=False)
        self.messages = ChatMessageRepository(db_path, initialize=False)
        self.resource_leases = ChatResourceLeaseService(
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

    def open_unit_of_work(self) -> ChatUnitOfWork:
        """创建 SQLite 事务生命周期对象，仅供持久化适配层组合操作。"""
        return SQLiteChatUnitOfWork(self.db_path)


__all__ = [
    "ChatInfrastructureCapabilityError",
    "ChatOutboxMessage",
    "ChatOutboxStore",
    "ChatPersistenceCapabilities",
    "ChatPersistenceConflictError",
    "ChatPersistenceError",
    "ChatPersistenceStore",
    "ChatStore",
    "ChatUniqueConstraintViolation",
    "ChatUnitOfWork",
    "DisabledChatOutbox",
    "SQLITE_SINGLE_INSTANCE_PERSISTENCE_CAPABILITIES",
    "SQLiteChatUnitOfWork",
]
