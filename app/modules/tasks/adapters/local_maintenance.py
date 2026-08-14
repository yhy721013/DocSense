"""与业务 Executor 分离的合并唤醒/周期维护调度器。"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
from threading import Event, Lock, Thread
import time
from typing import Callable


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LocalMaintenanceJob:
    name: str
    interval_seconds: float
    action: Callable[[], None]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Maintenance job name 必须是非空 str")
        if (
            isinstance(self.interval_seconds, bool)
            or not isinstance(self.interval_seconds, (int, float))
            or not math.isfinite(float(self.interval_seconds))
            or self.interval_seconds <= 0
        ):
            raise ValueError("Maintenance interval_seconds 必须是正数")
        if not callable(self.action):
            raise TypeError("Maintenance action 必须可调用")


class LocalMaintenanceScheduler:
    """维护动作自身必须从持久状态扫描；Event 仅用于提前唤醒。"""

    def __init__(
        self,
        *,
        jobs: tuple[LocalMaintenanceJob, ...],
        stop_grace_seconds: float,
        thread_name: str = "task-maintenance-scheduler",
    ) -> None:
        if not jobs or len({job.name for job in jobs}) != len(jobs):
            raise ValueError("jobs 必须非空且名称唯一")
        if (
            isinstance(stop_grace_seconds, bool)
            or not isinstance(stop_grace_seconds, (int, float))
            or not math.isfinite(float(stop_grace_seconds))
            or stop_grace_seconds <= 0
        ):
            raise ValueError("stop_grace_seconds 必须大于 0")
        if not isinstance(thread_name, str) or not thread_name.strip():
            raise ValueError("thread_name 必须是非空 str")
        self._jobs = jobs
        self._stop_grace = float(stop_grace_seconds)
        self._thread_name = thread_name.strip()
        self._wake = Event()
        self._stopping = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._healthy = True

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("LocalMaintenanceScheduler 不可重复启动")
            self._thread = Thread(
                target=self._run,
                name=self._thread_name,
                daemon=False,
            )
            try:
                self._thread.start()
            except Exception:
                # Thread.start 失败后不能把一个从未启动的 Thread 留成“已运行”假象；
                # 调用方可以据此安全执行启动回滚，但本实例仍禁止静默重试启动。
                self._healthy = False
                raise

    def wake_up(self) -> None:
        if not self._stopping.is_set():
            self._wake.set()

    def stop(self, *, timeout_seconds: float | None = None) -> bool:
        """停止周期扫描并有限等待；超时不遗忘仍存活的维护线程。"""

        with self._lock:
            if self._thread is None:
                raise RuntimeError("LocalMaintenanceScheduler 尚未启动")
            thread = self._thread
        if timeout_seconds is None:
            effective_timeout = self._stop_grace
        else:
            if isinstance(timeout_seconds, bool) or not isinstance(
                timeout_seconds,
                (int, float),
            ):
                raise TypeError("timeout_seconds 必须是数字或 None")
            effective_timeout = float(timeout_seconds)
            if not math.isfinite(effective_timeout) or effective_timeout < 0:
                raise ValueError("timeout_seconds 必须是非负有限数字")
        self._stopping.set()
        self._wake.set()
        thread.join(timeout=effective_timeout)
        if thread.is_alive():
            with self._lock:
                self._healthy = False
            logger.critical(
                "Task Maintenance 未在 grace 内停止: thread_name=%s "
                "reason_code=maintenance_stop_timeout",
                self._thread_name,
            )
            return False
        return True

    def is_healthy(self) -> bool:
        with self._lock:
            return self._healthy

    def close(self) -> None:
        """幂等释放调度线程；允许容器在未显式 start 的离线场景直接关闭。"""

        with self._lock:
            thread = self._thread
        if thread is None or not thread.is_alive():
            return
        self.stop(timeout_seconds=self._stop_grace)

    @property
    def thread_count(self) -> int:
        """返回仍存活的调度线程数；仅用于内部 readiness/诊断快照。"""

        with self._lock:
            return int(self._thread is not None and self._thread.is_alive())

    def _run(self) -> None:
        next_runs = {job.name: 0.0 for job in self._jobs}
        while not self._stopping.is_set():
            now = time.monotonic()
            for job in self._jobs:
                if now < next_runs[job.name]:
                    continue
                try:
                    job.action()
                except Exception as exc:
                    with self._lock:
                        self._healthy = False
                    logger.error(
                        "Task Maintenance 执行失败: job=%s reason_code=maintenance_job_error "
                        "error_type=%s",
                        job.name,
                        type(exc).__name__,
                    )
                next_runs[job.name] = time.monotonic() + job.interval_seconds
            wait_for = max(0.0, min(next_runs.values()) - time.monotonic())
            triggered = self._wake.wait(timeout=wait_for)
            self._wake.clear()
            if triggered and not self._stopping.is_set():
                # 提示可以合并、可以丢，但一旦本进程收到提示就立即重新扫描所有维护源；
                # action 自身仍必须从持久状态判断是否有工作，不能依赖提示携带任务身份。
                next_runs = {job.name: 0.0 for job in self._jobs}


__all__ = ["LocalMaintenanceJob", "LocalMaintenanceScheduler"]
