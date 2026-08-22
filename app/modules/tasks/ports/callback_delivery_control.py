"""Callback Guard 与投递控制事实的稳定内部端口。

本端口只描述 SQLite/MySQL 都能实现的持久化控制语义，不执行 Callback HTTP，也不读取
本地 JSON 诊断历史。业务模块负责构造公开载荷和分类网络结果；Task Control 只负责
latest-wins、发送租约、单调 fencing、结果未知冻结、显式恢复授权和人工解除审计。

所有写方法都必须在调用方提供的短 Unit of Work 中执行。网络请求、文件写盘和等待均
不得发生在该事务内。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from app.modules.tasks.domain import TaskBusinessRef, TaskExecutionAuthority, TaskId

from .clock import require_persisted_utc
from .recovery_finalization import RecoveryCallbackEligibilityCommand


def _required_text(value: object, *, name: str, maximum: int = 512) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    if len(normalized) > maximum:
        raise ValueError(f"{name} 最多 {maximum} 个字符")
    return normalized


def _required_later_lease(
    value: object,
    *,
    after: str,
    name: str,
) -> str:
    """校验新的租约截止时间严格晚于指定持久 UTC 时间。"""

    normalized = require_persisted_utc(value, name=name)
    if normalized <= after:
        raise ValueError(f"{name} 必须严格晚于 {after}")
    return normalized


class CallbackAdmissionConflict(str, Enum):
    """新 Task 受理时需要阻断的权威 Callback Guard 状态。"""

    NONE = "none"
    SENDING = "sending"
    OUTCOME_UNKNOWN = "outcome_unknown"


class CallbackDeliveryTrigger(str, Enum):
    """一次真实投递授权的来源；该值只进入内部审计。"""

    INITIAL_DELIVERY = "initial_delivery"
    EXPLICIT_CHECK_TASK_RECOVERY = "explicit_check_task_recovery"


class CallbackDeliveryOutcome(str, Enum):
    """业务 HTTP Adapter 必须返回的精确内部结果。"""

    SUCCESS = "success"
    REJECTED = "rejected"
    DEFINITELY_NOT_SENT = "definitely_not_sent"
    DELIVERY_OUTCOME_UNKNOWN = "delivery_outcome_unknown"
    SKIPPED = "skipped"
    STALE = "stale"


class CallbackAcquireOutcome(str, Enum):
    """取得 Callback 发送权的条件写结果。"""

    ACQUIRED = "acquired"
    STALE = "stale"
    BUSY = "busy"
    OUTCOME_UNKNOWN = "outcome_unknown"
    ALREADY_COMPLETED = "already_completed"
    INVALID_STATE = "invalid_state"


class CallbackControlMutationOutcome(str, Enum):
    """Callback 控制面写入的稳定结果，禁止以异常表达正常竞争。"""

    APPLIED = "applied"
    DUPLICATE = "duplicate"
    MISSING = "missing"
    STALE = "stale"
    AUTHORITY_LOST = "authority_lost"
    LEASE_EXPIRED = "lease_expired"
    INVALID_STATE = "invalid_state"
    OUTCOME_UNKNOWN = "outcome_unknown"


class CallbackValidationOutcome(str, Enum):
    """HTTP 外发前的最后一次权威复核结果。"""

    VALID = "valid"
    STALE = "stale"
    AUTHORITY_LOST = "authority_lost"
    LEASE_EXPIRED = "lease_expired"
    OUTCOME_UNKNOWN = "outcome_unknown"


class CallbackGuardState(str, Enum):
    """供事务外有界等待和运维诊断使用的 Guard 观察状态。"""

    IDLE = "idle"
    SENDING = "sending"
    OUTCOME_UNKNOWN = "outcome_unknown"


class CallbackReleaseOutcome(str, Enum):
    RELEASED = "released"
    ALREADY_RELEASED = "already_released"
    NOT_FROZEN = "not_frozen"


@dataclass(frozen=True, slots=True)
class CallbackEligibilityCommand:
    """在业务终态事务中登记 Callback 可投递事实。

    ``authority`` 使专用 Callback Store 能独立复核本次终态确由当前 Attempt 提交；它不能
    从数据库临时读取“当前 Authority”为调用者补权。
    """

    authority: TaskExecutionAuthority = field(repr=False)
    business_ref: TaskBusinessRef
    eligible_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.authority, TaskExecutionAuthority):
            raise TypeError("authority 必须是 TaskExecutionAuthority")
        if not isinstance(self.business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        object.__setattr__(
            self,
            "eligible_at",
            require_persisted_utc(self.eligible_at, name="eligible_at"),
        )


@dataclass(frozen=True, slots=True)
class CallbackDeliveryLease:
    """一次发送权的不透明能力；随机 token 不得进入 repr 或日志。"""

    task_id: TaskId
    business_ref: TaskBusinessRef
    lease_token: str = field(repr=False)
    fencing_token: int
    lease_expires_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        object.__setattr__(
            self,
            "lease_token",
            _required_text(self.lease_token, name="lease_token", maximum=256),
        )
        if (
            isinstance(self.fencing_token, bool)
            or not isinstance(self.fencing_token, int)
            or self.fencing_token <= 0
        ):
            raise ValueError("fencing_token 必须是正整数")
        object.__setattr__(
            self,
            "lease_expires_at",
            require_persisted_utc(
                self.lease_expires_at,
                name="lease_expires_at",
            ),
        )


@dataclass(frozen=True, slots=True)
class CallbackAcquireCommand:
    task_id: TaskId
    business_ref: TaskBusinessRef
    trigger: CallbackDeliveryTrigger
    lease_token: str = field(repr=False)
    acquired_at: str
    lease_expires_at: str
    expected_callback_attempts: int | None = None
    request_trace_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        if not isinstance(self.trigger, CallbackDeliveryTrigger):
            raise TypeError("trigger 必须是 CallbackDeliveryTrigger")
        object.__setattr__(
            self,
            "lease_token",
            _required_text(self.lease_token, name="lease_token", maximum=256),
        )
        acquired_at = require_persisted_utc(self.acquired_at, name="acquired_at")
        lease_expires_at = _required_later_lease(
            self.lease_expires_at,
            after=acquired_at,
            name="lease_expires_at",
        )
        object.__setattr__(self, "acquired_at", acquired_at)
        object.__setattr__(self, "lease_expires_at", lease_expires_at)
        if self.expected_callback_attempts is not None and (
            isinstance(self.expected_callback_attempts, bool)
            or not isinstance(self.expected_callback_attempts, int)
            or self.expected_callback_attempts < 0
        ):
            raise ValueError("expected_callback_attempts 必须是非负整数或 None")
        explicit = self.trigger is CallbackDeliveryTrigger.EXPLICIT_CHECK_TASK_RECOVERY
        if explicit != (self.expected_callback_attempts is not None):
            raise ValueError("显式 check-task 恢复必须携带 callback attempt 快照")
        if not isinstance(self.request_trace_id, str):
            raise TypeError("request_trace_id 必须是 str")
        trace_id = self.request_trace_id.strip()
        if len(trace_id) > 128:
            raise ValueError("request_trace_id 最多 128 个字符")
        object.__setattr__(self, "request_trace_id", trace_id)


@dataclass(frozen=True, slots=True)
class CallbackAcquireResult:
    outcome: CallbackAcquireOutcome
    lease: CallbackDeliveryLease | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, CallbackAcquireOutcome):
            raise TypeError("outcome 必须是 CallbackAcquireOutcome")
        if self.outcome is CallbackAcquireOutcome.ACQUIRED:
            if not isinstance(self.lease, CallbackDeliveryLease):
                raise TypeError("acquired 结果必须携带 CallbackDeliveryLease")
        elif self.lease is not None:
            raise ValueError("未取得发送权时不得携带 lease")


@dataclass(frozen=True, slots=True)
class CallbackHeartbeatCommand:
    lease: CallbackDeliveryLease = field(repr=False)
    heartbeat_at: str
    lease_expires_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.lease, CallbackDeliveryLease):
            raise TypeError("lease 必须是 CallbackDeliveryLease")
        heartbeat_at = require_persisted_utc(self.heartbeat_at, name="heartbeat_at")
        lease_expires_at = _required_later_lease(
            self.lease_expires_at,
            after=max(heartbeat_at, self.lease.lease_expires_at),
            name="lease_expires_at",
        )
        object.__setattr__(self, "heartbeat_at", heartbeat_at)
        object.__setattr__(self, "lease_expires_at", lease_expires_at)


@dataclass(frozen=True, slots=True)
class CallbackHeartbeatResult:
    outcome: CallbackControlMutationOutcome
    lease: CallbackDeliveryLease | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, CallbackControlMutationOutcome):
            raise TypeError("outcome 必须是 CallbackControlMutationOutcome")
        if self.outcome is CallbackControlMutationOutcome.APPLIED:
            if not isinstance(self.lease, CallbackDeliveryLease):
                raise TypeError("heartbeat applied 必须返回续期 lease")
        elif self.lease is not None:
            raise ValueError("heartbeat 未生效时不得返回 lease")


@dataclass(frozen=True, slots=True)
class CallbackValidationCommand:
    lease: CallbackDeliveryLease = field(repr=False)
    observed_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.lease, CallbackDeliveryLease):
            raise TypeError("lease 必须是 CallbackDeliveryLease")
        object.__setattr__(
            self,
            "observed_at",
            require_persisted_utc(self.observed_at, name="observed_at"),
        )


@dataclass(frozen=True, slots=True)
class CallbackCompleteCommand:
    lease: CallbackDeliveryLease = field(repr=False)
    outcome: CallbackDeliveryOutcome
    detail: str
    completed_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.lease, CallbackDeliveryLease):
            raise TypeError("lease 必须是 CallbackDeliveryLease")
        if not isinstance(self.outcome, CallbackDeliveryOutcome):
            raise TypeError("outcome 必须是 CallbackDeliveryOutcome")
        if self.outcome is CallbackDeliveryOutcome.STALE:
            raise ValueError("stale 表示未执行 HTTP，不得作为已发送结果完成 lease")
        if not isinstance(self.detail, str):
            raise TypeError("detail 必须是 str")
        detail = self.detail.strip()
        if len(detail) > 512:
            detail = detail[:512]
        object.__setattr__(self, "detail", detail)
        object.__setattr__(
            self,
            "completed_at",
            require_persisted_utc(self.completed_at, name="completed_at"),
        )


@dataclass(frozen=True, slots=True)
class CallbackGuardObservation:
    state: CallbackGuardState
    owner_task_id: TaskId | None = None
    lease_expires_at: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.state, CallbackGuardState):
            raise TypeError("state 必须是 CallbackGuardState")
        if self.owner_task_id is not None and not isinstance(self.owner_task_id, TaskId):
            raise TypeError("owner_task_id 必须是 TaskId 或 None")
        if self.lease_expires_at:
            object.__setattr__(
                self,
                "lease_expires_at",
                require_persisted_utc(
                    self.lease_expires_at,
                    name="lease_expires_at",
                ),
            )


@dataclass(frozen=True, slots=True)
class CallbackGuardSweepCommand:
    business_type: str
    observed_at: str
    limit: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "business_type",
            _required_text(self.business_type, name="business_type", maximum=64),
        )
        object.__setattr__(
            self,
            "observed_at",
            require_persisted_utc(self.observed_at, name="observed_at"),
        )
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or not 1 <= self.limit <= 1000:
            raise ValueError("limit 必须是 1..1000 的整数")


@dataclass(frozen=True, slots=True)
class CallbackGuardSweepResult:
    scanned_count: int
    frozen_count: int

    def __post_init__(self) -> None:
        for name in ("scanned_count", "frozen_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数")
        if self.frozen_count > self.scanned_count:
            raise ValueError("frozen_count 不得超过 scanned_count")


@dataclass(frozen=True, slots=True)
class CallbackReleaseUnknownCommand:
    business_ref: TaskBusinessRef
    released_by: str
    reason: str
    worker_stopped_confirmed: bool
    released_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        object.__setattr__(
            self,
            "released_by",
            _required_text(self.released_by, name="released_by", maximum=128),
        )
        object.__setattr__(
            self,
            "reason",
            _required_text(self.reason, name="reason", maximum=512),
        )
        if not isinstance(self.worker_stopped_confirmed, bool):
            raise TypeError("worker_stopped_confirmed 必须是 bool")
        if not self.worker_stopped_confirmed:
            raise ValueError("人工解除前必须确认旧 Worker 已停止或被隔离")
        object.__setattr__(
            self,
            "released_at",
            require_persisted_utc(self.released_at, name="released_at"),
        )


@runtime_checkable
class CallbackAdmissionConflictPort(Protocol):
    """在 Admission UoW 的同一一致性视图中读取 Guard 冲突。"""

    def get_admission_conflict(
        self,
        business_ref: TaskBusinessRef,
    ) -> CallbackAdmissionConflict:
        ...


@runtime_checkable
class CallbackDeliveryControlPort(Protocol):
    """完整 Callback Guard/Delivery 持久化边界；不包含 HTTP。"""

    def mark_eligible(
        self,
        command: CallbackEligibilityCommand,
    ) -> CallbackControlMutationOutcome:
        ...

    def mark_recovery_eligible(
        self,
        command: RecoveryCallbackEligibilityCommand,
    ) -> CallbackControlMutationOutcome:
        ...

    def acquire(self, command: CallbackAcquireCommand) -> CallbackAcquireResult:
        ...

    def heartbeat(self, command: CallbackHeartbeatCommand) -> CallbackHeartbeatResult:
        ...

    def validate(self, command: CallbackValidationCommand) -> CallbackValidationOutcome:
        ...

    def complete(self, command: CallbackCompleteCommand) -> CallbackControlMutationOutcome:
        ...

    def observe(
        self,
        business_ref: TaskBusinessRef,
        *,
        observed_at: str,
    ) -> CallbackGuardObservation:
        ...

    def freeze_expired(
        self,
        command: CallbackGuardSweepCommand,
    ) -> CallbackGuardSweepResult:
        ...

    def release_unknown(
        self,
        command: CallbackReleaseUnknownCommand,
    ) -> CallbackReleaseOutcome:
        ...


__all__ = [
    "CallbackAcquireCommand",
    "CallbackAcquireOutcome",
    "CallbackAcquireResult",
    "CallbackAdmissionConflict",
    "CallbackAdmissionConflictPort",
    "CallbackCompleteCommand",
    "CallbackControlMutationOutcome",
    "CallbackDeliveryControlPort",
    "CallbackDeliveryLease",
    "CallbackDeliveryOutcome",
    "CallbackDeliveryTrigger",
    "CallbackEligibilityCommand",
    "RecoveryCallbackEligibilityCommand",
    "CallbackGuardObservation",
    "CallbackGuardState",
    "CallbackGuardSweepCommand",
    "CallbackGuardSweepResult",
    "CallbackHeartbeatCommand",
    "CallbackHeartbeatResult",
    "CallbackReleaseOutcome",
    "CallbackReleaseUnknownCommand",
    "CallbackValidationCommand",
    "CallbackValidationOutcome",
]
