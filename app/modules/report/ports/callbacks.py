"""报告回调发送权、精确投递结果和 Guard 完成端口。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from app.modules.tasks.domain import TaskId

from app.modules.report.domain import ReportCallbackPayload, ReportId


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


class ReportCallbackAcquireOutcome(str, Enum):
    """获取业务键回调发送权的内部结果。"""

    ACQUIRED = "acquired"
    STALE = "stale"
    BUSY = "busy"
    OUTCOME_UNKNOWN = "outcome_unknown"
    ALREADY_COMPLETED = "already_completed"


class ReportCallbackAcquireReason(str, Enum):
    """区分首次投递与甲方通过 check-task 发起的显式同步恢复。

    首次投递不得自动重试已经形成明确失败结果的回调；显式恢复是甲方规定的补偿动作，
    允许在 ``failed`` 后再次取得发送权。两条路径仍共享同一 Guard、latest-wins 和
    fencing 规则，触发来源不能成为绕过并发门禁的理由。
    """

    INITIAL_DELIVERY = "initial_delivery"
    EXPLICIT_CHECK_TASK_RECOVERY = "explicit_check_task_recovery"


class ReportCallbackDeliveryOutcome(str, Enum):
    """HTTP Adapter 必须精确区分的投递结果。"""

    SUCCESS = "success"
    REJECTED = "rejected"
    DEFINITELY_NOT_SENT = "definitely_not_sent"
    DELIVERY_OUTCOME_UNKNOWN = "delivery_outcome_unknown"
    SKIPPED = "skipped"
    STALE = "stale"


class ReportCallbackWaitOutcome(str, Enum):
    """提交用例在数据库事务之外等待旧发送权释放的结果。"""

    RELEASED = "released"
    TIMED_OUT = "timed_out"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ReportCallbackReleaseOutcome(str, Enum):
    """内部人工解除 outcome-unknown 冻结的审计结果。"""

    RELEASED = "released"
    ALREADY_RELEASED = "already_released"
    NOT_FROZEN = "not_frozen"


@dataclass(frozen=True)
class ReportCallbackGuardLease:
    """一次回调发送权的不透明租约及单调 fencing token。"""

    task_id: TaskId
    report_id: ReportId
    token: str
    fencing_token: int
    deadline_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.report_id, ReportId):
            raise TypeError("report_id 必须是 ReportId")
        object.__setattr__(self, "token", _required_text(self.token, name="token"))
        if (
            isinstance(self.fencing_token, bool)
            or not isinstance(self.fencing_token, int)
            or self.fencing_token <= 0
        ):
            raise ValueError("fencing_token 必须是正整数")
        object.__setattr__(
            self,
            "deadline_at",
            _required_text(self.deadline_at, name="deadline_at"),
        )


@dataclass(frozen=True)
class ReportCallbackAcquireResult:
    """只有 acquired 结果可以携带发送租约。"""

    outcome: ReportCallbackAcquireOutcome
    lease: ReportCallbackGuardLease | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ReportCallbackAcquireOutcome):
            raise TypeError("outcome 必须是 ReportCallbackAcquireOutcome")
        if self.outcome is ReportCallbackAcquireOutcome.ACQUIRED:
            if not isinstance(self.lease, ReportCallbackGuardLease):
                raise TypeError("acquired 结果必须包含 Guard Lease")
        elif self.lease is not None:
            raise ValueError("未取得发送权时不得携带 Guard Lease")


@dataclass(frozen=True)
class ReportCallbackAcquire:
    """按 expected TaskId 获取发送权的命令。"""

    task_id: TaskId
    report_id: ReportId
    reason: ReportCallbackAcquireReason = ReportCallbackAcquireReason.INITIAL_DELIVERY

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.report_id, ReportId):
            raise TypeError("report_id 必须是 ReportId")
        if not isinstance(self.reason, ReportCallbackAcquireReason):
            raise TypeError("reason 必须是 ReportCallbackAcquireReason")


@dataclass(frozen=True)
class ReportCallbackRecoveryCandidate:
    """从 latest 报告投影恢复出的、尚需显式补发的不可变候选。"""

    task_id: TaskId
    report_id: ReportId
    payload: ReportCallbackPayload

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.report_id, ReportId):
            raise TypeError("report_id 必须是 ReportId")
        if not isinstance(self.payload, ReportCallbackPayload):
            raise TypeError("payload 必须是 ReportCallbackPayload")
        if self.payload.report_id != self.report_id:
            raise ValueError("回调候选 payload 与 report_id 不一致")


@dataclass(frozen=True)
class ReportCallbackGuardSweepResult:
    """一次有界过期 Guard 扫描结果。"""

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
class WaitForReportCallbackRelease:
    """按业务键等待旧 callback 发送权释放的无框架命令。"""

    report_id: ReportId
    timeout_seconds: float

    def __post_init__(self) -> None:
        if not isinstance(self.report_id, ReportId):
            raise TypeError("report_id 必须是 ReportId")
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
class ReportCallbackWaitResult:
    """等待结果；unknown 与超时都继续占用业务键。"""

    outcome: ReportCallbackWaitOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ReportCallbackWaitOutcome):
            raise TypeError("outcome 必须是 ReportCallbackWaitOutcome")


@dataclass(frozen=True)
class ReleaseUnknownReportCallback:
    """由受控内部入口发起的人工解除命令。

    阶段 1C 不把该命令暴露为 HTTP 参数或公开路由。``released_by`` 与 ``reason`` 是
    必填审计字段；``worker_stopped_confirmed`` 必须由运维人员在确认旧 Worker/旧进程
    已停止或被隔离后显式置为 ``True``。阶段 5/11 的运维命令只负责鉴权和采集这些
    内部审计值，不能绕过本命令。
    """

    report_id: ReportId
    released_by: str
    reason: str
    worker_stopped_confirmed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.report_id, ReportId):
            raise TypeError("report_id 必须是 ReportId")
        released_by = _required_text(self.released_by, name="released_by")
        reason = _required_text(self.reason, name="reason")
        if len(released_by) > 128:
            raise ValueError("released_by 最多 128 个字符")
        if len(reason) > 512:
            raise ValueError("reason 最多 512 个字符")
        if not isinstance(self.worker_stopped_confirmed, bool):
            raise TypeError("worker_stopped_confirmed 必须是 bool")
        if not self.worker_stopped_confirmed:
            raise ValueError("人工解除前必须确认旧 Worker 已停止或被隔离")
        object.__setattr__(self, "released_by", released_by)
        object.__setattr__(self, "reason", reason)


@dataclass(frozen=True)
class ReportCallbackReleaseResult:
    """人工解除的幂等结果；只有 ``released`` 表示本次发生状态转换。"""

    outcome: ReportCallbackReleaseOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ReportCallbackReleaseOutcome):
            raise TypeError("outcome 必须是 ReportCallbackReleaseOutcome")


@dataclass(frozen=True)
class DeliverReportCallback:
    """使用既有公开载荷投递一次回调的命令。"""

    lease: ReportCallbackGuardLease
    payload: ReportCallbackPayload

    def __post_init__(self) -> None:
        if not isinstance(self.lease, ReportCallbackGuardLease):
            raise TypeError("lease 必须是 ReportCallbackGuardLease")
        if not isinstance(self.payload, ReportCallbackPayload):
            raise TypeError("payload 必须是 ReportCallbackPayload")
        if self.payload.report_id != self.lease.report_id:
            raise ValueError("Callback payload 与 Guard Lease report_id 不一致")


@dataclass(frozen=True)
class ReportCallbackDeliveryResult:
    """投递结果及可审计、无敏感正文的内部说明。"""

    outcome: ReportCallbackDeliveryOutcome
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ReportCallbackDeliveryOutcome):
            raise TypeError("outcome 必须是 ReportCallbackDeliveryOutcome")
        if not isinstance(self.detail, str):
            raise TypeError("detail 必须是 str")


@runtime_checkable
class ReportCallbackPort(Protocol):
    """权威 latest 校验、发送权、网络投递和 outcome 持久化边界。

    Application 可以在调用前执行一次无锁 latest 预检查以减少无效数据库事务，但
    ``acquire`` 必须在同一事务中重新校验 expected TaskId 仍是最新 owner，并同时取得
    Guard。只有该事务结果能够授权 HTTP 投递。
    """

    def acquire(
        self,
        command: ReportCallbackAcquire,
    ) -> ReportCallbackAcquireResult:
        ...

    def wait_until_released(
        self,
        command: WaitForReportCallbackRelease,
    ) -> ReportCallbackWaitResult:
        """在数据库事务之外有界等待，不得持有 Repository/UoW 锁。"""
        ...

    def deliver(
        self,
        command: DeliverReportCallback,
    ) -> ReportCallbackDeliveryResult:
        ...

    def complete(
        self,
        lease: ReportCallbackGuardLease,
        result: ReportCallbackDeliveryResult,
        payload: ReportCallbackPayload,
    ) -> bool:
        """先按 Guard 条件完成权威事实，再尽力保存非权威历史副本。"""
        ...

    def freeze_expired(self, *, limit: int) -> ReportCallbackGuardSweepResult:
        """有界冻结过期 ``sending``；绝不重抢或重新发送结果未知的请求。"""
        ...

    def release_unknown(
        self,
        command: ReleaseUnknownReportCallback,
    ) -> ReportCallbackReleaseResult:
        """人工解除 unknown 冻结并保存操作者、原因和时间；不得隐式重发旧回调。"""
        ...


@runtime_checkable
class ReportCallbackRecoverySourcePort(Protocol):
    """读取 latest 报告投影并恢复为强类型同步补发候选。"""

    def load_recoverable(
        self,
        report_id: ReportId,
    ) -> ReportCallbackRecoveryCandidate | None:
        ...


__all__ = [
    "DeliverReportCallback",
    "ReportCallbackAcquire",
    "ReportCallbackAcquireOutcome",
    "ReportCallbackAcquireReason",
    "ReportCallbackAcquireResult",
    "ReportCallbackDeliveryOutcome",
    "ReportCallbackDeliveryResult",
    "ReportCallbackGuardLease",
    "ReportCallbackGuardSweepResult",
    "ReportCallbackPort",
    "ReportCallbackRecoveryCandidate",
    "ReportCallbackRecoverySourcePort",
    "ReportCallbackReleaseOutcome",
    "ReportCallbackReleaseResult",
    "ReportCallbackWaitOutcome",
    "ReportCallbackWaitResult",
    "ReleaseUnknownReportCallback",
    "WaitForReportCallbackRelease",
]
