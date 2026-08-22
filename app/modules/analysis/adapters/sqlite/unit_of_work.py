"""Analysis 组件与 Task Control 共用连接的短事务 Unit of Work。"""

from __future__ import annotations

from collections.abc import Callable
import sqlite3
import sys
from typing import cast

from app.modules.analysis.ports import AnalysisResourcePort, AnalysisResultSnapshotStorePort
from app.modules.analysis.adapters.sqlite.step_continuation_store import (
    SQLiteAnalysisStepContinuationStore,
)
from app.modules.tasks.adapters.sqlite.transaction import (
    SQLiteTransaction,
    SQLiteTransactionError,
    SQLiteTransactionManager,
)
from app.modules.tasks.ports import (
    CallbackDeliveryControlPort,
    TaskExecutionPort,
    TaskStepContinuationStorePort,
)


StoreBuilder = Callable[[sqlite3.Connection], object]


class SQLiteAnalysisExecutionUnitOfWork:
    """组合 Task、Callback 和 Analysis 组件 Store，不暴露原始连接。"""

    def __init__(
        self,
        transaction_manager: SQLiteTransactionManager,
        *,
        execution_builder: StoreBuilder,
        callback_delivery_builder: StoreBuilder,
        resource_builder: StoreBuilder,
        result_snapshot_builder: StoreBuilder,
        continuation_builder: StoreBuilder = SQLiteAnalysisStepContinuationStore,
    ) -> None:
        if not isinstance(transaction_manager, SQLiteTransactionManager):
            raise TypeError("transaction_manager 必须是 SQLiteTransactionManager")
        builders = {
            "execution": execution_builder,
            "callback_delivery": callback_delivery_builder,
            "resources": resource_builder,
            "results": result_snapshot_builder,
            "continuations": continuation_builder,
        }
        if any(not callable(builder) for builder in builders.values()):
            raise TypeError("Analysis Execution UoW 的 Store Builder 必须可调用")
        self._transaction_manager = transaction_manager
        self._builders = builders
        self._transaction: SQLiteTransaction | None = None
        self._stores: dict[str, object] = {}
        self._entered_once = False

    def __enter__(self) -> "SQLiteAnalysisExecutionUnitOfWork":
        if self._entered_once:
            raise SQLiteTransactionError(
                "unit_of_work_reentry_forbidden",
                "Analysis Execution UnitOfWork 只能进入一次",
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
                raise TypeError("Analysis Execution UoW Store Builder 不得返回 None")
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
        except KeyError as exc:  # pragma: no cover - 构造时已检查完整集合。
            raise SQLiteTransactionError(
                "unit_of_work_store_missing",
                f"Analysis Execution Store 未装配: {name}",
            ) from exc

    @property
    def execution(self) -> TaskExecutionPort:
        return cast(TaskExecutionPort, self._store("execution"))

    @property
    def callback_delivery(self) -> CallbackDeliveryControlPort:
        return cast(CallbackDeliveryControlPort, self._store("callback_delivery"))

    @property
    def resources(self) -> AnalysisResourcePort:
        return cast(AnalysisResourcePort, self._store("resources"))

    @property
    def results(self) -> AnalysisResultSnapshotStorePort:
        return cast(AnalysisResultSnapshotStorePort, self._store("results"))

    @property
    def continuations(self) -> TaskStepContinuationStorePort:
        return cast(TaskStepContinuationStorePort, self._store("continuations"))

    def _require_transaction(self) -> SQLiteTransaction:
        transaction = self._transaction
        if transaction is None or not transaction.active:
            raise SQLiteTransactionError(
                "unit_of_work_not_active",
                "Analysis Execution UnitOfWork 未处于活动事务",
            )
        return transaction

    def _clear(self) -> None:
        self._transaction = None
        self._stores = {}


class SQLiteAnalysisExecutionUnitOfWorkFactory:
    """为每次业务条件写创建独立 Analysis 组合 UoW。"""

    def __init__(
        self,
        transaction_manager: SQLiteTransactionManager,
        **builders: StoreBuilder,
    ) -> None:
        if not isinstance(transaction_manager, SQLiteTransactionManager):
            raise TypeError("transaction_manager 必须是 SQLiteTransactionManager")
        required = {
            "execution_builder",
            "callback_delivery_builder",
            "resource_builder",
            "result_snapshot_builder",
        }
        optional = {"continuation_builder"}
        if not required.issubset(builders) or set(builders) - required - optional:
            raise ValueError("Analysis Execution UoW Builder 集合不完整或包含未知项")
        if any(not callable(builder) for builder in builders.values()):
            raise TypeError("Analysis Execution UoW 的 Store Builder 必须可调用")
        self._transaction_manager = transaction_manager
        self._builders = dict(builders)
        self._builders.setdefault(
            "continuation_builder",
            SQLiteAnalysisStepContinuationStore,
        )

    def __call__(self) -> SQLiteAnalysisExecutionUnitOfWork:
        return SQLiteAnalysisExecutionUnitOfWork(
            self._transaction_manager,
            **self._builders,
        )


__all__ = [
    "SQLiteAnalysisExecutionUnitOfWork",
    "SQLiteAnalysisExecutionUnitOfWorkFactory",
]
