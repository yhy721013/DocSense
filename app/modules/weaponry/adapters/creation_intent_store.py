"""SQLite 武器谱外部创建意图 Store。"""

from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.modules.tasks.domain import TaskId
from app.modules.weaponry.ports import (
    ClaimWeaponryCreationIntentRecovery,
    CompleteWeaponryCreationIntentRecovery,
    QuarantineWeaponryCreationIntent,
    QuarantineWeaponryCreationIntentRecovery,
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
                    parent_external_ref, document_key, call_id,
                    owner_instance_id, state, external_ref, error_code,
                    recovery_fencing_token, recovery_lease_until, version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                if current.recovery_fencing_token > 0:
                    raise WeaponryPortStateError(
                        "creation_intent_recovery_fenced",
                        "创建意图已由恢复器终结，旧 Worker 不得重复提交",
                    )
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
                if current.recovery_fencing_token > 0:
                    raise WeaponryPortStateError(
                        "creation_intent_recovery_fenced",
                        "创建意图已由恢复器终结，旧 Worker 不得重复提交",
                    )
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

    def claim_recovery(
        self,
        command: ClaimWeaponryCreationIntentRecovery,
    ) -> WeaponryCreationIntent:
        """原子取得遗留意图恢复权；当前进程创建中的 pending 不允许自我接管。"""

        if not isinstance(command, ClaimWeaponryCreationIntentRecovery):
            raise TypeError("command 必须是 ClaimWeaponryCreationIntentRecovery")
        observed_at = self._parse_timestamp(command.observed_at)
        lease_until = self._parse_timestamp(command.lease_until)
        if lease_until <= observed_at:
            raise ValueError("恢复租约截止时间必须晚于 observed_at")

        with self._transaction() as connection:
            current = self._require(
                connection,
                command.task_id,
                command.intent_id,
            )
            self._require_version(current, command.expected_version)
            if current.state is WeaponryCreationIntentState.PENDING:
                if current.owner_instance_id == command.recovery_owner_id:
                    raise WeaponryPortStateError(
                        "creation_intent_owned_by_active_instance",
                        "当前进程不得恢复自己仍在创建的意图",
                    )
                fencing_token = current.recovery_fencing_token + 1
            elif current.state is WeaponryCreationIntentState.RECOVERING:
                lease_is_active = (
                    self._parse_timestamp(current.recovery_lease_until)
                    > observed_at
                )
                if (
                    current.owner_instance_id != command.recovery_owner_id
                    and lease_is_active
                ):
                    raise WeaponryPortStateError(
                        "creation_intent_recovery_lease_active",
                        "创建意图恢复租约仍由其他实例持有",
                    )
                fencing_token = current.recovery_fencing_token
                if current.owner_instance_id != command.recovery_owner_id:
                    fencing_token += 1
            else:
                raise WeaponryPortStateError(
                    "creation_intent_not_recoverable",
                    "创建意图已经离开可恢复状态",
                )

            updated = replace(
                current,
                owner_instance_id=command.recovery_owner_id,
                state=WeaponryCreationIntentState.RECOVERING,
                recovery_fencing_token=fencing_token,
                recovery_lease_until=lease_until.astimezone(timezone.utc).isoformat(),
                version=current.version + 1,
            )
            self._save(connection, updated, expected_version=current.version)
            return updated

    def complete_recovery(
        self,
        command: CompleteWeaponryCreationIntentRecovery,
    ) -> WeaponryCreationIntent:
        """只允许有效恢复所有者提交唯一查回的 workspace 标识。"""

        if not isinstance(command, CompleteWeaponryCreationIntentRecovery):
            raise TypeError(
                "command 必须是 CompleteWeaponryCreationIntentRecovery"
            )
        with self._transaction() as connection:
            current = self._require(
                connection,
                command.task_id,
                command.intent_id,
            )
            if current.state is WeaponryCreationIntentState.RESOLVED:
                self._require_recovery_identity(current, command)
                if current.external_ref != command.external_ref:
                    raise WeaponryPortStateError(
                        "creation_intent_external_ref_conflict",
                        "恢复结果已经解析为其他外部资源",
                    )
                return current
            self._require_recovery_authority(current, command)
            updated = replace(
                current,
                state=WeaponryCreationIntentState.RESOLVED,
                external_ref=command.external_ref,
                recovery_lease_until="",
                version=current.version + 1,
            )
            self._save(connection, updated, expected_version=current.version)
            return updated

    def quarantine_recovery(
        self,
        command: QuarantineWeaponryCreationIntentRecovery,
    ) -> WeaponryCreationIntent:
        """只允许有效恢复所有者冻结无法唯一查回的创建现场。"""

        if not isinstance(command, QuarantineWeaponryCreationIntentRecovery):
            raise TypeError(
                "command 必须是 QuarantineWeaponryCreationIntentRecovery"
            )
        with self._transaction() as connection:
            current = self._require(
                connection,
                command.task_id,
                command.intent_id,
            )
            if current.state is WeaponryCreationIntentState.QUARANTINED:
                self._require_recovery_identity(current, command)
                if current.error_code != command.error_code:
                    raise WeaponryPortStateError(
                        "creation_intent_quarantine_conflict",
                        "恢复意图已经按其他原因隔离",
                    )
                return current
            self._require_recovery_authority(current, command)
            updated = replace(
                current,
                state=WeaponryCreationIntentState.QUARANTINED,
                error_code=command.error_code,
                recovery_lease_until="",
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

    def list_recovery_candidates(
        self,
        *,
        active_instance_id: str,
        observed_at: str,
        limit: int,
    ) -> tuple[WeaponryCreationIntent, ...]:
        """列出非本实例 pending 或可续租的 recovering 意图。"""

        normalized_instance_id = str(active_instance_id or "").strip()
        if not normalized_instance_id:
            raise ValueError("active_instance_id 不能为空")
        normalized_observed_at = self._parse_timestamp(observed_at).isoformat()
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM weaponry_creation_intents
                WHERE (
                    state = 'pending'
                    AND (
                        owner_instance_id = ''
                        OR owner_instance_id != ?
                    )
                ) OR (
                    state = 'recovering'
                    AND (
                        owner_instance_id = ?
                        OR recovery_lease_until <= ?
                    )
                )
                ORDER BY task_id, intent_id
                LIMIT ?
                """,
                (
                    normalized_instance_id,
                    normalized_instance_id,
                    normalized_observed_at,
                    limit,
                ),
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
                    owner_instance_id TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL,
                    external_ref TEXT NOT NULL,
                    error_code TEXT NOT NULL,
                    recovery_fencing_token INTEGER NOT NULL DEFAULT 0,
                    recovery_lease_until TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL,
                    PRIMARY KEY (task_id, intent_id)
                );
                CREATE INDEX IF NOT EXISTS idx_weaponry_creation_intents_pending
                ON weaponry_creation_intents (state, task_id, intent_id);
                """
            )
            self._ensure_column(
                connection,
                "owner_instance_id",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                "recovery_fencing_token",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                "recovery_lease_until",
                "TEXT NOT NULL DEFAULT ''",
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
            SET owner_instance_id = ?, state = ?, external_ref = ?,
                error_code = ?, recovery_fencing_token = ?,
                recovery_lease_until = ?, version = ?
            WHERE task_id = ? AND intent_id = ? AND version = ?
            """,
            (
                intent.owner_instance_id,
                intent.state.value,
                intent.external_ref,
                intent.error_code,
                intent.recovery_fencing_token,
                intent.recovery_lease_until,
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
    def _require_version(
        intent: WeaponryCreationIntent,
        expected_version: int,
    ) -> None:
        if intent.version != expected_version:
            raise WeaponryPortStateError(
                "creation_intent_version_conflict",
                "创建意图版本不一致",
            )

    @classmethod
    def _require_recovery_authority(cls, intent, command) -> None:
        if intent.state is not WeaponryCreationIntentState.RECOVERING:
            raise WeaponryPortStateError(
                "creation_intent_not_recovering",
                "创建意图已经离开 recovering 状态",
            )
        cls._require_recovery_identity(intent, command)
        cls._require_version(intent, command.expected_version)

    @staticmethod
    def _require_recovery_identity(intent, command) -> None:
        if (
            intent.owner_instance_id != command.recovery_owner_id
            or intent.recovery_fencing_token
            != command.recovery_fencing_token
        ):
            raise WeaponryPortStateError(
                "creation_intent_recovery_fenced",
                "创建意图恢复所有者或 fencing token 已失效",
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
            intent.owner_instance_id,
            intent.state.value,
            intent.external_ref,
            intent.error_code,
            intent.recovery_fencing_token,
            intent.recovery_lease_until,
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
                owner_instance_id=str(row["owner_instance_id"]),
                state=WeaponryCreationIntentState(str(row["state"])),
                external_ref=str(row["external_ref"]),
                error_code=str(row["error_code"]),
                recovery_fencing_token=int(row["recovery_fencing_token"]),
                recovery_lease_until=str(row["recovery_lease_until"]),
                version=int(row["version"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WeaponryPortStateError(
                "creation_intent_payload_invalid", "创建意图持久化字段无效"
            ) from exc

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(weaponry_creation_intents)"
            ).fetchall()
        }
        if column not in columns:
            connection.execute(
                f"ALTER TABLE weaponry_creation_intents ADD COLUMN {column} {definition}"
            )

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("时间戳不能为空")
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("时间戳必须是 ISO-8601 格式") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("时间戳必须包含时区")
        return parsed.astimezone(timezone.utc)


__all__ = ["SQLiteWeaponryCreationIntentStoreAdapter"]
