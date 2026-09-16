"""报告任务级 Artifact 与外部清理资源的持久恢复契约。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from app.modules.tasks.domain import TaskBusinessRef, TaskId

from .artifacts import (
    ReportArtifactCategory,
    ReportArtifactRef,
    ReportArtifactScope,
)
from .audit import ReportAuditReceipt
from .rag import ReportRagCleanupRef, ReportRagLifecycleEvent


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


class ReportResourceState(str, Enum):
    """一份任务资源记录的恢复阶段。"""

    TRACKING = "tracking"
    CLEANUP_PENDING = "cleanup_pending"
    AUDIT_PENDING = "audit_pending"
    CLEANED = "cleaned"
    QUARANTINED = "quarantined"


class ReportCleanupPartState(str, Enum):
    """外部 RAG 与本地 Artifact 两部分清理的独立状态。"""

    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    FAILED = "failed"
    SUCCEEDED = "succeeded"


class ReportResourceCleanupOutcome(str, Enum):
    """一次同步清理/恢复调用的内部结果。"""

    CLEANED = "cleaned"
    PENDING = "pending"
    QUARANTINED = "quarantined"
    NOT_FOUND = "not_found"
    NOT_READY = "not_ready"


@dataclass(frozen=True)
class ReportResourceCleanupResult:
    """清理结果不会改变已提交的业务终态。"""

    outcome: ReportResourceCleanupOutcome
    pending_external: bool = False
    pending_artifact_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ReportResourceCleanupOutcome):
            raise TypeError("outcome 必须是 ReportResourceCleanupOutcome")
        if not isinstance(self.pending_external, bool):
            raise TypeError("pending_external 必须是 bool")
        if (
            isinstance(self.pending_artifact_count, bool)
            or not isinstance(self.pending_artifact_count, int)
            or self.pending_artifact_count < 0
        ):
            raise ValueError("pending_artifact_count 必须是非负整数")


@dataclass(frozen=True)
class ReportResourceSweepResult:
    """一次有界恢复扫描的汇总；失败任务保留 ID 供日志、指标和后续重试。"""

    requested_limit: int
    scanned_count: int
    cleaned_count: int = 0
    pending_count: int = 0
    quarantined_count: int = 0
    not_ready_count: int = 0
    missing_count: int = 0
    failed_task_ids: tuple[TaskId, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "requested_limit",
            "scanned_count",
            "cleaned_count",
            "pending_count",
            "quarantined_count",
            "not_ready_count",
            "missing_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数")
        if self.requested_limit < 1:
            raise ValueError("requested_limit 必须大于 0")
        if self.scanned_count > self.requested_limit:
            raise ValueError("scanned_count 不得超过 requested_limit")
        failed = tuple(self.failed_task_ids)
        if any(not isinstance(item, TaskId) for item in failed):
            raise TypeError("failed_task_ids 只能包含 TaskId")
        if len(set(failed)) != len(failed):
            raise ValueError("failed_task_ids 不得重复")
        classified = (
            self.cleaned_count
            + self.pending_count
            + self.quarantined_count
            + self.not_ready_count
            + self.missing_count
            + len(failed)
        )
        if classified != self.scanned_count:
            raise ValueError("恢复扫描分类数量与 scanned_count 不一致")
        object.__setattr__(self, "failed_task_ids", failed)


@dataclass(frozen=True)
class ReportResourceRecord:
    """一份可做 CAS 的任务资源恢复快照。

    最终 Artifact 的“所有权”不由本记录单独决定：SQLite Store 在 ``prepare_cleanup``
    时必须读取不可变 execution 终态结果并核对 Artifact 元数据，再生成 ``retained``。
    因而旧 execution 即使已经写出 output/report.html，也不能自行把它列入保留集合。
    """

    task_id: TaskId
    business_ref: TaskBusinessRef
    scope: ReportArtifactScope
    state: ReportResourceState = ReportResourceState.TRACKING
    external_state: ReportCleanupPartState = ReportCleanupPartState.NOT_REQUIRED
    artifact_state: ReportCleanupPartState = ReportCleanupPartState.PENDING
    cleanup_ref: ReportRagCleanupRef | None = None
    audit_receipt: ReportAuditReceipt | None = None
    final_artifact: ReportArtifactRef | None = None
    retained: tuple[ReportArtifactRef, ...] = ()
    pending_events: tuple[ReportRagLifecycleEvent, ...] = ()
    pending_events_succeeded: bool | None = None
    pending_artifacts: tuple[ReportArtifactRef, ...] = ()
    next_sequence_no: int | None = None
    external_attempt_open: bool = False
    external_attempt_token: str = ""
    external_attempt_started_at: float | None = None
    external_attempt_heartbeat_at: float | None = None
    attempt_count: int = 0
    operation_attempts: tuple[tuple[str, int], ...] = ()
    last_error_stage: str = ""
    last_error_message: str = ""
    version: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        if self.business_ref.business_type != "report":
            raise ValueError("报告资源 business_type 必须是 report")
        if not isinstance(self.scope, ReportArtifactScope):
            raise TypeError("scope 必须是 ReportArtifactScope")
        if self.scope.task_id != self.task_id:
            raise ValueError("scope 不属于当前 task_id")
        if not isinstance(self.state, ReportResourceState):
            raise TypeError("state 必须是 ReportResourceState")
        for name in ("external_state", "artifact_state"):
            if not isinstance(getattr(self, name), ReportCleanupPartState):
                raise TypeError(f"{name} 必须是 ReportCleanupPartState")
        if self.cleanup_ref is not None and not isinstance(
            self.cleanup_ref,
            ReportRagCleanupRef,
        ):
            raise TypeError("cleanup_ref 必须是 ReportRagCleanupRef 或 None")
        if self.audit_receipt is not None:
            if not isinstance(self.audit_receipt, ReportAuditReceipt):
                raise TypeError("audit_receipt 必须是 ReportAuditReceipt 或 None")
            if self.audit_receipt.task_id != self.task_id:
                raise ValueError("audit_receipt 不属于当前 task_id")
        if self.final_artifact is not None:
            if not isinstance(self.final_artifact, ReportArtifactRef):
                raise TypeError("final_artifact 必须是 ReportArtifactRef 或 None")
            if (
                self.final_artifact.task_id != self.task_id
                or self.final_artifact.category is not ReportArtifactCategory.REPORT_HTML
            ):
                raise ValueError("final_artifact 必须是当前任务的 report_html")

        retained = tuple(self.retained)
        pending_artifacts = tuple(self.pending_artifacts)
        for name, items in (
            ("retained", retained),
            ("pending_artifacts", pending_artifacts),
        ):
            if any(
                not isinstance(item, ReportArtifactRef)
                or item.task_id != self.task_id
                for item in items
            ):
                raise ValueError(f"{name} 只能包含当前任务 Artifact")
            identities = tuple(item.artifact_id for item in items)
            if len(set(identities)) != len(identities):
                raise ValueError(f"{name} 不得包含重复 Artifact")
        if any(
            item.category is not ReportArtifactCategory.REPORT_HTML
            for item in retained
        ):
            raise ValueError("retained 只能包含最终报告 Artifact")
        if retained and (
            self.final_artifact is None or retained != (self.final_artifact,)
        ):
            raise ValueError("retained 必须与已登记 final_artifact 完全一致")
        object.__setattr__(self, "retained", retained)
        object.__setattr__(self, "pending_artifacts", pending_artifacts)

        events = tuple(self.pending_events)
        if any(not isinstance(item, ReportRagLifecycleEvent) for item in events):
            raise TypeError("pending_events 只能包含 ReportRagLifecycleEvent")
        sequences = tuple(item.sequence_no for item in events)
        if sequences != tuple(sorted(set(sequences))):
            raise ValueError("pending_events sequence_no 必须严格递增")
        if bool(events) != (self.pending_events_succeeded is not None):
            raise ValueError("pending_events_succeeded 必须与 pending_events 同时存在")
        if self.pending_events_succeeded is not None and not isinstance(
            self.pending_events_succeeded,
            bool,
        ):
            raise TypeError("pending_events_succeeded 必须是 bool 或 None")
        object.__setattr__(self, "pending_events", events)
        if self.next_sequence_no is not None and (
            isinstance(self.next_sequence_no, bool)
            or not isinstance(self.next_sequence_no, int)
            or self.next_sequence_no <= 0
        ):
            raise ValueError("next_sequence_no 必须是正整数或 None")
        if not isinstance(self.external_attempt_open, bool):
            raise TypeError("external_attempt_open 必须是 bool")
        if not isinstance(self.external_attempt_token, str):
            raise TypeError("external_attempt_token 必须是 str")
        attempt_token = self.external_attempt_token.strip()
        object.__setattr__(self, "external_attempt_token", attempt_token)
        started_at = self.external_attempt_started_at
        if started_at is not None:
            if isinstance(started_at, bool) or not isinstance(started_at, (int, float)):
                raise TypeError("external_attempt_started_at 必须是数字或 None")
            started_at = float(started_at)
            if (
                started_at != started_at
                or started_at in (float("inf"), float("-inf"))
                or started_at < 0.0
            ):
                raise ValueError("external_attempt_started_at 必须是非负有限数字")
            object.__setattr__(self, "external_attempt_started_at", started_at)
        heartbeat_at = self.external_attempt_heartbeat_at
        if heartbeat_at is not None:
            if isinstance(heartbeat_at, bool) or not isinstance(
                heartbeat_at,
                (int, float),
            ):
                raise TypeError("external_attempt_heartbeat_at 必须是数字或 None")
            heartbeat_at = float(heartbeat_at)
            if (
                heartbeat_at != heartbeat_at
                or heartbeat_at in (float("inf"), float("-inf"))
                or heartbeat_at < 0.0
            ):
                raise ValueError("external_attempt_heartbeat_at 必须是非负有限数字")
            object.__setattr__(
                self,
                "external_attempt_heartbeat_at",
                heartbeat_at,
            )
        if self.external_attempt_open != bool(
            started_at is not None and heartbeat_at is not None and attempt_token
        ):
            raise ValueError(
                "外部清理占用必须同时包含 token、开始时间和心跳时间"
            )
        if not self.external_attempt_open and (
            attempt_token or started_at is not None or heartbeat_at is not None
        ):
            raise ValueError("未占用外部清理时不得残留 token 或时间")
        if (
            self.external_attempt_open
            and started_at is not None
            and heartbeat_at is not None
            and heartbeat_at < started_at
        ):
            raise ValueError("外部清理心跳时间不得早于开始时间")
        if (
            isinstance(self.attempt_count, bool)
            or not isinstance(self.attempt_count, int)
            or self.attempt_count < 0
        ):
            raise ValueError("attempt_count 必须是非负整数")
        operation_attempts = tuple(self.operation_attempts)
        normalized_attempts: list[tuple[str, int]] = []
        for item in operation_attempts:
            if not isinstance(item, tuple) or len(item) != 2:
                raise TypeError("operation_attempts 元素必须是二元 tuple")
            operation, attempt_no = item
            if not isinstance(operation, str) or not operation.strip():
                raise ValueError("operation_attempts operation 不能为空")
            if (
                isinstance(attempt_no, bool)
                or not isinstance(attempt_no, int)
                or attempt_no < 1
            ):
                raise ValueError("operation_attempts attempt_no 必须是正整数")
            normalized_attempts.append((operation.strip(), attempt_no))
        if len({operation for operation, _ in normalized_attempts}) != len(
            normalized_attempts
        ):
            raise ValueError("operation_attempts operation 不得重复")
        object.__setattr__(
            self,
            "operation_attempts",
            tuple(sorted(normalized_attempts)),
        )
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version < 0
        ):
            raise ValueError("version 必须是非负整数")
        for name in ("last_error_stage", "last_error_message"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"{name} 必须是 str")

        if self.state is ReportResourceState.AUDIT_PENDING:
            if not events or self.audit_receipt is None:
                raise ValueError("audit_pending 必须包含待追加事件和审计凭据")
        if self.state is ReportResourceState.CLEANED:
            if (
                self.external_state
                not in {
                    ReportCleanupPartState.NOT_REQUIRED,
                    ReportCleanupPartState.SUCCEEDED,
                }
                or self.artifact_state is not ReportCleanupPartState.SUCCEEDED
                or events
                or pending_artifacts
                or self.external_attempt_open
                or self.external_attempt_token
                or self.external_attempt_started_at is not None
                or self.external_attempt_heartbeat_at is not None
            ):
                raise ValueError("cleaned 记录仍包含未收敛资源")
        if self.state is ReportResourceState.QUARANTINED and (
            not self.last_error_stage.strip() or not self.last_error_message.strip()
        ):
            raise ValueError("quarantined 必须包含错误阶段和原因")


@runtime_checkable
class ReportResourceStorePort(Protocol):
    """持久化任务资源事实；实现不得执行文件或网络 I/O。"""

    def create(self, record: ReportResourceRecord) -> ReportResourceRecord:
        ...

    def get(self, task_id: TaskId) -> ReportResourceRecord | None:
        ...

    def save(
        self,
        record: ReportResourceRecord,
        *,
        expected_version: int,
    ) -> ReportResourceRecord:
        ...

    def prepare_cleanup(self, task_id: TaskId) -> ReportResourceRecord:
        """按 execution 终态权威推导 retained，并进入 cleanup_pending。"""
        ...

    def list_recoverable(self, *, limit: int) -> tuple[TaskId, ...]:
        ...

    def defer_recovery(
        self,
        task_id: TaskId,
        *,
        retry_at: str,
        reason: str,
    ) -> bool:
        """为仍可恢复的记录设置下次扫描时间；终态或缺失记录返回 ``False``。"""
        ...


@runtime_checkable
class ReportResourceRecoveryPort(Protocol):
    """供报告执行链登记、收口和恢复资源的高层内部边界。"""

    def register(
        self,
        task_id: TaskId,
        business_ref: TaskBusinessRef,
        scope: ReportArtifactScope,
    ) -> None:
        ...

    def track_rag_cleanup(
        self,
        task_id: TaskId,
        cleanup_ref: ReportRagCleanupRef,
    ) -> None:
        ...

    def track_audit(self, receipt: ReportAuditReceipt) -> None:
        ...

    def track_final_artifact(self, artifact: ReportArtifactRef) -> None:
        ...

    def cleanup(self, task_id: TaskId) -> ReportResourceCleanupResult:
        ...

    def recover(self, task_id: TaskId) -> ReportResourceCleanupResult:
        ...

    def sweep(self, *, limit: int) -> ReportResourceSweepResult:
        ...

    def quarantine(self, task_id: TaskId, *, stage: str, reason: str) -> None:
        ...


__all__ = [
    "ReportCleanupPartState",
    "ReportResourceCleanupOutcome",
    "ReportResourceCleanupResult",
    "ReportResourceRecord",
    "ReportResourceRecoveryPort",
    "ReportResourceState",
    "ReportResourceStorePort",
    "ReportResourceSweepResult",
]
