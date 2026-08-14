"""有界内存、可合并唤醒的阶段 2 本地 Task Executor 内核。"""

from __future__ import annotations

import logging
from threading import Event, Lock, Thread
import time
from typing import Callable

from app.modules.tasks.domain import TaskId, add_persisted_utc_seconds
from app.modules.tasks.ports import (
    ClockPort,
    TaskControlQueryUnitOfWorkFactory,
    TaskExecutionPermitPort,
    TaskExecutionUnitOfWorkFactory,
    TaskExecutionMutationOutcome,
    TaskDispatchDeferralCommand,
    TaskExecutionRuntimeOutcome,
    TaskExecutionRuntimePort,
)


logger = logging.getLogger(__name__)


class LocalTaskExecutor:
    """一个实现按 task_type 分别实例化；不缓存 accepted 全量队列。"""

    def __init__(
        self,
        *,
        task_type: str,
        worker_count: int,
        scan_interval_seconds: float,
        stop_grace_seconds: float,
        clock: ClockPort,
        query_uow_factory: TaskControlQueryUnitOfWorkFactory,
        execution_uow_factory: TaskExecutionUnitOfWorkFactory,
        permit: TaskExecutionPermitPort,
        runtime_factory: Callable[[str], TaskExecutionRuntimePort],
        thread_name_prefix: str,
        dispatch_failure_cooldown_seconds: float = 30,
    ) -> None:
        if not isinstance(task_type, str) or not task_type.strip():
            raise ValueError("task_type 必须是非空 str")
        if type(worker_count) is not int or worker_count <= 0:
            raise ValueError("worker_count 必须是正整数")
        for name, value in (
            ("scan_interval_seconds", scan_interval_seconds),
            ("stop_grace_seconds", stop_grace_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} 必须是正数")
        if not isinstance(clock, ClockPort):
            raise TypeError("clock 必须实现 ClockPort")
        if (
            not callable(query_uow_factory)
            or not callable(execution_uow_factory)
            or not callable(runtime_factory)
        ):
            raise TypeError("Query/Execution UoW Factory 与 runtime_factory 必须可调用")
        if not isinstance(permit, TaskExecutionPermitPort):
            raise TypeError("permit 必须实现 TaskExecutionPermitPort")
        self._task_type = task_type.strip()
        self._worker_count = worker_count
        self._scan_interval = float(scan_interval_seconds)
        self._stop_grace = float(stop_grace_seconds)
        self._clock = clock
        self._query_uow_factory = query_uow_factory
        self._execution_uow_factory = execution_uow_factory
        self._permit = permit
        self._runtime_factory = runtime_factory
        self._thread_name_prefix = thread_name_prefix.strip()
        if dispatch_failure_cooldown_seconds <= 0:
            raise ValueError("dispatch_failure_cooldown_seconds 必须大于 0")
        self._dispatch_failure_cooldown = float(dispatch_failure_cooldown_seconds)
        self._wake = Event()
        self._stopping = Event()
        self._lock = Lock()
        self._coordinator: Thread | None = None
        self._workers: dict[int, tuple[TaskId, Thread]] = {}
        self._active_runtimes: dict[int, TaskExecutionRuntimePort] = {}
        self._healthy = True
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                raise RuntimeError("LocalTaskExecutor 不可重复启动")
            self._started = True
            self._coordinator = Thread(
                target=self._run_loop,
                name=f"{self._thread_name_prefix}-coordinator",
                daemon=False,
            )
            self._coordinator.start()

    def wake_up(self) -> None:
        # Event 天然合并重复提示；持久化 scan 才是恢复真相。
        if not self._stopping.is_set():
            self._wake.set()

    def stop(self, *, timeout_seconds: float | None = None) -> bool:
        """停止领取并有限等待；超时只返回 False，绝不重置持久 Task。"""

        with self._lock:
            if not self._started or self._coordinator is None:
                raise RuntimeError("LocalTaskExecutor 尚未启动")
            coordinator = self._coordinator
        self._stopping.set()
        self._wake.set()
        with self._lock:
            active_runtimes = tuple(self._active_runtimes.values())
        for runtime in active_runtimes:
            try:
                runtime.request_cancellation()
            except Exception as exc:
                with self._lock:
                    self._healthy = False
                logger.error(
                    "LocalTaskExecutor 取消探针通知失败: task_type=%s "
                    "reason_code=executor_cancel_probe_error error_type=%s",
                    self._task_type,
                    type(exc).__name__,
                )
        if timeout_seconds is None:
            effective_timeout = self._stop_grace
        else:
            if isinstance(timeout_seconds, bool) or not isinstance(
                timeout_seconds, (int, float)
            ):
                raise TypeError("timeout_seconds 必须是数字或 None")
            effective_timeout = float(timeout_seconds)
            if effective_timeout <= 0:
                raise ValueError("timeout_seconds 必须大于 0")
        deadline = time.monotonic() + effective_timeout
        coordinator.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._lock:
            worker_threads = tuple(thread for _, thread in self._workers.values())
        for thread in worker_threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if coordinator.is_alive() or any(thread.is_alive() for thread in worker_threads):
            with self._lock:
                self._healthy = False
            logger.critical(
                "LocalTaskExecutor 未在 grace 内完全停止: task_type=%s "
                "reason_code=executor_stop_timeout",
                self._task_type,
            )
            return False
        return True

    def is_healthy(self) -> bool:
        with self._lock:
            return self._healthy

    @property
    def inflight_count(self) -> int:
        with self._lock:
            return len(self._workers)

    def _run_loop(self) -> None:
        try:
            while not self._stopping.is_set():
                self._reap_finished_workers()
                self._scan_once()
                self._wake.wait(timeout=self._scan_interval)
                self._wake.clear()
        except Exception as exc:
            with self._lock:
                self._healthy = False
            self._stopping.set()
            logger.error(
                "LocalTaskExecutor 扫描失败并停止新领取: task_type=%s "
                "reason_code=executor_scan_error error_type=%s",
                self._task_type,
                type(exc).__name__,
            )
        finally:
            self._reap_finished_workers()

    def _reap_finished_workers(self) -> None:
        with self._lock:
            finished = [slot for slot, (_, thread) in self._workers.items() if not thread.is_alive()]
            for slot in finished:
                self._workers.pop(slot, None)

    def _scan_once(self) -> None:
        if self._stopping.is_set():
            return
        with self._lock:
            free_slots = [slot for slot in range(self._worker_count) if slot not in self._workers]
            inflight_ids = {task_id for task_id, _thread in self._workers.values()}
        if not free_slots:
            return
        now = self._clock.now_utc()
        with self._query_uow_factory() as unit_of_work:
            task_ids = unit_of_work.queries.scan_runnable(
                self._task_type,
                not_after=now,
                # 最多读取一个 worker 窗口，并在进程内排除尚未完成 claim 的在途 ID；
                # 该集合大小始终不超过 worker_count，不会随持久积压增长。
                limit=self._worker_count,
            )
        eligible = tuple(task_id for task_id in task_ids if task_id not in inflight_ids)
        for slot, task_id in zip(free_slots, eligible):
            if self._stopping.is_set():
                return
            thread = Thread(
                target=self._run_task,
                args=(slot, task_id),
                name=f"{self._thread_name_prefix}-worker-{slot}",
                daemon=False,
            )
            with self._lock:
                if self._stopping.is_set():
                    return
                self._workers[slot] = (task_id, thread)
            thread.start()

    def _run_task(self, slot: int, task_id: TaskId) -> None:
        acquired = False
        try:
            acquired = self._permit.acquire_interruptibly(
                self._stopping.is_set,
                poll_interval_seconds=min(self._scan_interval, 0.5),
            )
            if not acquired or self._stopping.is_set():
                return
            runtime = self._runtime_factory(f"worker-{slot}")
            if not isinstance(runtime, TaskExecutionRuntimePort):
                raise TypeError("runtime_factory 返回值未实现 TaskExecutionRuntimePort")
            with self._lock:
                self._active_runtimes[slot] = runtime
            if self._stopping.is_set():
                runtime.request_cancellation()
            result = runtime.run(task_id)
            if result.outcome in {
                TaskExecutionRuntimeOutcome.INPUT_ERROR,
                TaskExecutionRuntimeOutcome.INFRASTRUCTURE_ERROR,
            }:
                self._persist_dispatch_cooldown(
                    task_id,
                    reason_code=f"runtime_{result.outcome.value}",
                )
            if result.outcome in {
                TaskExecutionRuntimeOutcome.CLOCK_UNSAFE,
                TaskExecutionRuntimeOutcome.INFRASTRUCTURE_ERROR,
            }:
                with self._lock:
                    self._healthy = False
                self._stopping.set()
                self._wake.set()
        except Exception as exc:
            logger.error(
                "LocalTaskExecutor Worker 异常: task_type=%s task_id=%s worker_slot=%d "
                "reason_code=executor_worker_error error_type=%s",
                self._task_type,
                task_id,
                slot,
                type(exc).__name__,
            )
        finally:
            with self._lock:
                self._active_runtimes.pop(slot, None)
            if acquired:
                self._permit.release()
            self._wake.set()

    def _persist_dispatch_cooldown(self, task_id: TaskId, *, reason_code: str) -> None:
        """仅 accepted/latest Task 会应用；已 claim 的运行中 Task 交给 Reaper 收敛。"""

        deferred_at = self._clock.now_utc()
        command = TaskDispatchDeferralCommand(
            task_id=task_id,
            task_type=self._task_type,
            reason_code=reason_code,
            deferred_at=deferred_at,
            next_dispatch_at=add_persisted_utc_seconds(
                deferred_at,
                seconds=self._dispatch_failure_cooldown,
            ),
        )
        with self._execution_uow_factory() as unit_of_work:
            outcome = unit_of_work.execution.defer_dispatch(command)
            if outcome is TaskExecutionMutationOutcome.APPLIED:
                unit_of_work.commit()
                logger.warning(
                    "LocalTaskExecutor 已持久化派发冷却: task_type=%s task_id=%s "
                    "reason_code=%s",
                    self._task_type,
                    task_id,
                    reason_code,
                )


__all__ = ["LocalTaskExecutor"]
