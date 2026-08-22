"""SQLite Task Control Store 与三类窄 Unit of Work 的标准装配。

该模块只组装已经冻结的内部 Port，不接入 Flask Blueprint、后台线程或业务 Runner。三个 Factory
共享同一 Transaction Manager，但每次调用都会打开独立连接和独立短事务；同一 UoW 内则只创建
一个 ``SQLiteTaskControlStore`` 实例，确保 Task/latest/Event/Recovery 条件写共用原子边界。
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from collections.abc import Callable

from .control_store import SQLiteTaskControlStore
from .callback_control_store import SQLiteCallbackControlStore
from .transaction import SQLiteTransactionManager
from .unit_of_work import (
    SQLiteTaskAdmissionUnitOfWorkFactory,
    SQLiteCallbackDeliveryUnitOfWorkFactory,
    SQLiteTaskExecutionUnitOfWorkFactory,
    SQLiteTaskControlQueryUnitOfWorkFactory,
    SQLiteTaskRecoveryUnitOfWorkFactory,
)


@dataclass(frozen=True, slots=True)
class SQLiteTaskControlUnitOfWorkFactories:
    """供未来 Composition Root 注入 Application 用例的窄 Factory 集合。"""

    admission: SQLiteTaskAdmissionUnitOfWorkFactory
    execution: SQLiteTaskExecutionUnitOfWorkFactory
    recovery: SQLiteTaskRecoveryUnitOfWorkFactory
    queries: SQLiteTaskControlQueryUnitOfWorkFactory
    callback_delivery: SQLiteCallbackDeliveryUnitOfWorkFactory


def build_sqlite_task_control_uow_factories(
    transaction_manager: SQLiteTransactionManager,
    *,
    recovery_finalization_preflight_builder: Callable[[sqlite3.Connection], object]
    | None = None,
    recovery_resume_preflight_builder: Callable[[sqlite3.Connection], object]
    | None = None,
) -> SQLiteTaskControlUnitOfWorkFactories:
    """使用唯一 Control Store 实现装配三类 UoW，不产生任何生产运行时副作用。"""

    if not isinstance(transaction_manager, SQLiteTransactionManager):
        raise TypeError("transaction_manager 必须是 SQLiteTransactionManager")
    return SQLiteTaskControlUnitOfWorkFactories(
        admission=SQLiteTaskAdmissionUnitOfWorkFactory(
            transaction_manager,
            admission_builder=SQLiteTaskControlStore,
            callback_conflict_builder=SQLiteTaskControlStore,
        ),
        execution=SQLiteTaskExecutionUnitOfWorkFactory(
            transaction_manager,
            execution_builder=SQLiteTaskControlStore,
            callback_delivery_builder=SQLiteCallbackControlStore,
        ),
        recovery=SQLiteTaskRecoveryUnitOfWorkFactory(
            transaction_manager,
            recovery_builder=SQLiteTaskControlStore,
            callback_delivery_builder=SQLiteCallbackControlStore,
            finalization_preflight_builder=recovery_finalization_preflight_builder,
            resume_preflight_builder=recovery_resume_preflight_builder,
        ),
        queries=SQLiteTaskControlQueryUnitOfWorkFactory(
            transaction_manager,
            query_builder=SQLiteTaskControlStore,
        ),
        callback_delivery=SQLiteCallbackDeliveryUnitOfWorkFactory(
            transaction_manager,
            callback_delivery_builder=SQLiteCallbackControlStore,
        ),
    )


__all__ = [
    "SQLiteTaskControlUnitOfWorkFactories",
    "build_sqlite_task_control_uow_factories",
]
