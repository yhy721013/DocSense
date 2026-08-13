"""任务执行许可与单实例所有权的内部运行时端口。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol, TypeVar, runtime_checkable

from app.modules.tasks.domain import TaskExecutionAuthority, TaskId

from .task_execution import (
    TaskExecutionMutationOutcome,
    TaskHeartbeatResult,
)


TAuthorizedResult = TypeVar("TAuthorizedResult")


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
        lost = self.outcome is LeaseSupervisorOutcome.AUTHORITY_LOST
        if lost != (self.last_mutation_outcome is not None):
            raise ValueError(
                "只有 authority_lost 必须且只能携带 last_mutation_outcome"
            )


class TaskExecutionStopRequested(RuntimeError):
    """Authority Session 已失权，v2 Workflow 必须停止继续推进。"""

    def __init__(self, result: LeaseSupervisorResult) -> None:
        if not isinstance(result, LeaseSupervisorResult):
            raise TypeError("result 必须是 LeaseSupervisorResult")
        super().__init__(f"Task 执行已请求停止: reason={result.outcome.value}")
        self.result = result


@runtime_checkable
class TaskExecutionAuthoritySessionPort(Protocol):
    """向 v2 Workflow 提供可轮换且可停止的完整执行能力。

    ``current_authority`` 只允许诊断观察。任何 Task 条件写都必须通过
    ``run_authorized`` 的短临界区执行；网络、模型、转换、对象删除和阻塞等待禁止
    放进该临界区。heartbeat 使用 ``renew_authority``，使数据库续租提交与内存
    Authority 替换在同一个能力门内完成，避免 expiry 旋转造成自竞争。
    """

    def current_authority(self) -> TaskExecutionAuthority:
        ...

    def run_authorized(
        self,
        operation: Callable[[TaskExecutionAuthority], TAuthorizedResult],
    ) -> TAuthorizedResult:
        ...

    def renew_authority(
        self,
        operation: Callable[[TaskExecutionAuthority], TaskHeartbeatResult],
    ) -> TaskHeartbeatResult:
        ...

    def request_stop(self, result: LeaseSupervisorResult) -> bool:
        """单向设置停止事实；返回本次调用是否首次设置。"""
        ...

    def stop_requested(self) -> bool:
        ...

    def stop_result(self) -> LeaseSupervisorResult | None:
        ...


@runtime_checkable
class LeaseHeartbeatSupervisorPort(Protocol):
    """监督一个 Authority 的 heartbeat；具体线程/协程实现对 Application 不可见。"""

    def start(
        self,
        session: TaskExecutionAuthoritySessionPort,
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

    def run(self, task_id: TaskId) -> "TaskExecutionRuntimeResult":
        ...


@runtime_checkable
class TaskWorkflowRunnerPort(Protocol):
    """阶段 2 v2 Workflow 入口；旧业务 Runner 不实现本协议。"""

    def run(self, session: TaskExecutionAuthoritySessionPort) -> None:
        ...


@runtime_checkable
class TaskLeaseTokenFactoryPort(Protocol):
    """为每次 claim 创建独立高熵 token；实现不得复用或记录 token。"""

    def new_token(self) -> str:
        ...


class TaskExecutionRuntimeOutcome(str, Enum):
    """单次 Runtime 编排的稳定内部结果，不映射公开任务状态。"""

    WORKFLOW_RETURNED = "workflow_returned"
    CLAIM_REJECTED = "claim_rejected"
    START_REJECTED = "start_rejected"
    AUTHORITY_LOST = "authority_lost"
    CLOCK_UNSAFE = "clock_unsafe"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    WORKFLOW_ERROR = "workflow_error"


@dataclass(frozen=True, slots=True)
class TaskExecutionRuntimeResult:
    """Runtime 结果只描述内部编排，不替业务伪造终态或 Callback。"""

    task_id: TaskId
    outcome: TaskExecutionRuntimeOutcome
    mutation_outcome: TaskExecutionMutationOutcome | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.outcome, TaskExecutionRuntimeOutcome):
            raise TypeError("outcome 必须是 TaskExecutionRuntimeOutcome")
        if self.mutation_outcome is not None and not isinstance(
            self.mutation_outcome,
            TaskExecutionMutationOutcome,
        ):
            raise TypeError("mutation_outcome 类型错误")


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
    "TaskExecutionAuthoritySessionPort",
    "TaskExecutionPermitPort",
    "TaskExecutionRuntimeOutcome",
    "TaskExecutionRuntimePort",
    "TaskExecutionRuntimeResult",
    "TaskExecutionStopRequested",
    "TaskLeaseTokenFactoryPort",
    "TaskWorkflowRunnerPort",
]
