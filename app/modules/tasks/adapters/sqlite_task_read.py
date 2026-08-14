"""Task Control v2 到统一 TaskReadPort 的只读适配器。"""

from __future__ import annotations

from app.modules.tasks.adapters.sqlite import SQLiteTaskControlStore
from app.modules.tasks.adapters.sqlite.transaction import SQLiteTransactionManager
from app.modules.tasks.domain import TaskBusinessRef, TaskId, TaskSnapshot


class SQLiteTaskControlReadAdapter:
    """每次读取使用独立短只读事务，不持有连接且不产生状态转换。"""

    def __init__(self, transaction_manager: SQLiteTransactionManager) -> None:
        if not isinstance(transaction_manager, SQLiteTransactionManager):
            raise TypeError("transaction_manager 必须是 SQLiteTransactionManager")
        self._transactions = transaction_manager

    def get_by_id(self, task_id: TaskId) -> TaskSnapshot | None:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        with self._transactions.begin(read_only=True) as transaction:
            snapshot = SQLiteTaskControlStore(
                transaction.connection
            ).read_snapshot_by_id(task_id)
            transaction.commit()
        return snapshot

    def get_latest(self, business_ref: TaskBusinessRef) -> TaskSnapshot | None:
        if not isinstance(business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        with self._transactions.begin(read_only=True) as transaction:
            snapshot = SQLiteTaskControlStore(
                transaction.connection
            ).read_latest_snapshot(business_ref)
            transaction.commit()
        return snapshot

    def get_latest_many(
        self,
        business_refs: tuple[TaskBusinessRef, ...],
    ) -> tuple[TaskSnapshot | None, ...]:
        refs = tuple(business_refs)
        if any(not isinstance(item, TaskBusinessRef) for item in refs):
            raise TypeError("business_refs 只能包含 TaskBusinessRef")
        # 一个请求批次共享同一只读快照，确保顺序和缺失位置严格与输入一致。
        with self._transactions.begin(read_only=True) as transaction:
            store = SQLiteTaskControlStore(transaction.connection)
            snapshots = tuple(store.read_latest_snapshot(item) for item in refs)
            transaction.commit()
        return snapshots


__all__ = ["SQLiteTaskControlReadAdapter"]
