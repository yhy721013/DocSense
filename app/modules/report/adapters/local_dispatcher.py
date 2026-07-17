"""SQLite 持久积压 + Event 常量空间唤醒的单实例报告 Dispatcher。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import os
import threading
import time
from typing import Any

from app.modules.report.ports import (
    ReportCallbackGuardSweepResult,
    ReportCallbackPort,
    ReportResourceRecoveryPort,
    ReportResourceSweepResult,
)
from app.modules.tasks.domain import TaskId
from app.modules.tasks.ports import (
    ProcessSingletonGuardPort,
    TaskCommandPort,
    TaskExecutionPermitPort,
    TaskQueueInspectionPort,
    TaskQueueSnapshot,
)
from app.services.core.config import ReportInfrastructureConfig


logger = logging.getLogger(__name__)

_REPORT_TASK_TYPE = "report"
_STATE_NEW = "new"
_STATE_STARTING = "starting"
_STATE_RUNNING = "running"
_STATE_STOPPING = "stopping"
_STATE_STOPPED = "stopped"
_STATE_CLOSED = "closed"

# 一个重型 execution 可以合法执行数分钟，因此队列诊断必须独立于执行 Worker。相同
# running 集合首次出现时立即告警，之后按固定窗口节流；清空后重置窗口。
_RUNNING_WARNING_INTERVAL_SECONDS = 30.0
_QUEUE_INSPECTION_INTERVAL_SECONDS = 30.0
_PERMIT_POLL_MAX_SECONDS = 0.1


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _positive_timeout(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} 必须是数字")
    normalized = float(value)
    if (
        normalized != normalized
        or normalized in (float("inf"), float("-inf"))
        or normalized <= 0.0
    ):
        raise ValueError(f"{name} 必须是正有限数字")
    return normalized


@dataclass(frozen=True)
class LocalReportDispatcherSnapshot:
    """只读运行指标；``buffered_task_count`` 恒为零以证明不保存积压列表。"""

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
    """以一个执行 Worker 持续领取数据库中的 accepted 报告任务。

    报告执行始终只有一个 Worker；资源恢复和只读队列诊断各使用一条独立维护线程，避免
    模型调用或一批清理工作阻塞另一个周期任务。``dispatch`` 仍只设置一个 Event，真实
    积压只存在于 SQLite，不会随任务数量增加 Python Queue、Future 或等待线程。
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
        if not isinstance(task_commands, TaskCommandPort):
            raise TypeError("task_commands 必须实现 TaskCommandPort")
        if not isinstance(queue_inspector, TaskQueueInspectionPort):
            raise TypeError("queue_inspector 必须实现 TaskQueueInspectionPort")
        if not isinstance(resources, ReportResourceRecoveryPort):
            raise TypeError("resources 必须实现 ReportResourceRecoveryPort")
        if not isinstance(callbacks, ReportCallbackPort):
            raise TypeError("callbacks 必须实现 ReportCallbackPort")
        if not callable(execute):
            raise TypeError("execute 必须可调用")
        if not isinstance(config, ReportInfrastructureConfig):
            raise TypeError("config 必须是 ReportInfrastructureConfig")
        if config.runtime_mode != "single_instance":
            raise RuntimeError("本地报告 Dispatcher 只支持 single_instance")
        if execution_limiter is not None and not isinstance(
            execution_limiter,
            TaskExecutionPermitPort,
        ):
            raise TypeError("execution_limiter 必须实现 TaskExecutionPermitPort")
        if process_guard is not None and not isinstance(
            process_guard,
            ProcessSingletonGuardPort,
        ):
            raise TypeError("process_guard 必须实现 ProcessSingletonGuardPort")
        if not callable(monotonic) or not callable(wall_clock):
            raise TypeError("Dispatcher 时钟必须可调用")

        self._task_commands = task_commands
        self._queue_inspector = queue_inspector
        self._resources = resources
        self._callbacks = callbacks
        self._execute = execute
        self._config = config
        self._execution_limiter = execution_limiter
        self._process_guard = process_guard
        self._monotonic = monotonic
        self._wall_clock = wall_clock

        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._state_lock = threading.RLock()
        self._worker_thread: threading.Thread | None = None
        self._resource_thread: threading.Thread | None = None
        self._queue_thread: threading.Thread | None = None
        self._worker_finished = threading.Event()
        self._resource_finished = threading.Event()
        self._queue_finished = threading.Event()
        self._worker_finished.set()
        self._resource_finished.set()
        self._queue_finished.set()
        self._guard_acquired = False
        self._owner_pid: int | None = None
        self._lifecycle_state = _STATE_NEW
        self._waiting_task_id: TaskId | None = None
        self._current_task_id: TaskId | None = None
        self._dispatch_count = 0
        self._merged_wakeup_count = 0
        self._scan_count = 0
        self._execution_count = 0
        self._execution_failure_count = 0
        self._accepted_deferral_count = 0
        self._accepted_deferral_failure_count = 0
        self._resource_sweep_count = 0
        self._resource_sweep_failure_count = 0
        self._queue_inspection_count = 0
        self._queue_inspection_failure_count = 0
        self._callback_guard_sweep_count = 0
        self._callback_guard_sweep_failure_count = 0
        self._callback_guard_frozen_count = 0
        self._fatal_error = ""
        self._next_running_warning_at = 0.0

    @property
    def has_process_guard(self) -> bool:
        """供组合根校验生产 Dispatcher 是否具备真正的跨进程门禁。"""

        return self._process_guard is not None

    def dispatch(self, task_id: TaskId) -> None:
        """合并唤醒信号；TaskId 仅用于校验和日志，不进入任何等待容器。"""

        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        with self._state_lock:
            self._raise_if_forked_locked()
            if self._lifecycle_state in {
                _STATE_STOPPING,
                _STATE_STOPPED,
                _STATE_CLOSED,
            }:
                raise RuntimeError("报告 Dispatcher 已停止接收唤醒")
            already_pending = self._wake_event.is_set()
            self._dispatch_count += 1
            if already_pending:
                self._merged_wakeup_count += 1
            self._wake_event.set()
        logger.debug(
            "报告 Dispatcher 已接收常量空间唤醒: task_id=%s merged=%s",
            task_id,
            already_pending,
        )

    def start(self) -> None:
        """幂等启动执行 Worker 与两个维护循环；构造对象本身不创建线程。"""

        started_threads: list[threading.Thread] = []
        try:
            with self._state_lock:
                if self._lifecycle_state in {_STATE_STARTING, _STATE_RUNNING}:
                    self._raise_if_forked_locked()
                    return
                if self._lifecycle_state in {
                    _STATE_STOPPING,
                    _STATE_STOPPED,
                    _STATE_CLOSED,
                }:
                    raise RuntimeError("已停止的报告 Dispatcher 不得重新启动")
                if self._process_guard is not None:
                    if not self._process_guard.acquire():
                        raise RuntimeError(
                            "报告 Dispatcher 单实例进程锁已被其他进程占用"
                        )
                    self._guard_acquired = True

                self._owner_pid = os.getpid()
                self._stop_event.clear()
                self._wake_event.clear()
                self._worker_finished.clear()
                self._resource_finished.clear()
                self._queue_finished.clear()
                self._lifecycle_state = _STATE_STARTING
                self._worker_thread = threading.Thread(
                    target=self._run_worker,
                    name="docsense-report-worker",
                    daemon=True,
                )
                self._resource_thread = threading.Thread(
                    target=self._run_resource_sweeper,
                    name="docsense-report-resource-sweeper",
                    daemon=True,
                )
                self._queue_thread = threading.Thread(
                    target=self._run_queue_inspector,
                    name="docsense-report-queue-inspector",
                    daemon=True,
                )
                for thread in self._threads_locked():
                    thread.start()
                    started_threads.append(thread)
                self._lifecycle_state = _STATE_RUNNING
        except Exception:
            self._stop_event.set()
            self._wake_event.set()
            with self._state_lock:
                # thread.start 本身极少失败，但一旦发生，未启动线程永远不会进入 finally。
                # 必须显式补齐其完成标记，否则最后一个已启动线程退出后无法释放进程锁。
                if self._worker_thread not in started_threads:
                    self._worker_finished.set()
                if self._resource_thread not in started_threads:
                    self._resource_finished.set()
                if self._queue_thread not in started_threads:
                    self._queue_finished.set()
                self._lifecycle_state = _STATE_STOPPING
            for thread in started_threads:
                thread.join(timeout=self._config.stop_timeout_seconds)
            self._complete_stop_if_finished()
            raise

        logger.info(
            "报告 Dispatcher 已启动: runtime_mode=%s accepted_batch_size=%d "
            "scan_interval_seconds=%.3f dispatch_failure_retry_seconds=%.3f "
            "resource_sweep_limit=%d process_guard=%s",
            self._config.runtime_mode,
            self._config.accepted_batch_size,
            self._config.scan_interval_seconds,
            self._config.dispatch_failure_retry_seconds,
            self._config.resource_sweep_limit,
            self.has_process_guard,
        )

    def stop(self, *, timeout_seconds: float | None = None) -> bool:
        """停止领取新任务，并以一个总超时等待执行与维护线程退出。"""

        timeout = (
            self._config.stop_timeout_seconds
            if timeout_seconds is None
            else _positive_timeout(timeout_seconds, name="timeout_seconds")
        )
        with self._state_lock:
            self._raise_if_forked_locked()
            threads = self._threads_locked()
            alive_threads = tuple(thread for thread in threads if thread.is_alive())
            if self._lifecycle_state in {_STATE_STOPPED, _STATE_CLOSED}:
                if not alive_threads:
                    return True
                logger.critical(
                    "Dispatcher 终态仍存在活动线程，已恢复 stopping 继续等待: state=%s",
                    self._lifecycle_state,
                )
            if self._lifecycle_state == _STATE_NEW:
                self._lifecycle_state = _STATE_STOPPED
                self._stop_event.set()
                self._wake_event.set()
                return True
            self._lifecycle_state = _STATE_STOPPING
            self._stop_event.set()
            self._wake_event.set()

        current = threading.current_thread()
        if any(thread is current for thread in alive_threads):
            logger.error("报告 Dispatcher 不得从自身后台线程同步等待停止")
            return False

        deadline = time.monotonic() + timeout
        for thread in alive_threads:
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(timeout=remaining)

        still_alive = tuple(thread for thread in alive_threads if thread.is_alive())
        if still_alive:
            with self._state_lock:
                current_task_id = self._current_task_id
                waiting_task_id = self._waiting_task_id
            logger.critical(
                "报告 Dispatcher 停止等待超时，不伪造线程已退出或回退任务状态: "
                "timeout_seconds=%.3f current_task_id=%s waiting_task_id=%s "
                "alive_threads=%s",
                timeout,
                current_task_id or "-",
                waiting_task_id or "-",
                ",".join(thread.name for thread in still_alive),
            )
            return False

        self._complete_stop_if_finished()
        logger.info("报告 Dispatcher 已停止")
        return True

    def close(self) -> None:
        """幂等关闭；若有限等待超时，保留 stopping 而不是伪造 closed。"""

        with self._state_lock:
            if self._lifecycle_state == _STATE_CLOSED:
                return
        stopped = self.stop(timeout_seconds=self._config.stop_timeout_seconds)
        if not stopped:
            logger.critical(
                "报告 Dispatcher 关闭等待超时，生命周期保持 stopping，等待进程级隔离"
            )
            return
        with self._state_lock:
            self._lifecycle_state = _STATE_CLOSED

    def snapshot(self) -> LocalReportDispatcherSnapshot:
        """返回线程安全运行快照，供离线验收和监控采集。"""

        with self._state_lock:
            worker_count = int(
                self._worker_thread is not None and self._worker_thread.is_alive()
            )
            maintenance_count = sum(
                int(thread is not None and thread.is_alive())
                for thread in (self._resource_thread, self._queue_thread)
            )
            return LocalReportDispatcherSnapshot(
                lifecycle_state=self._lifecycle_state,
                worker_thread_count=worker_count,
                maintenance_thread_count=maintenance_count,
                buffered_task_count=0,
                waiting_task_id=self._waiting_task_id,
                current_task_id=self._current_task_id,
                dispatch_count=self._dispatch_count,
                merged_wakeup_count=self._merged_wakeup_count,
                scan_count=self._scan_count,
                execution_count=self._execution_count,
                execution_failure_count=self._execution_failure_count,
                accepted_deferral_count=self._accepted_deferral_count,
                accepted_deferral_failure_count=(
                    self._accepted_deferral_failure_count
                ),
                resource_sweep_count=self._resource_sweep_count,
                resource_sweep_failure_count=self._resource_sweep_failure_count,
                queue_inspection_count=self._queue_inspection_count,
                queue_inspection_failure_count=(
                    self._queue_inspection_failure_count
                ),
                callback_guard_sweep_count=self._callback_guard_sweep_count,
                callback_guard_sweep_failure_count=(
                    self._callback_guard_sweep_failure_count
                ),
                callback_guard_frozen_count=self._callback_guard_frozen_count,
                ready=(
                    self._lifecycle_state == _STATE_RUNNING
                    and not self._fatal_error
                ),
                fatal_error=self._fatal_error,
            )

    def _run_worker(self) -> None:
        """只负责 accepted 领取与执行；维护任务不得阻塞该循环。"""

        try:
            while not self._stop_event.is_set():
                # 先清除旧唤醒；扫描期间的新唤醒会重新置位，并在本轮结束后立即复扫。
                self._wake_event.clear()
                full_batch = self._scan_accepted_once()
                if self._stop_event.is_set():
                    break
                if full_batch or self._wake_event.is_set():
                    continue
                self._wake_event.wait(
                    timeout=self._config.scan_interval_seconds
                )
        except BaseException:
            self._abort_all_loops("报告 Dispatcher 执行 Worker 非预期终止")
            raise
        finally:
            with self._state_lock:
                self._waiting_task_id = None
                self._current_task_id = None
            self._worker_finished.set()
            self._complete_stop_if_finished()

    def _run_resource_sweeper(self) -> None:
        """独立执行启动恢复和固定延迟 sweep，不与重型模型执行互相阻塞。"""

        try:
            next_run_at = self._monotonic()
            while not self._stop_event.is_set():
                wait_seconds = max(0.0, next_run_at - self._monotonic())
                if self._stop_event.wait(timeout=wait_seconds):
                    break
                self._sweep_callback_guards()
                self._sweep_resources()
                # 不允许同一恢复批次重叠；周期从本批完成后重新计算。
                next_run_at = (
                    self._monotonic()
                    + self._config.resource_sweep_interval_seconds
                )
        except BaseException:
            self._abort_all_loops("报告资源恢复维护线程非预期终止")
            raise
        finally:
            self._resource_finished.set()
            self._complete_stop_if_finished()

    def _run_queue_inspector(self) -> None:
        """独立执行只读队列诊断，避免一个慢 sweep 延迟 running 告警。"""

        try:
            next_run_at = self._monotonic()
            while not self._stop_event.is_set():
                wait_seconds = max(0.0, next_run_at - self._monotonic())
                if self._stop_event.wait(timeout=wait_seconds):
                    break
                self._inspect_queue()
                next_run_at = (
                    self._monotonic() + _QUEUE_INSPECTION_INTERVAL_SECONDS
                )
        except BaseException:
            self._abort_all_loops("报告队列诊断维护线程非预期终止")
            raise
        finally:
            self._queue_finished.set()
            self._complete_stop_if_finished()

    def _scan_accepted_once(self) -> bool:
        try:
            task_ids = tuple(
                self._task_commands.list_accepted(
                    _REPORT_TASK_TYPE,
                    limit=self._config.accepted_batch_size,
                )
            )
            if len(task_ids) > self._config.accepted_batch_size:
                raise RuntimeError("accepted 扫描返回数量超过配置上限")
            if any(not isinstance(item, TaskId) for item in task_ids):
                raise TypeError("accepted 扫描只能返回 TaskId")
            if len(set(task_ids)) != len(task_ids):
                raise RuntimeError("accepted 扫描返回重复 TaskId")
        except Exception:
            logger.exception("报告 accepted 有界扫描失败，等待下一固定周期重试")
            return False

        with self._state_lock:
            self._scan_count += 1
        failures: list[tuple[TaskId, str]] = []
        completed_scan = True
        for task_id in task_ids:
            if self._stop_event.is_set():
                completed_scan = False
                break
            permit_acquired = False
            try:
                if self._execution_limiter is not None:
                    with self._state_lock:
                        self._waiting_task_id = task_id
                    permit_acquired = self._execution_limiter.acquire_interruptibly(
                        self._stop_event.is_set,
                        poll_interval_seconds=min(
                            _PERMIT_POLL_MAX_SECONDS,
                            self._config.scan_interval_seconds,
                        ),
                    )
                    if not permit_acquired:
                        completed_scan = False
                        break

                # 与 stop 共用状态锁：若 stop 先取得锁，本任务不会开始；若 Worker 先把
                # task_id 标成 current，则它已经属于停机需要等待的在途执行。
                with self._state_lock:
                    self._waiting_task_id = None
                    if self._stop_event.is_set():
                        completed_scan = False
                        break
                    self._current_task_id = task_id
                self._execute(task_id)
                with self._state_lock:
                    self._execution_count += 1
            except Exception as exc:
                error_type = f"{type(exc).__module__}.{type(exc).__qualname__}"
                failures.append((task_id, error_type[:256]))
                with self._state_lock:
                    self._execution_failure_count += 1
                logger.exception(
                    "报告 execution 执行异常，将在批次末尾按 accepted 条件持久化冷却: "
                    "task_id=%s",
                    task_id,
                )
            finally:
                with self._state_lock:
                    self._waiting_task_id = None
                    self._current_task_id = None
                if permit_acquired and self._execution_limiter is not None:
                    try:
                        self._execution_limiter.release()
                    except Exception:
                        # 许可计数已经不可信时禁止继续启动重型任务；已有数据库事实保留，
                        # 重启后可由新的、健康的 Limiter 恢复。
                        logger.critical(
                            "报告执行许可归还失败，已停止 Dispatcher 后续领取",
                            exc_info=True,
                        )
                        with self._state_lock:
                            self._fatal_error = "execution_limiter_release_failed"
                        self._stop_event.set()
                        self._wake_event.set()
                        completed_scan = False

        deferral_failed = self._defer_failed_accepted(failures)
        return (
            completed_scan
            and not self._stop_event.is_set()
            and not deferral_failed
            and len(task_ids) == self._config.accepted_batch_size
        )

    def _defer_failed_accepted(
        self,
        failures: list[tuple[TaskId, str]],
    ) -> bool:
        """对当前有界页统一计算冷却终点，让坏任务暂时让出 FIFO 首页。"""

        if not failures:
            return False
        retry_at = (
            self._aware_wall_now()
            + timedelta(seconds=self._config.dispatch_failure_retry_seconds)
        ).isoformat()
        infrastructure_failure = False
        for task_id, reason in failures:
            try:
                deferred = self._task_commands.defer_accepted(
                    task_id,
                    retry_at=retry_at,
                    reason=reason,
                )
                if not isinstance(deferred, bool):
                    raise TypeError("defer_accepted 必须返回 bool")
                if deferred:
                    with self._state_lock:
                        self._accepted_deferral_count += 1
                else:
                    # 常见原因是执行函数已经成功 claim 后才发生异常；此时 execution
                    # 保持 running 并由诊断暴露，绝不能错误改回 accepted。
                    logger.info(
                        "异常任务已不再是 accepted，跳过领取冷却: task_id=%s",
                        task_id,
                    )
            except Exception:
                infrastructure_failure = True
                with self._state_lock:
                    self._accepted_deferral_failure_count += 1
                logger.exception(
                    "报告 accepted 冷却事实写入失败，将等待固定扫描周期后重试: "
                    "task_id=%s",
                    task_id,
                )
        return infrastructure_failure

    def _sweep_resources(self) -> None:
        try:
            result = self._resources.sweep(
                limit=self._config.resource_sweep_limit,
            )
            if not isinstance(result, ReportResourceSweepResult):
                raise TypeError("资源恢复 sweep 必须返回 ReportResourceSweepResult")
            with self._state_lock:
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
            with self._state_lock:
                self._resource_sweep_failure_count += 1
            logger.exception("报告资源恢复批次失败，Dispatcher 将按周期继续重试")

    def _sweep_callback_guards(self) -> None:
        """独立统计 Guard 维护失败；失败不得跳过同周期资源恢复。"""

        try:
            result = self._callbacks.freeze_expired(
                limit=self._config.resource_sweep_limit,
            )
            if not isinstance(result, ReportCallbackGuardSweepResult):
                raise TypeError("Callback Guard sweep 必须返回强类型结果")
            with self._state_lock:
                self._callback_guard_sweep_count += 1
                self._callback_guard_frozen_count += result.frozen_count
            logger.log(
                logging.WARNING if result.frozen_count else logging.DEBUG,
                "报告 Callback Guard 维护完成: scanned=%d frozen=%d",
                result.scanned_count,
                result.frozen_count,
            )
        except Exception:
            with self._state_lock:
                self._callback_guard_sweep_failure_count += 1
            logger.exception("报告 Callback Guard 维护失败，将按周期继续重试")

    def _inspect_queue(self) -> None:
        try:
            snapshot = self._queue_inspector.inspect_queue(
                _REPORT_TASK_TYPE,
                running_sample_limit=self._config.running_sample_limit,
            )
            if not isinstance(snapshot, TaskQueueSnapshot):
                raise TypeError("队列诊断必须返回 TaskQueueSnapshot")
            accepted_age = self._age_seconds(snapshot.oldest_accepted_at)
            running_age = self._age_seconds(snapshot.oldest_running_at)
            if snapshot.accepted_count:
                logger.info(
                    "报告持久积压快照: accepted=%d oldest_accepted_age_seconds=%s "
                    "running=%d",
                    snapshot.accepted_count,
                    self._format_age(accepted_age),
                    snapshot.running_count,
                )
            else:
                logger.debug(
                    "报告持久积压为空: running=%d",
                    snapshot.running_count,
                )
            if snapshot.running_count:
                now = self._monotonic()
                if now >= self._next_running_warning_at:
                    logger.warning(
                        "发现报告 running execution，仅观察且禁止自动重置: count=%d "
                        "oldest_running_age_seconds=%s task_ids=%s",
                        snapshot.running_count,
                        self._format_age(running_age),
                        ",".join(
                            item.value for item in snapshot.running_task_ids
                        )
                        or "-",
                    )
                    self._next_running_warning_at = (
                        now + _RUNNING_WARNING_INTERVAL_SECONDS
                    )
            else:
                self._next_running_warning_at = 0.0
            with self._state_lock:
                self._queue_inspection_count += 1
        except Exception:
            with self._state_lock:
                self._queue_inspection_failure_count += 1
            logger.exception("报告队列只读诊断失败，不影响后续周期扫描")

    def _abort_all_loops(self, message: str) -> None:
        logger.critical(message, exc_info=True)
        self._stop_event.set()
        self._wake_event.set()
        with self._state_lock:
            if not self._fatal_error:
                self._fatal_error = message
            if self._lifecycle_state not in {_STATE_STOPPED, _STATE_CLOSED}:
                self._lifecycle_state = _STATE_STOPPING

    def _complete_stop_if_finished(self) -> None:
        if not (
            self._worker_finished.is_set()
            and self._resource_finished.is_set()
            and self._queue_finished.is_set()
        ):
            return
        with self._state_lock:
            self._waiting_task_id = None
            self._current_task_id = None
            if self._lifecycle_state != _STATE_CLOSED:
                self._lifecycle_state = _STATE_STOPPED
        self._release_process_guard()

    def _release_process_guard(self) -> None:
        with self._state_lock:
            if not self._guard_acquired:
                return
            self._guard_acquired = False
            self._owner_pid = None
            guard = self._process_guard
        if guard is not None:
            try:
                guard.release()
            except Exception:
                logger.critical(
                    "报告 Dispatcher 单实例进程锁释放失败",
                    exc_info=True,
                )

    def _threads_locked(self) -> tuple[threading.Thread, ...]:
        return tuple(
            thread
            for thread in (
                self._worker_thread,
                self._resource_thread,
                self._queue_thread,
            )
            if thread is not None
        )

    def _raise_if_forked_locked(self) -> None:
        if self._owner_pid is not None and self._owner_pid != os.getpid():
            raise RuntimeError(
                "报告 Dispatcher 检测到 fork 后继承的失效线程状态；"
                "single_instance 模式禁止 preload/fork"
            )

    def _aware_wall_now(self) -> datetime:
        value = self._wall_clock()
        if not isinstance(value, datetime):
            raise TypeError("wall_clock 必须返回 datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("wall_clock 必须返回带时区 datetime")
        return value.astimezone(timezone.utc)

    def _age_seconds(self, timestamp: str | None) -> float | None:
        if timestamp is None:
            return None
        parsed = datetime.fromisoformat(timestamp)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("队列时间戳必须包含时区")
        return max(
            0.0,
            (self._aware_wall_now() - parsed.astimezone(timezone.utc)).total_seconds(),
        )

    @staticmethod
    def _format_age(value: float | None) -> str:
        return "unknown" if value is None else f"{value:.3f}"


__all__ = [
    "LocalReportDispatcherSnapshot",
    "LocalReportTaskDispatcher",
]
