"""武器谱回调 latest-wins、发送权与精确投递结果端口。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from app.modules.tasks.domain import TaskId
from app.modules.weaponry.domain import MAX_ARCHITECTURE_ID, WeaponryCallbackPayload

from .common import optional_text, positive_int, required_text


def _architecture_id(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > MAX_ARCHITECTURE_ID
    ):
        raise ValueError(
            f"architecture_id 必须是 1 到 {MAX_ARCHITECTURE_ID} 的整数"
        )
    return value


class WeaponryCallbackAcquireOutcome(str, Enum):
    ACQUIRED = "acquired"
    STALE = "stale"
    BUSY = "busy"
    OUTCOME_UNKNOWN = "outcome_unknown"
    ALREADY_COMPLETED = "already_completed"


class WeaponryCallbackAcquireReason(str, Enum):
    INITIAL_DELIVERY = "initial_delivery"
    EXPLICIT_CHECK_TASK_RECOVERY = "explicit_check_task_recovery"


class WeaponryCallbackDeliveryOutcome(str, Enum):
    SUCCESS = "success"
    REJECTED = "rejected"
    DEFINITELY_NOT_SENT = "definitely_not_sent"
    DELIVERY_OUTCOME_UNKNOWN = "delivery_outcome_unknown"
    SKIPPED = "skipped"
    STALE = "stale"


class WeaponryCallbackWaitOutcome(str, Enum):
    RELEASED = "released"
    TIMED_OUT = "timed_out"
    OUTCOME_UNKNOWN = "outcome_unknown"


class WeaponryCallbackReleaseOutcome(str, Enum):
    RELEASED = "released"
    ALREADY_RELEASED = "already_released"
    NOT_FROZEN = "not_frozen"


@dataclass(frozen=True)
class WeaponryCallbackGuardLease:
    """一次回调发送权的不透明租约和单调 fencing token。"""

    task_id: TaskId
    architecture_id: int
    token: str
    fencing_token: int
    deadline_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        _architecture_id(self.architecture_id)
        object.__setattr__(self, "token", required_text(self.token, name="token"))
        positive_int(self.fencing_token, name="fencing_token")
        object.__setattr__(
            self,
            "deadline_at",
            required_text(self.deadline_at, name="deadline_at"),
        )


@dataclass(frozen=True)
class AcquireWeaponryCallback:
    task_id: TaskId
    architecture_id: int
    reason: WeaponryCallbackAcquireReason = (
        WeaponryCallbackAcquireReason.INITIAL_DELIVERY
    )

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        _architecture_id(self.architecture_id)
        if not isinstance(self.reason, WeaponryCallbackAcquireReason):
            raise TypeError("reason 必须是 WeaponryCallbackAcquireReason")


@dataclass(frozen=True)
class WeaponryCallbackAcquireResult:
    outcome: WeaponryCallbackAcquireOutcome
    lease: WeaponryCallbackGuardLease | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, WeaponryCallbackAcquireOutcome):
            raise TypeError("outcome 必须是 WeaponryCallbackAcquireOutcome")
        if self.outcome is WeaponryCallbackAcquireOutcome.ACQUIRED:
            if not isinstance(self.lease, WeaponryCallbackGuardLease):
                raise TypeError("acquired 结果必须携带 Guard Lease")
        elif self.lease is not None:
            raise ValueError("未取得发送权时不得携带 Guard Lease")


@dataclass(frozen=True)
class DeliverWeaponryCallback:
    lease: WeaponryCallbackGuardLease
    payload: WeaponryCallbackPayload

    def __post_init__(self) -> None:
        if not isinstance(self.lease, WeaponryCallbackGuardLease):
            raise TypeError("lease 必须是 WeaponryCallbackGuardLease")
        if not isinstance(self.payload, WeaponryCallbackPayload):
            raise TypeError("payload 必须是 WeaponryCallbackPayload")
        if self.payload.architecture_id != self.lease.architecture_id:
            raise ValueError("Callback payload 与 Guard Lease architecture_id 不一致")


@dataclass(frozen=True)
class WeaponryCallbackDeliveryResult:
    outcome: WeaponryCallbackDeliveryOutcome
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, WeaponryCallbackDeliveryOutcome):
            raise TypeError("outcome 必须是 WeaponryCallbackDeliveryOutcome")
        object.__setattr__(self, "detail", optional_text(self.detail, name="detail"))


@dataclass(frozen=True)
class WeaponryCallbackRecoveryCandidate:
    task_id: TaskId
    architecture_id: int
    payload: WeaponryCallbackPayload

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        _architecture_id(self.architecture_id)
        if not isinstance(self.payload, WeaponryCallbackPayload):
            raise TypeError("payload 必须是 WeaponryCallbackPayload")
        if self.payload.architecture_id != self.architecture_id:
            raise ValueError("恢复候选 payload 与 architecture_id 不一致")


@dataclass(frozen=True)
class WeaponryCallbackGuardSweepResult:
    scanned_count: int
    frozen_count: int

    def __post_init__(self) -> None:
        for name in ("scanned_count", "frozen_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数")
        if self.frozen_count > self.scanned_count:
            raise ValueError("frozen_count 不得超过 scanned_count")


@dataclass(frozen=True)
class WaitForWeaponryCallbackRelease:
    architecture_id: int
    timeout_seconds: float

    def __post_init__(self) -> None:
        _architecture_id(self.architecture_id)
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds,
            (int, float),
        ):
            raise TypeError("timeout_seconds 必须是数字")
        timeout = float(self.timeout_seconds)
        if (
            timeout != timeout
            or timeout in (float("inf"), float("-inf"))
            or timeout <= 0.0
        ):
            raise ValueError("timeout_seconds 必须是正有限数字")
        object.__setattr__(self, "timeout_seconds", timeout)


@dataclass(frozen=True)
class WeaponryCallbackWaitResult:
    outcome: WeaponryCallbackWaitOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, WeaponryCallbackWaitOutcome):
            raise TypeError("outcome 必须是 WeaponryCallbackWaitOutcome")


@dataclass(frozen=True)
class ReleaseUnknownWeaponryCallback:
    architecture_id: int
    released_by: str
    reason: str
    worker_stopped_confirmed: bool

    def __post_init__(self) -> None:
        _architecture_id(self.architecture_id)
        released_by = required_text(self.released_by, name="released_by")
        reason = required_text(self.reason, name="reason")
        if len(released_by) > 128:
            raise ValueError("released_by 最多 128 个字符")
        if len(reason) > 512:
            raise ValueError("reason 最多 512 个字符")
        if not isinstance(self.worker_stopped_confirmed, bool):
            raise TypeError("worker_stopped_confirmed 必须是 bool")
        if not self.worker_stopped_confirmed:
            raise ValueError("人工解除前必须确认旧 Worker 已停止或隔离")
        object.__setattr__(self, "released_by", released_by)
        object.__setattr__(self, "reason", reason)


@dataclass(frozen=True)
class WeaponryCallbackReleaseResult:
    outcome: WeaponryCallbackReleaseOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, WeaponryCallbackReleaseOutcome):
            raise TypeError("outcome 必须是 WeaponryCallbackReleaseOutcome")


@runtime_checkable
class WeaponryCallbackPort(Protocol):
    """latest 校验、Guard、网络投递和 outcome 提交的统一边界。"""

    def acquire(
        self,
        command: AcquireWeaponryCallback,
    ) -> WeaponryCallbackAcquireResult:
        ...

    def wait_until_released(
        self,
        command: WaitForWeaponryCallbackRelease,
    ) -> WeaponryCallbackWaitResult:
        ...

    def deliver(
        self,
        command: DeliverWeaponryCallback,
    ) -> WeaponryCallbackDeliveryResult:
        ...

    def complete(
        self,
        lease: WeaponryCallbackGuardLease,
        result: WeaponryCallbackDeliveryResult,
        payload: WeaponryCallbackPayload,
    ) -> bool:
        ...

    def freeze_expired(self, *, limit: int) -> WeaponryCallbackGuardSweepResult:
        ...

    def release_unknown(
        self,
        command: ReleaseUnknownWeaponryCallback,
    ) -> WeaponryCallbackReleaseResult:
        ...


@runtime_checkable
class WeaponryCallbackRecoverySourcePort(Protocol):
    """按规范 architectureId 加载 latest 可恢复终态回调。"""

    def load_recoverable(
        self,
        architecture_id: int,
    ) -> WeaponryCallbackRecoveryCandidate | None:
        ...


__all__ = [
    "AcquireWeaponryCallback",
    "DeliverWeaponryCallback",
    "ReleaseUnknownWeaponryCallback",
    "WaitForWeaponryCallbackRelease",
    "WeaponryCallbackAcquireOutcome",
    "WeaponryCallbackAcquireReason",
    "WeaponryCallbackAcquireResult",
    "WeaponryCallbackDeliveryOutcome",
    "WeaponryCallbackDeliveryResult",
    "WeaponryCallbackGuardLease",
    "WeaponryCallbackGuardSweepResult",
    "WeaponryCallbackPort",
    "WeaponryCallbackRecoveryCandidate",
    "WeaponryCallbackRecoverySourcePort",
    "WeaponryCallbackReleaseOutcome",
    "WeaponryCallbackReleaseResult",
    "WeaponryCallbackWaitOutcome",
    "WeaponryCallbackWaitResult",
]
