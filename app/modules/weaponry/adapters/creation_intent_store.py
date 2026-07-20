"""SQLite 武器谱外部创建意图 Store。"""

from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Iterator

from app.modules.tasks.domain import TaskId
from app.modules.weaponry.ports import (
    QuarantineWeaponryCreationIntent,
    ResolveWeaponryCreationIntent,
    WeaponryCreationIntent,
    WeaponryCreationIntentKind,
    WeaponryCreationIntentReserveResult,
    WeaponryCreationIntentState,
    WeaponryPortStateError,
)


class SQLiteWeaponryCreationIntentStoreAdapter:
    """用唯一键和版本 CAS 保存 create 前置事实。

    表只保存资源身份、摘要和状态，不保存文件正文、Prompt、供应商 Token 或完整 URL。
    每个方法都使用独立短事务，为阶段 3 平移到 MySQL Repository 保留清晰边界。
    """

    def __init__(self, db_path: str, *, busy_timeout_ms: int = 30_000) -> None:
        if not isinstance(db_path, str) or not db_path.strip():
            raise ValueError("db_path 必须是非空 str")
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or busy_timeout_ms < 1
        ):
            raise ValueError("busy_timeout_ms 必须是正整数")
        self._db_path = str(Path(db_path))
        self._busy_timeout_ms = busy_timeout_ms
        self._initialize_schema()

    def reserve(
        self, intent: WeaponryCreationIntent
    ) -> WeaponryCreationIntentReserveResult:
        if not isinstance(intent, WeaponryCreationIntent):
            raise TypeError("intent 必须是 WeaponryCreationIntent")
        if intent.state is not WeaponryCreationIntentState.PENDING or intent.version != 0:
            raise ValueError("新建意图必须从 pending/version=0 开始")
        with self._transaction() as connection:
            existing = self._select(connection, intent.task_id, intent.intent_id)
            if existing is not None:
                decoded = self._decode(existing)
                if self._immutable_identity(decoded) != self._immutable_identity(intent):
                    raise WeaponryPortStateError(
                        "creation_intent_identity_conflict",
                        "同一 intent_id 已存在不同创建身份",
                    )
                return WeaponryCreationIntentReserveResult(False, decoded)
            connection.execute(
                """
                INSERT INTO weaponry_creation_intents (
                    task_id, intent_id, kind, expected_name, identity_digest,
                    parent_external_ref, document_key, call_id, state,
                    external_ref, error_code, version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._values(intent),
            )
        return WeaponryCreationIntentReserveResult(True, intent)

    def get(self, task_id: TaskId, intent_id: str) -> WeaponryCreationIntent | None:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        normalized_intent_id = str(intent_id or "").strip()
        if not normalized_intent_id:
            raise ValueError("intent_id 不能为空")
        with closing(self._connect()) as connection:
            row = self._select(connection, task_id, normalized_intent_id)
        return None if row is None else self._decode(row)

    def resolve(self, command: ResolveWeaponryCreationIntent) -> WeaponryCreationIntent:
        if not isinstance(command, ResolveWeaponryCreationIntent):
            raise TypeError("command 必须是 ResolveWeaponryCreationIntent")
        with self._transaction() as connection:
            current = self._require(connection, command.task_id, command.intent_id)
            if current.state is WeaponryCreationIntentState.RESOLVED:
                if current.external_ref != command.external_ref:
                    raise WeaponryPortStateError(
                        "creation_intent_external_ref_conflict",
                        "创建意图已经解析为其他外部资源",
                    )
                return current
            self._require_pending_version(current, command.expected_version)
            updated = replace(
                current,
                state=WeaponryCreationIntentState.RESOLVED,
                external_ref=command.external_ref,
                version=current.version + 1,
            )
            self._save(connection, updated, expected_version=current.version)
            return updated

    def quarantine(
        self, command: QuarantineWeaponryCreationIntent
    ) -> WeaponryCreationIntent:
        if not isinstance(command, QuarantineWeaponryCreationIntent):
            raise TypeError("command 必须是 QuarantineWeaponryCreationIntent")
        with self._transaction() as connection:
            current = self._require(connection, command.task_id, command.intent_id)
            if current.state is WeaponryCreationIntentState.QUARANTINED:
                if current.error_code == command.error_code:
                    return current
                raise WeaponryPortStateError(
                    "creation_intent_quarantine_conflict",
                    "创建意图已经按其他原因隔离",
                )
            self._require_pending_version(current, command.expected_version)
            updated = replace(
                current,
                state=WeaponryCreationIntentState.QUARANTINED,
                error_code=command.error_code,
                version=current.version + 1,
            )
            self._save(connection, updated, expected_version=current.version)
            return updated

    def list_pending(self, *, limit: int) -> tuple[WeaponryCreationIntent, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM weaponry_creation_intents
                WHERE state = 'pending'
                ORDER BY task_id, intent_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(self._decode(row) for row in rows)

    def _initialize_schema(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS weaponry_creation_intents (
                    task_id TEXT NOT NULL,
                    intent_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    expected_name TEXT NOT NULL,
                    identity_digest TEXT NOT NULL,
                    parent_external_ref TEXT NOT NULL,
                    document_key TEXT NOT NULL,
                    call_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    external_ref TEXT NOT NULL,
                    error_code TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    PRIMARY KEY (task_id, intent_id)
                );
                CREATE INDEX IF NOT EXISTS idx_weaponry_creation_intents_pending
                ON weaponry_creation_intents (state, task_id, intent_id);
                """
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
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
    def _select(
        connection: sqlite3.Connection, task_id: TaskId, intent_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM weaponry_creation_intents WHERE task_id = ? AND intent_id = ?",
            (task_id.value, intent_id),
        ).fetchone()

    def _require(
        self, connection: sqlite3.Connection, task_id: TaskId, intent_id: str
    ) -> WeaponryCreationIntent:
        row = self._select(connection, task_id, intent_id)
        if row is None:
            raise WeaponryPortStateError("creation_intent_not_found", "创建意图不存在")
        return self._decode(row)

    @staticmethod
    def _save(
        connection: sqlite3.Connection,
        intent: WeaponryCreationIntent,
        *,
        expected_version: int,
    ) -> None:
        updated = connection.execute(
            """
            UPDATE weaponry_creation_intents
            SET state = ?, external_ref = ?, error_code = ?, version = ?
            WHERE task_id = ? AND intent_id = ? AND version = ?
            """,
            (
                intent.state.value,
                intent.external_ref,
                intent.error_code,
                intent.version,
                intent.task_id.value,
                intent.intent_id,
                expected_version,
            ),
        ).rowcount
        if updated != 1:
            raise WeaponryPortStateError(
                "creation_intent_version_conflict", "创建意图版本不一致"
            )

    @staticmethod
    def _require_pending_version(
        intent: WeaponryCreationIntent, expected_version: int
    ) -> None:
        if intent.version != expected_version:
            raise WeaponryPortStateError(
                "creation_intent_version_conflict", "创建意图版本不一致"
            )
        if intent.state is not WeaponryCreationIntentState.PENDING:
            raise WeaponryPortStateError(
                "creation_intent_not_pending", "创建意图已经离开 pending 状态"
            )

    @staticmethod
    def _immutable_identity(intent: WeaponryCreationIntent) -> tuple[object, ...]:
        return (
            intent.task_id,
            intent.intent_id,
            intent.kind,
            intent.expected_name,
            intent.identity_digest,
            intent.parent_external_ref,
            intent.document_key,
            intent.call_id,
        )

    @staticmethod
    def _values(intent: WeaponryCreationIntent) -> tuple[object, ...]:
        return (
            intent.task_id.value,
            intent.intent_id,
            intent.kind.value,
            intent.expected_name,
            intent.identity_digest,
            intent.parent_external_ref,
            intent.document_key,
            intent.call_id,
            intent.state.value,
            intent.external_ref,
            intent.error_code,
            intent.version,
        )

    @staticmethod
    def _decode(row: sqlite3.Row) -> WeaponryCreationIntent:
        try:
            return WeaponryCreationIntent(
                task_id=TaskId(str(row["task_id"])),
                intent_id=str(row["intent_id"]),
                kind=WeaponryCreationIntentKind(str(row["kind"])),
                expected_name=str(row["expected_name"]),
                identity_digest=str(row["identity_digest"]),
                parent_external_ref=str(row["parent_external_ref"]),
                document_key=str(row["document_key"]),
                call_id=str(row["call_id"]),
                state=WeaponryCreationIntentState(str(row["state"])),
                external_ref=str(row["external_ref"]),
                error_code=str(row["error_code"]),
                version=int(row["version"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WeaponryPortStateError(
                "creation_intent_payload_invalid", "创建意图持久化字段无效"
            ) from exc


__all__ = ["SQLiteWeaponryCreationIntentStoreAdapter"]
