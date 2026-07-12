"""文件对话持久化层使用的稳定领域模型。"""

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

# 清理任务与资源租约独立持久化。租约描述“存在哪个”远端资源；任务记录“何时、如何”
# 重试补偿。将两种生命周期分离后，未来工作进程可安全重试幂等清理操作，而不会修改
# 资源身份本身。
CLEANUP_JOB_PENDING = "pending"
CLEANUP_JOB_RUNNING = "running"
CLEANUP_JOB_SUCCEEDED = "succeeded"
CLEANUP_JOB_FAILED = "failed"
CLEANUP_JOB_STATUSES = frozenset(
    {
        CLEANUP_JOB_PENDING,
        CLEANUP_JOB_RUNNING,
        CLEANUP_JOB_SUCCEEDED,
        CLEANUP_JOB_FAILED,
    }
)

# 清理原因是内部工作流标识，不是 HTTP/API 值。租约单独提供，因此同一对话归属的多个
# 临时线程可以独立重试，而不会让原因字符串承载过多含义。
CLEANUP_REASON_DELETE_CHAT = "delete_chat"
CLEANUP_REASON_TEMPORARY_THREAD = "temporary_thread"
CLEANUP_JOB_REASONS = frozenset(
    {
        CLEANUP_REASON_DELETE_CHAT,
        CLEANUP_REASON_TEMPORARY_THREAD,
    }
)


@dataclass(frozen=True)
class ChatSession:
    """文件对话会话的本地权威元数据。"""

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
class ChatDocumentBinding:
    """附加到对话会话的一条不可变文档版本。

    ``file_name`` 是业务侧文件键，源文件重新索引时可复用，因此刻意不作为绑定身份。
    ``document_ref`` 表示一个或多个运行实际使用的解析版本，``binding_id`` 则提供
    稳定的本地清理和审计键。
    """

    binding_id: str
    chat_id: str
    file_name: str
    original_name: str
    document_ref: str
    external_location: str
    added_by_run_id: str
    created_at: str


@dataclass(frozen=True)
class ChatRun:
    """`/llm/chat` 的一次内部标识执行尝试。

    ``run_id`` 是用于生命周期、消息和外部资源租约的实现键，刻意不属于 HTTP 或 SSE
    契约的一部分。
    """

    run_id: str
    chat_id: str
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
class ChatRunEvent:
    """一次文件对话运行产出的内部持久化事件。"""

    run_id: str
    event_seq: int
    event_type: str
    data: Mapping[str, Any]
    created_at: str

    def __post_init__(self) -> None:
        run_id = str(self.run_id or "").strip()
        event_type = str(self.event_type or "").strip()
        created_at = str(self.created_at or "").strip()
        if not run_id:
            raise ValueError("run_id cannot be empty")
        if isinstance(self.event_seq, bool) or not isinstance(self.event_seq, int):
            raise TypeError("event_seq must be int")
        if self.event_seq < 1:
            raise ValueError("event_seq must be positive")
        if not event_type:
            raise ValueError("event_type cannot be empty")
        if not isinstance(self.data, Mapping):
            raise TypeError("data must be a mapping")
        if not created_at:
            raise ValueError("created_at cannot be empty")
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "event_type", event_type)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "data", MappingProxyType(dict(self.data)))


@dataclass(frozen=True)
class ChatRunInputFile:
    """受理运行时捕获的不可变文档身份。"""

    file_name: str
    original_name: str
    document_ref: str
    external_location: str


@dataclass(frozen=True)
class ChatRunInput:
    """一条已受理运行可安全入队的消息与文档快照。"""

    run_id: str
    message: str
    files: tuple[ChatRunInputFile, ...]
    created_at: str


@dataclass(frozen=True)
class ChatMessageFile:
    """关联到一条用户消息的业务文件。"""

    message_id: str
    file_name: str
    original_name: str


@dataclass(frozen=True)
class ChatMessage:
    """本地持久化的对话消息。"""

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
    """用于对话清理与恢复的持久化外部资源租约。"""

    lease_id: str
    chat_id: str
    run_id: str
    resource_type: str
    external_ref: str
    status: str
    error_message: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ChatCleanupJob:
    """用于补偿或删除对话所属资源的持久化请求。"""

    job_id: str
    chat_id: str
    reason: str
    lease_id: str
    status: str
    attempt_count: int
    next_attempt_at: str
    error_message: str
    created_at: str
    updated_at: str
