"""Task Control 三类窄 Unit of Work 的 SQLite 适配实现。

UoW 只向 Application 暴露阶段 2-1 已冻结的 Port，不暴露原始 Connection。所有 Store Builder
拿到同一个活动事务连接；Application 必须显式 ``commit``，正常离开但未提交同样回滚。
"""

from __future__ import annotations

from collections.abc import Callable
import sqlite3
import sys
from typing import cast

from app.modules.tasks.ports.callback_delivery_control import (
    CallbackAdmissionConflictPort,
    CallbackDeliveryControlPort,
)
from app.modules.tasks.ports.task_admission import TaskAdmissionPort
from app.modules.tasks.ports.task_execution import TaskExecutionPort
from app.modules.tasks.ports.task_execution import TaskRunnableQueryPort
from app.modules.tasks.ports.task_recovery import TaskRecoveryPort
from app.modules.tasks.ports.recovery_finalization import (
    TaskRecoveryFinalizationPreflightPort,
)
from app.modules.tasks.ports.recovery_resume import TaskRecoveryResumePreflightPort

from .transaction import (
    SQLiteTransaction,
    SQLiteTransactionError,
    SQLiteTransactionManager,
)


StoreBuilder = Callable[[sqlite3.Connection], object]


class _SQLiteUnitOfWorkBase:
    """共享显式事务生命周期；子类只负责装配窄 Store。"""

    def __init__(self, transaction_manager: SQLiteTransactionManager) -> None:
        if not isinstance(transaction_manager, SQLiteTransactionManager):
            raise TypeError("transaction_manager 必须是 SQLiteTransactionManager")
        self._transaction_manager = transaction_manager
        self._transaction: SQLiteTransaction | None = None
        self._entered_once = False

    def _enter_transaction(self) -> sqlite3.Connection:
        if self._entered_once:
            raise SQLiteTransactionError(
                "unit_of_work_reentry_forbidden",
                "Task Control UnitOfWork 只能进入一次",
            )
        self._entered_once = True
        transaction = self._transaction_manager.begin(read_only=False)
        transaction.__enter__()
        self._transaction = transaction
        return transaction.connection

    def _rollback_failed_enter(self) -> None:
        transaction = self._transaction
        if transaction is None:
            return
        try:
            transaction.__exit__(*sys.exc_info())
        finally:
            self._transaction = None
            self._clear_stores()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> bool:
        # 调用方可以在 ``with`` 块内显式 commit/rollback；此时事务已经关闭，Python 随后
        # 仍会调用 __exit__。这里幂等返回，不能把成功提交误报为“UoW 未活动”。
        transaction = self._transaction
        if transaction is None:
            self._clear_stores()
            return False
        try:
            return transaction.__exit__(exc_type, exc_value, traceback)
        finally:
            if not transaction.active:
                self._transaction = None
                self._clear_stores()

    def commit(self) -> None:
        transaction = self._require_transaction()
        try:
            transaction.commit()
        finally:
            if not transaction.active:
                self._transaction = None
                self._clear_stores()

    def rollback(self) -> None:
        transaction = self._require_transaction()
        try:
            transaction.rollback()
        finally:
            if not transaction.active:
                self._transaction = None
                self._clear_stores()

    def _require_transaction(self) -> SQLiteTransaction:
        transaction = self._transaction
        if transaction is None or not transaction.active:
            raise SQLiteTransactionError(
                "unit_of_work_not_active",
                "Task Control UnitOfWork 未处于活动事务",
            )
        return transaction

    def _clear_stores(self) -> None:
        raise NotImplementedError


class SQLiteTaskAdmissionUnitOfWork(_SQLiteUnitOfWorkBase):
    """Task/latest/accepted Event 与 Callback 冲突检查共用的受理原子组。"""

    def __init__(
        self,
        transaction_manager: SQLiteTransactionManager,
        *,
        admission_builder: StoreBuilder,
        callback_conflict_builder: StoreBuilder,
    ) -> None:
        super().__init__(transaction_manager)
        if not callable(admission_builder) or not callable(callback_conflict_builder):
            raise TypeError("Admission UoW Store Builder 必须可调用")
        self._admission_builder = admission_builder
        self._callback_conflict_builder = callback_conflict_builder
        self._admission: object | None = None
        self._callback_conflicts: object | None = None

    def __enter__(self) -> "SQLiteTaskAdmissionUnitOfWork":
        connection = self._enter_transaction()
        try:
            self._admission = self._admission_builder(connection)
            # 标准装配中同一个 Control Store 同时实现 Admission/Callback Conflict Port；此时
            # 复用同一实例，避免一个原子组内出现两个看似独立的业务事实写入口。测试或未来
            # 专用 Adapter 使用不同 Builder 时，仍各自基于同一活动连接构造。
            if self._callback_conflict_builder is self._admission_builder:
                self._callback_conflicts = self._admission
            else:
                self._callback_conflicts = self._callback_conflict_builder(connection)
            if self._admission is None or self._callback_conflicts is None:
                raise TypeError("Admission UoW Store Builder 不得返回 None")
        except BaseException:
            self._rollback_failed_enter()
            raise
        return self

    @property
    def admission(self) -> TaskAdmissionPort:
        self._require_transaction()
        if self._admission is None:
            raise SQLiteTransactionError("unit_of_work_store_missing", "Admission Store 未装配")
        return cast(TaskAdmissionPort, self._admission)

    @property
    def callback_conflicts(self) -> CallbackAdmissionConflictPort:
        self._require_transaction()
        if self._callback_conflicts is None:
            raise SQLiteTransactionError(
                "unit_of_work_store_missing",
                "Callback Conflict Store 未装配",
            )
        return cast(CallbackAdmissionConflictPort, self._callback_conflicts)

    def _clear_stores(self) -> None:
        self._admission = None
        self._callback_conflicts = None


class SQLiteTaskExecutionUnitOfWork(_SQLiteUnitOfWorkBase):
    """claim/start/heartbeat/Step/progress/terminal 使用的窄执行原子组。"""

    def __init__(
        self,
        transaction_manager: SQLiteTransactionManager,
        *,
        execution_builder: StoreBuilder,
        callback_delivery_builder: StoreBuilder,
    ) -> None:
        super().__init__(transaction_manager)
        if not callable(execution_builder) or not callable(callback_delivery_builder):
            raise TypeError("Execution UoW Store Builder 必须可调用")
        self._execution_builder = execution_builder
        self._callback_delivery_builder = callback_delivery_builder
        self._execution: object | None = None
        self._callback_delivery: object | None = None

    def __enter__(self) -> "SQLiteTaskExecutionUnitOfWork":
        connection = self._enter_transaction()
        try:
            self._execution = self._execution_builder(connection)
            self._callback_delivery = self._callback_delivery_builder(connection)
            if self._execution is None or self._callback_delivery is None:
                raise TypeError("Execution UoW Store Builder 不得返回 None")
        except BaseException:
            self._rollback_failed_enter()
            raise
        return self

    @property
    def execution(self) -> TaskExecutionPort:
        self._require_transaction()
        if self._execution is None:
            raise SQLiteTransactionError("unit_of_work_store_missing", "Execution Store 未装配")
        return cast(TaskExecutionPort, self._execution)

    @property
    def callback_delivery(self) -> CallbackDeliveryControlPort:
        self._require_transaction()
        if self._callback_delivery is None:
            raise SQLiteTransactionError(
                "unit_of_work_store_missing",
                "Callback Delivery Store 未装配",
            )
        return cast(CallbackDeliveryControlPort, self._callback_delivery)

    def _clear_stores(self) -> None:
        self._execution = None
        self._callback_delivery = None


class SQLiteCallbackDeliveryUnitOfWork(_SQLiteUnitOfWorkBase):
    """Callback Control 独立短事务；不暴露 Task Execution 写入口。"""

    def __init__(
        self,
        transaction_manager: SQLiteTransactionManager,
        *,
        callback_delivery_builder: StoreBuilder,
    ) -> None:
        super().__init__(transaction_manager)
        if not callable(callback_delivery_builder):
            raise TypeError("Callback Delivery UoW Store Builder 必须可调用")
        self._callback_delivery_builder = callback_delivery_builder
        self._callback_delivery: object | None = None

    def __enter__(self) -> "SQLiteCallbackDeliveryUnitOfWork":
        connection = self._enter_transaction()
        try:
            self._callback_delivery = self._callback_delivery_builder(connection)
            if self._callback_delivery is None:
                raise TypeError("Callback Delivery UoW Store Builder 不得返回 None")
        except BaseException:
            self._rollback_failed_enter()
            raise
        return self

    @property
    def callback_delivery(self) -> CallbackDeliveryControlPort:
        self._require_transaction()
        if self._callback_delivery is None:
            raise SQLiteTransactionError(
                "unit_of_work_store_missing",
                "Callback Delivery Store 未装配",
            )
        return cast(CallbackDeliveryControlPort, self._callback_delivery)

    def _clear_stores(self) -> None:
        self._callback_delivery = None


class SQLiteTaskRecoveryUnitOfWork(_SQLiteUnitOfWorkBase):
    """过期复核、Recovery Case、Observation/Decision 使用的窄恢复原子组。"""

    def __init__(
        self,
        transaction_manager: SQLiteTransactionManager,
        *,
        recovery_builder: StoreBuilder,
        callback_delivery_builder: StoreBuilder | None = None,
        finalization_preflight_builder: StoreBuilder | None = None,
        resume_preflight_builder: StoreBuilder | None = None,
    ) -> None:
        super().__init__(transaction_manager)
        if not callable(recovery_builder):
            raise TypeError("Recovery UoW Store Builder 必须可调用")
        self._recovery_builder = recovery_builder
        self._callback_delivery_builder = callback_delivery_builder
        self._finalization_preflight_builder = finalization_preflight_builder
        self._resume_preflight_builder = resume_preflight_builder
        self._recovery: object | None = None
        self._callback_delivery: object | None = None
        self._finalization_preflight: object | None = None
        self._resume_preflight: object | None = None

    def __enter__(self) -> "SQLiteTaskRecoveryUnitOfWork":
        connection = self._enter_transaction()
        try:
            self._recovery = self._recovery_builder(connection)
            if self._recovery is None:
                raise TypeError("Recovery UoW Store Builder 不得返回 None")
            if self._callback_delivery_builder is not None:
                self._callback_delivery = self._callback_delivery_builder(connection)
                if self._callback_delivery is None:
                    raise TypeError("Recovery Callback Store Builder 不得返回 None")
            if self._finalization_preflight_builder is not None:
                self._finalization_preflight = self._finalization_preflight_builder(
                    connection
                )
                if self._finalization_preflight is None:
                    raise TypeError("Recovery 终态预检 Builder 不得返回 None")
            if self._resume_preflight_builder is not None:
                self._resume_preflight = self._resume_preflight_builder(connection)
                if self._resume_preflight is None:
                    raise TypeError("Recovery 续跑预检 Builder 不得返回 None")
        except BaseException:
            self._rollback_failed_enter()
            raise
        return self

    @property
    def recovery(self) -> TaskRecoveryPort:
        self._require_transaction()
        if self._recovery is None:
            raise SQLiteTransactionError("unit_of_work_store_missing", "Recovery Store 未装配")
        return cast(TaskRecoveryPort, self._recovery)

    @property
    def callback_delivery(self) -> CallbackDeliveryControlPort:
        self._require_transaction()
        if self._callback_delivery is None:
            raise SQLiteTransactionError(
                "unit_of_work_store_missing",
                "Recovery Callback Store 未装配，恢复终态严格关闭",
            )
        return cast(CallbackDeliveryControlPort, self._callback_delivery)

    @property
    def finalization_preflight(self) -> TaskRecoveryFinalizationPreflightPort:
        self._require_transaction()
        if self._finalization_preflight is None:
            raise SQLiteTransactionError(
                "unit_of_work_store_missing",
                "Recovery 业务结果预检未装配，恢复终态严格关闭",
            )
        return cast(
            TaskRecoveryFinalizationPreflightPort,
            self._finalization_preflight,
        )

    @property
    def resume_preflight(self) -> TaskRecoveryResumePreflightPort:
        self._require_transaction()
        if self._resume_preflight is None:
            raise SQLiteTransactionError(
                "unit_of_work_store_missing",
                "Recovery 业务续跑预检未装配，retry_authorized 严格关闭",
            )
        return cast(TaskRecoveryResumePreflightPort, self._resume_preflight)

    def _clear_stores(self) -> None:
        self._recovery = None
        self._callback_delivery = None
        self._finalization_preflight = None
        self._resume_preflight = None


class SQLiteTaskControlQueryUnitOfWork:
    """独立短只读事务；不会获取 ``BEGIN IMMEDIATE`` 写锁。"""

    def __init__(
        self,
        transaction_manager: SQLiteTransactionManager,
        *,
        query_builder: StoreBuilder,
    ) -> None:
        if not isinstance(transaction_manager, SQLiteTransactionManager):
            raise TypeError("transaction_manager 必须是 SQLiteTransactionManager")
        if not callable(query_builder):
            raise TypeError("Query UoW Store Builder 必须可调用")
        self._transaction_manager = transaction_manager
        self._query_builder = query_builder
        self._transaction: SQLiteTransaction | None = None
        self._queries: object | None = None
        self._entered_once = False

    def __enter__(self) -> "SQLiteTaskControlQueryUnitOfWork":
        if self._entered_once:
            raise SQLiteTransactionError(
                "unit_of_work_reentry_forbidden",
                "Task Control Query UnitOfWork 只能进入一次",
            )
        self._entered_once = True
        transaction = self._transaction_manager.begin(read_only=True)
        transaction.__enter__()
        self._transaction = transaction
        try:
            self._queries = self._query_builder(transaction.connection)
            if self._queries is None:
                raise TypeError("Query UoW Store Builder 不得返回 None")
        except BaseException:
            transaction.__exit__(*sys.exc_info())
            self._transaction = None
            self._queries = None
            raise
        return self

    @property
    def queries(self) -> TaskRunnableQueryPort:
        transaction = self._transaction
        if transaction is None or not transaction.active or self._queries is None:
            raise SQLiteTransactionError(
                "unit_of_work_not_active",
                "Task Control Query UnitOfWork 未处于活动事务",
            )
        return cast(TaskRunnableQueryPort, self._queries)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> bool:
        transaction = self._transaction
        self._transaction = None
        self._queries = None
        if transaction is None:
            return False
        return transaction.__exit__(exc_type, exc_value, traceback)


class SQLiteTaskAdmissionUnitOfWorkFactory:
    def __init__(
        self,
        transaction_manager: SQLiteTransactionManager,
        *,
        admission_builder: StoreBuilder,
        callback_conflict_builder: StoreBuilder,
    ) -> None:
        self._transaction_manager = transaction_manager
        self._admission_builder = admission_builder
        self._callback_conflict_builder = callback_conflict_builder

    def __call__(self) -> SQLiteTaskAdmissionUnitOfWork:
        return SQLiteTaskAdmissionUnitOfWork(
            self._transaction_manager,
            admission_builder=self._admission_builder,
            callback_conflict_builder=self._callback_conflict_builder,
        )


class SQLiteTaskExecutionUnitOfWorkFactory:
    def __init__(
        self,
        transaction_manager: SQLiteTransactionManager,
        *,
        execution_builder: StoreBuilder,
        callback_delivery_builder: StoreBuilder,
    ) -> None:
        self._transaction_manager = transaction_manager
        self._execution_builder = execution_builder
        self._callback_delivery_builder = callback_delivery_builder

    def __call__(self) -> SQLiteTaskExecutionUnitOfWork:
        return SQLiteTaskExecutionUnitOfWork(
            self._transaction_manager,
            execution_builder=self._execution_builder,
            callback_delivery_builder=self._callback_delivery_builder,
        )


class SQLiteCallbackDeliveryUnitOfWorkFactory:
    def __init__(
        self,
        transaction_manager: SQLiteTransactionManager,
        *,
        callback_delivery_builder: StoreBuilder,
    ) -> None:
        self._transaction_manager = transaction_manager
        self._callback_delivery_builder = callback_delivery_builder

    def __call__(self) -> SQLiteCallbackDeliveryUnitOfWork:
        return SQLiteCallbackDeliveryUnitOfWork(
            self._transaction_manager,
            callback_delivery_builder=self._callback_delivery_builder,
        )


class SQLiteTaskRecoveryUnitOfWorkFactory:
    def __init__(
        self,
        transaction_manager: SQLiteTransactionManager,
        *,
        recovery_builder: StoreBuilder,
        callback_delivery_builder: StoreBuilder | None = None,
        finalization_preflight_builder: StoreBuilder | None = None,
        resume_preflight_builder: StoreBuilder | None = None,
    ) -> None:
        self._transaction_manager = transaction_manager
        self._recovery_builder = recovery_builder
        self._callback_delivery_builder = callback_delivery_builder
        self._finalization_preflight_builder = finalization_preflight_builder
        self._resume_preflight_builder = resume_preflight_builder

    def __call__(self) -> SQLiteTaskRecoveryUnitOfWork:
        return SQLiteTaskRecoveryUnitOfWork(
            self._transaction_manager,
            recovery_builder=self._recovery_builder,
            callback_delivery_builder=self._callback_delivery_builder,
            finalization_preflight_builder=self._finalization_preflight_builder,
            resume_preflight_builder=self._resume_preflight_builder,
        )


class SQLiteTaskControlQueryUnitOfWorkFactory:
    def __init__(
        self,
        transaction_manager: SQLiteTransactionManager,
        *,
        query_builder: StoreBuilder,
    ) -> None:
        self._transaction_manager = transaction_manager
        self._query_builder = query_builder

    def __call__(self) -> SQLiteTaskControlQueryUnitOfWork:
        return SQLiteTaskControlQueryUnitOfWork(
            self._transaction_manager,
            query_builder=self._query_builder,
        )


__all__ = [
    "SQLiteTaskAdmissionUnitOfWork",
    "SQLiteTaskAdmissionUnitOfWorkFactory",
    "SQLiteTaskExecutionUnitOfWork",
    "SQLiteTaskExecutionUnitOfWorkFactory",
    "SQLiteCallbackDeliveryUnitOfWork",
    "SQLiteCallbackDeliveryUnitOfWorkFactory",
    "SQLiteTaskRecoveryUnitOfWork",
    "SQLiteTaskRecoveryUnitOfWorkFactory",
    "SQLiteTaskControlQueryUnitOfWork",
    "SQLiteTaskControlQueryUnitOfWorkFactory",
]
