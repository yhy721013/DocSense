"""任务执行许可与单实例所有权的内部运行时端口。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol, runtime_checkable

from app.modules.tasks.domain import TaskExecutionAuthority, TaskId

from .task_execution import TaskExecutionMutationOutcome


@runtime_checkable
class TaskExecutionPermitPort(Protocol):
    """可中断地获取共享重型资源许可。

    该端口只表达“等待许可、取消等待、归还许可”三件事，不知道线程、信号量或
    AnythingLLM。Dispatcher 因此可以在收到停机信号后终止尚未开始的许可等待，避免
    一个仍为 ``accepted`` 的任务在应用已经停止后才突然进入业务执行。
    """

    def acquire_interruptibly(
        self,
        cancel_requested: Callable[[], bool],
        *,
        poll_interval_seconds: float,
    ) -> bool:
        ...

    def release(self) -> None:
        ...


@runtime_checkable
class ProcessSingletonGuardPort(Protocol):
    """单实例进程所有权端口；实现不得依赖仅存在于当前 Python 进程的锁。"""

    def acquire(self) -> bool:
        ...

    def release(self) -> None:
        ...


class LeaseSupervisorOutcome(str, Enum):
    """租约监督结束原因；失权必须显式传回执行 Runtime。"""

    STOPPED = "stopped"
    AUTHORITY_LOST = "authority_lost"
    CLOCK_UNSAFE = "clock_unsafe"
    INFRASTRUCTURE_ERROR = "infrastructure_error"


@dataclass(frozen=True, slots=True)
class LeaseSupervisorResult:
    outcome: LeaseSupervisorOutcome
    last_mutation_outcome: TaskExecutionMutationOutcome | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, LeaseSupervisorOutcome):
            raise TypeError("outcome 必须是 LeaseSupervisorOutcome")
        if self.last_mutation_outcome is not None and not isinstance(
            self.last_mutation_outcome,
            TaskExecutionMutationOutcome,
        ):
            raise TypeError("last_mutation_outcome 类型错误")


@runtime_checkable
class LeaseHeartbeatSupervisorPort(Protocol):
    """监督一个 Authority 的 heartbeat；具体线程/协程实现对 Application 不可见。"""

    def start(
        self,
        authority: TaskExecutionAuthority,
        *,
        authority_lost: Callable[[LeaseSupervisorResult], None],
    ) -> None:
        ...

    def stop(self) -> LeaseSupervisorResult:
        ...


@runtime_checkable
class LocalTaskExecutorPort(Protocol):
    """某一 task_type 的本地执行器能力对象。"""

    def start(self) -> None:
        ...

    def wake_up(self) -> None:
        """发送可丢提示；持久扫描才是恢复真相。"""
        ...

    def stop(self) -> None:
        ...

    def is_healthy(self) -> bool:
        ...


@runtime_checkable
class TaskExecutionRuntimePort(Protocol):
    """供应商无关的单 Task 运行入口。"""

    def run(self, task_id: TaskId) -> None:
        ...


@runtime_checkable
class LocalMaintenanceSchedulerPort(Protocol):
    """本地维护扫描调度能力；不等同于可靠任务队列。"""

    def start(self) -> None:
        ...

    def wake_up(self) -> None:
        ...

    def stop(self) -> None:
        ...

    def is_healthy(self) -> bool:
        ...


__all__ = [
    "LeaseHeartbeatSupervisorPort",
    "LeaseSupervisorOutcome",
    "LeaseSupervisorResult",
    "LocalMaintenanceSchedulerPort",
    "LocalTaskExecutorPort",
    "ProcessSingletonGuardPort",
    "TaskExecutionPermitPort",
    "TaskExecutionRuntimePort",
]
