"""Weaponry 组件与 Task Control 共用连接的短事务 Unit of Work。"""

from __future__ import annotations

from collections.abc import Callable
import sqlite3
import sys
from typing import cast

from app.modules.tasks.adapters.sqlite.transaction import (
    SQLiteTransaction,
    SQLiteTransactionError,
    SQLiteTransactionManager,
)
from app.modules.tasks.ports import (
    CallbackAdmissionConflictPort,
    CallbackDeliveryControlPort,
    TaskAdmissionPort,
    TaskExecutionPort,
)
from app.modules.weaponry.ports import (
    WeaponryCreationIntentStorePort,
    WeaponryInteractionAuditPort,
    WeaponryResourceStorePort,
    WeaponryResultSnapshotStorePort,
    WeaponryTaskDocumentSnapshotStorePort,
)
from app.modules.weaponry.adapters.sqlite.step_continuation_store import (
    SQLiteWeaponryStepContinuationStore,
)
from app.modules.tasks.ports import TaskStepContinuationStorePort


StoreBuilder = Callable[[sqlite3.Connection], object]


class SQLiteWeaponryAdmissionUnitOfWork:
    """原子写入 Task accepted 事实与 Weaponry 文档身份快照。"""

    def __init__(
        self,
        transaction_manager: SQLiteTransactionManager,
        *,
        admission_builder: StoreBuilder,
        callback_conflict_builder: StoreBuilder,
        document_snapshot_builder: StoreBuilder,
    ) -> None:
        if not isinstance(transaction_manager, SQLiteTransactionManager):
            raise TypeError("transaction_manager 必须是 SQLiteTransactionManager")
        builders = {
            "admission_builder": admission_builder,
            "callback_conflict_builder": callback_conflict_builder,
            "document_snapshot_builder": document_snapshot_builder,
        }
        if any(not callable(builder) for builder in builders.values()):
            raise TypeError("Weaponry Admission UoW 的 Store Builder 必须可调用")
        self._transaction_manager = transaction_manager
        self._builders = builders
        self._transaction: SQLiteTransaction | None = None
        self._stores: dict[str, object] = {}
        self._entered_once = False

    def __enter__(self) -> "SQLiteWeaponryAdmissionUnitOfWork":
        if self._entered_once:
            raise SQLiteTransactionError(
                "unit_of_work_reentry_forbidden",
                "Weaponry Admission UnitOfWork 只能进入一次",
            )
        self._entered_once = True
        transaction = self._transaction_manager.begin(read_only=False)
        transaction.__enter__()
        self._transaction = transaction
        try:
            connection = transaction.connection
            self._stores = {
                name: builder(connection) for name, builder in self._builders.items()
            }
            if any(store is None for store in self._stores.values()):
                raise TypeError("Weaponry Admission UoW Store Builder 不得返回 None")
        except BaseException:
            try:
                transaction.__exit__(*sys.exc_info())
            finally:
                self._clear()
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        transaction = self._transaction
        if transaction is None:
            self._clear()
            return False
        try:
            return transaction.__exit__(exc_type, exc_value, traceback)
        finally:
            if not transaction.active:
                self._clear()

    def commit(self) -> None:
        transaction = self._require_transaction()
        try:
            transaction.commit()
        finally:
            if not transaction.active:
                self._clear()

    def rollback(self) -> None:
        transaction = self._require_transaction()
        try:
            transaction.rollback()
        finally:
            if not transaction.active:
                self._clear()

    def _store(self, name: str) -> object:
        self._require_transaction()
        return self._stores[name]

    @property
    def admission(self) -> TaskAdmissionPort:
        return cast(TaskAdmissionPort, self._store("admission_builder"))

    @property
    def callback_conflicts(self) -> CallbackAdmissionConflictPort:
        return cast(
            CallbackAdmissionConflictPort,
            self._store("callback_conflict_builder"),
        )

    @property
    def document_snapshots(self) -> WeaponryTaskDocumentSnapshotStorePort:
        return cast(
            WeaponryTaskDocumentSnapshotStorePort,
            self._store("document_snapshot_builder"),
        )

    def _require_transaction(self) -> SQLiteTransaction:
        transaction = self._transaction
        if transaction is None or not transaction.active:
            raise SQLiteTransactionError(
                "unit_of_work_not_active",
                "Weaponry Admission UnitOfWork 未处于活动事务",
            )
        return transaction

    def _clear(self) -> None:
        self._transaction = None
        self._stores = {}


class SQLiteWeaponryAdmissionUnitOfWorkFactory:
    """为每次受理创建独立短事务。"""

    def __init__(
        self,
        transaction_manager: SQLiteTransactionManager,
        **builders: StoreBuilder,
    ) -> None:
        if not isinstance(transaction_manager, SQLiteTransactionManager):
            raise TypeError("transaction_manager 必须是 SQLiteTransactionManager")
        required = {
            "admission_builder",
            "callback_conflict_builder",
            "document_snapshot_builder",
        }
        if set(builders) != required:
            raise ValueError("Weaponry Admission UoW Builder 集合不完整或包含未知项")
        self._transaction_manager = transaction_manager
        self._builders = dict(builders)

    def __call__(self) -> SQLiteWeaponryAdmissionUnitOfWork:
        return SQLiteWeaponryAdmissionUnitOfWork(
            self._transaction_manager,
            **self._builders,
        )


class SQLiteWeaponryExecutionUnitOfWork:
    """组合 Task、Callback 和四类 Weaponry Store，不暴露原始连接。"""

    def __init__(
        self,
        transaction_manager: SQLiteTransactionManager,
        *,
        execution_builder: StoreBuilder,
        callback_delivery_builder: StoreBuilder,
        document_snapshot_builder: StoreBuilder,
        creation_intent_builder: StoreBuilder,
        interaction_audit_builder: StoreBuilder,
        resource_builder: StoreBuilder,
        result_snapshot_builder: StoreBuilder,
        continuation_builder: StoreBuilder = SQLiteWeaponryStepContinuationStore,
    ) -> None:
        if not isinstance(transaction_manager, SQLiteTransactionManager):
            raise TypeError("transaction_manager 必须是 SQLiteTransactionManager")
        builders = {
            "execution_builder": execution_builder,
            "callback_delivery_builder": callback_delivery_builder,
            "document_snapshot_builder": document_snapshot_builder,
            "creation_intent_builder": creation_intent_builder,
            "interaction_audit_builder": interaction_audit_builder,
            "resource_builder": resource_builder,
            "result_snapshot_builder": result_snapshot_builder,
            "continuation_builder": continuation_builder,
        }
        if any(not callable(builder) for builder in builders.values()):
            raise TypeError("Weaponry Execution UoW 的 Store Builder 必须可调用")
        self._transaction_manager = transaction_manager
        self._builders = builders
        self._transaction: SQLiteTransaction | None = None
        self._stores: dict[str, object] = {}
        self._entered_once = False

    def __enter__(self) -> "SQLiteWeaponryExecutionUnitOfWork":
        if self._entered_once:
            raise SQLiteTransactionError(
                "unit_of_work_reentry_forbidden",
                "Weaponry Execution UnitOfWork 只能进入一次",
            )
        self._entered_once = True
        transaction = self._transaction_manager.begin(read_only=False)
        transaction.__enter__()
        self._transaction = transaction
        try:
            connection = transaction.connection
            self._stores = {
                name: builder(connection) for name, builder in self._builders.items()
            }
            if any(store is None for store in self._stores.values()):
                raise TypeError("Weaponry Execution UoW Store Builder 不得返回 None")
        except BaseException:
            try:
                transaction.__exit__(*sys.exc_info())
            finally:
                self._clear()
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        transaction = self._transaction
        if transaction is None:
            self._clear()
            return False
        try:
            return transaction.__exit__(exc_type, exc_value, traceback)
        finally:
            if not transaction.active:
                self._clear()

    def commit(self) -> None:
        transaction = self._require_transaction()
        try:
            transaction.commit()
        finally:
            if not transaction.active:
                self._clear()

    def rollback(self) -> None:
        transaction = self._require_transaction()
        try:
            transaction.rollback()
        finally:
            if not transaction.active:
                self._clear()

    def _store(self, name: str) -> object:
        self._require_transaction()
        try:
            return self._stores[name]
        except KeyError as exc:  # pragma: no cover - 构造期已完整填充。
            raise SQLiteTransactionError(
                "unit_of_work_store_missing",
                f"Weaponry Execution Store 未装配: {name}",
            ) from exc

    @property
    def execution(self) -> TaskExecutionPort:
        return cast(TaskExecutionPort, self._store("execution_builder"))

    @property
    def callback_delivery(self) -> CallbackDeliveryControlPort:
        return cast(
            CallbackDeliveryControlPort,
            self._store("callback_delivery_builder"),
        )

    @property
    def document_snapshots(self) -> WeaponryTaskDocumentSnapshotStorePort:
        return cast(
            WeaponryTaskDocumentSnapshotStorePort,
            self._store("document_snapshot_builder"),
        )

    @property
    def creation_intents(self) -> WeaponryCreationIntentStorePort:
        return cast(
            WeaponryCreationIntentStorePort,
            self._store("creation_intent_builder"),
        )

    @property
    def interaction_audits(self) -> WeaponryInteractionAuditPort:
        return cast(
            WeaponryInteractionAuditPort,
            self._store("interaction_audit_builder"),
        )

    @property
    def resources(self) -> WeaponryResourceStorePort:
        return cast(WeaponryResourceStorePort, self._store("resource_builder"))

    @property
    def results(self) -> WeaponryResultSnapshotStorePort:
        """返回与 Task 终态共享当前事务的完整 Callback 结果快照 Store。"""

        return cast(
            WeaponryResultSnapshotStorePort,
            self._store("result_snapshot_builder"),
        )

    @property
    def continuations(self) -> TaskStepContinuationStorePort:
        return cast(
            TaskStepContinuationStorePort,
            self._store("continuation_builder"),
        )

    def _require_transaction(self) -> SQLiteTransaction:
        transaction = self._transaction
        if transaction is None or not transaction.active:
            raise SQLiteTransactionError(
                "unit_of_work_not_active",
                "Weaponry Execution UnitOfWork 未处于活动事务",
            )
        return transaction

    def _clear(self) -> None:
        self._transaction = None
        self._stores = {}


class SQLiteWeaponryExecutionUnitOfWorkFactory:
    """每次调用创建新的 Weaponry 业务组合 UoW。"""

    def __init__(self, transaction_manager: SQLiteTransactionManager, **builders: StoreBuilder) -> None:
        if not isinstance(transaction_manager, SQLiteTransactionManager):
            raise TypeError("transaction_manager 必须是 SQLiteTransactionManager")
        required = {
            "execution_builder",
            "callback_delivery_builder",
            "document_snapshot_builder",
            "creation_intent_builder",
            "interaction_audit_builder",
            "resource_builder",
            "result_snapshot_builder",
        }
        optional = {"continuation_builder"}
        if not required.issubset(builders) or set(builders) - required - optional:
            raise ValueError("Weaponry Execution UoW Builder 集合不完整或包含未知项")
        if any(not callable(builder) for builder in builders.values()):
            raise TypeError("Weaponry Execution UoW 的 Store Builder 必须可调用")
        self._transaction_manager = transaction_manager
        self._builders = dict(builders)
        self._builders.setdefault(
            "continuation_builder",
            SQLiteWeaponryStepContinuationStore,
        )

    def __call__(self) -> SQLiteWeaponryExecutionUnitOfWork:
        return SQLiteWeaponryExecutionUnitOfWork(
            self._transaction_manager,
            **self._builders,
        )


__all__ = [
    "SQLiteWeaponryAdmissionUnitOfWork",
    "SQLiteWeaponryAdmissionUnitOfWorkFactory",
    "SQLiteWeaponryExecutionUnitOfWork",
    "SQLiteWeaponryExecutionUnitOfWorkFactory",
]
