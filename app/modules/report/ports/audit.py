"""报告 RAG 完整交互轨迹的原子审计端口。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from app.modules.tasks.domain import TaskBusinessRef, TaskId

from .rag import ReportRagLifecycleEvent, ReportRagTrace


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


class ReportRagAuditOutcome(str, Enum):
    """本次被审计 RAG 业务调用的结果。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class PersistReportRagTrace:
    """原子写入主交互、attempts 和初始生命周期事件的命令。"""

    task_id: TaskId
    business_ref: TaskBusinessRef
    idempotency_key: str
    prompt: str
    trace: ReportRagTrace
    outcome: ReportRagAuditOutcome
    error_code: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        if self.business_ref.business_type != "report":
            raise ValueError("报告审计 business_type 必须是 report")
        object.__setattr__(
            self,
            "idempotency_key",
            _required_text(self.idempotency_key, name="idempotency_key"),
        )
        object.__setattr__(
            self,
            "prompt",
            _required_text(self.prompt, name="prompt"),
        )
        if not isinstance(self.trace, ReportRagTrace):
            raise TypeError("trace 必须是 ReportRagTrace")
        if not isinstance(self.outcome, ReportRagAuditOutcome):
            raise TypeError("outcome 必须是 ReportRagAuditOutcome")
        if not isinstance(self.error_code, str):
            raise TypeError("error_code 必须是 str")
        if self.outcome is ReportRagAuditOutcome.SUCCEEDED and self.error_code:
            raise ValueError("成功审计命令不得携带 error_code")
        if self.outcome is ReportRagAuditOutcome.SUCCEEDED and not self.trace.succeeded:
            raise ValueError("成功审计命令不得携带失败 trace")
        if (
            self.outcome is ReportRagAuditOutcome.FAILED
            and not self.error_code.strip()
        ):
            raise ValueError("失败审计命令必须携带 error_code")
        if self.outcome is ReportRagAuditOutcome.FAILED and self.trace.succeeded:
            raise ValueError("失败审计命令必须携带失败 trace")


@dataclass(frozen=True)
class ReportAuditReceipt:
    """完整原子审计已经提交的凭据。"""

    task_id: TaskId
    idempotency_key: str
    audit_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        for name in ("idempotency_key", "audit_id"):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name=name),
            )


@dataclass(frozen=True)
class AppendReportLifecycleEvents:
    """审计成功后幂等追加 close/cleanup 事件的命令。"""

    receipt: ReportAuditReceipt
    events: tuple[ReportRagLifecycleEvent, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, ReportAuditReceipt):
            raise TypeError("receipt 必须是 ReportAuditReceipt")
        events = tuple(self.events)
        if not events or any(
            not isinstance(item, ReportRagLifecycleEvent) for item in events
        ):
            raise ValueError("events 必须包含 ReportRagLifecycleEvent")
        sequences = tuple(item.sequence_no for item in events)
        if sequences != tuple(sorted(set(sequences))):
            raise ValueError("events sequence_no 必须严格递增且不重复")
        object.__setattr__(self, "events", events)


@runtime_checkable
class ReportInteractionAuditPort(Protocol):
    """持久化完整交互；未返回合法 Receipt 不得进入成功终态。"""

    def persist_trace(self, command: PersistReportRagTrace) -> ReportAuditReceipt:
        ...

    def append_lifecycle_events(
        self,
        command: AppendReportLifecycleEvents,
    ) -> None:
        ...


__all__ = [
    "AppendReportLifecycleEvents",
    "PersistReportRagTrace",
    "ReportAuditReceipt",
    "ReportInteractionAuditPort",
    "ReportRagAuditOutcome",
]
