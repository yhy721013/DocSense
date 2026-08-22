"""Analysis Step、审计、资源、终态与 Callback 资格的组合 UoW 契约。"""

from __future__ import annotations

from typing import Protocol, Self, runtime_checkable

from app.modules.analysis.ports import AnalysisResourcePort, AnalysisResultSnapshotStorePort
from app.modules.tasks.ports import (
    CallbackDeliveryControlPort,
    TaskExecutionPort,
    TaskStepContinuationStorePort,
)


@runtime_checkable
class AnalysisExecutionUnitOfWork(Protocol):
    """同一短事务中的 Task Control 与 Analysis 组件事实。"""

    def __enter__(self) -> Self: ...
    def __exit__(self, exc_type, exc_value, traceback) -> bool | None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...

    @property
    def execution(self) -> TaskExecutionPort: ...

    @property
    def callback_delivery(self) -> CallbackDeliveryControlPort: ...

    @property
    def resources(self) -> AnalysisResourcePort: ...

    @property
    def results(self) -> AnalysisResultSnapshotStorePort: ...

    @property
    def continuations(self) -> TaskStepContinuationStorePort: ...


@runtime_checkable
class AnalysisExecutionUnitOfWorkFactory(Protocol):
    def __call__(self) -> AnalysisExecutionUnitOfWork: ...


__all__ = ["AnalysisExecutionUnitOfWork", "AnalysisExecutionUnitOfWorkFactory"]
