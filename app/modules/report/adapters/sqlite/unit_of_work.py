"""Report 组件与 Task Control 共用连接的短事务 Unit of Work。"""

from __future__ import annotations

from collections.abc import Callable
import sqlite3
import sys
from typing import cast

from app.modules.report.ports import ReportResourceStorePort
from app.modules.report.adapters.sqlite.step_continuation_store import (
    SQLiteReportStepContinuationStore,
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


class SQLiteReportExecutionUnitOfWork:
    """组合 Task、Callback 与 Report Resource Store，但不暴露原始 Connection。"""

    def __init__(
        self,
        transaction_manager: SQLiteTransactionManager,
        *,
        execution_builder: StoreBuilder,
        callback_delivery_builder: StoreBuilder,
        resource_builder: StoreBuilder,
        continuation_builder: StoreBuilder = SQLiteReportStepContinuationStore,
    ) -> None:
        if not isinstance(transaction_manager, SQLiteTransactionManager):
            raise TypeError("transaction_manager 必须是 SQLiteTransactionManager")
        for name, builder in (
            ("execution_builder", execution_builder),
            ("callback_delivery_builder", callback_delivery_builder),
            ("resource_builder", resource_builder),
            ("continuation_builder", continuation_builder),
        ):
            if not callable(builder):
                raise TypeError(f"{name} 必须可调用")
        self._transaction_manager = transaction_manager
        self._execution_builder = execution_builder
        self._callback_delivery_builder = callback_delivery_builder
        self._resource_builder = resource_builder
        self._continuation_builder = continuation_builder
        self._transaction: SQLiteTransaction | None = None
        self._execution: object | None = None
        self._callback_delivery: object | None = None
        self._resources: object | None = None
        self._continuations: object | None = None
        self._entered_once = False

    def __enter__(self) -> "SQLiteReportExecutionUnitOfWork":
        if self._entered_once:
            raise SQLiteTransactionError(
                "unit_of_work_reentry_forbidden",
                "Report Execution UnitOfWork 只能进入一次",
            )
        self._entered_once = True
        transaction = self._transaction_manager.begin(read_only=False)
        transaction.__enter__()
        self._transaction = transaction
        try:
            connection = transaction.connection
            self._execution = self._execution_builder(connection)
            self._callback_delivery = self._callback_delivery_builder(connection)
            self._resources = self._resource_builder(connection)
            self._continuations = self._continuation_builder(connection)
            if any(
                value is None
                for value in (
                    self._execution,
                    self._callback_delivery,
                    self._resources,
                    self._continuations,
                )
            ):
                raise TypeError("Report Execution UoW Store Builder 不得返回 None")
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

    @property
    def execution(self) -> TaskExecutionPort:
        self._require_transaction()
        if self._execution is None:
            raise SQLiteTransactionError(
                "unit_of_work_store_missing",
                "Report Execution Store 未装配",
            )
        return cast(TaskExecutionPort, self._execution)

    @property
    def callback_delivery(self) -> CallbackDeliveryControlPort:
        self._require_transaction()
        if self._callback_delivery is None:
            raise SQLiteTransactionError(
                "unit_of_work_store_missing",
                "Report Callback Store 未装配",
            )
        return cast(CallbackDeliveryControlPort, self._callback_delivery)

    @property
    def resources(self) -> ReportResourceStorePort:
        self._require_transaction()
        if self._resources is None:
            raise SQLiteTransactionError(
                "unit_of_work_store_missing",
                "Report Resource Store 未装配",
            )
        return cast(ReportResourceStorePort, self._resources)

    @property
    def continuations(self) -> TaskStepContinuationStorePort:
        self._require_transaction()
        if self._continuations is None:
            raise SQLiteTransactionError(
                "unit_of_work_store_missing",
                "Report Continuation Store 未装配",
            )
        return cast(TaskStepContinuationStorePort, self._continuations)

    def _require_transaction(self) -> SQLiteTransaction:
        transaction = self._transaction
        if transaction is None or not transaction.active:
            raise SQLiteTransactionError(
                "unit_of_work_not_active",
                "Report Execution UnitOfWork 未处于活动事务",
            )
        return transaction

    def _clear(self) -> None:
        self._transaction = None
        self._execution = None
        self._callback_delivery = None
        self._resources = None
        self._continuations = None


class SQLiteReportExecutionUnitOfWorkFactory:
    """每次调用创建新的 Report 业务组合 UoW。"""

    def __init__(
        self,
        transaction_manager: SQLiteTransactionManager,
        *,
        execution_builder: StoreBuilder,
        callback_delivery_builder: StoreBuilder,
        resource_builder: StoreBuilder,
        continuation_builder: StoreBuilder = SQLiteReportStepContinuationStore,
    ) -> None:
        if not isinstance(transaction_manager, SQLiteTransactionManager):
            raise TypeError("transaction_manager 必须是 SQLiteTransactionManager")
        for name, builder in (
            ("execution_builder", execution_builder),
            ("callback_delivery_builder", callback_delivery_builder),
            ("resource_builder", resource_builder),
            ("continuation_builder", continuation_builder),
        ):
            if not callable(builder):
                raise TypeError(f"{name} 必须可调用")
        self._transaction_manager = transaction_manager
        self._execution_builder = execution_builder
        self._callback_delivery_builder = callback_delivery_builder
        self._resource_builder = resource_builder
        self._continuation_builder = continuation_builder

    def __call__(self) -> SQLiteReportExecutionUnitOfWork:
        return SQLiteReportExecutionUnitOfWork(
            self._transaction_manager,
            execution_builder=self._execution_builder,
            callback_delivery_builder=self._callback_delivery_builder,
            resource_builder=self._resource_builder,
            continuation_builder=self._continuation_builder,
        )


__all__ = [
    "SQLiteReportExecutionUnitOfWork",
    "SQLiteReportExecutionUnitOfWorkFactory",
]
