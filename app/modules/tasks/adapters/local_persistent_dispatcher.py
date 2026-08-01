"""单实例持久任务扫描、常量空间唤醒和显式生命周期通用内核。

该模块只依赖 tasks 的领域类型与端口，不知道报告、武器谱、AnythingLLM、Callback 或
资源记录。业务模块只需注入任务类型、执行函数和有界维护任务，即可复用已经过阶段 1C
验证的停机、毒任务冷却、只读 running 诊断和跨进程单实例语义。

这里的 ``threading.Event`` 只是可丢失的唤醒提示。所有待执行事实仍保存在 Repository；
无论 accepted 任务有 5 条、50 条还是更多，内核都不会为每条任务创建线程、Future 或内存
队列项。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import os
import threading
import time
from typing import Any

from app.modules.tasks.domain import TaskId
from app.modules.tasks.ports import (
    ProcessSingletonGuardPort,
    TaskCommandPort,
    TaskExecutionPermitPort,
    TaskQueueInspectionPort,
    TaskQueueSnapshot,
)


logger = logging.getLogger(__name__)

_STATE_NEW = "new"
_STATE_STARTING = "starting"
_STATE_RUNNING = "running"
_STATE_STOPPING = "stopping"
_STATE_STOPPED = "stopped"
_STATE_CLOSED = "closed"
_PERMIT_POLL_MAX_SECONDS = 0.1


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


def _positive_number(value: object, *, name: str) -> float:
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


def _bounded_positive_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} 必须是整数")
    if value < 1 or value > 1000:
        raise ValueError(f"{name} 必须是 1~1000 的整数")
    return value


@dataclass(frozen=True)
class LocalPersistentDispatcherSettings:
    """与具体业务无关的本地 Dispatcher 运行参数。"""

    task_type: str
    business_label: str
    thread_name_prefix: str
    scan_interval_seconds: float
    accepted_batch_size: int
    dispatch_failure_retry_seconds: float
    running_sample_limit: int
    stop_timeout_seconds: float
    queue_inspection_interval_seconds: float = 30.0
    running_warning_interval_seconds: float = 30.0

    def __post_init__(self) -> None:
        for name in ("task_type", "business_label", "thread_name_prefix"):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name=name),
            )
        for name in (
            "scan_interval_seconds",
            "dispatch_failure_retry_seconds",
            "stop_timeout_seconds",
            "queue_inspection_interval_seconds",
            "running_warning_interval_seconds",
        ):
            object.__setattr__(
                self,
                name,
                _positive_number(getattr(self, name), name=name),
            )
        for name in ("accepted_batch_size", "running_sample_limit"):
            object.__setattr__(
                self,
                name,
                _bounded_positive_int(getattr(self, name), name=name),
            )


@dataclass(frozen=True)
class LocalPersistentMaintenanceTask:
    """一个与重型执行 Worker 隔离的固定延迟维护任务。"""

    name: str
    thread_name: str
    interval_seconds: float
    execute: Callable[[], object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _required_text(self.name, name="name"))
        object.__setattr__(
            self,
            "thread_name",
            _required_text(self.thread_name, name="thread_name"),
        )
        object.__setattr__(
            self,
            "interval_seconds",
            _positive_number(self.interval_seconds, name="interval_seconds"),
        )
        if not callable(self.execute):
            raise TypeError("execute 必须可调用")


@dataclass(frozen=True)
class LocalPersistentMaintenanceSnapshot:
    """单个维护任务的可测试计数。"""

    name: str
    success_count: int
    failure_count: int


@dataclass(frozen=True)
class LocalPersistentDispatcherSnapshot:
    """通用内核的只读运行快照。"""

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
    queue_inspection_count: int
    queue_inspection_failure_count: int
    maintenance: tuple[LocalPersistentMaintenanceSnapshot, ...]
    ready: bool
    fatal_error: str

    def maintenance_by_name(self, name: str) -> LocalPersistentMaintenanceSnapshot:
        """按稳定名称读取维护指标；未知名称属于组合错误。"""

        normalized = _required_text(name, name="name")
        for item in self.maintenance:
            if item.name == normalized:
                return item
        raise KeyError(normalized)


class LocalPersistentTaskDispatcher:
    """扫描 Repository 中 accepted 事实的单执行 Worker 内核。

    构造函数不会创建线程或获取进程锁。组合根必须显式调用 ``start``；显式注入的
    离线应用因此可以安全导入和构造，而不会在测试收集阶段偷偷启动后台服务。
    """

    def __init__(
        self,
        *,
        task_commands: TaskCommandPort[Any, Any, Any],
        queue_inspector: TaskQueueInspectionPort,
        execute: Callable[[TaskId], object],
        settings: LocalPersistentDispatcherSettings,
        maintenance_tasks: tuple[LocalPersistentMaintenanceTask, ...] = (),
        execution_limiter: TaskExecutionPermitPort | None = None,
        process_guard: ProcessSingletonGuardPort | None = None,
        startup_gate: Callable[[], None] | None = None,
        accepted_deferral_handler: Callable[[TaskId, str], bool] | None = None,
        fatal_error_handler: Callable[[str], None] | None = None,
        event_logger: logging.Logger | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not isinstance(task_commands, TaskCommandPort):
            raise TypeError("task_commands 必须实现 TaskCommandPort")
        if not isinstance(queue_inspector, TaskQueueInspectionPort):
            raise TypeError("queue_inspector 必须实现 TaskQueueInspectionPort")
        if not callable(execute):
            raise TypeError("execute 必须可调用")
        if not isinstance(settings, LocalPersistentDispatcherSettings):
            raise TypeError("settings 必须是 LocalPersistentDispatcherSettings")
        tasks = tuple(maintenance_tasks)
        if any(not isinstance(item, LocalPersistentMaintenanceTask) for item in tasks):
            raise TypeError("maintenance_tasks 只能包含 LocalPersistentMaintenanceTask")
        names = tuple(item.name for item in tasks)
        thread_names = tuple(item.thread_name for item in tasks)
        if len(set(names)) != len(names):
            raise ValueError("maintenance task name 不得重复")
        if len(set(thread_names)) != len(thread_names):
            raise ValueError("maintenance thread_name 不得重复")
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
        if startup_gate is not None and not callable(startup_gate):
            raise TypeError("startup_gate 必须可调用或为 None")
        if accepted_deferral_handler is not None and not callable(
            accepted_deferral_handler
        ):
            raise TypeError("accepted_deferral_handler 必须可调用或为 None")
        if fatal_error_handler is not None and not callable(fatal_error_handler):
            raise TypeError("fatal_error_handler 必须可调用或为 None")
        if event_logger is not None and not isinstance(event_logger, logging.Logger):
            raise TypeError("event_logger 必须是 logging.Logger 或 None")
        if not callable(monotonic) or not callable(wall_clock):
            raise TypeError("Dispatcher 时钟必须可调用")

        self._task_commands = task_commands
        self._queue_inspector = queue_inspector
        self._execute = execute
        self._settings = settings
        self._maintenance_tasks = tasks
        self._execution_limiter = execution_limiter
        self._process_guard = process_guard
        self._startup_gate = startup_gate
        # 少数业务需要把受理前失败的退避时间与自身持久化计数放在同一事务内计算。
        # 内核只保留“失败后必须持久化冷却”的通用语义；未注入时继续使用既有固定
        # retry_at 行为，避免 Report/Weaponry 的已验证链路发生行为变化。
        self._accepted_deferral_handler = accepted_deferral_handler
        # 生产组合根可以把不可恢复的线程退出升级为进程退出，让编排器重启并重新扫描
        # 已持久化 accepted 事实。默认不注入，保证离线测试和其他业务的既有行为不变。
        self._fatal_error_handler = fatal_error_handler
        self._logger = event_logger or logger
        self._monotonic = monotonic
        self._wall_clock = wall_clock

        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._state_lock = threading.RLock()
        self._worker_thread: threading.Thread | None = None
        self._queue_thread: threading.Thread | None = None
        self._maintenance_threads: dict[str, threading.Thread] = {}
        self._worker_finished = threading.Event()
        self._queue_finished = threading.Event()
        self._maintenance_finished = {
            item.name: threading.Event() for item in self._maintenance_tasks
        }
        self._worker_finished.set()
        self._queue_finished.set()
        for finished in self._maintenance_finished.values():
            finished.set()
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
        self._queue_inspection_count = 0
        self._queue_inspection_failure_count = 0
        self._maintenance_success_count = {item.name: 0 for item in tasks}
        self._maintenance_failure_count = {item.name: 0 for item in tasks}
        self._fatal_error = ""
        self._next_running_warning_at = 0.0

    @property
    def has_process_guard(self) -> bool:
        return self._process_guard is not None

    @property
    def task_commands(self) -> TaskCommandPort[Any, Any, Any]:
        """暴露只读依赖身份，供组合根校验 Submit/Run/Dispatcher 同链。"""

        return self._task_commands

    @property
    def execution_limiter(self) -> TaskExecutionPermitPort | None:
        return self._execution_limiter

    def dispatch(self, task_id: TaskId) -> None:
        """合并唤醒信号；TaskId 只用于校验和日志，不进入等待容器。"""

        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        self._signal_wakeup(task_id)

    def wake_up(self) -> None:
        """发送不携带业务身份的常量空间唤醒信号。

        某些批量受理器只需要通知“持久积压可能变了”，并不会向 Dispatcher 暴露每个
        execution 的内部 TaskId。Event 只缩短下一次扫描等待，不是可靠队列；即使该
        信号丢失，Worker 仍会按固定周期从 Repository 重新发现 accepted 事实。
        """

        self._signal_wakeup(None)

    def _signal_wakeup(self, task_id: TaskId | None) -> None:
        """在同一处维护带/不带任务身份的唤醒计数和生命周期门禁。"""

        with self._state_lock:
            self._raise_if_forked_locked()
            if self._lifecycle_state in {
                _STATE_STOPPING,
                _STATE_STOPPED,
                _STATE_CLOSED,
            }:
                raise RuntimeError(
                    f"{self._settings.business_label} Dispatcher 已停止接收唤醒"
                )
            already_pending = self._wake_event.is_set()
            self._dispatch_count += 1
            if already_pending:
                self._merged_wakeup_count += 1
            self._wake_event.set()
        self._logger.debug(
            "%s Dispatcher 已接收常量空间唤醒: task_id=%s merged=%s",
            self._settings.business_label,
            task_id or "-",
            already_pending,
        )

    def start(self) -> None:
        """幂等启动一个执行 Worker、一个队列诊断和所有注入维护任务。"""

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
                    raise RuntimeError(
                        f"已停止的{self._settings.business_label} Dispatcher 不得重新启动"
                    )
                if self._process_guard is not None:
                    if not self._process_guard.acquire():
                        raise RuntimeError(
                            f"{self._settings.business_label} Dispatcher "
                            "单实例进程锁已被其他进程占用"
                        )
                    self._guard_acquired = True

                # 启动门禁必须位于跨进程锁之后、后台线程之前。业务模块可在这里完成
                # 必须串行化的外部资源准备；若门禁失败，统一异常路径会释放进程锁，
                # Worker 尚未创建，因此不会领取到依赖尚未就绪的任务。
                if self._startup_gate is not None:
                    self._startup_gate()

                self._owner_pid = os.getpid()
                self._stop_event.clear()
                self._wake_event.clear()
                self._worker_finished.clear()
                self._queue_finished.clear()
                for finished in self._maintenance_finished.values():
                    finished.clear()
                self._lifecycle_state = _STATE_STARTING
                self._worker_thread = threading.Thread(
                    target=self._run_worker,
                    name=f"{self._settings.thread_name_prefix}-worker",
                    daemon=True,
                )
                self._queue_thread = threading.Thread(
                    target=self._run_queue_inspector,
                    name=f"{self._settings.thread_name_prefix}-queue-inspector",
                    daemon=True,
                )
                self._maintenance_threads = {
                    item.name: threading.Thread(
                        target=self._run_maintenance,
                        args=(item,),
                        name=item.thread_name,
                        daemon=True,
                    )
                    for item in self._maintenance_tasks
                }
                for thread in self._threads_locked():
                    thread.start()
                    started_threads.append(thread)
                self._lifecycle_state = _STATE_RUNNING
        except Exception:
            self._stop_event.set()
            self._wake_event.set()
            with self._state_lock:
                self._mark_unstarted_threads_finished(started_threads)
                self._lifecycle_state = _STATE_STOPPING
            for thread in started_threads:
                thread.join(timeout=self._settings.stop_timeout_seconds)
            self._complete_stop_if_finished()
            raise

        self._logger.info(
            "%s Dispatcher 已启动: task_type=%s accepted_batch_size=%d "
            "scan_interval_seconds=%.3f dispatch_failure_retry_seconds=%.3f "
            "maintenance_task_count=%d process_guard=%s",
            self._settings.business_label,
            self._settings.task_type,
            self._settings.accepted_batch_size,
            self._settings.scan_interval_seconds,
            self._settings.dispatch_failure_retry_seconds,
            len(self._maintenance_tasks),
            self.has_process_guard,
        )

    def stop(self, *, timeout_seconds: float | None = None) -> bool:
        """停止领取新任务，并在一个总超时内等待所有线程退出。"""

        timeout = (
            self._settings.stop_timeout_seconds
            if timeout_seconds is None
            else _positive_number(timeout_seconds, name="timeout_seconds")
        )
        with self._state_lock:
            self._raise_if_forked_locked()
            threads = self._threads_locked()
            alive_threads = tuple(thread for thread in threads if thread.is_alive())
            if self._lifecycle_state in {_STATE_STOPPED, _STATE_CLOSED}:
                if not alive_threads:
                    return True
                self._logger.critical(
                    "%s Dispatcher 终态仍存在活动线程，恢复 stopping 继续等待: state=%s",
                    self._settings.business_label,
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
            self._logger.error(
                "%s Dispatcher 不得从自身后台线程同步等待停止",
                self._settings.business_label,
            )
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
            self._logger.critical(
                "%s Dispatcher 停止等待超时，不伪造线程已退出或回退任务状态: "
                "timeout_seconds=%.3f current_task_id=%s waiting_task_id=%s "
                "alive_threads=%s",
                self._settings.business_label,
                timeout,
                current_task_id or "-",
                waiting_task_id or "-",
                ",".join(thread.name for thread in still_alive),
            )
            return False

        self._complete_stop_if_finished()
        self._logger.info("%s Dispatcher 已停止", self._settings.business_label)
        return True

    def close(self) -> None:
        """幂等关闭；执行函数未退出时保留 stopping，绝不制造 closed 假阳性。"""

        with self._state_lock:
            if self._lifecycle_state == _STATE_CLOSED:
                return
        stopped = self.stop(timeout_seconds=self._settings.stop_timeout_seconds)
        if not stopped:
            self._logger.critical(
                "%s Dispatcher 关闭等待超时，生命周期保持 stopping，等待进程级隔离",
                self._settings.business_label,
            )
            return
        with self._state_lock:
            self._lifecycle_state = _STATE_CLOSED

    def snapshot(self) -> LocalPersistentDispatcherSnapshot:
        """返回线程安全快照；``buffered_task_count`` 永远为零。"""

        with self._state_lock:
            worker_count = int(
                self._worker_thread is not None and self._worker_thread.is_alive()
            )
            maintenance_count = int(
                self._queue_thread is not None and self._queue_thread.is_alive()
            ) + sum(
                int(thread.is_alive())
                for thread in self._maintenance_threads.values()
            )
            maintenance = tuple(
                LocalPersistentMaintenanceSnapshot(
                    name=item.name,
                    success_count=self._maintenance_success_count[item.name],
                    failure_count=self._maintenance_failure_count[item.name],
                )
                for item in self._maintenance_tasks
            )
            return LocalPersistentDispatcherSnapshot(
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
                queue_inspection_count=self._queue_inspection_count,
                queue_inspection_failure_count=self._queue_inspection_failure_count,
                maintenance=maintenance,
                ready=(
                    self._lifecycle_state == _STATE_RUNNING
                    and not self._fatal_error
                ),
                fatal_error=self._fatal_error,
            )

    def _run_worker(self) -> None:
        try:
            while not self._stop_event.is_set():
                # 先清除旧唤醒；扫描期间到达的新唤醒会重新置位，并在本轮结束后复扫。
                self._wake_event.clear()
                full_batch = self._scan_accepted_once()
                if self._stop_event.is_set():
                    break
                if full_batch or self._wake_event.is_set():
                    continue
                self._wake_event.wait(
                    timeout=self._settings.scan_interval_seconds
                )
        except BaseException:
            self._abort_all_loops(
                f"{self._settings.business_label} Dispatcher 执行 Worker 非预期终止"
            )
            raise
        finally:
            with self._state_lock:
                self._waiting_task_id = None
                self._current_task_id = None
            self._worker_finished.set()
            self._complete_stop_if_finished()

    def _run_maintenance(self, task: LocalPersistentMaintenanceTask) -> None:
        try:
            next_run_at = self._monotonic()
            while not self._stop_event.is_set():
                wait_seconds = max(0.0, next_run_at - self._monotonic())
                if self._stop_event.wait(timeout=wait_seconds):
                    break
                try:
                    task.execute()
                except Exception:
                    with self._state_lock:
                        self._maintenance_failure_count[task.name] += 1
                    self._logger.exception(
                        "%s Dispatcher 维护任务失败，将按固定周期继续: task=%s",
                        self._settings.business_label,
                        task.name,
                    )
                else:
                    with self._state_lock:
                        self._maintenance_success_count[task.name] += 1
                # 固定延迟而非固定频率，禁止同一维护批次发生重叠。
                next_run_at = self._monotonic() + task.interval_seconds
        except BaseException:
            self._abort_all_loops(
                f"{self._settings.business_label} Dispatcher 维护线程非预期终止: "
                f"task={task.name}"
            )
            raise
        finally:
            self._maintenance_finished[task.name].set()
            self._complete_stop_if_finished()

    def _run_queue_inspector(self) -> None:
        try:
            next_run_at = self._monotonic()
            while not self._stop_event.is_set():
                wait_seconds = max(0.0, next_run_at - self._monotonic())
                if self._stop_event.wait(timeout=wait_seconds):
                    break
                self._inspect_queue()
                next_run_at = (
                    self._monotonic()
                    + self._settings.queue_inspection_interval_seconds
                )
        except BaseException:
            self._abort_all_loops(
                f"{self._settings.business_label}队列诊断维护线程非预期终止"
            )
            raise
        finally:
            self._queue_finished.set()
            self._complete_stop_if_finished()

    def _scan_accepted_once(self) -> bool:
        try:
            task_ids = tuple(
                self._task_commands.list_accepted(
                    self._settings.task_type,
                    limit=self._settings.accepted_batch_size,
                )
            )
            if len(task_ids) > self._settings.accepted_batch_size:
                raise RuntimeError("accepted 扫描返回数量超过配置上限")
            if any(not isinstance(item, TaskId) for item in task_ids):
                raise TypeError("accepted 扫描只能返回 TaskId")
            if len(set(task_ids)) != len(task_ids):
                raise RuntimeError("accepted 扫描返回重复 TaskId")
        except Exception:
            self._logger.exception(
                "%s accepted 有界扫描失败，等待下一固定周期重试",
                self._settings.business_label,
            )
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
                            self._settings.scan_interval_seconds,
                        ),
                    )
                    if not permit_acquired:
                        completed_scan = False
                        break

                # 与 stop 共享状态锁：若 stop 先取得锁，本任务不会开始；反之 current
                # 已经成为在途执行，stop 必须等待它，而不是把 running 改回 accepted。
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
                self._logger.exception(
                    "%s execution 执行异常，将在批次末尾按 accepted 条件持久化冷却: "
                    "task_id=%s",
                    self._settings.business_label,
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
                        self._logger.critical(
                            "%s执行许可归还失败，已停止 Dispatcher 后续领取",
                            self._settings.business_label,
                            exc_info=True,
                        )
                        self._record_fatal_error(
                            "execution_limiter_release_failed"
                        )
                        self._stop_event.set()
                        self._wake_event.set()
                        completed_scan = False

        deferral_failed = self._defer_failed_accepted(failures)
        return (
            completed_scan
            and not self._stop_event.is_set()
            and not deferral_failed
            and len(task_ids) == self._settings.accepted_batch_size
        )

    def _defer_failed_accepted(
        self,
        failures: list[tuple[TaskId, str]],
    ) -> bool:
        """持久化领取前失败的冷却终点，避免毒任务长期占住 FIFO 首页。"""

        if not failures:
            return False
        retry_at = None
        if self._accepted_deferral_handler is None:
            retry_at = (
                self._aware_wall_now()
                + timedelta(seconds=self._settings.dispatch_failure_retry_seconds)
            ).isoformat()
        infrastructure_failure = False
        for task_id, reason in failures:
            try:
                if self._accepted_deferral_handler is not None:
                    # 自定义处理器必须自行在持久化事务内计算 retry_at；不能把内核中
                    # 读取到的易失内存计数当成跨重启或未来多实例下的退避事实。
                    deferred = self._accepted_deferral_handler(task_id, reason)
                else:
                    deferred = self._task_commands.defer_accepted(
                        task_id,
                        retry_at=retry_at or "",
                        reason=reason,
                    )
                if not isinstance(deferred, bool):
                    raise TypeError("accepted 失败冷却处理器必须返回 bool")
                if deferred:
                    with self._state_lock:
                        self._accepted_deferral_count += 1
                else:
                    self._logger.info(
                        "%s异常任务已不再是 accepted，跳过领取冷却: task_id=%s",
                        self._settings.business_label,
                        task_id,
                    )
            except Exception:
                infrastructure_failure = True
                with self._state_lock:
                    self._accepted_deferral_failure_count += 1
                self._logger.exception(
                    "%s accepted 冷却事实写入失败，将等待固定扫描周期后重试: "
                    "task_id=%s",
                    self._settings.business_label,
                    task_id,
                )
        return infrastructure_failure

    def _inspect_queue(self) -> None:
        try:
            snapshot = self._queue_inspector.inspect_queue(
                self._settings.task_type,
                running_sample_limit=self._settings.running_sample_limit,
            )
            if not isinstance(snapshot, TaskQueueSnapshot):
                raise TypeError("队列诊断必须返回 TaskQueueSnapshot")
            if snapshot.task_type != self._settings.task_type:
                raise RuntimeError("队列诊断返回了其他任务类型")
            accepted_age = self._age_seconds(snapshot.oldest_accepted_at)
            running_age = self._age_seconds(snapshot.oldest_running_at)
            if snapshot.accepted_count:
                self._logger.info(
                    "%s持久积压快照: accepted=%d oldest_accepted_age_seconds=%s "
                    "running=%d",
                    self._settings.business_label,
                    snapshot.accepted_count,
                    self._format_age(accepted_age),
                    snapshot.running_count,
                )
            else:
                self._logger.debug(
                    "%s持久积压为空: running=%d",
                    self._settings.business_label,
                    snapshot.running_count,
                )
            if snapshot.running_count:
                now = self._monotonic()
                if now >= self._next_running_warning_at:
                    # running execution 可能意味着进程中断后遗留的未决执行，因此需要以
                    # WARNING 暴露给运维；限频器保证同一实例不会随扫描周期持续刷屏。
                    # TaskId 属于内部执行标识，日志只保留聚合数量与年龄，避免泄露。
                    self._logger.warning(
                        "发现%s running execution，仅观察且禁止自动重置: count=%d "
                        "oldest_running_age_seconds=%s",
                        self._settings.business_label,
                        snapshot.running_count,
                        self._format_age(running_age),
                    )
                    self._next_running_warning_at = (
                        now + self._settings.running_warning_interval_seconds
                    )
            else:
                self._next_running_warning_at = 0.0
            with self._state_lock:
                self._queue_inspection_count += 1
        except Exception:
            with self._state_lock:
                self._queue_inspection_failure_count += 1
            self._logger.exception(
                "%s队列只读诊断失败，不影响后续周期扫描",
                self._settings.business_label,
            )

    def _abort_all_loops(self, message: str) -> None:
        self._logger.critical(message, exc_info=True)
        self._stop_event.set()
        self._wake_event.set()
        with self._state_lock:
            if self._lifecycle_state not in {_STATE_STOPPED, _STATE_CLOSED}:
                self._lifecycle_state = _STATE_STOPPING
        self._record_fatal_error(message)

    def _record_fatal_error(self, message: str) -> None:
        """只记录并通知一次不可恢复故障，避免多个线程重复终止同一进程。"""

        should_notify = False
        with self._state_lock:
            if not self._fatal_error:
                self._fatal_error = message
                should_notify = True
        if not should_notify or self._fatal_error_handler is None:
            return
        try:
            self._fatal_error_handler(message)
        except Exception:
            # 终止处理器本身失败时仍保持 fatal/readiness=false；不能让异常覆盖原始
            # 故障或阻止其余线程按 stop_event 收口。
            self._logger.critical(
                "%s Dispatcher 致命故障处理器执行失败: fatal_error=%s",
                self._settings.business_label,
                message,
                exc_info=True,
            )

    def _complete_stop_if_finished(self) -> None:
        if not self._all_finished():
            return
        with self._state_lock:
            self._waiting_task_id = None
            self._current_task_id = None
            if self._lifecycle_state != _STATE_CLOSED:
                self._lifecycle_state = _STATE_STOPPED
        self._release_process_guard()

    def _all_finished(self) -> bool:
        return (
            self._worker_finished.is_set()
            and self._queue_finished.is_set()
            and all(
                finished.is_set()
                for finished in self._maintenance_finished.values()
            )
        )

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
                with self._state_lock:
                    if not self._fatal_error:
                        self._fatal_error = "process_guard_release_failed"
                self._logger.critical(
                    "%s Dispatcher 单实例进程锁释放失败",
                    self._settings.business_label,
                    exc_info=True,
                )

    def _threads_locked(self) -> tuple[threading.Thread, ...]:
        return tuple(
            thread
            for thread in (
                self._worker_thread,
                *self._maintenance_threads.values(),
                self._queue_thread,
            )
            if thread is not None
        )

    def _mark_unstarted_threads_finished(
        self,
        started_threads: list[threading.Thread],
    ) -> None:
        if self._worker_thread not in started_threads:
            self._worker_finished.set()
        if self._queue_thread not in started_threads:
            self._queue_finished.set()
        for name, thread in self._maintenance_threads.items():
            if thread not in started_threads:
                self._maintenance_finished[name].set()

    def _raise_if_forked_locked(self) -> None:
        if self._owner_pid is not None and self._owner_pid != os.getpid():
            raise RuntimeError(
                f"{self._settings.business_label} Dispatcher 检测到 fork 后继承的失效线程状态；"
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
    "LocalPersistentDispatcherSettings",
    "LocalPersistentDispatcherSnapshot",
    "LocalPersistentMaintenanceSnapshot",
    "LocalPersistentMaintenanceTask",
    "LocalPersistentTaskDispatcher",
]
