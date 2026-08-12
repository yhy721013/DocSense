"""Task Control 窄用例 Unit of Work 协议。

UoW 不暴露 SQLite Connection。Application 必须显式 ``commit``；未提交的正常退出、
异常或取消均由 Adapter 回滚。Store 不得自行提交、关闭连接或在内部隐藏重试。
"""

from __future__ import annotations

from typing import Protocol, Self, runtime_checkable

from .callback_delivery_control import CallbackAdmissionConflictPort
from .task_admission import TaskAdmissionPort
from .task_execution import TaskExecutionPort
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
    """claim/start/Step/终态各自使用的窄 Execution UoW。"""

    @property
    def execution(self) -> TaskExecutionPort:
        ...


@runtime_checkable
class TaskRecoveryUnitOfWork(_UnitOfWorkLifecycle, Protocol):
    """过期复核或 Recovery Case 条件写使用的窄 UoW。"""

    @property
    def recovery(self) -> TaskRecoveryPort:
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
class TaskRecoveryUnitOfWorkFactory(Protocol):
    def __call__(self) -> TaskRecoveryUnitOfWork:
        ...


__all__ = [
    "TaskAdmissionUnitOfWork",
    "TaskAdmissionUnitOfWorkFactory",
    "TaskExecutionUnitOfWork",
    "TaskExecutionUnitOfWorkFactory",
    "TaskRecoveryUnitOfWork",
    "TaskRecoveryUnitOfWorkFactory",
]
