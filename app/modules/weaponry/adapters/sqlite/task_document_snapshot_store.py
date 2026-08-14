"""Weaponry v2 任务文档快照的 SQLite 唯一物理 Writer。"""

from __future__ import annotations

from collections.abc import Sequence
import sqlite3
from typing import Protocol

from app.modules.tasks.adapters.sqlite.transaction import SQLiteTransactionManager
from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.weaponry.domain import WeaponryDocumentSnapshot


class _SnapshotTransaction(Protocol):
    @property
    def connection(self) -> sqlite3.Connection: ...

    def __enter__(self) -> "_SnapshotTransaction": ...

    def __exit__(self, exc_type, exc_value, traceback) -> bool | None: ...

    def commit(self) -> None: ...


class _BorrowedSnapshotTransaction:
    """借用业务 UoW 活动连接，绝不提交或回滚外层事务。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection 必须是 sqlite3.Connection")
        self._connection = connection

    @property
    def connection(self) -> sqlite3.Connection:
        if not self._connection.in_transaction:
            raise RuntimeError("Weaponry 文档快照 Store 借用连接必须处于活动事务")
        return self._connection

    def __enter__(self) -> "_BorrowedSnapshotTransaction":
        self.connection
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        return False

    def commit(self) -> None:
        self.connection


class SQLiteWeaponryTaskDocumentSnapshotStore:
    """按 TaskId 保存不可变文档身份，不使用业务键覆盖历史 execution。"""

    def __init__(
        self,
        transaction_manager: SQLiteTransactionManager | None = None,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if (transaction_manager is None) == (connection is None):
            raise ValueError("transaction_manager 与 connection 必须且只能提供一个")
        if transaction_manager is not None and not isinstance(
            transaction_manager,
            SQLiteTransactionManager,
        ):
            raise TypeError("transaction_manager 必须是 SQLiteTransactionManager")
        if connection is not None and not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection 必须是 sqlite3.Connection")
        self._transactions = transaction_manager
        self._borrowed_connection = connection

    @classmethod
    def from_connection(
        cls,
        connection: sqlite3.Connection,
    ) -> "SQLiteWeaponryTaskDocumentSnapshotStore":
        return cls(connection=connection)

    def _begin(self, *, read_only: bool = False) -> _SnapshotTransaction:
        if self._transactions is not None:
            return self._transactions.begin(read_only=read_only)
        assert self._borrowed_connection is not None
        return _BorrowedSnapshotTransaction(self._borrowed_connection)

    def replace_for_task(
        self,
        *,
        task_id: TaskId,
        business_ref: TaskBusinessRef,
        documents: Sequence[WeaponryDocumentSnapshot],
    ) -> tuple[WeaponryDocumentSnapshot, ...]:
        """在调用方短事务中写入完整快照；新库不迁移旧业务键覆盖语义。"""

        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        if business_ref.business_type != "weaponry":
            raise ValueError("文档快照 business_type 必须是 weaponry")
        frozen = tuple(documents)
        if any(not isinstance(item, WeaponryDocumentSnapshot) for item in frozen):
            raise TypeError("documents 只能包含 WeaponryDocumentSnapshot")
        if tuple(item.sequence_no for item in frozen) != tuple(
            range(1, len(frozen) + 1)
        ):
            raise ValueError("documents.sequence_no 必须从 1 连续递增")

        with self._begin() as transaction:
            connection = transaction.connection
            execution = connection.execute(
                """
                SELECT business_type, business_key
                FROM llm_task_executions WHERE execution_id = ?
                """,
                (task_id.value,),
            ).fetchone()
            if (
                execution is None
                or execution["business_type"] != "weaponry"
                or execution["business_key"] != business_ref.business_key
            ):
                raise ValueError("文档快照与 Task execution 身份不一致")
            connection.execute(
                "DELETE FROM weaponry_task_document_snapshots WHERE task_id = ?",
                (task_id.value,),
            )
            connection.executemany(
                """
                INSERT INTO weaponry_task_document_snapshots (
                    task_id, business_key, sequence_no, document_key, file_name,
                    original_name, ingested_file_name, source_architecture_id,
                    external_document_ref, anything_document_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(
                    (
                        task_id.value,
                        business_ref.business_key,
                        item.sequence_no,
                        item.document_key,
                        item.file_name,
                        item.original_name,
                        item.ingested_file_name,
                        item.source_architecture_id,
                        item.external_document_ref,
                        item.anything_document_id,
                    )
                    for item in frozen
                ),
            )
            transaction.commit()
        return frozen

    def list_for_task(self, task_id: TaskId) -> tuple[WeaponryDocumentSnapshot, ...]:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        with self._begin(read_only=True) as transaction:
            rows = transaction.connection.execute(
                """
                SELECT sequence_no, document_key, file_name, original_name,
                       ingested_file_name, source_architecture_id,
                       external_document_ref, anything_document_id
                FROM weaponry_task_document_snapshots
                WHERE task_id = ? ORDER BY sequence_no ASC
                """,
                (task_id.value,),
            ).fetchall()
            transaction.commit()
        return tuple(
            WeaponryDocumentSnapshot(
                sequence_no=int(row["sequence_no"]),
                document_key=str(row["document_key"]),
                file_name=str(row["file_name"]),
                original_name=str(row["original_name"]),
                ingested_file_name=str(row["ingested_file_name"]),
                source_architecture_id=int(row["source_architecture_id"]),
                external_document_ref=str(row["external_document_ref"]),
                anything_document_id=str(row["anything_document_id"]),
            )
            for row in rows
        )


__all__ = ["SQLiteWeaponryTaskDocumentSnapshotStore"]
