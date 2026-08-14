"""Task Control 窄用例 Unit of Work 协议。

UoW 不暴露 SQLite Connection。Application 必须显式 ``commit``；未提交的正常退出、
异常或取消均由 Adapter 回滚。Store 不得自行提交、关闭连接或在内部隐藏重试。
"""

from __future__ import annotations

from typing import Protocol, Self, runtime_checkable

from .callback_delivery_control import (
    CallbackAdmissionConflictPort,
    CallbackDeliveryControlPort,
)
from .task_admission import TaskAdmissionPort
from .task_execution import TaskExecutionPort, TaskRunnableQueryPort
from .task_recovery import TaskRecoveryPort


class _UnitOfWorkLifecycle(Protocol):
    """所有窄 UoW 共享的显式事务生命周期。"""

    def __enter__(self) -> Self:
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> bool | None:
        ...

    def commit(self) -> None:
        ...

    def rollback(self) -> None:
        ...


@runtime_checkable
class TaskAdmissionUnitOfWork(_UnitOfWorkLifecycle, Protocol):
    """受理原子组：Task、latest 投影、Callback 冲突和 accepted Event。"""

    @property
    def admission(self) -> TaskAdmissionPort:
        ...

    @property
    def callback_conflicts(self) -> CallbackAdmissionConflictPort:
        ...


@runtime_checkable
class TaskExecutionUnitOfWork(_UnitOfWorkLifecycle, Protocol):
    """claim/start/Step/终态使用的窄 UoW；终态可原子登记 Callback 资格。"""

    @property
    def execution(self) -> TaskExecutionPort:
        ...

    @property
    def callback_delivery(self) -> CallbackDeliveryControlPort:
        ...


@runtime_checkable
class CallbackDeliveryUnitOfWork(_UnitOfWorkLifecycle, Protocol):
    """Callback claim/heartbeat/完成/冻结/解除使用的独立短事务。"""

    @property
    def callback_delivery(self) -> CallbackDeliveryControlPort:
        ...


@runtime_checkable
class TaskRecoveryUnitOfWork(_UnitOfWorkLifecycle, Protocol):
    """过期复核或 Recovery Case 条件写使用的窄 UoW。"""

    @property
    def recovery(self) -> TaskRecoveryPort:
        ...


@runtime_checkable
class TaskControlQueryUnitOfWork(Protocol):
    """独立短只读事务；不暴露 commit，读取结果永远不能授予写权限。"""

    def __enter__(self) -> Self:
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> bool | None:
        ...

    @property
    def queries(self) -> TaskRunnableQueryPort:
        ...


@runtime_checkable
class TaskAdmissionUnitOfWorkFactory(Protocol):
    def __call__(self) -> TaskAdmissionUnitOfWork:
        ...


@runtime_checkable
class TaskExecutionUnitOfWorkFactory(Protocol):
    def __call__(self) -> TaskExecutionUnitOfWork:
        ...


@runtime_checkable
class CallbackDeliveryUnitOfWorkFactory(Protocol):
    def __call__(self) -> CallbackDeliveryUnitOfWork:
        ...


@runtime_checkable
class TaskRecoveryUnitOfWorkFactory(Protocol):
    def __call__(self) -> TaskRecoveryUnitOfWork:
        ...


@runtime_checkable
class TaskControlQueryUnitOfWorkFactory(Protocol):
    def __call__(self) -> TaskControlQueryUnitOfWork:
        ...


__all__ = [
    "TaskAdmissionUnitOfWork",
    "TaskAdmissionUnitOfWorkFactory",
    "TaskExecutionUnitOfWork",
    "TaskExecutionUnitOfWorkFactory",
    "CallbackDeliveryUnitOfWork",
    "CallbackDeliveryUnitOfWorkFactory",
    "TaskRecoveryUnitOfWork",
    "TaskRecoveryUnitOfWorkFactory",
    "TaskControlQueryUnitOfWork",
    "TaskControlQueryUnitOfWorkFactory",
]
