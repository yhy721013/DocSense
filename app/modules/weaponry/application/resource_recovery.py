"""武器谱终态外部资源的有界、持久、保守恢复用例。"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
import logging

from app.modules.tasks.domain import TaskExecutionSnapshot, TaskId, TaskSnapshot
from app.modules.tasks.ports import TaskCommandPort, TaskReadPort
from app.modules.weaponry.domain import WeaponryInputSnapshot
from app.modules.weaponry.ports import (
    AcquireWeaponryCleanupLease,
    CleanupWeaponryExternalResource,
    CompleteWeaponryResourceCleanup,
    PrepareWeaponryResourceCleanup,
    QuarantineWeaponryResources,
    ReleaseWeaponryCleanupLease,
    WeaponryCleanupLeaseAcquireOutcome,
    WeaponryCleanupLeaseAcquireResult,
    WeaponryBoundedMaintenancePort,
    WeaponryExternalResourceCleanupPort,
    WeaponryExternalResourceCleanupResult,
    WeaponryInteractionAuditPort,
    WeaponryPortStateError,
    WeaponryResourceCleanupOutcome,
    WeaponryResourceRecord,
    WeaponryResourceRecordState,
    WeaponryResourceStorePort,
    WeaponryTrackedResourceState,
)


logger = logging.getLogger(__name__)

_TERMINAL_EXECUTION_STATES = frozenset({"succeeded", "failed", "stale"})
_ACTIVE_EXECUTION_STATES = frozenset({"accepted", "running"})


class WeaponryResourceRecoveryOutcome(str, Enum):
    CLEANED = "cleaned"
    PENDING = "pending"
    QUARANTINED = "quarantined"
    BUSY = "busy"
    NOT_READY = "not_ready"
    MISSING = "missing"
    FAILED = "failed"


@dataclass(frozen=True)
class WeaponryResourceRecoveryResult:
    task_id: TaskId
    outcome: WeaponryResourceRecoveryOutcome
    cleaned_resource_count: int = 0
    error_code: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.outcome, WeaponryResourceRecoveryOutcome):
            raise TypeError("outcome 必须是 WeaponryResourceRecoveryOutcome")
        if (
            isinstance(self.cleaned_resource_count, bool)
            or not isinstance(self.cleaned_resource_count, int)
            or self.cleaned_resource_count < 0
        ):
            raise ValueError("cleaned_resource_count 必须是非负整数")
        if not isinstance(self.error_code, str):
            raise TypeError("error_code 必须是 str")


@dataclass(frozen=True)
class WeaponryResourceRecoverySweepResult:
    requested_limit: int
    scanned_count: int
    cleaned_count: int
    cleaned_resource_count: int
    pending_count: int
    quarantined_count: int
    not_ready_count: int
    missing_count: int
    failed_count: int

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数")
        if self.requested_limit < 1:
            raise ValueError("requested_limit 必须是正整数")
        if self.scanned_count > self.requested_limit:
            raise ValueError("scanned_count 不得超过 requested_limit")


class WeaponryResourceRecoveryService:
    """只清理已登记 owned 资源；不重放模型/检索调用，也不猜测 running 已死亡。

    单次 ``recover`` 最多执行一个外部删除。这个限制保证清理租约只覆盖一个最长
    60 秒 HTTP 调用，并让每轮维护工作量可预测。剩余资源由持久记录在后续扫描中继续处理，
    业务积压没有数量上限，也不进入内存队列。
    """

    def __init__(
        self,
        *,
        store: WeaponryResourceStorePort,
        cleaner: WeaponryExternalResourceCleanupPort,
        audit: WeaponryInteractionAuditPort,
        task_commands: TaskCommandPort | None = None,
        task_reader: TaskReadPort | None = None,
        creation_intent_recovery: WeaponryBoundedMaintenancePort | None = None,
    ) -> None:
        if not isinstance(store, WeaponryResourceStorePort):
            raise TypeError("store 必须实现 WeaponryResourceStorePort")
        if not isinstance(cleaner, WeaponryExternalResourceCleanupPort):
            raise TypeError("cleaner 必须实现 WeaponryExternalResourceCleanupPort")
        if not isinstance(audit, WeaponryInteractionAuditPort):
            raise TypeError("audit 必须实现 WeaponryInteractionAuditPort")
        if (task_commands is None) == (task_reader is None):
            raise ValueError("task_commands 与 task_reader 必须且只能提供一个")
        if task_commands is not None and not isinstance(task_commands, TaskCommandPort):
            raise TypeError("task_commands 必须实现 TaskCommandPort")
        if task_reader is not None and not isinstance(task_reader, TaskReadPort):
            raise TypeError("task_reader 必须实现 TaskReadPort")
        if creation_intent_recovery is not None and not isinstance(
            creation_intent_recovery,
            WeaponryBoundedMaintenancePort,
        ):
            raise TypeError(
                "creation_intent_recovery 必须实现 WeaponryBoundedMaintenancePort"
            )
        self._store = store
        self._cleaner = cleaner
        self._audit = audit
        self._task_commands = task_commands
        self._task_reader = task_reader
        self._creation_intent_recovery = creation_intent_recovery

    @property
    def store(self) -> WeaponryResourceStorePort:
        return self._store

    def run_once(
        self,
        *,
        limit: int,
        stop_requested: Callable[[], bool] | None = None,
    ) -> WeaponryResourceRecoverySweepResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        if stop_requested is not None and not callable(stop_requested):
            raise TypeError("stop_requested 必须可调用或为 None")
        should_stop = stop_requested or (lambda: False)
        if self._creation_intent_recovery is not None and not should_stop():
            # 先把 create 崩溃窗口收敛成可审计隔离事实，随后资源扫描才能看到完整现场。
            # 两个扫描分别有界，limit 是单类工作页大小而不是业务积压上限。
            try:
                self._creation_intent_recovery.run_once(limit=limit)
            except Exception:
                # 创建意图查询依赖 AnythingLLM；供应商短暂不可用不能阻塞已经登记资源
                # 的本地恢复与 Callback Guard 维护。
                logger.exception("武器谱创建意图恢复批次失败，继续执行资源恢复")
        task_ids = (
            () if should_stop() else self._store.list_recoverable(limit=limit)
        )
        if not isinstance(task_ids, tuple) or any(
            not isinstance(task_id, TaskId) for task_id in task_ids
        ):
            raise TypeError("Resource Store list_recoverable 必须返回 TaskId tuple")

        # ``limit`` 约束本轮资源恢复操作数，而不是只约束唯一任务数。同一大任务可能登记
        # 数百个 extraction workspace/thread；按任务每 30 秒只处理一项会让清理速度远低于
        # 创建速度。这里用轮转队列保证多任务各推进一项后，才继续处理仍有安全候选的任务。
        # 每次 ``recover`` 仍只执行一个外部副作用并单独提交 lease/fencing 检查点；停止信号
        # 会在两项之间生效，不会让一个大批次阻塞 Dispatcher 的关闭流程。
        pending_tasks = deque(task_ids)
        results: list[WeaponryResourceRecoveryResult] = []
        while pending_tasks and len(results) < limit and not should_stop():
            task_id = pending_tasks.popleft()
            try:
                result = self.recover(task_id)
            except Exception:
                # 一条损坏记录不能阻塞同批其他任务。异常分类保留在日志，下一轮仍从
                # 持久 Store 重新读取，不在内存中累计失败任务。
                logger.exception(
                    "武器谱资源恢复发生未收敛异常: task_id=%s",
                    task_id.value,
                )
                result = WeaponryResourceRecoveryResult(
                    task_id,
                    WeaponryResourceRecoveryOutcome.FAILED,
                    error_code="weaponry_resource_recovery_exception",
                )
            results.append(result)
            if (
                result.outcome is WeaponryResourceRecoveryOutcome.PENDING
                and result.cleaned_resource_count > 0
            ):
                pending_tasks.append(task_id)

        def count(*outcomes: WeaponryResourceRecoveryOutcome) -> int:
            return sum(result.outcome in outcomes for result in results)

        sweep = WeaponryResourceRecoverySweepResult(
            requested_limit=limit,
            scanned_count=len(results),
            cleaned_count=count(WeaponryResourceRecoveryOutcome.CLEANED),
            cleaned_resource_count=sum(
                result.cleaned_resource_count for result in results
            ),
            pending_count=count(
                WeaponryResourceRecoveryOutcome.PENDING,
                WeaponryResourceRecoveryOutcome.BUSY,
            ),
            quarantined_count=count(
                WeaponryResourceRecoveryOutcome.QUARANTINED
            ),
            not_ready_count=count(WeaponryResourceRecoveryOutcome.NOT_READY),
            missing_count=count(WeaponryResourceRecoveryOutcome.MISSING),
            failed_count=count(WeaponryResourceRecoveryOutcome.FAILED),
        )
        logger.log(
            logging.ERROR if sweep.failed_count else logging.DEBUG,
            "武器谱资源有界恢复扫描完成: requested_limit=%d scanned=%d cleaned=%d "
            "cleaned_resources=%d pending=%d quarantined=%d not_ready=%d "
            "missing=%d failed=%d",
            sweep.requested_limit,
            sweep.scanned_count,
            sweep.cleaned_count,
            sweep.cleaned_resource_count,
            sweep.pending_count,
            sweep.quarantined_count,
            sweep.not_ready_count,
            sweep.missing_count,
            sweep.failed_count,
        )
        return sweep

    def recover(self, task_id: TaskId) -> WeaponryResourceRecoveryResult:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        record = self._store.get(task_id)
        if record is None:
            return WeaponryResourceRecoveryResult(
                task_id,
                WeaponryResourceRecoveryOutcome.MISSING,
            )
        if record.state is WeaponryResourceRecordState.CLEANED:
            return WeaponryResourceRecoveryResult(
                task_id,
                WeaponryResourceRecoveryOutcome.CLEANED,
            )
        if record.state is WeaponryResourceRecordState.QUARANTINED:
            return WeaponryResourceRecoveryResult(
                task_id,
                WeaponryResourceRecoveryOutcome.QUARANTINED,
                error_code=record.last_error_code,
            )

        if record.state is WeaponryResourceRecordState.TRACKING:
            preparation = self._prepare_terminal_tracking(record)
            if isinstance(preparation, WeaponryResourceRecoveryResult):
                return preparation
            record = preparation
            if record.state is WeaponryResourceRecordState.CLEANED:
                return WeaponryResourceRecoveryResult(
                    task_id,
                    WeaponryResourceRecoveryOutcome.CLEANED,
                )

        pending_audits = self._audit.list_pending(task_id, limit=1)
        if pending_audits:
            return self._quarantine(
                record,
                error_code="weaponry_interaction_outcome_unknown",
                reason=(
                    "存在未完成 Interaction Audit；禁止重放外部调用或自动删除现场"
                ),
            )

        acquire = self._store.acquire_cleanup(
            AcquireWeaponryCleanupLease(task_id, record.version)
        )
        if not isinstance(acquire, WeaponryCleanupLeaseAcquireResult):
            raise TypeError("Resource Store acquire_cleanup 返回类型错误")
        if acquire.outcome is WeaponryCleanupLeaseAcquireOutcome.BUSY:
            return WeaponryResourceRecoveryResult(
                task_id,
                WeaponryResourceRecoveryOutcome.BUSY,
            )
        if acquire.outcome is WeaponryCleanupLeaseAcquireOutcome.NOT_READY:
            return WeaponryResourceRecoveryResult(
                task_id,
                WeaponryResourceRecoveryOutcome.NOT_READY,
            )
        lease = acquire.lease
        if lease is None or lease.task_id != task_id:
            raise TypeError("Resource Store 返回无效 cleanup lease")

        record = self._required_record(task_id)
        candidates = record.owned_cleanup_candidates
        if not candidates:
            return self._quarantine(
                record,
                error_code="weaponry_cleanup_candidate_missing",
                reason="cleanup_pending 记录没有可处理的 owned 资源",
            )
        resource = candidates[0]
        if resource.state is WeaponryTrackedResourceState.CLEANUP_UNKNOWN:
            return self._quarantine(
                record,
                error_code="weaponry_resource_cleanup_outcome_unknown",
                reason="资源上次删除结果未知，禁止自动重试",
            )

        try:
            cleanup = self._cleaner.cleanup(
                CleanupWeaponryExternalResource(task_id, resource)
            )
        except Exception:
            logger.exception(
                "武器谱清理 Adapter 异常逃逸，无法确认外部副作用: task_id=%s "
                "resource_id=%s",
                task_id.value,
                resource.resource_id,
            )
            return self._quarantine_after_checkpoint_loss(
                task_id,
                error_code="weaponry_cleanup_adapter_exception",
                reason="清理 Adapter 异常逃逸，外部删除结果无法确认",
            )
        if not isinstance(cleanup, WeaponryExternalResourceCleanupResult):
            return self._quarantine_after_checkpoint_loss(
                task_id,
                error_code="weaponry_cleanup_port_contract_invalid",
                reason="外部清理 Adapter 返回类型无效",
            )
        try:
            updated = self._store.complete_cleanup(
                CompleteWeaponryResourceCleanup(
                    task_id=task_id,
                    lease=lease,
                    resource_id=resource.resource_id,
                    outcome=cleanup.outcome,
                    expected_version=record.version,
                    error_code=cleanup.error_code,
                )
            )
        except Exception:
            logger.exception(
                "武器谱外部删除后清理检查点提交失败，准备隔离: task_id=%s "
                "resource_id=%s outcome=%s",
                task_id.value,
                resource.resource_id,
                cleanup.outcome.value,
            )
            return self._quarantine_after_checkpoint_loss(
                task_id,
                error_code="weaponry_cleanup_checkpoint_failed",
                reason="外部删除已发生，但清理结果未能可靠提交",
            )

        if cleanup.outcome is WeaponryResourceCleanupOutcome.OUTCOME_UNKNOWN:
            return self._quarantine(
                updated,
                error_code=cleanup.error_code,
                reason="外部 DELETE 结果未知，禁止自动重放",
            )
        if updated.state is WeaponryResourceRecordState.CLEANED:
            return WeaponryResourceRecoveryResult(
                task_id,
                WeaponryResourceRecoveryOutcome.CLEANED,
                cleaned_resource_count=1,
            )

        self._release_best_effort(updated)
        return WeaponryResourceRecoveryResult(
            task_id,
            WeaponryResourceRecoveryOutcome.PENDING,
            cleaned_resource_count=(
                1
                if cleanup.outcome is WeaponryResourceCleanupOutcome.SUCCEEDED
                else 0
            ),
            error_code=cleanup.error_code,
        )

    def _prepare_terminal_tracking(
        self,
        record: WeaponryResourceRecord,
    ) -> WeaponryResourceRecord | WeaponryResourceRecoveryResult:
        execution = (
            self._task_commands.get_execution(record.task_id)
            if self._task_commands is not None
            else self._task_reader.get_by_id(record.task_id)
        )
        if execution is None:
            # 资源记录存在而执行事实缺失，说明本地数据库已经失去证明资源是否仍被使用
            # 的依据。自动删除和继续轮询都不安全：前者可能误删，后者会制造永久热扫描。
            # 因此一次性隔离并等待人工对账，绝不凭记录年龄推测 Worker 已死亡。
            return self._quarantine(
                record,
                error_code="weaponry_execution_missing_for_resource",
                reason="tracking 资源找不到对应 execution，禁止自动删除",
            )
        if not isinstance(execution, (TaskExecutionSnapshot, TaskSnapshot)):
            return self._quarantine(
                record,
                error_code="weaponry_execution_snapshot_invalid",
                reason="任务读取端口返回类型无效",
            )
        if execution.execution_state in _ACTIVE_EXECUTION_STATES:
            return WeaponryResourceRecoveryResult(
                record.task_id,
                WeaponryResourceRecoveryOutcome.NOT_READY,
            )
        identity_matches = (
            execution.execution_state in _TERMINAL_EXECUTION_STATES
            and execution.task_id == record.task_id
            and execution.business_ref == record.business_ref
        )
        if isinstance(execution, TaskExecutionSnapshot):
            snapshot = execution.input_snapshot
            identity_matches = bool(
                identity_matches
                and isinstance(snapshot, WeaponryInputSnapshot)
                and snapshot.task_id == record.task_id.value
                and str(snapshot.architecture_id) == record.business_ref.business_key
            )
        elif execution.task_type != "weaponry":
            identity_matches = False
        if not identity_matches:
            return self._quarantine(
                record,
                error_code="weaponry_execution_resource_identity_mismatch",
                reason="终态 execution 与资源记录身份不一致",
            )
        try:
            return self._store.prepare_cleanup(
                PrepareWeaponryResourceCleanup(record.task_id, record.version)
            )
        except WeaponryPortStateError as exc:
            if exc.error_code == "resource_version_conflict":
                return WeaponryResourceRecoveryResult(
                    record.task_id,
                    WeaponryResourceRecoveryOutcome.BUSY,
                    error_code=exc.error_code,
                )
            raise

    def _quarantine(
        self,
        record: WeaponryResourceRecord,
        *,
        error_code: str,
        reason: str,
    ) -> WeaponryResourceRecoveryResult:
        quarantined = self._store.quarantine(
            QuarantineWeaponryResources(
                task_id=record.task_id,
                expected_version=record.version,
                error_code=error_code,
                reason=reason,
            )
        )
        return WeaponryResourceRecoveryResult(
            record.task_id,
            WeaponryResourceRecoveryOutcome.QUARANTINED,
            error_code=quarantined.last_error_code,
        )

    def _quarantine_after_checkpoint_loss(
        self,
        task_id: TaskId,
        *,
        error_code: str,
        reason: str,
    ) -> WeaponryResourceRecoveryResult:
        latest = self._store.get(task_id)
        if latest is None:
            return WeaponryResourceRecoveryResult(
                task_id,
                WeaponryResourceRecoveryOutcome.FAILED,
                error_code=error_code,
            )
        if latest.state is WeaponryResourceRecordState.CLEANED:
            return WeaponryResourceRecoveryResult(
                task_id,
                WeaponryResourceRecoveryOutcome.CLEANED,
            )
        if latest.state is WeaponryResourceRecordState.QUARANTINED:
            return WeaponryResourceRecoveryResult(
                task_id,
                WeaponryResourceRecoveryOutcome.QUARANTINED,
                error_code=latest.last_error_code,
            )
        try:
            return self._quarantine(
                latest,
                error_code=error_code,
                reason=reason,
            )
        except Exception:
            logger.critical(
                "武器谱清理检查点丢失后隔离也失败，必须人工处理: task_id=%s",
                task_id.value,
                exc_info=True,
            )
            return WeaponryResourceRecoveryResult(
                task_id,
                WeaponryResourceRecoveryOutcome.FAILED,
                error_code=error_code,
            )

    def _release_best_effort(self, record: WeaponryResourceRecord) -> None:
        lease = record.cleanup_lease
        if lease is None:
            return
        try:
            self._store.release_cleanup(
                ReleaseWeaponryCleanupLease(lease, record.version)
            )
        except Exception:
            # lease 有截止时间；释放失败只延迟下一轮，不允许因此重放刚才的外部 DELETE。
            logger.warning(
                "武器谱资源清理租约释放失败，等待过期恢复: task_id=%s",
                record.task_id.value,
                exc_info=True,
            )

    def _required_record(self, task_id: TaskId) -> WeaponryResourceRecord:
        record = self._store.get(task_id)
        if not isinstance(record, WeaponryResourceRecord):
            raise WeaponryPortStateError(
                "resource_record_not_found",
                "资源清理租约取得后记录消失",
            )
        return record


__all__ = [
    "WeaponryResourceRecoveryOutcome",
    "WeaponryResourceRecoveryResult",
    "WeaponryResourceRecoveryService",
    "WeaponryResourceRecoverySweepResult",
]
