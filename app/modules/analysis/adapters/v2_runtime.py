"""Analysis 阶段 2 Executor 与持久维护扫描的本地生命周期门面。"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
from threading import Lock
import time

from app.modules.analysis.adapters.local_dispatcher import (
    LocalAnalysisDispatcherSnapshot,
)
from app.modules.analysis.adapters.runtime_config import AnalysisInfrastructureConfig
from app.modules.analysis.application import (
    AnalysisResourceSweepResult,
    RecoverAnalysisResources,
)
from app.modules.analysis.ports import (
    AnalysisCallbackGuardSweepResult,
    AnalysisCallbackPort,
)
from app.modules.tasks.adapters import (
    LocalMaintenanceJob,
    LocalMaintenanceScheduler,
    LocalTaskExecutor,
)
from app.modules.tasks.ports import TaskExecutionPermitPort


logger = logging.getLogger(__name__)
_CALLBACK_JOB = "analysis_callback_guard_sweep"
_RESOURCE_JOB = "analysis_resource_recovery"


@dataclass(frozen=True, slots=True)
class AnalysisV2MaintenanceSnapshot:
    """独立维护线程的最小只读诊断，不进入公开接口。"""

    thread_count: int
    callback_count: int
    callback_failure_count: int
    callback_frozen_count: int
    resource_count: int
    resource_failure_count: int
    healthy: bool


class AnalysisV2Maintenance:
    """周期扫描是恢复真相；Event 仅用于缩短终态后的等待时间。"""

    def __init__(
        self,
        *,
        callbacks: AnalysisCallbackPort,
        resources: RecoverAnalysisResources,
        config: AnalysisInfrastructureConfig,
    ) -> None:
        if not isinstance(callbacks, AnalysisCallbackPort):
            raise TypeError("callbacks 必须实现 AnalysisCallbackPort")
        if not isinstance(resources, RecoverAnalysisResources):
            raise TypeError("resources 必须是 RecoverAnalysisResources")
        if not isinstance(config, AnalysisInfrastructureConfig):
            raise TypeError("config 必须是 AnalysisInfrastructureConfig")
        self._callbacks = callbacks
        self._resources = resources
        self._limit = config.resource_sweep_batch_size
        self._lock = Lock()
        self._state = "new"
        self._callback_count = 0
        self._callback_failures = 0
        self._callback_frozen = 0
        self._resource_count = 0
        self._resource_failures = 0
        self._scheduler = LocalMaintenanceScheduler(
            jobs=(
                LocalMaintenanceJob(
                    _CALLBACK_JOB,
                    config.resource_sweep_interval_seconds,
                    self._sweep_callbacks,
                ),
                LocalMaintenanceJob(
                    _RESOURCE_JOB,
                    config.resource_sweep_interval_seconds,
                    self._sweep_resources,
                ),
            ),
            stop_grace_seconds=config.stop_timeout_seconds,
            thread_name="analysis-v2-maintenance",
        )

    @property
    def callbacks(self) -> AnalysisCallbackPort:
        return self._callbacks

    @property
    def resources(self) -> RecoverAnalysisResources:
        return self._resources

    def start(self) -> None:
        with self._lock:
            if self._state != "new":
                raise RuntimeError("Analysis v2 Maintenance 只能启动一次")
            self._state = "starting"
        try:
            self._scheduler.start()
        except Exception:
            with self._lock:
                self._state = "stopped"
            raise
        with self._lock:
            self._state = "running"
        logger.info("Analysis v2 独立维护已启动: bounded_limit=%d", self._limit)

    def wake_up(self) -> None:
        self._scheduler.wake_up()

    def stop(self, *, timeout_seconds: float | None = None) -> bool:
        stopped = self._scheduler.stop(timeout_seconds=timeout_seconds)
        with self._lock:
            self._state = "stopped" if stopped else "stopping"
        return stopped

    def close(self) -> None:
        with self._lock:
            state = self._state
        if state in {"starting", "running", "stopping"} and not self.stop():
            raise RuntimeError("Analysis v2 Maintenance 仍有线程未停止")
        with self._lock:
            self._state = "closed"

    def snapshot(self) -> AnalysisV2MaintenanceSnapshot:
        with self._lock:
            return AnalysisV2MaintenanceSnapshot(
                thread_count=self._scheduler.thread_count,
                callback_count=self._callback_count,
                callback_failure_count=self._callback_failures,
                callback_frozen_count=self._callback_frozen,
                resource_count=self._resource_count,
                resource_failure_count=self._resource_failures,
                healthy=self._scheduler.is_healthy(),
            )

    def _sweep_callbacks(self) -> None:
        try:
            result = self._callbacks.freeze_expired(limit=self._limit)
            if not isinstance(result, AnalysisCallbackGuardSweepResult):
                raise TypeError("Analysis Callback Guard sweep 返回类型错误")
            with self._lock:
                self._callback_count += 1
                self._callback_frozen += result.frozen_count
        except Exception:
            with self._lock:
                self._callback_failures += 1
            logger.exception(
                "Analysis v2 Callback Guard 维护失败；禁止据此自动补发 Callback"
            )

    def _sweep_resources(self) -> None:
        try:
            result = self._resources.run_once(limit=self._limit)
            if not isinstance(result, AnalysisResourceSweepResult):
                raise TypeError("Analysis 资源恢复 sweep 返回类型错误")
            with self._lock:
                self._resource_count += 1
        except Exception:
            with self._lock:
                self._resource_failures += 1
            logger.exception(
                "Analysis v2 资源维护失败；禁止自动重放远端 RAG close/delete"
            )


class AnalysisV2TaskDispatcher:
    """保留既有生命周期/诊断形状，执行权只来自 v2 lease/fencing。"""

    def __init__(
        self,
        *,
        executor: LocalTaskExecutor,
        maintenance: AnalysisV2Maintenance,
        worker_count: int,
    ) -> None:
        if not isinstance(executor, LocalTaskExecutor):
            raise TypeError("executor 必须是 LocalTaskExecutor")
        if not isinstance(maintenance, AnalysisV2Maintenance):
            raise TypeError("maintenance 必须是 AnalysisV2Maintenance")
        if isinstance(worker_count, bool) or not isinstance(worker_count, int) or worker_count < 1:
            raise ValueError("worker_count 必须是正整数")
        self._executor = executor
        self._maintenance = maintenance
        self._worker_count = worker_count
        self._lock = Lock()
        self._state = "new"
        self._dispatch_count = 0
        self._merged_wakeup_count = 0

    @property
    def callbacks(self) -> AnalysisCallbackPort:
        return self._maintenance.callbacks

    @property
    def resources(self) -> RecoverAnalysisResources:
        return self._maintenance.resources

    @property
    def execution_limiter(self) -> TaskExecutionPermitPort:
        return self._executor.execution_limiter

    @property
    def uses_task_control_authority(self) -> bool:
        """新 Dispatcher 不再获取旧进程锁，跨进程竞争由 SQLite Authority 裁决。"""

        return True

    def wake_up(self) -> None:
        with self._lock:
            self._dispatch_count += 1
            if self._state != "running":
                self._merged_wakeup_count += 1
        self._executor.wake_up()

    def wake_maintenance(self) -> None:
        self._maintenance.wake_up()

    def start(self) -> None:
        with self._lock:
            if self._state != "new":
                raise RuntimeError("Analysis v2 Dispatcher 只能启动一次")
            self._state = "starting"
        maintenance_started = False
        try:
            # 先启动启动/周期恢复扫描，再开放新 Task claim。若后者失败，只停止本轮
            # 已启动的维护线程，绝不修改任何持久 execution/lease 事实。
            self._maintenance.start()
            maintenance_started = True
            self._executor.start()
        except Exception:
            if maintenance_started:
                try:
                    if not self._maintenance.stop():
                        logger.critical("Analysis v2 Executor 启动失败后的维护停机未完成")
                except Exception:
                    logger.critical(
                        "Analysis v2 Executor 启动失败后的维护停机异常",
                        exc_info=True,
                    )
            with self._lock:
                self._state = "stopped"
            raise
        with self._lock:
            self._state = "running"
        logger.info("Analysis v2 Dispatcher 已启动: worker_count=%d", self._worker_count)

    def stop(self, *, timeout_seconds: float | None = None) -> bool:
        if timeout_seconds is None:
            deadline = None
        else:
            if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
                raise TypeError("timeout_seconds 必须是数字或 None")
            normalized = float(timeout_seconds)
            if not math.isfinite(normalized) or normalized <= 0:
                raise ValueError("timeout_seconds 必须是正有限数字")
            deadline = time.monotonic() + normalized
        with self._lock:
            if self._state in {"stopped", "closed"}:
                return True
            if self._state not in {"starting", "running", "stopping"}:
                raise RuntimeError("Analysis v2 Dispatcher 尚未启动")
            self._state = "stopping"

        executor_error: Exception | None = None
        maintenance_error: Exception | None = None
        executor_stopped = False
        maintenance_stopped = False
        try:
            try:
                executor_stopped = self._executor.stop(
                    timeout_seconds=(
                        None
                        if deadline is None
                        else max(0.000001, deadline - time.monotonic())
                    )
                )
            except Exception as exc:
                executor_error = exc
            try:
                maintenance_stopped = self._maintenance.stop(
                    timeout_seconds=(
                        None
                        if deadline is None
                        else max(0.0, deadline - time.monotonic())
                    )
                )
            except Exception as exc:
                maintenance_error = exc
        finally:
            with self._lock:
                self._state = (
                    "stopped"
                    if executor_error is None
                    and maintenance_error is None
                    and executor_stopped
                    and maintenance_stopped
                    else "stopping"
                )
        if executor_error is not None:
            if maintenance_error is not None:
                logger.critical(
                    "Analysis v2 Dispatcher 双通道停机异常: executor=%s maintenance=%s",
                    type(executor_error).__name__,
                    type(maintenance_error).__name__,
                )
            raise executor_error
        if maintenance_error is not None:
            raise maintenance_error
        return executor_stopped and maintenance_stopped

    def close(self) -> None:
        with self._lock:
            state = self._state
        if state in {"starting", "running", "stopping"}:
            if not self.stop():
                raise RuntimeError("Analysis v2 Dispatcher 仍有后台线程未停止")
        elif state == "new":
            self._maintenance.close()
        with self._lock:
            self._state = "closed"

    def snapshot(self) -> LocalAnalysisDispatcherSnapshot:
        with self._lock:
            state = self._state
            dispatch_count = self._dispatch_count
            merged = self._merged_wakeup_count
        maintenance = self._maintenance.snapshot()
        executor_healthy = self._executor.is_healthy()
        healthy = executor_healthy and maintenance.healthy
        ready = state == "running" and healthy
        if not executor_healthy:
            fatal_error = "executor_unhealthy"
        elif not maintenance.healthy:
            fatal_error = "maintenance_unhealthy"
        else:
            fatal_error = ""
        return LocalAnalysisDispatcherSnapshot(
            lifecycle_state=state,
            worker_thread_count=(
                self._worker_count
                if state in {"starting", "running", "stopping"}
                else 0
            ),
            maintenance_thread_count=maintenance.thread_count,
            buffered_task_count=0,
            waiting_task_id=None,
            current_task_id=None,
            dispatch_count=dispatch_count,
            merged_wakeup_count=merged,
            scan_count=0,
            execution_count=0,
            execution_failure_count=0,
            accepted_deferral_count=0,
            accepted_deferral_failure_count=0,
            queue_inspection_count=0,
            queue_inspection_failure_count=0,
            resource_sweep_count=maintenance.resource_count,
            resource_sweep_failure_count=maintenance.resource_failure_count,
            callback_guard_sweep_count=maintenance.callback_count,
            callback_guard_sweep_failure_count=maintenance.callback_failure_count,
            callback_guard_frozen_count=maintenance.callback_frozen_count,
            poisoned_snapshot_count=0,
            poisoned_snapshot_failure_count=0,
            ready=ready,
            fatal_error=fatal_error,
        )


__all__ = [
    "AnalysisV2Maintenance",
    "AnalysisV2MaintenanceSnapshot",
    "AnalysisV2TaskDispatcher",
]
