"""Report Step、资源事实、终态与 Callback 资格的业务组合 UoW 契约。"""

from __future__ import annotations

from typing import Protocol, Self, runtime_checkable

from app.modules.report.ports import ReportResourceStorePort
from app.modules.tasks.ports import (
    CallbackDeliveryControlPort,
    TaskExecutionPort,
    TaskStepContinuationStorePort,
)


@runtime_checkable
class ReportExecutionUnitOfWork(Protocol):
    """同一短事务中的通用控制事实与 Report 组件事实。

    这是 Report Application 对一次业务事务的编排需求，而不是一个可跨模块复用的
    基础 Port。外部 HTTP、文件与模型调用必须发生在 UoW 之外。
    """

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type, exc_value, traceback) -> bool | None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    @property
    def execution(self) -> TaskExecutionPort: ...

    @property
    def callback_delivery(self) -> CallbackDeliveryControlPort: ...

    @property
    def resources(self) -> ReportResourceStorePort: ...

    @property
    def continuations(self) -> TaskStepContinuationStorePort: ...


@runtime_checkable
class ReportExecutionUnitOfWorkFactory(Protocol):
    def __call__(self) -> ReportExecutionUnitOfWork: ...


__all__ = ["ReportExecutionUnitOfWork", "ReportExecutionUnitOfWorkFactory"]
