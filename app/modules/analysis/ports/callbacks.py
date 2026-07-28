"""文件 callback Guard、HTTP 投递与同步恢复共用的强类型 Port。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from app.modules.analysis.domain.task_inputs import FrozenJsonObject

from .common import AnalysisExecutionRef


class AnalysisCallbackAcquireOutcome(str, Enum):
    ACQUIRED = "acquired"
    WAIT_FOR_OWNER = "wait_for_owner"
    SKIPPED = "skipped"
    STALE = "stale"
    FROZEN = "frozen"


class AnalysisCallbackWaitOutcome(str, Enum):
    RELEASED = "released"
    TIMED_OUT = "timed_out"
    FROZEN = "frozen"


class AnalysisCallbackDeliveryOutcome(str, Enum):
    DELIVERED = "delivered"
    DEFINITELY_NOT_SENT = "definitely_not_sent"
    REJECTED = "rejected"
    OUTCOME_UNKNOWN = "outcome_unknown"
    SKIPPED = "skipped"
    STALE = "stale"


@dataclass(frozen=True)
class AnalysisCallbackRequest:
    """获取发送权的命令。

    ``allow_failed_retry`` 与 ``allow_outcome_unknown_retry`` 只允许由
    ``/llm/check-task`` 对应的同步恢复用例显式传入。正常 Worker 不能因为一次明确失败
    或未知投递结果而自动重发；其中未知结果的显式补发采用 at-least-once 语义，接收方
    必须按业务键幂等处理可能重复到达的相同结果。
    """

    execution: AnalysisExecutionRef
    callback_url: str
    payload: FrozenJsonObject
    allow_failed_retry: bool = False
    allow_outcome_unknown_retry: bool = False
    expected_callback_attempts: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        if not isinstance(self.callback_url, str):
            raise TypeError("callback_url 必须是 str")
        if not isinstance(self.payload, FrozenJsonObject):
            raise TypeError("payload 必须是 FrozenJsonObject")
        if not isinstance(self.allow_failed_retry, bool):
            raise TypeError("allow_failed_retry 必须是 bool")
        if not isinstance(self.allow_outcome_unknown_retry, bool):
            raise TypeError("allow_outcome_unknown_retry 必须是 bool")
        if self.expected_callback_attempts is not None and (
            isinstance(self.expected_callback_attempts, bool)
            or not isinstance(self.expected_callback_attempts, int)
            or self.expected_callback_attempts < 0
        ):
            raise ValueError("expected_callback_attempts 必须是非负整数或 None")
        if self.allow_failed_retry != (self.expected_callback_attempts is not None):
            raise ValueError(
                "同步失败恢复必须同时携带 allow_failed_retry 与 expected_callback_attempts"
            )
        if self.allow_outcome_unknown_retry and not self.allow_failed_retry:
            raise ValueError(
                "未知结果显式补发必须同时启用 allow_failed_retry"
            )


@dataclass(frozen=True)
class AnalysisCallbackGuardLease:
    """一次有限发送权；token/version 只用于内部条件完成。"""

    execution: AnalysisExecutionRef
    lease_token: str
    lease_version: int
    expires_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        for name in ("lease_token", "expires_at"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} 必须是非空 str")
        if (
            isinstance(self.lease_version, bool)
            or not isinstance(self.lease_version, int)
            or self.lease_version < 1
        ):
            raise ValueError("lease_version 必须是正整数")


@dataclass(frozen=True)
class AnalysisCallbackAcquireResult:
    execution: AnalysisExecutionRef
    outcome: AnalysisCallbackAcquireOutcome
    lease: AnalysisCallbackGuardLease | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        if not isinstance(self.outcome, AnalysisCallbackAcquireOutcome):
            raise TypeError("outcome 必须是 AnalysisCallbackAcquireOutcome")
        if self.lease is not None and not isinstance(
            self.lease,
            AnalysisCallbackGuardLease,
        ):
            raise TypeError("lease 必须是 AnalysisCallbackGuardLease 或 None")
        if self.outcome is AnalysisCallbackAcquireOutcome.ACQUIRED and self.lease is None:
            raise ValueError("acquired 结果必须携带 lease")
        if self.outcome is not AnalysisCallbackAcquireOutcome.ACQUIRED and self.lease is not None:
            raise ValueError("未获得发送权时不得携带 lease")
        if self.lease is not None and self.lease.execution != self.execution:
            raise ValueError("lease 必须属于 acquire 结果 execution")


@dataclass(frozen=True)
class WaitForAnalysisCallbackRelease:
    execution: AnalysisExecutionRef
    timeout_seconds: float
    poll_seconds: float

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        for name in ("timeout_seconds", "poll_seconds"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value <= 0
                or value != value
                or value in (float("inf"), float("-inf"))
            ):
                raise ValueError(f"{name} 必须是正数")


@dataclass(frozen=True)
class AnalysisCallbackWaitResult:
    execution: AnalysisExecutionRef
    outcome: AnalysisCallbackWaitOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        if not isinstance(self.outcome, AnalysisCallbackWaitOutcome):
            raise TypeError("outcome 必须是 AnalysisCallbackWaitOutcome")


@dataclass(frozen=True)
class AnalysisCallbackDeliveryRequest:
    """持有 Guard Lease 后才允许进入网络发送或显式收敛为空地址。

    空 ``callback_url`` 不是参数缺失：它表示当前运行配置没有回调地址。该事实仍必须在
    已获得的 Guard 上完成为 ``skipped``，否则同一 ``fileName`` 会永久停留在可发送状态。
    """

    lease: AnalysisCallbackGuardLease
    callback_url: str
    payload: FrozenJsonObject

    def __post_init__(self) -> None:
        if not isinstance(self.lease, AnalysisCallbackGuardLease):
            raise TypeError("lease 必须是 AnalysisCallbackGuardLease")
        if not isinstance(self.callback_url, str):
            raise TypeError("callback_url 必须是 str")
        if not isinstance(self.payload, FrozenJsonObject):
            raise TypeError("payload 必须是 FrozenJsonObject")


@dataclass(frozen=True)
class AnalysisCallbackDelivery:
    execution: AnalysisExecutionRef
    lease_token: str
    lease_version: int
    outcome: AnalysisCallbackDeliveryOutcome
    detail_code: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        if not isinstance(self.lease_token, str) or not self.lease_token.strip():
            raise ValueError("lease_token 必须是非空 str")
        object.__setattr__(self, "lease_token", self.lease_token.strip())
        if (
            isinstance(self.lease_version, bool)
            or not isinstance(self.lease_version, int)
            or self.lease_version < 1
        ):
            raise ValueError("lease_version 必须是正整数")
        if not isinstance(self.outcome, AnalysisCallbackDeliveryOutcome):
            raise TypeError("outcome 必须是 AnalysisCallbackDeliveryOutcome")
        if not isinstance(self.detail_code, str):
            raise TypeError("detail_code 必须是 str")
        object.__setattr__(self, "detail_code", self.detail_code.strip())
        if self.outcome is AnalysisCallbackDeliveryOutcome.DELIVERED and self.detail_code:
            raise ValueError("投递成功不得携带 detail_code")
        if self.outcome is not AnalysisCallbackDeliveryOutcome.DELIVERED and not self.detail_code:
            raise ValueError("非成功投递结果必须携带 detail_code")


@dataclass(frozen=True)
class AnalysisCallbackRecoveryCandidate:
    """从最新 file 投影恢复出的、允许同步补发的内部候选。"""

    execution: AnalysisExecutionRef
    payload: FrozenJsonObject
    callback_attempts: int

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        if not isinstance(self.payload, FrozenJsonObject):
            raise TypeError("payload 必须是 FrozenJsonObject")
        if (
            isinstance(self.callback_attempts, bool)
            or not isinstance(self.callback_attempts, int)
            or self.callback_attempts < 0
        ):
            raise ValueError("callback_attempts 必须是非负整数")


@dataclass(frozen=True)
class AnalysisCallbackGuardSweepResult:
    scanned_count: int
    frozen_count: int

    def __post_init__(self) -> None:
        for name in ("scanned_count", "frozen_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数")
        if self.frozen_count > self.scanned_count:
            raise ValueError("frozen_count 不能超过 scanned_count")


@runtime_checkable
class AnalysisCallbackPort(Protocol):
    """latest 校验、发送权、网络投递和 Guard 完成的统一边界。"""

    def acquire(
        self,
        request: AnalysisCallbackRequest,
    ) -> AnalysisCallbackAcquireResult:
        ...

    def wait_until_released(
        self,
        request: WaitForAnalysisCallbackRelease,
    ) -> AnalysisCallbackWaitResult:
        ...

    def deliver(
        self,
        request: AnalysisCallbackDeliveryRequest,
    ) -> AnalysisCallbackDelivery:
        ...

    def complete(
        self,
        lease: AnalysisCallbackGuardLease,
        delivery: AnalysisCallbackDelivery,
        payload: FrozenJsonObject,
    ) -> bool:
        ...

    def freeze_expired(self, *, limit: int) -> AnalysisCallbackGuardSweepResult:
        ...


@runtime_checkable
class AnalysisCallbackRecoverySourcePort(Protocol):
    """按 fileName 读取 latest 投影中的可恢复回调，不重新执行分析任务。"""

    def load_recoverable(
        self,
        file_name: str,
    ) -> AnalysisCallbackRecoveryCandidate | None:
        ...


__all__ = (
    "AnalysisCallbackAcquireOutcome",
    "AnalysisCallbackAcquireResult",
    "AnalysisCallbackDelivery",
    "AnalysisCallbackDeliveryOutcome",
    "AnalysisCallbackDeliveryRequest",
    "AnalysisCallbackGuardLease",
    "AnalysisCallbackGuardSweepResult",
    "AnalysisCallbackPort",
    "AnalysisCallbackRecoveryCandidate",
    "AnalysisCallbackRecoverySourcePort",
    "AnalysisCallbackRequest",
    "AnalysisCallbackWaitOutcome",
    "AnalysisCallbackWaitResult",
    "WaitForAnalysisCallbackRelease",
)
