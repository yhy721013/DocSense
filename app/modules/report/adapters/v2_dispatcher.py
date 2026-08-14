"""Report 既有 Dispatcher 生命周期到阶段 2 LocalTaskExecutor 的薄门面。"""

from __future__ import annotations

import logging
import math
from threading import Lock
import time

from app.modules.report.adapters.local_dispatcher import LocalReportDispatcherSnapshot
from app.modules.report.adapters.v2_maintenance import ReportV2Maintenance
from app.modules.report.ports import ReportCallbackPort
from app.modules.tasks.adapters import LocalTaskExecutor
from app.modules.tasks.domain import TaskId


logger = logging.getLogger(__name__)


class ReportV2TaskDispatcher:
    """保留组合根生命周期/诊断形状，执行权完全来自 v2 SQLite claim。"""

    def __init__(
        self,
        *,
        executor: LocalTaskExecutor,
        maintenance: ReportV2Maintenance,
    ) -> None:
        if not isinstance(executor, LocalTaskExecutor):
            raise TypeError("executor 必须是 LocalTaskExecutor")
        if not isinstance(maintenance, ReportV2Maintenance):
            raise TypeError("maintenance 必须是 ReportV2Maintenance")
        self._executor = executor
        self._maintenance = maintenance
        self._lock = Lock()
        self._state = "new"
        self._dispatch_count = 0
        self._merged_wakeup_count = 0

    @property
    def callbacks(self) -> ReportCallbackPort:
        return self._maintenance.callbacks

    @property
    def resources(self):
        """暴露只读依赖身份，仅供组合根验证生产对象图唯一性。"""

        return self._maintenance.resources

    def dispatch(self, task_id: TaskId) -> None:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        with self._lock:
            self._dispatch_count += 1
            if self._state != "running":
                self._merged_wakeup_count += 1
        self._executor.wake_up()

    def wake_maintenance(self) -> None:
        """终态后的可丢提示；真实恢复工作始终从专用 Store 扫描。"""

        self._maintenance.wake_up()

    def start(self) -> None:
        with self._lock:
            if self._state != "new":
                raise RuntimeError("Report v2 Dispatcher 只能启动一次")
            self._state = "starting"
        maintenance_started = False
        try:
            # 先开放启动扫描，再启动新业务领取。若 Executor 启动失败，立即停止本轮
            # 启动的维护线程；持久 Task/Guard/资源事实均不被回滚或重置。
            self._maintenance.start()
            maintenance_started = True
            self._executor.start()
        except Exception:
            if maintenance_started:
                try:
                    if not self._maintenance.stop():
                        logger.critical(
                            "Report v2 Executor 启动失败后的维护停机未完成"
                        )
                except Exception:
                    logger.critical(
                        "Report v2 Executor 启动失败后的维护停机异常",
                        exc_info=True,
                    )
            with self._lock:
                self._state = "stopped"
            raise
        with self._lock:
            self._state = "running"
        logger.info("Report v2 Dispatcher 已启动")

    def stop(self, *, timeout_seconds: float | None = None) -> bool:
        # 参数校验必须早于生命周期切换，防止非法调用把 Dispatcher 留在 stopping。
        if timeout_seconds is None:
            deadline = None
        else:
            if isinstance(timeout_seconds, bool) or not isinstance(
                timeout_seconds,
                (int, float),
            ):
                raise TypeError("timeout_seconds 必须是数字或 None")
            normalized_timeout = float(timeout_seconds)
            if not math.isfinite(normalized_timeout) or normalized_timeout <= 0:
                raise ValueError("timeout_seconds 必须是正有限数字")
            deadline = time.monotonic() + normalized_timeout
        with self._lock:
            if self._state in {"stopped", "closed"}:
                return True
            if self._state not in {"running", "starting", "stopping"}:
                raise RuntimeError("Report v2 Dispatcher 尚未启动")
            self._state = "stopping"

        executor_stopped = False
        maintenance_stopped = False
        executor_error: Exception | None = None
        maintenance_error: Exception | None = None
        try:
            executor_timeout = (
                None if deadline is None else max(0.000001, deadline - time.monotonic())
            )
            try:
                executor_stopped = self._executor.stop(
                    timeout_seconds=executor_timeout
                )
            except Exception as exc:
                executor_error = exc

            # Executor 停止失败不能跳过维护线程停机；两者没有共享业务事务，必须各自
            # 尽最大努力释放。显式总 deadline 则保证第二段只使用剩余预算。
            maintenance_timeout = (
                None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            try:
                maintenance_stopped = self._maintenance.stop(
                    timeout_seconds=maintenance_timeout
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
                    "Report v2 Dispatcher 双通道停机异常: executor_error=%s "
                    "maintenance_error=%s",
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
        if state in {"running", "starting", "stopping"}:
            if not self.stop():
                raise RuntimeError("Report v2 Dispatcher 仍有后台线程未停止")
        elif state == "new":
            self._maintenance.close()
        with self._lock:
            self._state = "closed"

    def snapshot(self) -> LocalReportDispatcherSnapshot:
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
        return LocalReportDispatcherSnapshot(
            lifecycle_state=state,
            worker_thread_count=1 if state in {"starting", "running", "stopping"} else 0,
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
            resource_sweep_count=maintenance.resource_sweep_count,
            resource_sweep_failure_count=(
                maintenance.resource_sweep_failure_count
            ),
            queue_inspection_count=0,
            queue_inspection_failure_count=0,
            callback_guard_sweep_count=maintenance.callback_guard_sweep_count,
            callback_guard_sweep_failure_count=(
                maintenance.callback_guard_sweep_failure_count
            ),
            callback_guard_frozen_count=maintenance.callback_guard_frozen_count,
            ready=ready,
            fatal_error=fatal_error,
        )


__all__ = ["ReportV2TaskDispatcher"]
