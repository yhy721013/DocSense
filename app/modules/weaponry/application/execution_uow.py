"""Weaponry Step、组件事实、终态与 Callback 资格的组合 UoW 契约。"""

from __future__ import annotations

from typing import Protocol, Self, runtime_checkable

from app.modules.tasks.ports import (
    CallbackAdmissionConflictPort,
    CallbackDeliveryControlPort,
    TaskAdmissionPort,
    TaskExecutionPort,
    TaskStepContinuationStorePort,
)
from app.modules.weaponry.ports import (
    WeaponryCreationIntentStorePort,
    WeaponryInteractionAuditPort,
    WeaponryResourceStorePort,
    WeaponryResultSnapshotStorePort,
    WeaponryTaskDocumentSnapshotStorePort,
)


@runtime_checkable
class WeaponryAdmissionUnitOfWork(Protocol):
    """Task 受理、Callback 冲突与文档身份快照的单事务边界。"""

    def __enter__(self) -> Self: ...
    def __exit__(self, exc_type, exc_value, traceback) -> bool | None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...

    @property
    def admission(self) -> TaskAdmissionPort: ...

    @property
    def callback_conflicts(self) -> CallbackAdmissionConflictPort: ...

    @property
    def document_snapshots(self) -> WeaponryTaskDocumentSnapshotStorePort: ...


@runtime_checkable
class WeaponryAdmissionUnitOfWorkFactory(Protocol):
    def __call__(self) -> WeaponryAdmissionUnitOfWork: ...


@runtime_checkable
class WeaponryExecutionUnitOfWork(Protocol):
    """同一短事务中的通用 Control 与 Weaponry 组件事实。"""

    def __enter__(self) -> Self: ...
    def __exit__(self, exc_type, exc_value, traceback) -> bool | None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...

    @property
    def execution(self) -> TaskExecutionPort: ...

    @property
    def callback_delivery(self) -> CallbackDeliveryControlPort: ...

    @property
    def document_snapshots(self) -> WeaponryTaskDocumentSnapshotStorePort: ...

    @property
    def creation_intents(self) -> WeaponryCreationIntentStorePort: ...

    @property
    def interaction_audits(self) -> WeaponryInteractionAuditPort: ...

    @property
    def resources(self) -> WeaponryResourceStorePort: ...

    @property
    def results(self) -> WeaponryResultSnapshotStorePort: ...

    @property
    def continuations(self) -> TaskStepContinuationStorePort: ...


@runtime_checkable
class WeaponryExecutionUnitOfWorkFactory(Protocol):
    def __call__(self) -> WeaponryExecutionUnitOfWork: ...


__all__ = [
    "WeaponryAdmissionUnitOfWork",
    "WeaponryAdmissionUnitOfWorkFactory",
    "WeaponryExecutionUnitOfWork",
    "WeaponryExecutionUnitOfWorkFactory",
]
