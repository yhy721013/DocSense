"""Report 资源恢复事实的 SQLite 唯一物理 Writer。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
import logging
import sqlite3
from typing import Any, Protocol

from app.modules.tasks.adapters.sqlite.transaction import SQLiteTransactionManager
from app.modules.report.application.artifact_identity import report_artifact_result_ref


logger = logging.getLogger(__name__)
_RESOURCE_STATES = frozenset(
    {"tracking", "cleanup_pending", "audit_pending", "cleaned", "quarantined"}
)


class _ResourceTransaction(Protocol):
    """独立事务与 Report UoW 借用事务共同需要的最小生命周期。"""

    @property
    def connection(self) -> sqlite3.Connection: ...

    def __enter__(self) -> "_ResourceTransaction": ...

    def __exit__(self, exc_type, exc_value, traceback) -> bool | None: ...

    def commit(self) -> None: ...


class _BorrowedResourceTransaction:
    """借用 Report Execution UoW 的活动连接，绝不越权提交或回滚。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection 必须是 sqlite3.Connection")
        self._connection = connection

    @property
    def connection(self) -> sqlite3.Connection:
        if not self._connection.in_transaction:
            raise RuntimeError("Report 资源 Store 借用连接必须处于活动事务")
        return self._connection

    def __enter__(self) -> "_BorrowedResourceTransaction":
        self.connection
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        return False

    def commit(self) -> None:
        # 真正提交只能由最外层 Report Execution UoW 执行；这里保留现有方法结构，
        # 同时避免组件 Store 提前提交 Task/Step/资源原子组。
        self.connection


def _required_text(value: object, *, name: str, maximum: int = 256) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    if len(normalized) > maximum:
        raise ValueError(f"{name} 长度不能超过 {maximum}")
    return normalized


def _utc(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是包含时区的 ISO 时间")
    normalized = value.strip()
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} 必须是包含时区的 ISO 时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} 必须包含时区")
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("record_payload 必须可编码为 Canonical JSON") from exc


class SQLiteReportResourceStore:
    """提供旧映射层所需的窄原始操作，但 SQL 只存在于 Report 模块。

    每个方法使用独立短事务；Store 不执行文件删除、供应商 HTTP、Callback 或审计写入。
    CAS 未命中返回 ``None``/``False``，不会在基础设施层隐藏重试。
    """

    def __init__(
        self,
        transaction_manager: SQLiteTransactionManager | None = None,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """构造独立短事务 Store，或借用业务 UoW 的同一活动连接。

        两种模式严格互斥。生产维护/恢复使用 Transaction Manager；Report Runner 的
        Step 原子组使用 ``from_connection``，从而由最外层 UoW 一次提交。
        """

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
    ) -> "SQLiteReportResourceStore":
        """创建只在调用方 Report UoW 生命周期内有效的连接作用域 Store。"""

        return cls(connection=connection)

    def _begin(self, *, read_only: bool = False) -> _ResourceTransaction:
        if self._transactions is not None:
            return self._transactions.begin(read_only=read_only)
        assert self._borrowed_connection is not None
        return _BorrowedResourceTransaction(self._borrowed_connection)

    @staticmethod
    def _select(connection: sqlite3.Connection, execution_id: str) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT execution_id, business_type, business_key, artifact_namespace,
                   state, record_payload, version, recovery_deferral_count,
                   next_recovery_at, last_recovery_reason, created_at, updated_at
            FROM report_resource_records
            WHERE execution_id = ?
            """,
            (execution_id,),
        ).fetchone()

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        try:
            payload = json.loads(row["record_payload"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("report 资源恢复记录 JSON 已损坏") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("report 资源恢复记录 payload 必须是对象")
        return {
            "execution_id": row["execution_id"],
            "business_type": row["business_type"],
            "business_key": row["business_key"],
            "artifact_namespace": row["artifact_namespace"],
            "state": row["state"],
            "record_payload": payload,
            "version": int(row["version"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_task_execution(self, execution_id: str) -> dict[str, Any] | None:
        normalized_id = _required_text(execution_id, name="execution_id")
        with self._begin(read_only=True) as transaction:
            row = transaction.connection.execute(
                """
                SELECT execution_id, business_type, business_key,
                       execution_state, result_payload
                FROM llm_task_executions WHERE execution_id = ?
                """,
                (normalized_id,),
            ).fetchone()
            transaction.commit()
        if row is None:
            return None
        result_payload = None
        if row["result_payload"] is not None:
            try:
                result_payload = json.loads(row["result_payload"])
            except (TypeError, ValueError) as exc:
                raise RuntimeError("report execution result_payload 已损坏") from exc
        return {
            "execution_id": row["execution_id"],
            "business_type": row["business_type"],
            "business_key": row["business_key"],
            "execution_state": row["execution_state"],
            "result_payload": result_payload,
        }

    def create_report_resource_record(
        self,
        *,
        execution_id: str,
        business_type: str,
        business_key: str,
        artifact_namespace: str,
        state: str,
        record_payload: Mapping[str, Any],
        created_at: str,
    ) -> dict[str, Any]:
        normalized_id = _required_text(execution_id, name="execution_id")
        normalized_type = _required_text(business_type, name="business_type")
        if normalized_type != "report":
            raise ValueError("report 资源记录 business_type 必须是 report")
        normalized_key = _required_text(business_key, name="business_key")
        namespace = _required_text(artifact_namespace, name="artifact_namespace")
        normalized_state = _required_text(state, name="state")
        if normalized_state not in _RESOURCE_STATES:
            raise ValueError("report 资源记录 state 无效")
        if not isinstance(record_payload, Mapping):
            raise TypeError("record_payload 必须是 Mapping")
        serialized = _canonical_json(record_payload)
        timestamp = _utc(created_at, name="created_at")

        with self._begin() as transaction:
            connection = transaction.connection
            execution = connection.execute(
                """
                SELECT business_type, business_key FROM llm_task_executions
                WHERE execution_id = ?
                """,
                (normalized_id,),
            ).fetchone()
            if (
                execution is None
                or execution["business_type"] != normalized_type
                or execution["business_key"] != normalized_key
            ):
                raise ValueError("report 资源记录与 execution 身份不一致")
            existing = self._select(connection, normalized_id)
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO report_resource_records (
                        execution_id, business_type, business_key,
                        artifact_namespace, state, record_payload, version,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        normalized_id,
                        normalized_type,
                        normalized_key,
                        namespace,
                        normalized_state,
                        serialized,
                        timestamp,
                        timestamp,
                    ),
                )
                existing = self._select(connection, normalized_id)
            elif (
                existing["business_type"] != normalized_type
                or existing["business_key"] != normalized_key
                or existing["artifact_namespace"] != namespace
            ):
                raise ValueError("report 资源记录幂等键发生身份冲突")
            if existing is None:
                raise RuntimeError("report 资源记录创建后不可见")
            result = self._decode(existing)
            transaction.commit()
        logger.info(
            "Report 资源事实已登记: task_id=%s state=%s version=%d",
            normalized_id,
            result["state"],
            result["version"],
        )
        return result

    def get_report_resource_record(self, execution_id: str) -> dict[str, Any] | None:
        normalized_id = _required_text(execution_id, name="execution_id")
        with self._begin(read_only=True) as transaction:
            row = self._select(transaction.connection, normalized_id)
            transaction.commit()
        return self._decode(row) if row is not None else None

    def save_report_resource_record(
        self,
        *,
        execution_id: str,
        business_type: str,
        business_key: str,
        artifact_namespace: str,
        state: str,
        record_payload: Mapping[str, Any],
        expected_version: int,
        updated_at: str,
    ) -> dict[str, Any] | None:
        normalized_id = _required_text(execution_id, name="execution_id")
        normalized_type = _required_text(business_type, name="business_type")
        normalized_key = _required_text(business_key, name="business_key")
        namespace = _required_text(artifact_namespace, name="artifact_namespace")
        normalized_state = _required_text(state, name="state")
        if normalized_type != "report" or normalized_state not in _RESOURCE_STATES:
            raise ValueError("report 资源记录身份或 state 无效")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version 必须是正整数")
        if not isinstance(record_payload, Mapping):
            raise TypeError("record_payload 必须是 Mapping")
        serialized = _canonical_json(record_payload)
        timestamp = _utc(updated_at, name="updated_at")
        with self._begin() as transaction:
            cursor = transaction.connection.execute(
                """
                UPDATE report_resource_records
                SET state = ?, record_payload = ?, version = version + 1,
                    next_recovery_at = NULL, last_recovery_reason = '', updated_at = ?
                WHERE execution_id = ? AND business_type = ? AND business_key = ?
                  AND artifact_namespace = ? AND version = ?
                """,
                (
                    normalized_state,
                    serialized,
                    timestamp,
                    normalized_id,
                    normalized_type,
                    normalized_key,
                    namespace,
                    expected_version,
                ),
            )
            row = self._select(transaction.connection, normalized_id) if cursor.rowcount == 1 else None
            transaction.commit()
        return self._decode(row) if row is not None else None

    def prepare_report_resource_cleanup(
        self,
        execution_id: str,
        *,
        updated_at: str,
    ) -> dict[str, Any]:
        normalized_id = _required_text(execution_id, name="execution_id")
        timestamp = _utc(updated_at, name="updated_at")
        with self._begin() as transaction:
            connection = transaction.connection
            row = self._select(connection, normalized_id)
            execution = connection.execute(
                """
                SELECT execution_state, result_payload FROM llm_task_executions
                WHERE execution_id = ?
                """,
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise ValueError("report 资源记录不存在")
            if execution is None:
                raise ValueError("report execution 不存在")
            current = self._decode(row)
            if current["state"] != "tracking":
                transaction.commit()
                return current
            execution_state = execution["execution_state"]
            if execution_state not in {"succeeded", "failed", "stale"}:
                raise RuntimeError("report execution 尚未形成可清理终态")
            payload = dict(current["record_payload"])
            tracked_artifact = payload.get("final_artifact")
            retained: list[Mapping[str, Any]] = []
            if execution_state == "succeeded":
                try:
                    result_payload = json.loads(execution["result_payload"])
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("成功 report execution 缺少结果事实") from exc
                if not isinstance(result_payload, Mapping):
                    raise RuntimeError("成功 report execution 缺少结果事实")
                terminal_ref = result_payload.get("result_ref")
                if not isinstance(tracked_artifact, Mapping):
                    raise RuntimeError("成功 report execution 缺少已跟踪 Artifact")
                if terminal_ref != report_artifact_result_ref(tracked_artifact):
                    raise RuntimeError("终态 Artifact 引用与任务级资源记录不一致")
                retained = [dict(tracked_artifact)]
            payload.update(
                retained=retained,
                artifact_state="pending",
                external_state="pending" if payload.get("cleanup_ref") else "not_required",
                last_error_stage="",
                last_error_message="",
            )
            cursor = connection.execute(
                """
                UPDATE report_resource_records
                SET state = 'cleanup_pending', record_payload = ?, version = version + 1,
                    next_recovery_at = NULL, last_recovery_reason = '', updated_at = ?
                WHERE execution_id = ? AND state = 'tracking' AND version = ?
                """,
                (_canonical_json(payload), timestamp, normalized_id, current["version"]),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("report 资源清理准备 CAS 未命中")
            prepared = self._select(connection, normalized_id)
            if prepared is None:
                raise RuntimeError("report 资源清理准备后记录不可见")
            result = self._decode(prepared)
            transaction.commit()
        return result

    def defer_report_resource_recovery(
        self,
        execution_id: str,
        *,
        retry_at: str,
        reason: str,
    ) -> bool:
        normalized_id = _required_text(execution_id, name="execution_id")
        timestamp = _utc(retry_at, name="retry_at")
        normalized_reason = _required_text(reason, name="reason", maximum=256)
        with self._begin() as transaction:
            cursor = transaction.connection.execute(
                """
                UPDATE report_resource_records
                SET recovery_deferral_count = recovery_deferral_count + 1,
                    next_recovery_at = ?, last_recovery_reason = ?
                WHERE execution_id = ?
                  AND state IN ('tracking', 'cleanup_pending', 'audit_pending')
                """,
                (timestamp, normalized_reason, normalized_id),
            )
            deferred = cursor.rowcount == 1
            transaction.commit()
        logger.log(
            logging.WARNING if deferred else logging.DEBUG,
            "Report 资源恢复冷却已记录: task_id=%s deferred=%s retry_at=%s",
            normalized_id,
            deferred,
            timestamp,
        )
        return deferred

    def list_recoverable_report_resource_ids(
        self,
        *,
        limit: int,
        ready_at: str | None = None,
    ) -> tuple[str, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit 必须是正整数")
        timestamp = _utc(
            ready_at
            or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            name="ready_at",
        )
        with self._begin(read_only=True) as transaction:
            rows = transaction.connection.execute(
                """
                SELECT resource.execution_id
                FROM report_resource_records AS resource
                JOIN llm_task_executions AS execution
                  ON execution.execution_id = resource.execution_id
                WHERE (
                    resource.state IN ('cleanup_pending', 'audit_pending')
                    OR (
                        resource.state = 'tracking'
                        AND execution.execution_state IN ('succeeded', 'failed', 'stale')
                    )
                )
                  AND (resource.next_recovery_at IS NULL OR resource.next_recovery_at <= ?)
                ORDER BY resource.updated_at, resource.execution_id
                LIMIT ?
                """,
                (timestamp, limit),
            ).fetchall()
            transaction.commit()
        return tuple(str(row["execution_id"]) for row in rows)


__all__ = ["SQLiteReportResourceStore", "report_artifact_result_ref"]
