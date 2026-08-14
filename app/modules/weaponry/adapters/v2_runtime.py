"""Weaponry 既有生命周期门面到阶段 2 Executor/维护调度器的薄适配。"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
from threading import Lock
import time
from collections.abc import Callable

from app.modules.tasks.adapters import (
    LocalMaintenanceJob,
    LocalMaintenanceScheduler,
    LocalTaskExecutor,
)
from app.modules.tasks.domain import TaskId
from app.modules.tasks.ports import TaskExecutionPermitPort
from app.modules.weaponry.application import (
    RunWeaponryOutcome,
    RunWeaponryResult,
    WeaponryResourceRecoveryService,
    WeaponryResourceRecoverySweepResult,
)
from app.modules.weaponry.ports import (
    WeaponryCallbackGuardSweepResult,
    WeaponryCallbackPort,
)

from .local_dispatcher import LocalWeaponryDispatcherSnapshot
from .runtime_config import WeaponryRuntimeConfig


logger = logging.getLogger(__name__)
_CALLBACK_JOB = "weaponry_callback_guard_sweep"
_RESOURCE_JOB = "weaponry_resource_recovery"


@dataclass(frozen=True, slots=True)
class WeaponryV2MaintenanceSnapshot:
    thread_count: int
    callback_count: int
    callback_failure_count: int
    resource_count: int
    resource_failure_count: int
    healthy: bool


class WeaponryV2ResultMetrics:
    """只累计稳定分类，不保存业务正文、URL、Prompt 或响应。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self.execution_count = 0
        self.execution_failure_count = 0
        self.succeeded = 0
        self.provider_capacity = 0
        self.business_zero = 0
        self.input_contract = 0
        self.other_failed = 0

    def observe(self, result: RunWeaponryResult) -> None:
        codes = tuple(
            code.strip().lower()
            for code in (*result.diagnostic_error_codes, result.error_code)
            if code.strip()
        )
        provider = any(
            marker in code
            for code in codes
            for marker in ("capacity", "payload_too_large", "rate_limit", "rate_limited")
        )
        contract = any(
            marker in code
            for code in codes
            for marker in ("contract", "mismatch", "invalid", "unsupported", "schema", "profile")
        )
        with self._lock:
            # LocalTaskExecutor 只有在真正取得 Task Authority 并调用业务 Runner 后才会
            # 触发 observer。因此这里的总数表示“已进入 Weaponry v2 Workflow 的执行
            # 次数”，而不是 wakeup、claim 竞争或维护扫描次数。
            self.execution_count += 1
            if result.outcome is not RunWeaponryOutcome.SUCCEEDED:
                # recovery_required 虽然不是公开 failed 终态，但本次 Worker 执行没有
                # 完成业务闭环，必须进入失败指标；否则供应商结果未知现场会从运行健康度
                # 中消失。具体隔离原因仍由 Task/Step/Recovery 指标负责区分。
                self.execution_failure_count += 1
            if result.outcome is RunWeaponryOutcome.SUCCEEDED:
                self.succeeded += 1
                if not codes and result.selected_evidence_count == 0:
                    self.business_zero += 1
            elif result.outcome is RunWeaponryOutcome.FAILED and not (provider or contract):
                self.other_failed += 1
            if provider:
                self.provider_capacity += 1
            if contract:
                self.input_contract += 1

    def snapshot(self) -> tuple[int, int, int, int, int, int, int]:
        with self._lock:
            return (
                self.execution_count,
                self.execution_failure_count,
                self.succeeded,
                self.provider_capacity,
                self.business_zero,
                self.input_contract,
                self.other_failed,
            )


class WeaponryV2Maintenance:
    """周期扫描是恢复真相；唤醒 Event 只减少终态后的等待时间。"""

    def __init__(
        self,
        *,
        callbacks: WeaponryCallbackPort,
        resources: WeaponryResourceRecoveryService,
        config: WeaponryRuntimeConfig,
    ) -> None:
        if not isinstance(callbacks, WeaponryCallbackPort):
            raise TypeError("callbacks 必须实现 WeaponryCallbackPort")
        if not isinstance(resources, WeaponryResourceRecoveryService):
            raise TypeError("resources 必须是 WeaponryResourceRecoveryService")
        if not isinstance(config, WeaponryRuntimeConfig):
            raise TypeError("config 必须是 WeaponryRuntimeConfig")
        self._callbacks = callbacks
        self._resources = resources
        self._limit = config.maintenance_limit
        self._lock = Lock()
        self._state = "new"
        self._callback_count = 0
        self._callback_failures = 0
        self._resource_count = 0
        self._resource_failures = 0
        self._scheduler = LocalMaintenanceScheduler(
            jobs=(
                LocalMaintenanceJob(
                    _CALLBACK_JOB,
                    config.maintenance_interval_seconds,
                    self._sweep_callbacks,
                ),
                LocalMaintenanceJob(
                    _RESOURCE_JOB,
                    config.maintenance_interval_seconds,
                    self._sweep_resources,
                ),
            ),
            stop_grace_seconds=config.stop_timeout_seconds,
            thread_name="weaponry-v2-maintenance",
        )

    @property
    def callbacks(self) -> WeaponryCallbackPort:
        return self._callbacks

    @property
    def resources(self) -> WeaponryResourceRecoveryService:
        return self._resources

    def start(self) -> None:
        with self._lock:
            if self._state != "new":
                raise RuntimeError("Weaponry v2 Maintenance 只能启动一次")
            self._state = "starting"
        try:
            self._scheduler.start()
        except Exception:
            with self._lock:
                self._state = "stopped"
            raise
        with self._lock:
            self._state = "running"
        logger.info("Weaponry v2 独立维护已启动: bounded_limit=%d", self._limit)

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
            raise RuntimeError("Weaponry v2 Maintenance 仍有线程未停止")
        with self._lock:
            self._state = "closed"

    def snapshot(self) -> WeaponryV2MaintenanceSnapshot:
        with self._lock:
            return WeaponryV2MaintenanceSnapshot(
                self._scheduler.thread_count,
                self._callback_count,
                self._callback_failures,
                self._resource_count,
                self._resource_failures,
                self._scheduler.is_healthy(),
            )

    def _sweep_callbacks(self) -> None:
        try:
            result = self._callbacks.freeze_expired(limit=self._limit)
            if not isinstance(result, WeaponryCallbackGuardSweepResult):
                raise TypeError("Callback Guard sweep 返回类型错误")
            with self._lock:
                self._callback_count += 1
        except Exception:
            with self._lock:
                self._callback_failures += 1
            logger.exception("Weaponry v2 Callback Guard 维护失败")

    def _sweep_resources(self) -> None:
        try:
            result = self._resources.run_once(limit=self._limit)
            if not isinstance(result, WeaponryResourceRecoverySweepResult):
                raise TypeError("资源恢复 sweep 返回类型错误")
            with self._lock:
                self._resource_count += 1
        except Exception:
            with self._lock:
                self._resource_failures += 1
            logger.exception("Weaponry v2 资源维护失败")


class WeaponryV2TaskDispatcher:
    """保留现有容器生命周期形状，执行权仅来自 v2 SQLite claim。"""

    def __init__(
        self,
        *,
        executor: LocalTaskExecutor,
        maintenance: WeaponryV2Maintenance,
        metrics: WeaponryV2ResultMetrics,
        worker_count: int,
        startup_gate: Callable[[], None] | None = None,
    ) -> None:
        if not isinstance(executor, LocalTaskExecutor):
            raise TypeError("executor 必须是 LocalTaskExecutor")
        if not isinstance(maintenance, WeaponryV2Maintenance):
            raise TypeError("maintenance 必须是 WeaponryV2Maintenance")
        if not isinstance(metrics, WeaponryV2ResultMetrics):
            raise TypeError("metrics 必须是 WeaponryV2ResultMetrics")
        if type(worker_count) is not int or worker_count < 1:
            raise ValueError("worker_count 必须是正整数")
        if startup_gate is not None and not callable(startup_gate):
            raise TypeError("startup_gate 必须可调用或为 None")
        self._executor = executor
        self._maintenance = maintenance
        self._metrics = metrics
        self._worker_count = worker_count
        self._startup_gate = startup_gate or (lambda: None)
        self._lock = Lock()
        self._state = "new"
        self._dispatch_count = 0
        self._merged_count = 0

    @property
    def callbacks(self) -> WeaponryCallbackPort:
        return self._maintenance.callbacks

    @property
    def execution_limiter(self) -> TaskExecutionPermitPort:
        """仅供组合根校验容量池归属，不允许调用方绕过 Executor 领取许可。"""

        return self._executor.execution_limiter

    @property
    def uses_task_control_authority(self) -> bool:
        """声明领取安全性来自持久 lease/fencing，而不是旧进程文件锁。"""

        return True

    @property
    def resources(self) -> WeaponryResourceRecoveryService:
        return self._maintenance.resources

    def dispatch(self, task_id: TaskId) -> None:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        with self._lock:
            self._dispatch_count += 1
            if self._state != "running":
                self._merged_count += 1
        self._executor.wake_up()

    def wake_maintenance(self) -> None:
        self._maintenance.wake_up()

    def start(self) -> None:
        with self._lock:
            if self._state != "new":
                raise RuntimeError("Weaponry v2 Dispatcher 只能启动一次")
            self._state = "starting"
        maintenance_started = False
        try:
            # 术语目录与生产能力门禁必须早于任何维护/Worker 线程；失败时没有
            # 部分启动，也不会领取 Task。
            self._startup_gate()
            self._maintenance.start()
            maintenance_started = True
            self._executor.start()
        except Exception:
            if maintenance_started:
                try:
                    if not self._maintenance.stop():
                        logger.critical(
                            "Weaponry v2 Executor 启动失败后的维护停机未完成"
                        )
                except Exception:
                    logger.critical(
                        "Weaponry v2 Executor 启动失败后的维护停机异常",
                        exc_info=True,
                    )
            with self._lock:
                self._state = "stopped"
            raise
        with self._lock:
            self._state = "running"
        logger.info("Weaponry v2 Dispatcher 已启动")

    def stop(self, *, timeout_seconds: float | None = None) -> bool:
        if timeout_seconds is None:
            deadline = None
        else:
            if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
                raise TypeError("timeout_seconds 必须是数字或 None")
            timeout = float(timeout_seconds)
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError("timeout_seconds 必须是正有限数字")
            deadline = time.monotonic() + timeout
        with self._lock:
            if self._state in {"stopped", "closed"}:
                return True
            if self._state not in {"starting", "running", "stopping"}:
                raise RuntimeError("Weaponry v2 Dispatcher 尚未启动")
            self._state = "stopping"
        executor_stopped = False
        maintenance_stopped = False
        executor_error: Exception | None = None
        maintenance_error: Exception | None = None
        try:
            executor_timeout = (
                None
                if deadline is None
                else max(0.000001, deadline - time.monotonic())
            )
            try:
                executor_stopped = self._executor.stop(
                    timeout_seconds=executor_timeout
                )
            except Exception as exc:
                executor_error = exc

            # 两类线程没有可共同回滚的事务。即使 Executor 停机异常，也必须继续尝试
            # 停止维护扫描；显式 deadline 下，维护只消费剩余预算。
            maintenance_timeout = (
                None
                if deadline is None
                else max(0.0, deadline - time.monotonic())
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
                    "Weaponry v2 Dispatcher 双通道停机异常: "
                    "executor_error=%s maintenance_error=%s",
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
        if state in {"starting", "running", "stopping"} and not self.stop():
            raise RuntimeError("Weaponry v2 Dispatcher 仍有后台线程未停止")
        elif state == "new":
            self._maintenance.close()
        with self._lock:
            self._state = "closed"

    def snapshot(self) -> LocalWeaponryDispatcherSnapshot:
        with self._lock:
            state = self._state
            dispatch_count = self._dispatch_count
            merged = self._merged_count
        maintenance = self._maintenance.snapshot()
        (
            execution_count,
            execution_failures,
            succeeded,
            provider,
            zero,
            contract,
            failed,
        ) = self._metrics.snapshot()
        healthy = self._executor.is_healthy() and maintenance.healthy
        return LocalWeaponryDispatcherSnapshot(
            lifecycle_state=state,
            worker_thread_count=self._worker_count if state in {"starting", "running", "stopping"} else 0,
            maintenance_thread_count=maintenance.thread_count,
            buffered_task_count=0,
            waiting_task_id=None,
            current_task_id=None,
            dispatch_count=dispatch_count,
            merged_wakeup_count=merged,
            scan_count=0,
            execution_count=execution_count,
            execution_failure_count=execution_failures,
            accepted_deferral_count=0,
            accepted_deferral_failure_count=0,
            queue_inspection_count=0,
            queue_inspection_failure_count=0,
            resource_maintenance_count=maintenance.resource_count,
            resource_maintenance_failure_count=maintenance.resource_failure_count,
            callback_guard_maintenance_count=maintenance.callback_count,
            callback_guard_maintenance_failure_count=maintenance.callback_failure_count,
            succeeded_result_count=succeeded,
            provider_capacity_error_count=provider,
            business_zero_result_count=zero,
            input_contract_error_count=contract,
            other_failed_result_count=failed,
            ready=state == "running" and healthy,
            fatal_error="" if healthy else "weaponry_v2_runtime_unhealthy",
        )


__all__ = [
    "WeaponryV2Maintenance",
    "WeaponryV2ResultMetrics",
    "WeaponryV2TaskDispatcher",
]
