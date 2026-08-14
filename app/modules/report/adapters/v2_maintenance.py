"""Report v2 Callback Guard 与资源恢复的独立持久扫描装配。"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
from threading import Lock

from app.modules.report.adapters.runtime_config import ReportRuntimeConfig
from app.modules.report.ports import (
    ReportCallbackGuardSweepResult,
    ReportCallbackPort,
    ReportResourceRecoveryPort,
    ReportResourceSweepResult,
)
from app.modules.tasks.adapters import LocalMaintenanceJob, LocalMaintenanceScheduler


logger = logging.getLogger(__name__)

_CALLBACK_JOB_NAME = "report_callback_guard_sweep"
_RESOURCE_JOB_NAME = "report_resource_recovery"


@dataclass(frozen=True, slots=True)
class ReportV2MaintenanceSnapshot:
    """不含业务正文、URL 或租约 Token 的内部维护指标快照。"""

    thread_count: int
    callback_guard_sweep_count: int
    callback_guard_sweep_failure_count: int
    callback_guard_frozen_count: int
    resource_sweep_count: int
    resource_sweep_failure_count: int
    healthy: bool


class ReportV2Maintenance:
    """把两个独立状态机接入同一合并唤醒/周期扫描器。

    Callback Job 只调用 Callback Control Store 的过期冻结，不获取发送权、不执行 HTTP、
    不自动重发 unknown。Resource Job 只从 Report Resource Store 扫描 terminal 后的
    tracking/pending 事实并收口外部资源。两个 Job 分别捕获故障和计数，任一批次失败
    都不会跳过或永久关闭另一个状态机；周期扫描才是恢复保障，Event 只是可丢提示。
    """

    def __init__(
        self,
        *,
        callbacks: ReportCallbackPort,
        resources: ReportResourceRecoveryPort,
        config: ReportRuntimeConfig,
    ) -> None:
        if not isinstance(callbacks, ReportCallbackPort):
            raise TypeError("callbacks 必须实现 ReportCallbackPort")
        if not isinstance(resources, ReportResourceRecoveryPort):
            raise TypeError("resources 必须实现 ReportResourceRecoveryPort")
        if not isinstance(config, ReportRuntimeConfig):
            raise TypeError("config 必须是 ReportRuntimeConfig")
        self._callbacks = callbacks
        self._resources = resources
        self._limit = config.resource_sweep_limit
        self._lock = Lock()
        self._state = "new"
        self._callback_guard_sweep_count = 0
        self._callback_guard_sweep_failure_count = 0
        self._callback_guard_frozen_count = 0
        self._resource_sweep_count = 0
        self._resource_sweep_failure_count = 0
        self._scheduler = LocalMaintenanceScheduler(
            jobs=(
                LocalMaintenanceJob(
                    _CALLBACK_JOB_NAME,
                    config.resource_sweep_interval_seconds,
                    self._sweep_callback_guards,
                ),
                LocalMaintenanceJob(
                    _RESOURCE_JOB_NAME,
                    config.resource_sweep_interval_seconds,
                    self._sweep_resources,
                ),
            ),
            stop_grace_seconds=config.stop_timeout_seconds,
            thread_name="report-v2-maintenance",
        )

    @property
    def callbacks(self) -> ReportCallbackPort:
        """返回唯一 Guard Adapter，供组合根校验 Worker/check-task/维护身份。"""

        return self._callbacks

    @property
    def resources(self) -> ReportResourceRecoveryPort:
        """返回唯一资源恢复状态机，供组合根阻止生产装配出第二套 Store 链。"""

        return self._resources

    def start(self) -> None:
        with self._lock:
            if self._state != "new":
                raise RuntimeError("Report v2 Maintenance 只能启动一次")
            self._state = "starting"
        try:
            self._scheduler.start()
        except Exception:
            with self._lock:
                self._state = "stopped"
            raise
        with self._lock:
            self._state = "running"
        logger.info(
            "Report v2 独立维护已启动: job_count=2 bounded_limit=%d",
            self._limit,
        )

    def wake_up(self) -> None:
        """合并一次维护提示；提示不携带 TaskId，也不授予任何写权限。"""

        self._scheduler.wake_up()

    def stop(self, *, timeout_seconds: float | None = None) -> bool:
        # 先完成参数校验，再改变生命周期状态。否则调用方误传参数时，实例会被永久
        # 卡在 stopping，既无法重试合法停机，也无法通过 close() 收敛线程。
        if timeout_seconds is not None:
            if isinstance(timeout_seconds, bool) or not isinstance(
                timeout_seconds,
                (int, float),
            ):
                raise TypeError("timeout_seconds 必须是数字或 None")
            normalized_timeout = float(timeout_seconds)
            if not math.isfinite(normalized_timeout) or normalized_timeout < 0:
                raise ValueError("timeout_seconds 必须是非负有限数字")
        else:
            normalized_timeout = None
        with self._lock:
            if self._state in {"stopped", "closed"}:
                return True
            if self._state not in {"starting", "running", "stopping"}:
                raise RuntimeError("Report v2 Maintenance 尚未启动")
            self._state = "stopping"
        try:
            stopped = self._scheduler.stop(timeout_seconds=normalized_timeout)
        except Exception:
            # 底层异常时不能假称线程已退出；保留 stopping 允许调用方再次尽力收敛。
            with self._lock:
                self._state = "stopping"
            raise
        with self._lock:
            self._state = "stopped" if stopped else "stopping"
        return stopped

    def close(self) -> None:
        with self._lock:
            state = self._state
        if state in {"starting", "running", "stopping"}:
            if not self.stop():
                raise RuntimeError("Report v2 Maintenance 仍有线程未停止")
        with self._lock:
            self._state = "closed"

    def is_healthy(self) -> bool:
        return self._scheduler.is_healthy()

    def snapshot(self) -> ReportV2MaintenanceSnapshot:
        with self._lock:
            return ReportV2MaintenanceSnapshot(
                thread_count=self._scheduler.thread_count,
                callback_guard_sweep_count=self._callback_guard_sweep_count,
                callback_guard_sweep_failure_count=(
                    self._callback_guard_sweep_failure_count
                ),
                callback_guard_frozen_count=self._callback_guard_frozen_count,
                resource_sweep_count=self._resource_sweep_count,
                resource_sweep_failure_count=self._resource_sweep_failure_count,
                healthy=self._scheduler.is_healthy(),
            )

    def _sweep_callback_guards(self) -> None:
        try:
            result = self._callbacks.freeze_expired(limit=self._limit)
            if not isinstance(result, ReportCallbackGuardSweepResult):
                raise TypeError("Callback Guard sweep 必须返回强类型结果")
            with self._lock:
                self._callback_guard_sweep_count += 1
                self._callback_guard_frozen_count += result.frozen_count
            logger.log(
                logging.WARNING if result.frozen_count else logging.DEBUG,
                "Report v2 Callback Guard 有界维护完成: scanned=%d frozen=%d",
                result.scanned_count,
                result.frozen_count,
            )
        except Exception:
            with self._lock:
                self._callback_guard_sweep_failure_count += 1
            logger.exception(
                "Report v2 Callback Guard 维护失败，资源维护与后续周期不受影响"
            )

    def _sweep_resources(self) -> None:
        try:
            result = self._resources.sweep(limit=self._limit)
            if not isinstance(result, ReportResourceSweepResult):
                raise TypeError("资源恢复 sweep 必须返回 ReportResourceSweepResult")
            with self._lock:
                self._resource_sweep_count += 1
            logger.log(
                logging.ERROR if result.failed_task_ids else logging.DEBUG,
                "Report v2 资源有界维护完成: scanned=%d cleaned=%d pending=%d "
                "quarantined=%d not_ready=%d missing=%d failed=%d",
                result.scanned_count,
                result.cleaned_count,
                result.pending_count,
                result.quarantined_count,
                result.not_ready_count,
                result.missing_count,
                len(result.failed_task_ids),
            )
        except Exception:
            with self._lock:
                self._resource_sweep_failure_count += 1
            logger.exception(
                "Report v2 资源维护失败，Callback Guard 与后续周期不受影响"
            )


__all__ = ["ReportV2Maintenance", "ReportV2MaintenanceSnapshot"]
