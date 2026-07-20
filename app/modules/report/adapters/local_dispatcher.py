"""报告业务对通用持久任务 Dispatcher 内核的薄适配器。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import threading
import time
from typing import Any

from app.modules.report.ports import (
    ReportCallbackGuardSweepResult,
    ReportCallbackPort,
    ReportResourceRecoveryPort,
    ReportResourceSweepResult,
)
from app.modules.tasks.adapters.local_persistent_dispatcher import (
    LocalPersistentDispatcherSettings,
    LocalPersistentMaintenanceTask,
    LocalPersistentTaskDispatcher,
)
from app.modules.tasks.domain import TaskId
from app.modules.tasks.ports import (
    ProcessSingletonGuardPort,
    TaskCommandPort,
    TaskExecutionPermitPort,
    TaskQueueInspectionPort,
)
from app.services.core.config import ReportInfrastructureConfig


logger = logging.getLogger(__name__)

_REPORT_TASK_TYPE = "report"
_REPORT_MAINTENANCE_NAME = "report-resource-and-callback-guard"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LocalReportDispatcherSnapshot:
    """保留阶段 1C 已冻结的报告 Dispatcher 观测形状。"""

    lifecycle_state: str
    worker_thread_count: int
    maintenance_thread_count: int
    buffered_task_count: int
    waiting_task_id: TaskId | None
    current_task_id: TaskId | None
    dispatch_count: int
    merged_wakeup_count: int
    scan_count: int
    execution_count: int
    execution_failure_count: int
    accepted_deferral_count: int
    accepted_deferral_failure_count: int
    resource_sweep_count: int
    resource_sweep_failure_count: int
    queue_inspection_count: int
    queue_inspection_failure_count: int
    callback_guard_sweep_count: int
    callback_guard_sweep_failure_count: int
    callback_guard_frozen_count: int
    ready: bool
    fatal_error: str


class LocalReportTaskDispatcher:
    """组合报告维护语义，并把通用生命周期委托给 tasks 内核。

    报告仍只有一条业务执行 Worker。Callback Guard 与资源恢复保持阶段 1C 的同周期、
    同维护线程顺序；只读队列诊断由通用内核的另一条线程执行，因此重型模型调用不会
    阻塞维护，Report 的既有线程数量、日志口径和快照字段也保持兼容。
    """

    def __init__(
        self,
        *,
        task_commands: TaskCommandPort[Any, Any, Any],
        queue_inspector: TaskQueueInspectionPort,
        resources: ReportResourceRecoveryPort,
        callbacks: ReportCallbackPort,
        execute: Callable[[TaskId], object],
        config: ReportInfrastructureConfig,
        execution_limiter: TaskExecutionPermitPort | None = None,
        process_guard: ProcessSingletonGuardPort | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not isinstance(resources, ReportResourceRecoveryPort):
            raise TypeError("resources 必须实现 ReportResourceRecoveryPort")
        if not isinstance(callbacks, ReportCallbackPort):
            raise TypeError("callbacks 必须实现 ReportCallbackPort")
        if not isinstance(config, ReportInfrastructureConfig):
            raise TypeError("config 必须是 ReportInfrastructureConfig")
        if config.runtime_mode != "single_instance":
            raise RuntimeError("本地报告 Dispatcher 只支持 single_instance")

        self._resources = resources
        self._callbacks = callbacks
        self._config = config
        self._business_state_lock = threading.RLock()
        self._resource_sweep_count = 0
        self._resource_sweep_failure_count = 0
        self._callback_guard_sweep_count = 0
        self._callback_guard_sweep_failure_count = 0
        self._callback_guard_frozen_count = 0

        settings = LocalPersistentDispatcherSettings(
            task_type=_REPORT_TASK_TYPE,
            business_label="报告",
            thread_name_prefix="docsense-report",
            scan_interval_seconds=config.scan_interval_seconds,
            accepted_batch_size=config.accepted_batch_size,
            dispatch_failure_retry_seconds=(
                config.dispatch_failure_retry_seconds
            ),
            running_sample_limit=config.running_sample_limit,
            stop_timeout_seconds=config.stop_timeout_seconds,
        )
        self._kernel = LocalPersistentTaskDispatcher(
            task_commands=task_commands,
            queue_inspector=queue_inspector,
            execute=execute,
            settings=settings,
            maintenance_tasks=(
                LocalPersistentMaintenanceTask(
                    name=_REPORT_MAINTENANCE_NAME,
                    thread_name="docsense-report-resource-sweeper",
                    interval_seconds=config.resource_sweep_interval_seconds,
                    execute=self._run_report_maintenance_once,
                ),
            ),
            execution_limiter=execution_limiter,
            process_guard=process_guard,
            event_logger=logger,
            monotonic=monotonic,
            wall_clock=wall_clock,
        )

    @property
    def has_process_guard(self) -> bool:
        """供生产组合根验证跨进程单实例门禁。"""

        return self._kernel.has_process_guard

    @property
    def task_commands(self) -> TaskCommandPort[Any, Any, Any]:
        return self._kernel.task_commands

    @property
    def execution_limiter(self) -> TaskExecutionPermitPort | None:
        return self._kernel.execution_limiter

    def dispatch(self, task_id: TaskId) -> None:
        self._kernel.dispatch(task_id)

    def start(self) -> None:
        self._kernel.start()
        logger.info(
            "报告 Dispatcher 配置确认: runtime_mode=%s resource_sweep_limit=%d",
            self._config.runtime_mode,
            self._config.resource_sweep_limit,
        )

    def stop(self, *, timeout_seconds: float | None = None) -> bool:
        return self._kernel.stop(timeout_seconds=timeout_seconds)

    def close(self) -> None:
        self._kernel.close()

    def snapshot(self) -> LocalReportDispatcherSnapshot:
        common = self._kernel.snapshot()
        with self._business_state_lock:
            return LocalReportDispatcherSnapshot(
                lifecycle_state=common.lifecycle_state,
                worker_thread_count=common.worker_thread_count,
                maintenance_thread_count=common.maintenance_thread_count,
                buffered_task_count=common.buffered_task_count,
                waiting_task_id=common.waiting_task_id,
                current_task_id=common.current_task_id,
                dispatch_count=common.dispatch_count,
                merged_wakeup_count=common.merged_wakeup_count,
                scan_count=common.scan_count,
                execution_count=common.execution_count,
                execution_failure_count=common.execution_failure_count,
                accepted_deferral_count=common.accepted_deferral_count,
                accepted_deferral_failure_count=(
                    common.accepted_deferral_failure_count
                ),
                resource_sweep_count=self._resource_sweep_count,
                resource_sweep_failure_count=self._resource_sweep_failure_count,
                queue_inspection_count=common.queue_inspection_count,
                queue_inspection_failure_count=(
                    common.queue_inspection_failure_count
                ),
                callback_guard_sweep_count=self._callback_guard_sweep_count,
                callback_guard_sweep_failure_count=(
                    self._callback_guard_sweep_failure_count
                ),
                callback_guard_frozen_count=self._callback_guard_frozen_count,
                ready=common.ready,
                fatal_error=common.fatal_error,
            )

    def _run_report_maintenance_once(self) -> None:
        """保持 Guard 先于资源恢复；前者失败不得跳过后者。"""

        self._sweep_callback_guards()
        self._sweep_resources()

    def _sweep_resources(self) -> None:
        try:
            result = self._resources.sweep(
                limit=self._config.resource_sweep_limit,
            )
            if not isinstance(result, ReportResourceSweepResult):
                raise TypeError("资源恢复 sweep 必须返回 ReportResourceSweepResult")
            with self._business_state_lock:
                self._resource_sweep_count += 1
            logger.info(
                "报告 Dispatcher 资源恢复轮询完成: scanned=%d cleaned=%d "
                "pending=%d quarantined=%d not_ready=%d missing=%d failed=%d",
                result.scanned_count,
                result.cleaned_count,
                result.pending_count,
                result.quarantined_count,
                result.not_ready_count,
                result.missing_count,
                len(result.failed_task_ids),
            )
        except Exception:
            with self._business_state_lock:
                self._resource_sweep_failure_count += 1
            logger.exception("报告资源恢复批次失败，Dispatcher 将按周期继续重试")

    def _sweep_callback_guards(self) -> None:
        try:
            result = self._callbacks.freeze_expired(
                limit=self._config.resource_sweep_limit,
            )
            if not isinstance(result, ReportCallbackGuardSweepResult):
                raise TypeError("Callback Guard sweep 必须返回强类型结果")
            with self._business_state_lock:
                self._callback_guard_sweep_count += 1
                self._callback_guard_frozen_count += result.frozen_count
            logger.log(
                logging.WARNING if result.frozen_count else logging.DEBUG,
                "报告 Callback Guard 维护完成: scanned=%d frozen=%d",
                result.scanned_count,
                result.frozen_count,
            )
        except Exception:
            with self._business_state_lock:
                self._callback_guard_sweep_failure_count += 1
            logger.exception("报告 Callback Guard 维护失败，将按周期继续重试")


__all__ = [
    "LocalReportDispatcherSnapshot",
    "LocalReportTaskDispatcher",
]
