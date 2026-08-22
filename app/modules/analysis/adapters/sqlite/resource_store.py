"""Analysis v2 资源事实的 SQLite Store；不依赖 ``LLMTaskService``。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import json
import logging
import sqlite3

from app.modules.analysis.domain.task_inputs import FrozenJsonObject
from app.modules.analysis.ports import (
    AnalysisExecutionRef,
    AnalysisResourceCommand,
    AnalysisResourceRecord,
    AnalysisResourceScanBatch,
    AnalysisResourceState,
)
from app.modules.tasks.adapters.sqlite.transaction import SQLiteTransactionManager
from app.modules.tasks.domain import TaskId
from app.modules.tasks.ports import require_persisted_utc

from .result_snapshot_store import _BorrowedTransaction


logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class AnalysisResourceStoreConcurrencyError(RuntimeError):
    """资源 ``state + version`` 条件写未命中的可判定并发结果。"""


class SQLiteAnalysisV2ResourceStoreAdapter:
    """直接映射 ``analysis_resource_records``，并支持借用业务 UoW 连接。"""

    def __init__(
        self,
        transaction_manager: SQLiteTransactionManager | None = None,
        *,
        connection: sqlite3.Connection | None = None,
        clock: Callable[[], str] = _utc_now,
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
        if not callable(clock):
            raise TypeError("clock 必须可调用")
        self._transactions = transaction_manager
        self._connection = connection
        self._clock = clock

    @classmethod
    def from_connection(
        cls,
        connection: sqlite3.Connection,
    ) -> "SQLiteAnalysisV2ResourceStoreAdapter":
        return cls(connection=connection)

    def _begin(self, *, read_only: bool = False):
        if self._transactions is not None:
            return self._transactions.begin(read_only=read_only)
        assert self._connection is not None
        return _BorrowedTransaction(self._connection)

    def _now(self) -> str:
        return require_persisted_utc(self._clock(), name="resource_clock")

    @staticmethod
    def _serialize(payload: FrozenJsonObject) -> str:
        return json.dumps(
            payload.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @staticmethod
    def _require_execution(
        connection: sqlite3.Connection,
        execution: AnalysisExecutionRef,
    ) -> None:
        row = connection.execute(
            "SELECT business_type, business_key, batch_id, batch_sequence "
            "FROM llm_task_executions WHERE execution_id = ?",
            (execution.task_id.value,),
        ).fetchone()
        if (
            row is None
            or row["business_type"] != "file"
            or row["business_key"] != execution.file_name
            or row["batch_id"] != execution.batch_id
            or int(row["batch_sequence"] or 0) != execution.batch_sequence
        ):
            raise ValueError("Analysis 资源与 execution 身份不一致")

    def create(self, command: AnalysisResourceCommand) -> AnalysisResourceRecord:
        if not isinstance(command, AnalysisResourceCommand):
            raise TypeError("command 必须是 AnalysisResourceCommand")
        if command.expected_state is not None or command.expected_version is not None:
            raise ValueError("资源创建不得携带 expected state/version")
        timestamp = self._now()
        with self._begin() as transaction:
            connection = transaction.connection
            self._require_execution(connection, command.execution)
            try:
                connection.execute(
                    """
                    INSERT INTO analysis_resource_records (
                        execution_id, business_type, business_key, state,
                        record_payload, version, recovery_deferral_count,
                        next_recovery_at, last_recovery_reason, created_at, updated_at
                    ) VALUES (?, 'file', ?, 'tracking', ?, 1, 0, NULL, '', ?, ?)
                    """,
                    (
                        command.execution.task_id.value,
                        command.execution.file_name,
                        self._serialize(command.record_payload),
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise AnalysisResourceStoreConcurrencyError(
                    "Analysis 资源记录已存在或身份冲突"
                ) from exc
            row = self._select(connection, command.execution.task_id)
            transaction.commit()
        return self._decode(row)

    def get(self, execution: AnalysisExecutionRef) -> AnalysisResourceRecord | None:
        if not isinstance(execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        with self._begin(read_only=True) as transaction:
            row = self._select(transaction.connection, execution.task_id)
            transaction.commit()
        if row is None:
            return None
        record = self._decode(row)
        if record.execution != execution:
            raise RuntimeError("Analysis 资源读取返回了其他 execution")
        return record

    def advance(self, command: AnalysisResourceCommand) -> AnalysisResourceRecord:
        if not isinstance(command, AnalysisResourceCommand):
            raise TypeError("command 必须是 AnalysisResourceCommand")
        if command.expected_state is None or command.expected_version is None:
            raise ValueError("资源推进必须携带 expected state/version")
        timestamp = self._now()
        with self._begin() as transaction:
            connection = transaction.connection
            self._require_execution(connection, command.execution)
            cursor = connection.execute(
                """
                UPDATE analysis_resource_records
                SET state = ?, record_payload = ?, version = version + 1,
                    recovery_deferral_count = 0, next_recovery_at = NULL,
                    last_recovery_reason = '', updated_at = ?
                WHERE execution_id = ? AND business_type = 'file'
                  AND business_key = ? AND state = ? AND version = ?
                """,
                (
                    command.target_state.value,
                    self._serialize(command.record_payload),
                    timestamp,
                    command.execution.task_id.value,
                    command.execution.file_name,
                    command.expected_state.value,
                    command.expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise AnalysisResourceStoreConcurrencyError(
                    "Analysis 资源记录 state/version 条件写未命中"
                )
            row = self._select(connection, command.execution.task_id)
            transaction.commit()
        return self._decode(row)

    def list_recoverable(self, *, limit: int) -> AnalysisResourceScanBatch:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit 必须是 1~1000 的整数")
        now = self._now()
        with self._begin() as transaction:
            connection = transaction.connection
            rows = connection.execute(
                """
                SELECT resource.*, execution.batch_id, execution.batch_sequence
                FROM analysis_resource_records AS resource
                JOIN llm_task_executions AS execution
                  ON execution.execution_id = resource.execution_id
                WHERE resource.state IN ('cleanup_pending','audit_pending')
                  AND (resource.next_recovery_at IS NULL OR resource.next_recovery_at <= ?)
                ORDER BY resource.updated_at, resource.execution_id LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            records: list[AnalysisResourceRecord] = []
            quarantined = 0
            pending = 0
            for row in rows:
                try:
                    records.append(self._decode(row))
                except (TypeError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
                    # 毒记录按当前 version 条件隔离，避免长期占据稳定扫描头部；日志只含
                    # 内部 TaskId 与异常类型，不输出资源引用或 payload。
                    cursor = connection.execute(
                        """
                        UPDATE analysis_resource_records
                        SET state = 'quarantined', version = version + 1,
                            last_recovery_reason = ?, next_recovery_at = NULL,
                            updated_at = ?
                        WHERE execution_id = ? AND version = ?
                          AND state IN ('cleanup_pending','audit_pending')
                        """,
                        (
                            f"resource_decode_{type(exc).__name__}"[:256],
                            now,
                            row["execution_id"],
                            row["version"],
                        ),
                    )
                    if cursor.rowcount == 1:
                        quarantined += 1
                    else:
                        pending += 1
                    logger.critical(
                        "Analysis v2 毒资源记录已条件隔离: task_id=%s isolated=%s error_type=%s",
                        row["execution_id"],
                        cursor.rowcount == 1,
                        type(exc).__name__,
                    )
            transaction.commit()
        return AnalysisResourceScanBatch(tuple(records), quarantined, pending)

    def defer_recovery(
        self,
        execution: AnalysisExecutionRef,
        *,
        expected_version: int,
        retry_at: str,
        reason: str,
    ) -> AnalysisResourceRecord:
        if not isinstance(execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        retry_at = require_persisted_utc(retry_at, name="retry_at")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason 必须是非空 str")
        timestamp = self._now()
        with self._begin() as transaction:
            connection = transaction.connection
            cursor = connection.execute(
                """
                UPDATE analysis_resource_records
                SET version = version + 1,
                    recovery_deferral_count = recovery_deferral_count + 1,
                    next_recovery_at = ?, last_recovery_reason = ?, updated_at = ?
                WHERE execution_id = ? AND business_key = ? AND version = ?
                  AND state IN ('cleanup_pending','audit_pending')
                """,
                (
                    retry_at,
                    reason.strip()[:256],
                    timestamp,
                    execution.task_id.value,
                    execution.file_name,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise AnalysisResourceStoreConcurrencyError(
                    "Analysis 资源恢复延期条件写未命中"
                )
            row = self._select(connection, execution.task_id)
            transaction.commit()
        return self._decode(row)

    def quarantine_recovery_record(
        self,
        execution: AnalysisExecutionRef,
        *,
        expected_state: AnalysisResourceState,
        expected_version: int,
        reason: str,
    ) -> bool:
        if not isinstance(execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        if not isinstance(expected_state, AnalysisResourceState):
            raise TypeError("expected_state 必须是 AnalysisResourceState")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason 必须是非空 str")
        with self._begin() as transaction:
            cursor = transaction.connection.execute(
                """
                UPDATE analysis_resource_records
                SET state = 'quarantined', version = version + 1,
                    next_recovery_at = NULL, last_recovery_reason = ?, updated_at = ?
                WHERE execution_id = ? AND business_key = ?
                  AND state = ? AND version = ?
                """,
                (
                    reason.strip()[:256],
                    self._now(),
                    execution.task_id.value,
                    execution.file_name,
                    expected_state.value,
                    expected_version,
                ),
            )
            transaction.commit()
        return cursor.rowcount == 1

    @staticmethod
    def _select(connection: sqlite3.Connection, task_id: TaskId):
        return connection.execute(
            """
            SELECT resource.*, execution.batch_id, execution.batch_sequence
            FROM analysis_resource_records AS resource
            JOIN llm_task_executions AS execution
              ON execution.execution_id = resource.execution_id
            WHERE resource.execution_id = ?
            """,
            (task_id.value,),
        ).fetchone()

    @staticmethod
    def _decode(row: Mapping[str, object] | None) -> AnalysisResourceRecord:
        if row is None:
            raise RuntimeError("Analysis 资源记录不存在")
        if row["business_type"] != "file":
            raise RuntimeError("Analysis 资源 business_type 无效")
        raw_payload = json.loads(str(row["record_payload"]))
        if not isinstance(raw_payload, dict):
            raise RuntimeError("Analysis 资源 payload 必须是对象")
        return AnalysisResourceRecord(
            execution=AnalysisExecutionRef(
                TaskId(str(row["execution_id"])),
                str(row["business_key"]),
                str(row["batch_id"]),
                int(row["batch_sequence"]),
            ),
            state=AnalysisResourceState(str(row["state"])),
            version=int(row["version"]),
            record_payload=FrozenJsonObject.from_mapping(raw_payload, name="analysis_resource"),
            recovery_deferral_count=int(row["recovery_deferral_count"]),
            next_recovery_at=(
                str(row["next_recovery_at"])
                if row["next_recovery_at"] is not None
                else None
            ),
            last_recovery_reason=str(row["last_recovery_reason"]),
        )


__all__ = [
    "AnalysisResourceStoreConcurrencyError",
    "SQLiteAnalysisV2ResourceStoreAdapter",
]
