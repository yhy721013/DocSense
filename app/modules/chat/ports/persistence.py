"""Chat 应用层使用的持久化能力、错误和未来 Outbox 协议。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable


class ChatPersistenceError(RuntimeError):
    """持久化适配器在 Chat 边界内暴露的稳定基础异常。"""


class ChatInfrastructureCapabilityError(ChatPersistenceError):
    """当前适配器没有提供调用方要求的基础设施能力。"""


class ChatResourceLeaseSessionUnavailableError(ChatPersistenceError):
    """受保护资源租约与会话删除状态发生竞争。"""

    def __init__(self, *, conversation_id: str, status: str) -> None:
        self.conversation_id = str(conversation_id or "").strip()
        self.status = str(status or "").strip()
        if not self.conversation_id or not self.status:
            raise ValueError("conversation_id and status cannot be empty")
        super().__init__("chat session is not active for a new resource lease")


@dataclass(frozen=True)
class ChatPersistenceCapabilities:
    """持久化适配器经过验证的能力声明，不把预留能力描述成已实现。"""

    supports_single_instance: bool
    supports_shared_instances: bool
    supports_atomic_transactions: bool
    supports_conditional_updates: bool
    supports_unique_constraints: bool
    supports_event_ledger: bool
    supports_transactional_outbox: bool


@dataclass(frozen=True)
class ChatOutboxMessage:
    """未来可靠投递使用的内部消息；它不属于 HTTP 或 SSE 合同。"""

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
    """事务 Outbox 的产品无关端口。"""

    @property
    def enabled(self) -> bool:
        """返回当前实现是否可用于可靠持久化投递。"""
        ...

    def enqueue(self, message: ChatOutboxMessage) -> None:
        """在业务事务中登记一条待投递内部消息。"""
        ...


@runtime_checkable
class ChatPersistenceStore(Protocol):
    """Chat 应用服务所需仓储集合的结构化端口。

    阶段 1 保持既有仓储对象的运行时结构，避免机械迁移同时重写算法；阶段 2 的新数据库世代会
    以内部 ``conversation_id`` 重新细化这些子仓储协议。这里使用 ``Any`` 只隔离具体 SQLite
    类名，不降低运行时约束，组合根仍通过能力声明和严格测试验证实际适配器。
    """

    capabilities: ChatPersistenceCapabilities
    identities: Any
    sessions: Any
    document_bindings: Any
    runs: Any
    run_inputs: Any
    scopes: Any
    session_scope_bindings: Any
    events: Any
    messages: Any
    message_sources: Any
    resource_leases: Any
    cleanup_jobs: Any
    outbox: ChatOutboxStore


__all__ = [
    "ChatInfrastructureCapabilityError",
    "ChatOutboxMessage",
    "ChatOutboxStore",
    "ChatPersistenceCapabilities",
    "ChatPersistenceError",
    "ChatPersistenceStore",
    "ChatResourceLeaseSessionUnavailableError",
]
