"""Weaponry 外部交互 reserve/complete 的 SQLite 原子审计 Store。"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.tasks.adapters.sqlite.transaction import SQLiteTransactionManager
from app.modules.weaponry.ports import (
    CompleteWeaponryInteraction,
    ReserveWeaponryInteraction,
    WeaponryAuditOutcome,
    WeaponryAuditReceipt,
    WeaponryAuditReservation,
    WeaponryAuditReserveOutcome,
    WeaponryAuditReserveResult,
    WeaponryCallIdentity,
    WeaponryOperation,
    WeaponryPortStateError,
)


logger = logging.getLogger(__name__)


class SQLiteWeaponryInteractionAuditAdapter:
    """把每个 attempt 的 pending 事实先于外部调用原子写入 SQLite。

    表中只保存 SHA-256、字符数、计数和稳定身份，不保存 Prompt、Evidence 正文、模型回答、
    URL、Token 或供应商原始响应。``reserve``/``complete`` 都是短 ``BEGIN IMMEDIATE`` 事务；
    调用模型、检索或翻译必须由 Application 在两个方法之间、事务之外执行。
    """

    def __init__(
        self,
        db_path: str | None = None,
        *,
        transaction_manager: SQLiteTransactionManager | None = None,
        connection: sqlite3.Connection | None = None,
        busy_timeout_ms: int = 30_000,
    ) -> None:
        modes = sum(value is not None for value in (db_path, transaction_manager, connection))
        if modes != 1:
            raise ValueError("db_path、transaction_manager 与 connection 必须且只能提供一个")
        if db_path is not None and (not isinstance(db_path, str) or not db_path.strip()):
            raise ValueError("db_path 必须是非空 str")
        if connection is not None and not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection 必须是 sqlite3.Connection")
        if transaction_manager is not None and not isinstance(
            transaction_manager,
            SQLiteTransactionManager,
        ):
            raise TypeError("transaction_manager 必须是 SQLiteTransactionManager")
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or busy_timeout_ms < 1
        ):
            raise ValueError("busy_timeout_ms 必须是正整数")
        self._db_path = str(Path(db_path)) if db_path is not None else ""
        self._borrowed_connection = connection
        self._transactions = transaction_manager
        self._strict_control_mode = db_path is None
        self._busy_timeout_ms = busy_timeout_ms
        if db_path is not None:
            self._initialize_schema()

    @classmethod
    def from_connection(
        cls,
        connection: sqlite3.Connection,
    ) -> "SQLiteWeaponryInteractionAuditAdapter":
        return cls(connection=connection)

    def reserve(
        self,
        command: ReserveWeaponryInteraction,
    ) -> WeaponryAuditReserveResult:
        if not isinstance(command, ReserveWeaponryInteraction):
            raise TypeError("command 必须是 ReserveWeaponryInteraction")
        payload = self._reserve_payload(command)
        reservation_id = self._reservation_id(command.call.attempt_key)
        now = self._now()
        outcome = WeaponryAuditReserveOutcome.RESERVED
        with self._transaction() as connection:
            if self._strict_control_mode:
                execution = connection.execute(
                    """
                    SELECT business_type, business_key FROM llm_task_executions
                    WHERE execution_id = ?
                    """,
                    (command.call.task_id.value,),
                ).fetchone()
                if (
                    execution is None
                    or execution["business_type"] != "weaponry"
                    or execution["business_key"] != command.business_ref.business_key
                ):
                    raise WeaponryPortStateError(
                        "interaction_audit_execution_identity_mismatch",
                        "交互审计与 Weaponry execution 身份不一致",
                    )
            existing = connection.execute(
                """
                SELECT reservation_id, reserve_payload_json, state
                FROM weaponry_interaction_audits
                WHERE attempt_key = ?
                """,
                (command.call.attempt_key,),
            ).fetchone()
            if existing is not None:
                if existing["reserve_payload_json"] != payload:
                    raise WeaponryPortStateError(
                        "audit_reservation_conflict",
                        "同一 attempt_key 已绑定不同审计输入",
                    )
                state = str(existing["state"])
                if state == "completed":
                    outcome = WeaponryAuditReserveOutcome.COMPLETED
                elif state == "pending":
                    outcome = WeaponryAuditReserveOutcome.PENDING
                else:
                    raise WeaponryPortStateError(
                        "audit_record_state_invalid",
                        "审计记录存在未知状态",
                    )
                reservation_id = str(existing["reservation_id"])
            else:
                connection.execute(
                    """
                    INSERT INTO weaponry_interaction_audits (
                        reservation_id, audit_id, attempt_key, task_id,
                        business_key, call_id, operation, field_sequence,
                        document_sequence, item_sequence, attempt_no, reserve_payload_json,
                        state, outcome, complete_payload_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reservation_id,
                        "",
                        command.call.attempt_key,
                        command.call.task_id.value,
                        command.business_ref.business_key,
                        command.call.call_id,
                        command.call.operation.value,
                        command.call.field_sequence,
                        command.call.document_sequence,
                        command.call.item_sequence,
                        command.call.attempt_no,
                        payload,
                        "pending",
                        "",
                        "",
                        now,
                        now,
                    ),
                )
        logger.log(
            logging.INFO
            if outcome is WeaponryAuditReserveOutcome.RESERVED
            else logging.WARNING,
            "武器谱交互审计预留分类完成: task_id=%s call_id=%s "
            "attempt_no=%d outcome=%s",
            command.call.task_id.value,
            command.call.call_id,
            command.call.attempt_no,
            outcome.value,
        )
        return WeaponryAuditReserveResult(
            outcome=outcome,
            reservation=WeaponryAuditReservation(
                reservation_id=reservation_id,
                business_ref=command.business_ref,
                call=command.call,
            ),
        )

    def complete(
        self,
        command: CompleteWeaponryInteraction,
    ) -> WeaponryAuditReceipt:
        if not isinstance(command, CompleteWeaponryInteraction):
            raise TypeError("command 必须是 CompleteWeaponryInteraction")
        reservation = command.reservation
        completion_payload = self._completion_payload(command)
        audit_id = self._audit_id(reservation.reservation_id)
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM weaponry_interaction_audits
                WHERE reservation_id = ?
                """,
                (reservation.reservation_id,),
            ).fetchone()
            if row is None:
                raise WeaponryPortStateError(
                    "audit_reservation_missing",
                    "审计完成前不存在匹配的 pending 预留",
                )
            self._verify_reservation_row(row, reservation)
            if row["state"] == "completed":
                if row["complete_payload_json"] != completion_payload:
                    raise WeaponryPortStateError(
                        "audit_completion_conflict",
                        "同一审计预留已按不同结果完成",
                    )
                audit_id = str(row["audit_id"])
            elif row["state"] != "pending":
                raise WeaponryPortStateError(
                    "audit_state_invalid",
                    "审计记录状态不允许完成",
                )
            else:
                updated = connection.execute(
                    """
                    UPDATE weaponry_interaction_audits
                    SET audit_id = ?, state = 'completed', outcome = ?,
                        complete_payload_json = ?, updated_at = ?
                    WHERE reservation_id = ? AND state = 'pending'
                    """,
                    (
                        audit_id,
                        command.outcome.value,
                        completion_payload,
                        self._now(),
                        reservation.reservation_id,
                    ),
                ).rowcount
                if updated != 1:
                    raise WeaponryPortStateError(
                        "audit_completion_cas_lost",
                        "审计完成条件更新失权",
                    )
        logger.info(
            "武器谱交互审计已完成: task_id=%s call_id=%s outcome=%s",
            reservation.call.task_id.value,
            reservation.call.call_id,
            command.outcome.value,
        )
        return WeaponryAuditReceipt(
            audit_id=audit_id,
            reservation_id=reservation.reservation_id,
            task_id=reservation.call.task_id,
            attempt_key=reservation.call.attempt_key,
        )

    def list_pending(
        self,
        task_id: TaskId,
        *,
        limit: int,
    ) -> tuple[WeaponryAuditReservation, ...]:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM weaponry_interaction_audits
                WHERE task_id = ? AND state = 'pending'
                ORDER BY created_at, attempt_key
                LIMIT ?
                """,
                (task_id.value, limit),
            ).fetchall()
        reservations = tuple(self._reservation_from_row(row) for row in rows)
        logger.info(
            "武器谱 pending 交互审计已读取: task_id=%s pending_count=%d limit=%d",
            task_id.value,
            len(reservations),
            limit,
        )
        return reservations

    def _initialize_schema(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._read_connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS weaponry_interaction_audits (
                    reservation_id TEXT PRIMARY KEY,
                    audit_id TEXT NOT NULL,
                    attempt_key TEXT NOT NULL UNIQUE,
                    task_id TEXT NOT NULL,
                    business_key TEXT NOT NULL,
                    call_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    field_sequence INTEGER NOT NULL,
                    document_sequence INTEGER,
                    item_sequence INTEGER,
                    attempt_no INTEGER NOT NULL,
                    reserve_payload_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('pending', 'completed')),
                    outcome TEXT NOT NULL,
                    complete_payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_weaponry_interaction_audits_pending
                ON weaponry_interaction_audits (task_id, state, created_at, attempt_key);
                """
            )
            # 1D-4 为 TABLE 单元格翻译增加来源内子项身份。项目已经明确不兼容历史
            # Worker/业务数据，但开发环境可能保留 1D-3B 创建的空表；这里仅做无损加列，
            # 避免 ``CREATE TABLE IF NOT EXISTS`` 留下无法写入新调用身份的旧结构。
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(weaponry_interaction_audits)"
                ).fetchall()
            }
            if "item_sequence" not in columns:
                connection.execute(
                    "ALTER TABLE weaponry_interaction_audits "
                    "ADD COLUMN item_sequence INTEGER"
                )
                logger.info("武器谱交互审计表已补充 item_sequence 列")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        if self._transactions is not None:
            with self._transactions.begin(read_only=False) as transaction:
                yield transaction.connection
                transaction.commit()
            return
        if self._borrowed_connection is not None:
            if not self._borrowed_connection.in_transaction:
                raise RuntimeError("Weaponry Interaction Audit Store 借用连接必须处于活动事务")
            yield self._borrowed_connection
            return
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        if self._transactions is not None:
            with self._transactions.begin(read_only=True) as transaction:
                yield transaction.connection
                transaction.commit()
            return
        if self._borrowed_connection is not None:
            if not self._borrowed_connection.in_transaction:
                raise RuntimeError("Weaponry Interaction Audit Store 借用连接必须处于活动事务")
            yield self._borrowed_connection
            return
        with closing(self._connect()) as connection:
            yield connection

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._db_path,
            timeout=self._busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return connection

    @staticmethod
    def _reserve_payload(command: ReserveWeaponryInteraction) -> str:
        return json.dumps(
            {
                "input_digest": command.input_digest,
                "input_chars": command.input_chars,
                "allowed_document_keys": list(command.allowed_document_keys),
                "source_marker_digests": list(command.source_marker_digests),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _completion_payload(command: CompleteWeaponryInteraction) -> str:
        return json.dumps(
            {
                "outcome": command.outcome.value,
                "output_digest": command.output_digest,
                "output_chars": command.output_chars,
                "candidate_count": command.candidate_count,
                "selected_count": command.selected_count,
                "source_count": command.source_count,
                "verified_source_count": command.verified_source_count,
                "missing_source_count": command.missing_source_count,
                "mismatched_source_count": command.mismatched_source_count,
                "rejection_reasons": list(command.rejection_reasons),
                "error_code": command.error_code,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _verify_reservation_row(
        row: sqlite3.Row,
        reservation: WeaponryAuditReservation,
    ) -> None:
        expected = (
            reservation.call.task_id.value,
            reservation.business_ref.business_key,
            reservation.call.call_id,
            reservation.call.attempt_key,
        )
        actual = (
            row["task_id"],
            row["business_key"],
            row["call_id"],
            row["attempt_key"],
        )
        if actual != expected:
            raise WeaponryPortStateError(
                "audit_reservation_identity_mismatch",
                "审计预留身份与持久化记录不一致",
            )

    @staticmethod
    def _reservation_from_row(row: sqlite3.Row) -> WeaponryAuditReservation:
        try:
            operation = WeaponryOperation(str(row["operation"]))
            call = WeaponryCallIdentity(
                task_id=TaskId(str(row["task_id"])),
                field_sequence=int(row["field_sequence"]),
                document_sequence=(
                    int(row["document_sequence"])
                    if row["document_sequence"] is not None
                    else None
                ),
                operation=operation,
                attempt_no=int(row["attempt_no"]),
                item_sequence=(
                    int(row["item_sequence"])
                    if row["item_sequence"] is not None
                    else None
                ),
            )
            reservation = WeaponryAuditReservation(
                reservation_id=str(row["reservation_id"]),
                business_ref=TaskBusinessRef(
                    "weaponry",
                    str(row["business_key"]),
                ),
                call=call,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WeaponryPortStateError(
                "audit_record_invalid",
                "pending 审计记录字段、类型或枚举值无效",
            ) from exc
        if str(row["attempt_key"]) != reservation.call.attempt_key:
            raise WeaponryPortStateError(
                "audit_record_projection_mismatch",
                "pending 审计记录身份列相互不一致",
            )
        return reservation

    @staticmethod
    def _reservation_id(attempt_key: str) -> str:
        digest = hashlib.sha256(attempt_key.encode("utf-8")).hexdigest()
        return f"weaponry-audit-reservation-{digest[:32]}"

    @staticmethod
    def _audit_id(reservation_id: str) -> str:
        digest = hashlib.sha256(
            f"completed\x1f{reservation_id}".encode("utf-8")
        ).hexdigest()
        return f"weaponry-audit-{digest[:32]}"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


__all__ = ["SQLiteWeaponryInteractionAuditAdapter"]
