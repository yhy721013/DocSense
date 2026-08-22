"""Analysis 终态 Callback 快照的 SQLite 唯一物理 Writer。"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from app.modules.analysis.domain.task_inputs import FrozenJsonObject
from app.modules.analysis.ports import AnalysisResultSnapshot
from app.modules.tasks.adapters.sqlite.transaction import SQLiteTransactionManager
from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.tasks.ports import require_persisted_utc


class _BorrowedTransaction:
    """借用外层 UoW 连接；commit/rollback 的唯一所有者仍是外层。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> "_BorrowedTransaction":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        return False

    def commit(self) -> None:
        return None


class SQLiteAnalysisResultSnapshotStore:
    """保存完整公开 Callback JSON；支持独立事务与业务组合 UoW 借用连接。"""

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
        self._connection = connection

    @classmethod
    def from_connection(cls, connection: sqlite3.Connection) -> "SQLiteAnalysisResultSnapshotStore":
        return cls(connection=connection)

    def _begin(self, *, read_only: bool = False):
        if self._transactions is not None:
            return self._transactions.begin(read_only=read_only)
        assert self._connection is not None
        return _BorrowedTransaction(self._connection)

    def save(
        self,
        *,
        task_id: TaskId,
        business_ref: TaskBusinessRef,
        payload: FrozenJsonObject,
        created_at: str,
    ) -> AnalysisResultSnapshot:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if (
            not isinstance(business_ref, TaskBusinessRef)
            or business_ref.business_type != "file"
        ):
            raise TypeError("business_ref 必须是 file TaskBusinessRef")
        if not isinstance(payload, FrozenJsonObject):
            raise TypeError("payload 必须是 FrozenJsonObject")
        timestamp = require_persisted_utc(created_at, name="created_at")
        serialized = json.dumps(
            payload.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        with self._begin() as transaction:
            connection = transaction.connection
            execution = connection.execute(
                "SELECT business_type, business_key, batch_id, batch_sequence "
                "FROM llm_task_executions "
                "WHERE execution_id = ?",
                (task_id.value,),
            ).fetchone()
            if (
                execution is None
                or execution["business_type"] != "file"
                or execution["business_key"] != business_ref.business_key
            ):
                raise ValueError("结果快照与 Analysis execution 身份不一致")
            existing = connection.execute(
                "SELECT callback_payload_json, result_digest, created_at "
                "FROM analysis_result_snapshots WHERE task_id = ?",
                (task_id.value,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO analysis_result_snapshots (
                        task_id, business_key, result_schema_version,
                        callback_payload_json, result_digest, created_at
                    ) VALUES (?, ?, 1, ?, ?, ?)
                    """,
                    (task_id.value, business_ref.business_key, serialized, digest, timestamp),
                )
            elif existing["callback_payload_json"] != serialized or existing["result_digest"] != digest:
                raise ValueError("同一 TaskId 已存在不同 Analysis 结果快照")
            else:
                timestamp = require_persisted_utc(str(existing["created_at"]), name="created_at")
            transaction.commit()
        return AnalysisResultSnapshot(
            task_id,
            business_ref,
            payload,
            digest,
            timestamp,
            str(execution["batch_id"]),
            int(execution["batch_sequence"]),
        )

    def get(self, task_id: TaskId) -> AnalysisResultSnapshot | None:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        with self._begin(read_only=True) as transaction:
            row = transaction.connection.execute(
                """
                SELECT result.business_key, result.result_schema_version,
                       result.callback_payload_json, result.result_digest,
                       result.created_at, execution.business_type,
                       execution.business_key AS execution_business_key,
                       execution.batch_id, execution.batch_sequence
                FROM analysis_result_snapshots AS result
                JOIN llm_task_executions AS execution
                  ON execution.execution_id = result.task_id
                WHERE result.task_id = ?
                """,
                (task_id.value,),
            ).fetchone()
            transaction.commit()
        if row is None:
            return None
        if int(row["result_schema_version"]) != 1:
            raise RuntimeError("Analysis 结果快照 Schema 版本不受支持")
        serialized = str(row["callback_payload_json"])
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        if digest != row["result_digest"]:
            raise RuntimeError("Analysis 结果快照摘要不一致")
        if (
            row["business_type"] != "file"
            or row["execution_business_key"] != row["business_key"]
        ):
            raise RuntimeError("Analysis 结果快照与 execution 身份不一致")
        try:
            raw = json.loads(serialized)
            if not isinstance(raw, dict):
                raise ValueError("Callback 顶层必须是对象")
            payload = FrozenJsonObject.from_mapping(raw, name="analysis_callback_payload")
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Analysis 结果快照已损坏") from exc
        return AnalysisResultSnapshot(
            task_id,
            TaskBusinessRef("file", str(row["business_key"])),
            payload,
            digest,
            require_persisted_utc(str(row["created_at"]), name="created_at"),
            str(row["batch_id"]),
            int(row["batch_sequence"]),
        )


__all__ = ["SQLiteAnalysisResultSnapshotStore"]
