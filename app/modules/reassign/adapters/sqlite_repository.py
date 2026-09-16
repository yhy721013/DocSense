"""分类节点变更本地事实的 SQLite Repository。

每个 :class:`SQLiteReassignmentUnitOfWork` 都使用独立连接和短 ``BEGIN IMMEDIATE`` 事务。
本适配器只读写 SQLite，不持有 HTTP Client，也不调用 Knowledge Port；因此 Application 可以在
每个外部写前后分别提交事实，避免把网络等待带入数据库锁范围。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.modules.reassign.domain import (
    ReassignmentContractError,
    ReassignmentBindingState,
    ReassignmentDocumentSnapshot,
    ReassignmentMutationOutcome,
    ReassignmentOperation,
    ReassignmentOperationStatus,
    ReassignmentRawValue,
    ReassignmentStep,
    ReassignmentStepName,
    ReassignmentStepState,
    ReassignmentTerminalEvidenceKind,
    architecture_id_storage_value,
    operation_holds_document_protection,
    record_step_write_intent,
    transition_operation_status,
    transition_step_state,
)
from app.modules.reassign.ports import (
    ReassignmentAuditEvent,
    ReassignmentBestEffortPinCompletion,
    ReassignmentEventType,
    ReassignmentExpiredLeaseTakeoverRequest,
    ReassignmentLease,
    ReassignmentLeaseUpdateResult,
    ReassignmentLocalCommitState,
    ReassignmentLocalCommitRequest,
    ReassignmentNoSideEffectFailureRequest,
    ReassignmentOperationRecord,
    ReassignmentOperationTransition,
    ReassignmentRecoveryCursor,
    ReassignmentRecoveryFinalizationRequest,
    ReassignmentRecoveryObservation,
    ReassignmentRecoveryObservationRecord,
    ReassignmentRepositoryPort,
    ReassignmentReservationOutcome,
    ReassignmentReservationRequest,
    ReassignmentReservationResult,
    ReassignmentStepCompletion,
    ReassignmentStepRecord,
    ReassignmentUnitOfWork,
    ReassignmentWorkspacePreparationClaim,
    ReassignmentWorkspacePreparationClaimOutcome,
    ReassignmentWorkspacePreparationClaimRequest,
    ReassignmentWorkspacePreparationClaimResult,
    ReassignmentWorkspaceMappingRequest,
    ReassignmentWorkspacePreparationFactRequest,
    ReassignmentWorkspaceOwnership,
    ReassignmentWriteOutcome,
)


logger = logging.getLogger(__name__)

_ACTIVE_OPERATION_STATUSES = tuple(
    status.value
    for status in ReassignmentOperationStatus
    if operation_holds_document_protection(status)
)
_ACTIVE_OPERATION_STATUS_SQL = ", ".join(
    f"'{status}'" for status in _ACTIVE_OPERATION_STATUSES
)
_TERMINAL_OPERATION_STATUSES = frozenset(
    {
        ReassignmentOperationStatus.SUCCEEDED,
        ReassignmentOperationStatus.FAILED,
        ReassignmentOperationStatus.COMPENSATED,
    }
)
_ACTIVE_OPERATION_INDEX_NAME = "uq_reassign_active_document"
_ACTIVE_OPERATION_INDEX_STATEMENT = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {_ACTIVE_OPERATION_INDEX_NAME}
ON reassign_operations(document_row_id)
WHERE status IN ({_ACTIVE_OPERATION_STATUS_SQL})
"""
_WORKSPACE_PREPARATION_CLAIM_ACTIVE = "active"
_WORKSPACE_PREPARATION_CLAIM_RELEASED = "released"

# Operation 已冻结文档业务主键、来源分类、AnythingLLM 引用与路径快照，因此它是独立的
# 历史审计事实，不是仍依附于 ``documents`` 当前行的子实体。活动 Operation 对业务行删除的
# 保护由 DatabaseService 的显式状态门禁负责；终态 Operation 则必须允许在文档删除后继续
# 留存。若把 document_row_id 建成外键，会迫使系统在“无法删除文档”和“破坏外键”之间二选一。
_REASSIGN_OPERATIONS_CREATE_STATEMENT = """
CREATE TABLE IF NOT EXISTS reassign_operations (
    operation_id TEXT PRIMARY KEY,
    document_row_id INTEGER NOT NULL,
    file_name TEXT NOT NULL,
    source_architecture_id INTEGER NOT NULL,
    source_architecture_raw_json TEXT NOT NULL,
    target_architecture_raw_json TEXT NOT NULL,
    anything_doc_id TEXT,
    doc_path TEXT,
    original_file_name TEXT,
    source_workspace_slug TEXT,
    target_workspace_slug TEXT,
    target_workspace_ownership TEXT,
    status TEXT NOT NULL,
    current_step TEXT,
    lease_owner TEXT NOT NULL,
    lease_token TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    error_code TEXT,
    error_summary TEXT,
    recovery_required_fencing_token INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
)
"""
_REASSIGN_OPERATION_COLUMNS = (
    "operation_id",
    "document_row_id",
    "file_name",
    "source_architecture_id",
    "source_architecture_raw_json",
    "target_architecture_raw_json",
    "anything_doc_id",
    "doc_path",
    "original_file_name",
    "source_workspace_slug",
    "target_workspace_slug",
    "target_workspace_ownership",
    "status",
    "current_step",
    "lease_owner",
    "lease_token",
    "lease_expires_at",
    "fencing_token",
    "error_code",
    "error_summary",
    "recovery_required_fencing_token",
    "created_at",
    "updated_at",
    "finished_at",
)

# ``sqlite3.Connection.executescript()`` 会在执行前隐式提交当前事务，不能用于本模块的
# 原子 Schema 初始化。每条 DDL 单独 execute，才能让缺列检查、三张表、索引和触发器共同回滚。
_SCHEMA_STATEMENTS = (
    _REASSIGN_OPERATIONS_CREATE_STATEMENT,
    _ACTIVE_OPERATION_INDEX_STATEMENT,
    """
    CREATE INDEX IF NOT EXISTS ix_reassign_operations_document_status
    ON reassign_operations(document_row_id, status, updated_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_reassign_operations_recovery_scan
    ON reassign_operations(status, lease_expires_at, operation_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS reassign_steps (
        operation_id TEXT NOT NULL,
        step_name TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL,
        write_intent_recorded INTEGER NOT NULL,
        external_reference TEXT,
        probe_outcome TEXT,
        mutation_started_at TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        last_attempt_fencing_token INTEGER,
        error_code TEXT,
        error_summary TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(operation_id, step_name),
        FOREIGN KEY(operation_id) REFERENCES reassign_operations(operation_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reassign_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id TEXT NOT NULL,
        sequence_no INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        step_name TEXT,
        operation_status TEXT,
        detail_code TEXT,
        reference_digest TEXT,
        fencing_token INTEGER,
        attempt_count INTEGER,
        probe_outcome TEXT,
        actor_digest TEXT,
        reason_code TEXT,
        occurred_at TEXT NOT NULL,
        UNIQUE(operation_id, sequence_no),
        FOREIGN KEY(operation_id) REFERENCES reassign_operations(operation_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_reassign_events_operation_sequence
    ON reassign_events(operation_id, sequence_no)
    """,
    """
    CREATE TABLE IF NOT EXISTS reassign_workspace_preparation_claims (
        target_architecture_id INTEGER PRIMARY KEY,
        operation_id TEXT NOT NULL,
        claim_owner TEXT NOT NULL,
        claim_token TEXT NOT NULL,
        fencing_token INTEGER NOT NULL,
        lease_expires_at TEXT NOT NULL,
        state TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(operation_id) REFERENCES reassign_operations(operation_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_reassign_workspace_preparation_claims_expiry
    ON reassign_workspace_preparation_claims(state, lease_expires_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS reassign_recovery_observations (
        observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id TEXT NOT NULL,
        fencing_token INTEGER NOT NULL,
        local_commit_state TEXT NOT NULL,
        source_binding_state TEXT NOT NULL,
        target_binding_state TEXT NOT NULL,
        remote_membership_required INTEGER NOT NULL,
        actor_digest TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        FOREIGN KEY(operation_id) REFERENCES reassign_operations(operation_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_reassign_recovery_observations_operation_fencing
    ON reassign_recovery_observations(operation_id, fencing_token, observation_id DESC)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS reassign_events_append_only_update
    BEFORE UPDATE ON reassign_events
    BEGIN
        SELECT RAISE(ABORT, 'reassign_events 只允许追加');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS reassign_events_append_only_delete
    BEFORE DELETE ON reassign_events
    BEGIN
        SELECT RAISE(ABORT, 'reassign_events 只允许追加');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS reassign_recovery_observations_append_only_update
    BEFORE UPDATE ON reassign_recovery_observations
    BEGIN
        SELECT RAISE(ABORT, 'reassign_recovery_observations 只允许追加');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS reassign_recovery_observations_append_only_delete
    BEFORE DELETE ON reassign_recovery_observations
    BEGIN
        SELECT RAISE(ABORT, 'reassign_recovery_observations 只允许追加');
    END
    """,
)

# 1E-2R 仍处于未接线阶段，但开发库可能已经初始化过早期 1E-2 Schema。这里仅执行
# 可回滚的加列迁移，不重建 Operation/Step/Event，更不能丢弃恢复现场。
_ADDITIVE_SCHEMA_COLUMNS = {
    "reassign_operations": (
        ("recovery_required_fencing_token", "INTEGER"),
        ("target_workspace_ownership", "TEXT"),
    ),
    "reassign_steps": (
        ("last_attempt_fencing_token", "INTEGER"),
    ),
    "reassign_events": (
        ("fencing_token", "INTEGER"),
        ("attempt_count", "INTEGER"),
        ("probe_outcome", "TEXT"),
        ("actor_digest", "TEXT"),
        ("reason_code", "TEXT"),
    ),
}


def _remove_legacy_document_foreign_key(connection: sqlite3.Connection) -> None:
    """将早期 1E 开发库的 Operation→documents 外键迁移为独立历史快照。

    SQLite 不支持直接删除外键，只能在同一事务内重建父表。连接在事务开始前已关闭外键
    执行检查，避免已有 Step/Event 子表在替换 Operation 表时被误删；所有固定列逐列复制，
    任一异常都会回滚，绝不使用 ``SELECT *`` 吞掉列顺序变化。
    """

    foreign_keys = connection.execute(
        "PRAGMA foreign_key_list(reassign_operations)"
    ).fetchall()
    if not any(str(row[2]) == "documents" for row in foreign_keys):
        return

    logger.warning("检测到历史 reassign 文档外键，准备迁移为独立审计快照")
    temporary_table = "reassign_operations_without_document_fk"
    connection.execute(f"DROP TABLE IF EXISTS {temporary_table}")
    connection.execute(
        _REASSIGN_OPERATIONS_CREATE_STATEMENT.replace(
            "reassign_operations",
            temporary_table,
            1,
        )
    )
    column_sql = ", ".join(_REASSIGN_OPERATION_COLUMNS)
    connection.execute(
        f"INSERT INTO {temporary_table} ({column_sql}) "
        f"SELECT {column_sql} FROM reassign_operations"
    )
    connection.execute("DROP TABLE reassign_operations")
    connection.execute(
        f"ALTER TABLE {temporary_table} RENAME TO reassign_operations"
    )
    logger.info("历史 reassign 文档外键迁移完成，Operation 审计事实已与业务行解耦")


def _ensure_active_operation_index(connection: sqlite3.Connection) -> None:
    """确保既有库的活动文档唯一索引与当前领域状态集合完全一致。

    ``CREATE INDEX IF NOT EXISTS`` 不会更新已经存在的部分索引。若未来领域模型增加或移除
    “持有文档保护”的状态，继续沿用旧谓词会造成 SQLite 与 Repository 查询语义分叉。
    因此初始化时读取索引定义，只在谓词不一致时于同一事务内重建；如果历史数据已经违反
    新唯一约束，SQLite 会明确拒绝启动，避免多实例环境继续带病写入。
    """

    row = connection.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'index' AND name = ?
        """,
        (_ACTIVE_OPERATION_INDEX_NAME,),
    ).fetchone()
    index_sql = str(row["sql"]) if row is not None and row["sql"] else ""
    persisted_statuses = frozenset(re.findall(r"'([^']+)'", index_sql))
    expected_statuses = frozenset(_ACTIVE_OPERATION_STATUSES)
    if persisted_statuses == expected_statuses:
        return

    logger.warning(
        "分类节点变更活动文档唯一索引谓词过期，准备事务内重建: "
        "persisted_statuses=%s expected_statuses=%s",
        sorted(persisted_statuses),
        sorted(expected_statuses),
    )
    connection.execute(f"DROP INDEX IF EXISTS {_ACTIVE_OPERATION_INDEX_NAME}")
    connection.execute(_ACTIVE_OPERATION_INDEX_STATEMENT)


def _required_text(value: object, *, name: str) -> str:
    """校验 Adapter 自己生成或读取的必填内部文本。"""

    if not isinstance(value, str):
        raise ReassignmentContractError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ReassignmentContractError(f"{name} 不能为空")
    return normalized


def _raw_json_text(raw: ReassignmentRawValue) -> str:
    """序列化已深冻结的原始 ID；任何异常都表示内部契约损坏。"""

    if not isinstance(raw, ReassignmentRawValue):
        raise ReassignmentContractError("architecture_raw 必须是 ReassignmentRawValue")
    try:
        return raw.canonical_json()
    except (TypeError, ValueError) as exc:
        raise ReassignmentContractError("architecture_raw 无法序列化为安全 JSON") from exc


def _raw_from_json_text(value: object, *, name: str) -> ReassignmentRawValue:
    """从 SQLite JSON 文本恢复原始 ID，拒绝被损坏的持久化事实。"""

    text = _required_text(value, name=name)
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ReassignmentContractError(f"{name} 不是合法 JSON") from exc
    raw = ReassignmentRawValue.from_external_value(parsed)
    if raw.value is None:
        raise ReassignmentContractError(f"{name} 不能为 null")
    return raw


def _sqlite_architecture_storage_value(
    raw: ReassignmentRawValue,
    *,
    name: str,
) -> int:
    """生成当前兼容契约允许写入 SQLite INTEGER 权威列的值。

    原始 JSON 仍完整保存在 Operation 中；这里只处理已冻结的本地存储投影。
    当前冻结兼容仅允许 ``false``、整数和十进制整数字符串；``true``、浮点数、数组、对象、
    非数字文本及越界值都作为内部契约错误交给 Application 走既有失败路径。
    """

    try:
        return architecture_id_storage_value(raw, name=name)
    except (TypeError, ValueError) as exc:
        # 新请求已在领域命令构造阶段执行同一验证；这里仍需防御历史损坏事实，
        # 并把领域校验错误转换成 Repository 边界统一使用的契约错误。
        raise ReassignmentContractError(
            f"{name} 不能投影为 SQLite INTEGER"
        ) from exc


def _parse_enum(enum_type: type, value: object, *, name: str):
    """把 SQLite 文本严格恢复为领域枚举，避免字符串绕过状态机。"""

    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ReassignmentContractError(f"{name} 包含未知枚举值") from exc


class SQLiteReassignmentRepository(ReassignmentRepositoryPort):
    """在现有 knowledge_base SQLite 中保存 reassign Operation/Step/Event 事实。"""

    def __init__(
        self,
        db_path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        busy_timeout_seconds: float = 30.0,
        initialize_schema: bool = True,
    ) -> None:
        if isinstance(db_path, Path):
            normalized_path = str(db_path)
        elif isinstance(db_path, str):
            normalized_path = db_path.strip()
        else:
            raise TypeError("db_path 必须是 str 或 Path")
        if not normalized_path:
            raise ValueError("db_path 不能为空")
        if clock is not None and not callable(clock):
            raise TypeError("clock 必须是可调用对象或 None")
        if (
            isinstance(busy_timeout_seconds, bool)
            or not isinstance(busy_timeout_seconds, (int, float))
            or not math.isfinite(float(busy_timeout_seconds))
            or not float(busy_timeout_seconds) > 0.0
        ):
            raise ValueError("busy_timeout_seconds 必须是正数")
        if not isinstance(initialize_schema, bool):
            raise TypeError("initialize_schema 必须是 bool")

        self._db_path = normalized_path
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._busy_timeout_seconds = float(busy_timeout_seconds)
        self._transaction_context = threading.local()
        # 离线审计命令必须能够真正保持只读：默认生产/测试构造仍执行幂等加法初始化，
        # 但诊断调用方可明确关闭它，避免仅为查看待恢复 Operation 就获取写锁或变更 Schema。
        if initialize_schema:
            self._initialize_schema()

    @property
    def db_path(self) -> str:
        """仅供内部组合与离线诊断使用的 SQLite 路径，不属于公开接口。"""

        return self._db_path

    def unit_of_work(
        self,
        *,
        read_only: bool = False,
    ) -> "SQLiteReassignmentUnitOfWork":
        """返回尚未开始事务的新 UoW；调用方必须使用 ``with`` 限定其生命周期。"""

        if not isinstance(read_only, bool):
            raise TypeError("read_only 必须是 bool")
        return SQLiteReassignmentUnitOfWork(
            self,
            transaction_context=self._transaction_context,
            read_only=read_only,
        )

    def _connect(self) -> sqlite3.Connection:
        """创建独立 SQLite 连接，避免跨请求或跨线程共享可变事务状态。"""

        connection = sqlite3.connect(
            self._db_path,
            timeout=self._busy_timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            f"PRAGMA busy_timeout = {int(self._busy_timeout_seconds * 1000)}"
        )
        return connection

    def _now_utc_text(self) -> str:
        """读取注入时钟并规范化为可跨实例比较的 UTC ISO-8601 文本。"""

        value = self._clock()
        if not isinstance(value, datetime):
            raise ReassignmentContractError("clock 必须返回 datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ReassignmentContractError("clock 必须返回带时区 datetime")
        return (
            value.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _normalize_utc_text(value: object, *, name: str) -> str:
        """校验调用方给出的 lease 时间，并统一为固定 UTC 格式。"""

        text = _required_text(value, name=name)
        iso_value = f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
        try:
            parsed = datetime.fromisoformat(iso_value)
        except ValueError as exc:
            raise ReassignmentContractError(
                f"{name} 必须是带时区的 ISO-8601 时间"
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ReassignmentContractError(f"{name} 必须包含时区")
        return (
            parsed.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

    @classmethod
    def _is_expired(cls, expires_at: str, *, now: str) -> bool:
        """在 Adapter 层比较已规范化 lease 时间；Domain 不读取当前时钟。"""

        expiry = cls._normalize_utc_text(expires_at, name="lease_expires_at")
        normalized_now = cls._normalize_utc_text(now, name="now")
        return expiry <= normalized_now

    @staticmethod
    def _document_digest(snapshot: ReassignmentDocumentSnapshot) -> str:
        """生成日志和审计可用的最小文档摘要，避免输出文件名或路径。"""

        identity = f"{snapshot.document_row_id}:{snapshot.file_name}".encode("utf-8")
        return hashlib.sha256(identity).hexdigest()[:16]

    @staticmethod
    def _text_digest(value: str) -> str:
        """对内部操作者等不透明值取摘要，审计可关联但不保存原文。"""

        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

    def _initialize_schema(self) -> None:
        """幂等创建本模块加法表和索引，绝不迁移或重建公开 documents 表。"""

        connection = self._connect()
        try:
            # 仅 Schema 协调迁移需要暂时关闭外键执行检查；必须发生在 BEGIN 之前，
            # SQLite 才会接受该 PRAGMA。正常 UoW 连接始终保持 foreign_keys=ON。
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(documents)")
            }
            required_columns = {
                "id",
                "file_name",
                "original_name",
                "architecture_id",
                "anything_doc_id",
                "doc_path",
            }
            missing_columns = sorted(required_columns - columns)
            if missing_columns:
                raise ReassignmentContractError(
                    "documents 表缺少 reassign 所需列: "
                    + ", ".join(missing_columns)
                )
            workspace_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(workspaces)")
            }
            missing_workspace_columns = sorted(
                {"architecture_id", "workspace_slug"} - workspace_columns
            )
            if missing_workspace_columns:
                raise ReassignmentContractError(
                    "workspaces 表缺少 reassign 所需列: "
                    + ", ".join(missing_workspace_columns)
                )

            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
            _ensure_active_operation_index(connection)
            for table_name, columns_to_add in _ADDITIVE_SCHEMA_COLUMNS.items():
                existing_columns = {
                    str(row["name"])
                    for row in connection.execute(
                        f"PRAGMA table_info({table_name})"
                    )
                }
                for column_name, column_type in columns_to_add:
                    if column_name in existing_columns:
                        continue
                    connection.execute(
                        f"ALTER TABLE {table_name} "
                        f"ADD COLUMN {column_name} {column_type}"
                    )
            operation_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(reassign_operations)"
                )
            }
            if "target_workspace_created" in operation_columns:
                # 兼容只在 1E 开发阶段出现过的布尔列：true/false 分别迁移为明确归属。
                # 先回填为当前三态；后续协调重建只复制当前固定列，旧布尔列才可安全退出。
                connection.execute(
                    """
                    UPDATE reassign_operations
                    SET target_workspace_ownership = CASE target_workspace_created
                        WHEN 1 THEN 'created_by_operation'
                        WHEN 0 THEN 'preexisting'
                    END
                    WHERE target_workspace_ownership IS NULL
                      AND target_workspace_created IN (0, 1)
                    """
                )
            # 早期 1E-2 开发库没有恢复 fencing 列。升级时以当前 fencing 作为保守
            # 基线：只有后续接管得到更大的 fencing，才允许离开隔离或重试。
            connection.execute(
                """
                UPDATE reassign_operations
                SET recovery_required_fencing_token = fencing_token
                WHERE status = 'recovery_required'
                  AND recovery_required_fencing_token IS NULL
                """
            )
            connection.execute(
                """
                UPDATE reassign_steps
                SET last_attempt_fencing_token = (
                    SELECT fencing_token
                    FROM reassign_operations
                    WHERE reassign_operations.operation_id =
                          reassign_steps.operation_id
                )
                WHERE attempt_count > 0
                  AND last_attempt_fencing_token IS NULL
                """
            )
            _remove_legacy_document_foreign_key(connection)
            # 重建 Operation 表会连同其索引一起删除；重新执行幂等 Schema 语句恢复全部
            # 索引/触发器，并再次核对活动状态部分唯一索引的谓词。
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
            _ensure_active_operation_index(connection)
            connection.commit()
        except Exception as exc:
            connection.rollback()
            logger.error(
                "分类节点变更 SQLite 事实表初始化失败: error_type=%s",
                type(exc).__name__,
            )
            raise
        finally:
            connection.close()
        logger.info("分类节点变更 SQLite 事实表初始化完成")


class SQLiteReassignmentUnitOfWork(ReassignmentUnitOfWork):
    """单次短 SQLite 事务；所有可写方法均要求有效 lease/fencing。"""

    def __init__(
        self,
        repository: SQLiteReassignmentRepository,
        *,
        transaction_context: threading.local,
        read_only: bool = False,
    ) -> None:
        self._repository = repository
        self._transaction_context = transaction_context
        self._read_only = read_only
        self._connection: sqlite3.Connection | None = None
        self._active = False
        self._pending_logs: list[tuple[int, str, tuple[object, ...]]] = []

    @property
    def active(self) -> bool:
        return self._active

    @property
    def read_only(self) -> bool:
        return self._read_only

    def __enter__(self) -> "SQLiteReassignmentUnitOfWork":
        if self._active or self._connection is not None:
            raise ReassignmentContractError("UnitOfWork 不能重复进入")
        if bool(getattr(self._transaction_context, "active", False)):
            raise ReassignmentContractError("同一调用上下文禁止嵌套 UnitOfWork")
        connection = self._repository._connect()
        try:
            connection.execute("BEGIN" if self._read_only else "BEGIN IMMEDIATE")
        except Exception:
            connection.close()
            raise
        self._connection = connection
        self._transaction_context.active = True
        self._active = True
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> bool:
        if not self._active:
            return False
        if exc_type is not None:
            self.rollback()
            return False
        self.commit()
        return False

    def commit(self) -> None:
        """提交事务后再输出成功日志，避免回滚操作被误记为已生效。"""

        connection = self._require_connection()
        try:
            connection.commit()
        except Exception as exc:
            try:
                connection.rollback()
            finally:
                self._close(clear_logs=True)
            logger.error(
                "分类节点变更 SQLite 短事务提交失败: error_type=%s",
                type(exc).__name__,
            )
            raise
        pending_logs = tuple(self._pending_logs)
        self._close(clear_logs=True)
        for level, message, arguments in pending_logs:
            logger.log(level, message, *arguments)

    def rollback(self) -> None:
        """回滚未提交的本地事实，不输出已撤销的成功日志。"""

        connection = self._require_connection()
        try:
            connection.rollback()
        finally:
            self._close(clear_logs=True)

    def _close(self, *, clear_logs: bool) -> None:
        connection = self._connection
        self._connection = None
        self._active = False
        self._transaction_context.active = False
        if clear_logs:
            self._pending_logs.clear()
        if connection is not None:
            connection.close()

    def _require_connection(self) -> sqlite3.Connection:
        if not self._active or self._connection is None:
            raise ReassignmentContractError("UnitOfWork 未处于活动事务")
        return self._connection

    def _require_writable(self) -> None:
        """阻止恢复扫描或诊断查询通过只读 UoW 意外修改本地事实。"""

        self._require_connection()
        if self._read_only:
            raise ReassignmentContractError("只读 UnitOfWork 禁止执行写操作")

    def _stage_log(
        self,
        level: int,
        message: str,
        *arguments: object,
    ) -> None:
        """暂存只含内部 ID/摘要的日志，等待成功提交后统一发出。"""

        self._pending_logs.append((level, message, arguments))

    def _now(self) -> str:
        return self._repository._now_utc_text()

    def _fetch_document_snapshot(
        self,
        *,
        file_name: str,
        source_architecture_id: int,
    ) -> ReassignmentDocumentSnapshot | None:
        connection = self._require_connection()
        row = connection.execute(
            """
            SELECT id, file_name, architecture_id, anything_doc_id, doc_path, original_name
            FROM documents
            WHERE file_name = ? AND architecture_id = ?
            """,
            (file_name, source_architecture_id),
        ).fetchone()
        if row is None:
            return None
        return ReassignmentDocumentSnapshot(
            document_row_id=row["id"],
            file_name=row["file_name"],
            source_architecture_id=row["architecture_id"],
            anything_doc_id=row["anything_doc_id"],
            doc_path=row["doc_path"],
            original_file_name=row["original_name"],
        )

    def get_document_snapshot(
        self,
        *,
        file_name: str,
        source_architecture_id: int,
    ) -> ReassignmentDocumentSnapshot | None:
        """按当前接口锁定的 ``fileName + int(oldArchitectureId)`` 精确读取文档。"""

        normalized_file_name = _required_text(file_name, name="file_name")
        if (
            isinstance(source_architecture_id, bool)
            or not isinstance(source_architecture_id, int)
        ):
            raise ReassignmentContractError("source_architecture_id 必须是 int")
        return self._fetch_document_snapshot(
            file_name=normalized_file_name,
            source_architecture_id=source_architecture_id,
        )

    def _operation_record_from_row(
        self,
        row: sqlite3.Row,
    ) -> ReassignmentOperationRecord:
        source_raw = _raw_from_json_text(
            row["source_architecture_raw_json"],
            name="source_architecture_raw_json",
        )
        target_raw = _raw_from_json_text(
            row["target_architecture_raw_json"],
            name="target_architecture_raw_json",
        )
        current_step = row["current_step"]
        operation = ReassignmentOperation(
            operation_id=row["operation_id"],
            document=ReassignmentDocumentSnapshot(
                document_row_id=row["document_row_id"],
                file_name=row["file_name"],
                source_architecture_id=row["source_architecture_id"],
                anything_doc_id=row["anything_doc_id"],
                doc_path=row["doc_path"],
                original_file_name=row["original_file_name"],
            ),
            source_architecture_id=row["source_architecture_id"],
            source_architecture_raw=source_raw,
            target_architecture_raw=target_raw,
            status=_parse_enum(
                ReassignmentOperationStatus,
                row["status"],
                name="operations.status",
            ),
            current_step=(
                None
                if current_step is None
                else _parse_enum(
                    ReassignmentStepName,
                    current_step,
                    name="operations.current_step",
                )
            ),
            lease_owner=row["lease_owner"],
            lease_token=row["lease_token"],
            lease_expires_at=row["lease_expires_at"],
            fencing_token=row["fencing_token"],
        )
        ownership = row["target_workspace_ownership"]
        return ReassignmentOperationRecord(
            operation=operation,
            source_workspace_slug=row["source_workspace_slug"],
            target_workspace_slug=row["target_workspace_slug"],
            target_workspace_ownership=(
                None
                if ownership is None
                else _parse_enum(
                    ReassignmentWorkspaceOwnership,
                    ownership,
                    name="operations.target_workspace_ownership",
                )
            ),
            error_code=row["error_code"],
            error_summary=row["error_summary"],
            recovery_required_fencing_token=(
                row["recovery_required_fencing_token"]
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            finished_at=row["finished_at"],
        )

    def _step_record_from_row(self, row: sqlite3.Row) -> ReassignmentStepRecord:
        write_intent = row["write_intent_recorded"]
        if write_intent not in (0, 1):
            raise ReassignmentContractError("steps.write_intent_recorded 不是布尔值")
        probe_outcome = row["probe_outcome"]
        return ReassignmentStepRecord(
            step=ReassignmentStep(
                operation_id=row["operation_id"],
                step_name=_parse_enum(
                    ReassignmentStepName,
                    row["step_name"],
                    name="steps.step_name",
                ),
                idempotency_key=row["idempotency_key"],
                state=_parse_enum(
                    ReassignmentStepState,
                    row["state"],
                    name="steps.state",
                ),
                write_intent_recorded=bool(write_intent),
                external_reference=row["external_reference"],
                error_code=row["error_code"],
                error_summary=row["error_summary"],
            ),
            attempt_count=row["attempt_count"],
            last_attempt_fencing_token=row["last_attempt_fencing_token"],
            mutation_started_at=row["mutation_started_at"],
            probe_outcome=(
                None
                if probe_outcome is None
                else _parse_enum(
                    ReassignmentMutationOutcome,
                    probe_outcome,
                    name="steps.probe_outcome",
                )
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _event_from_row(self, row: sqlite3.Row) -> ReassignmentAuditEvent:
        step_name = row["step_name"]
        operation_status = row["operation_status"]
        return ReassignmentAuditEvent(
            operation_id=row["operation_id"],
            sequence_no=row["sequence_no"],
            event_type=_parse_enum(
                ReassignmentEventType,
                row["event_type"],
                name="events.event_type",
            ),
            occurred_at=row["occurred_at"],
            step_name=(
                None
                if step_name is None
                else _parse_enum(
                    ReassignmentStepName,
                    step_name,
                    name="events.step_name",
                )
            ),
            operation_status=(
                None
                if operation_status is None
                else _parse_enum(
                    ReassignmentOperationStatus,
                    operation_status,
                    name="events.operation_status",
                )
            ),
            detail_code=row["detail_code"],
            reference_digest=row["reference_digest"],
            fencing_token=row["fencing_token"],
            attempt_count=row["attempt_count"],
            probe_outcome=(
                None
                if row["probe_outcome"] is None
                else _parse_enum(
                    ReassignmentMutationOutcome,
                    row["probe_outcome"],
                    name="events.probe_outcome",
                )
            ),
            actor_digest=row["actor_digest"],
            reason_code=row["reason_code"],
        )

    def _get_operation_record(
        self,
        operation_id: str,
    ) -> ReassignmentOperationRecord | None:
        connection = self._require_connection()
        row = connection.execute(
            "SELECT * FROM reassign_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        return None if row is None else self._operation_record_from_row(row)

    def get_operation(
        self,
        operation_id: str,
    ) -> ReassignmentOperationRecord | None:
        """读取一条内部 Operation；返回值不得被 Presenter 直接序列化。"""

        return self._get_operation_record(
            _required_text(operation_id, name="operation_id")
        )

    def _get_step_record(
        self,
        *,
        operation_id: str,
        step_name: ReassignmentStepName,
    ) -> ReassignmentStepRecord | None:
        connection = self._require_connection()
        row = connection.execute(
            """
            SELECT * FROM reassign_steps
            WHERE operation_id = ? AND step_name = ?
            """,
            (operation_id, step_name.value),
        ).fetchone()
        return None if row is None else self._step_record_from_row(row)

    def get_step(
        self,
        *,
        operation_id: str,
        step_name: ReassignmentStepName,
    ) -> ReassignmentStepRecord | None:
        """读取指定固定步骤。"""

        if not isinstance(step_name, ReassignmentStepName):
            raise TypeError("step_name 必须是 ReassignmentStepName")
        return self._get_step_record(
            operation_id=_required_text(operation_id, name="operation_id"),
            step_name=step_name,
        )

    def list_steps(self, operation_id: str) -> tuple[ReassignmentStepRecord, ...]:
        """按固定步骤名读取所有检查点，便于后续恢复服务判定现场。"""

        normalized_operation_id = _required_text(
            operation_id,
            name="operation_id",
        )
        connection = self._require_connection()
        rows = connection.execute(
            """
            SELECT * FROM reassign_steps
            WHERE operation_id = ?
            ORDER BY rowid ASC
            """,
            (normalized_operation_id,),
        ).fetchall()
        return tuple(self._step_record_from_row(row) for row in rows)

    def list_events(self, operation_id: str) -> tuple[ReassignmentAuditEvent, ...]:
        """按不可变 sequence_no 读取审计，禁止 UPDATE/DELETE 语义。"""

        normalized_operation_id = _required_text(
            operation_id,
            name="operation_id",
        )
        connection = self._require_connection()
        rows = connection.execute(
            """
            SELECT operation_id, sequence_no, event_type, step_name, operation_status,
                   detail_code, reference_digest, fencing_token, attempt_count,
                   probe_outcome, actor_digest, reason_code, occurred_at
            FROM reassign_events
            WHERE operation_id = ?
            ORDER BY sequence_no ASC
            """,
            (normalized_operation_id,),
        ).fetchall()
        return tuple(self._event_from_row(row) for row in rows)

    def list_recoverable_operations(
        self,
        *,
        limit: int,
        cursor: ReassignmentRecoveryCursor | None = None,
    ) -> tuple[ReassignmentOperationRecord, ...]:
        """有界扫描 lease 已过期且仍持有文档保护的 Operation。

        扫描使用稳定复合游标，避免恢复器在多批处理中使用 OFFSET 后因并发更新而漏项。
        调用方应使用 ``unit_of_work(read_only=True)``，让诊断与扫描不占 SQLite 写锁。
        """

        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit 必须是 int")
        if limit < 1 or limit > 500:
            raise ValueError("limit 必须在 1 到 500 之间")
        if cursor is not None and not isinstance(cursor, ReassignmentRecoveryCursor):
            raise TypeError("cursor 必须是 ReassignmentRecoveryCursor 或 None")
        now = self._now()
        connection = self._require_connection()
        parameters: list[object] = [now]
        cursor_clause = ""
        if cursor is not None:
            normalized_cursor_expiry = self._repository._normalize_utc_text(
                cursor.lease_expires_at,
                name="cursor.lease_expires_at",
            )
            cursor_clause = """
              AND (
                    lease_expires_at > ?
                    OR (lease_expires_at = ? AND operation_id > ?)
                  )
            """
            parameters.extend(
                [
                    normalized_cursor_expiry,
                    normalized_cursor_expiry,
                    cursor.operation_id,
                ]
            )
        parameters.append(limit)
        rows = connection.execute(
            f"""
            SELECT * FROM reassign_operations
            WHERE status IN ({_ACTIVE_OPERATION_STATUS_SQL})
              AND lease_expires_at <= ?
              {cursor_clause}
            ORDER BY lease_expires_at ASC, operation_id ASC
            LIMIT ?
            """,
            tuple(parameters),
        ).fetchall()
        return tuple(self._operation_record_from_row(row) for row in rows)

    def _local_commit_state_for_record(
        self,
        record: ReassignmentOperationRecord,
    ) -> ReassignmentLocalCommitState:
        """以冻结行身份读取当前分类，避免恢复器依据旧步骤状态推测本地 CAS。

        本地提交的条件更新只约束 ``id/file_name/source_architecture_id/anything_doc_id/doc_path``。
        恢复探测严格复用这组身份条件；行被删除、外部文档标识变化或分类落在第三个值时，都
        只能报告冲突，绝不能把它误判为“还未提交”。
        """

        document = record.operation.document
        connection = self._require_connection()
        row = connection.execute(
            """
            SELECT id, file_name, architecture_id, anything_doc_id, doc_path
            FROM documents
            WHERE id = ?
            """,
            (document.document_row_id,),
        ).fetchone()
        if row is None:
            return ReassignmentLocalCommitState.CONFLICT
        if (
            row["file_name"] != document.file_name
            or row["anything_doc_id"] != document.anything_doc_id
            or row["doc_path"] != document.doc_path
        ):
            return ReassignmentLocalCommitState.CONFLICT
        current_architecture = row["architecture_id"]
        if current_architecture == document.source_architecture_id:
            return ReassignmentLocalCommitState.SOURCE_UNCHANGED
        target_architecture = _sqlite_architecture_storage_value(
            record.operation.target_architecture_raw,
            name="operation.target_architecture_raw",
        )
        if current_architecture == target_architecture:
            return ReassignmentLocalCommitState.TARGET_COMMITTED
        return ReassignmentLocalCommitState.CONFLICT

    def probe_local_commit_state(
        self,
        operation_id: str,
    ) -> ReassignmentLocalCommitState:
        """读取恢复所需的本地 CAS 权威事实；允许在只读 UoW 内调用。"""

        normalized_operation_id = _required_text(
            operation_id,
            name="operation_id",
        )
        record = self._get_operation_record(normalized_operation_id)
        if record is None:
            raise ReassignmentContractError("Operation 不存在，无法探测本地提交状态")
        return self._local_commit_state_for_record(record)

    def _append_event(
        self,
        *,
        operation_id: str,
        event_type: ReassignmentEventType,
        step_name: ReassignmentStepName | None = None,
        operation_status: ReassignmentOperationStatus | None = None,
        detail_code: str | None = None,
        reference_digest: str | None = None,
        fencing_token: int | None = None,
        attempt_count: int | None = None,
        probe_outcome: ReassignmentMutationOutcome | None = None,
        actor_digest: str | None = None,
        reason_code: str | None = None,
    ) -> ReassignmentAuditEvent:
        """在当前事务中追加一个事件并由唯一序号约束保护顺序。"""

        connection = self._require_connection()
        row = connection.execute(
            """
            SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next_sequence
            FROM reassign_events
            WHERE operation_id = ?
            """,
            (operation_id,),
        ).fetchone()
        event = ReassignmentAuditEvent(
            operation_id=operation_id,
            sequence_no=row["next_sequence"],
            event_type=event_type,
            occurred_at=self._now(),
            step_name=step_name,
            operation_status=operation_status,
            detail_code=detail_code,
            reference_digest=reference_digest,
            fencing_token=fencing_token,
            attempt_count=attempt_count,
            probe_outcome=probe_outcome,
            actor_digest=actor_digest,
            reason_code=reason_code,
        )
        connection.execute(
            """
            INSERT INTO reassign_events (
                operation_id, sequence_no, event_type, step_name, operation_status,
                detail_code, reference_digest, fencing_token, attempt_count,
                probe_outcome, actor_digest, reason_code, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.operation_id,
                event.sequence_no,
                event.event_type.value,
                event.step_name.value if event.step_name is not None else None,
                (
                    event.operation_status.value
                    if event.operation_status is not None
                    else None
                ),
                event.detail_code,
                event.reference_digest,
                event.fencing_token,
                event.attempt_count,
                (
                    event.probe_outcome.value
                    if event.probe_outcome is not None
                    else None
                ),
                event.actor_digest,
                event.reason_code,
                event.occurred_at,
            ),
        )
        return event

    def reserve(
        self,
        request: ReassignmentReservationRequest,
    ) -> ReassignmentReservationResult:
        """原子冻结文档身份、创建八个步骤并取得单调 fencing token。"""

        self._require_writable()
        if not isinstance(request, ReassignmentReservationRequest):
            raise TypeError("request 必须是 ReassignmentReservationRequest")
        now = self._now()
        normalized_expiry = self._repository._normalize_utc_text(
            request.lease_expires_at,
            name="lease_expires_at",
        )
        if self._repository._is_expired(normalized_expiry, now=now):
            raise ReassignmentContractError("初始 lease_expires_at 必须晚于当前时间")

        command = request.command
        snapshot = self._fetch_document_snapshot(
            file_name=command.file_name,
            source_architecture_id=command.old_architecture_id_query_value,
        )
        if snapshot is None:
            self._stage_log(
                logging.WARNING,
                "分类节点变更保留失败: reason=document_not_found source_architecture_id=%s",
                command.old_architecture_id_query_value,
            )
            return ReassignmentReservationResult(
                ReassignmentReservationOutcome.DOCUMENT_NOT_FOUND
            )

        connection = self._require_connection()
        if connection.execute(
            "SELECT 1 FROM reassign_operations WHERE operation_id = ?",
            (request.operation_id,),
        ).fetchone() is not None:
            raise ReassignmentContractError("operation_id 不能重复使用")
        source_workspace_row = connection.execute(
            "SELECT workspace_slug FROM workspaces WHERE architecture_id = ?",
            (snapshot.source_architecture_id,),
        ).fetchone()
        source_workspace_slug = (
            None if source_workspace_row is None else source_workspace_row["workspace_slug"]
        )
        fencing_row = connection.execute(
            """
            SELECT COALESCE(MAX(fencing_token), 0) + 1 AS next_fencing
            FROM reassign_operations
            WHERE document_row_id = ?
            """,
            (snapshot.document_row_id,),
        ).fetchone()
        operation = ReassignmentOperation(
            operation_id=request.operation_id,
            document=snapshot,
            source_architecture_id=snapshot.source_architecture_id,
            source_architecture_raw=command.old_architecture_id_raw,
            target_architecture_raw=command.new_architecture_id_raw,
            status=ReassignmentOperationStatus.RESERVED,
            current_step=ReassignmentStepName.RESERVE_DOCUMENT,
            lease_owner=request.lease_owner,
            lease_token=request.lease_token,
            lease_expires_at=normalized_expiry,
            fencing_token=fencing_row["next_fencing"],
        )
        record = ReassignmentOperationRecord(
            operation=operation,
            source_workspace_slug=source_workspace_slug,
            target_workspace_slug=None,
            target_workspace_ownership=None,
            error_code=None,
            error_summary=None,
            recovery_required_fencing_token=None,
            created_at=now,
            updated_at=now,
        )
        try:
            connection.execute(
                """
                INSERT INTO reassign_operations (
                    operation_id, document_row_id, file_name, source_architecture_id,
                    source_architecture_raw_json, target_architecture_raw_json,
                    anything_doc_id, doc_path, original_file_name, source_workspace_slug,
                    target_workspace_slug, target_workspace_ownership, status, current_step,
                    lease_owner, lease_token, lease_expires_at, fencing_token,
                    error_code, error_summary, recovery_required_fencing_token,
                    created_at, updated_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation.operation_id,
                    snapshot.document_row_id,
                    snapshot.file_name,
                    snapshot.source_architecture_id,
                    _raw_json_text(operation.source_architecture_raw),
                    _raw_json_text(operation.target_architecture_raw),
                    snapshot.anything_doc_id,
                    snapshot.doc_path,
                    snapshot.original_file_name,
                    record.source_workspace_slug,
                    None,
                    None,
                    operation.status.value,
                    operation.current_step.value,
                    operation.lease_owner,
                    operation.lease_token,
                    operation.lease_expires_at,
                    operation.fencing_token,
                    None,
                    None,
                    None,
                    now,
                    now,
                    None,
                ),
            )
        except sqlite3.IntegrityError as exc:
            active_row = connection.execute(
                f"""
                SELECT operation_id FROM reassign_operations
                WHERE document_row_id = ?
                  AND status IN ({_ACTIVE_OPERATION_STATUS_SQL})
                """,
                (snapshot.document_row_id,),
            ).fetchone()
            if active_row is not None:
                self._stage_log(
                    logging.WARNING,
                    "分类节点变更保留冲突: document_digest=%s",
                    self._repository._document_digest(snapshot),
                )
                return ReassignmentReservationResult(
                    ReassignmentReservationOutcome.ACTIVE_OPERATION_EXISTS
                )
            raise ReassignmentContractError("创建 reassign Operation 违反唯一约束") from exc

        for step_name in ReassignmentStepName:
            if step_name is ReassignmentStepName.RESERVE_DOCUMENT:
                step = ReassignmentStep(
                    operation_id=operation.operation_id,
                    step_name=step_name,
                    idempotency_key=self._step_idempotency_key(operation, step_name),
                    state=ReassignmentStepState.SUCCEEDED,
                    write_intent_recorded=True,
                )
            else:
                step = ReassignmentStep(
                    operation_id=operation.operation_id,
                    step_name=step_name,
                    idempotency_key=self._step_idempotency_key(operation, step_name),
                )
            connection.execute(
                """
                INSERT INTO reassign_steps (
                    operation_id, step_name, idempotency_key, state, write_intent_recorded,
                    external_reference, probe_outcome, mutation_started_at, attempt_count,
                    last_attempt_fencing_token, error_code, error_summary,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    step.operation_id,
                    step.step_name.value,
                    step.idempotency_key,
                    step.state.value,
                    int(step.write_intent_recorded),
                    step.external_reference,
                    None,
                    None,
                    0,
                    None,
                    step.error_code,
                    step.error_summary,
                    now,
                    now,
                ),
            )
        self._append_event(
            operation_id=operation.operation_id,
            event_type=ReassignmentEventType.OPERATION_RESERVED,
            step_name=ReassignmentStepName.RESERVE_DOCUMENT,
            operation_status=operation.status,
            reference_digest=self._repository._document_digest(snapshot),
            fencing_token=operation.fencing_token,
        )
        self._stage_log(
            logging.INFO,
            "分类节点变更已保留执行权: operation_id=%s document_digest=%s fencing_token=%s",
            operation.operation_id,
            self._repository._document_digest(snapshot),
            operation.fencing_token,
        )
        return ReassignmentReservationResult(
            ReassignmentReservationOutcome.ACQUIRED,
            record,
        )

    @staticmethod
    def _step_idempotency_key(
        operation: ReassignmentOperation,
        step_name: ReassignmentStepName,
    ) -> str:
        """延迟导入纯规则，避免 Adapter 重复实现幂等键算法。"""

        from app.modules.reassign.domain import build_step_idempotency_key

        return build_step_idempotency_key(operation, step_name)

    def get_workspace_slug(self, architecture_raw: ReassignmentRawValue) -> str | None:
        """按原始目标 ID 查询 workspace 映射，不在端口层擅自规范化类型。"""

        if not isinstance(architecture_raw, ReassignmentRawValue):
            raise TypeError("architecture_raw 必须是 ReassignmentRawValue")
        connection = self._require_connection()
        storage_value = _sqlite_architecture_storage_value(
            architecture_raw,
            name="architecture_raw",
        )
        row = connection.execute(
            "SELECT workspace_slug FROM workspaces WHERE architecture_id = ?",
            (storage_value,),
        ).fetchone()
        if row is None:
            return None
        return _required_text(row["workspace_slug"], name="workspace_slug")

    def acquire_workspace_preparation_claim(
        self,
        request: ReassignmentWorkspacePreparationClaimRequest,
    ) -> ReassignmentWorkspacePreparationClaimResult | ReassignmentWriteOutcome:
        """原子申请同目标 workspace 的持久化准备权。

        该方法在短事务中先检查 ``workspaces`` mapping，再检查按目标分类唯一的 claim。这样
        多实例不会因为各自读到“尚无 mapping”而同时创建同名资源；过期 claim 只能以更大的
        fencing token 接管。网络创建仍由 Application 在提交本事务后发起。
        """

        self._require_writable()
        if not isinstance(request, ReassignmentWorkspacePreparationClaimRequest):
            raise TypeError(
                "request 必须是 ReassignmentWorkspacePreparationClaimRequest"
            )
        owned = self._owned_operation(request.operation_lease)
        if isinstance(owned, ReassignmentWriteOutcome):
            return owned
        if (
            owned.operation.target_architecture_raw.canonical_json()
            != request.target_architecture_raw.canonical_json()
        ):
            raise ReassignmentContractError("目标 workspace 准备权与 Operation 目标分类不一致")

        now = self._now()
        claim_expiry = self._repository._normalize_utc_text(
            request.claim_expires_at,
            name="claim_expires_at",
        )
        if self._repository._is_expired(claim_expiry, now=now):
            raise ReassignmentContractError("目标 workspace 准备权的 lease 已过期")
        target_value = _sqlite_architecture_storage_value(
            request.target_architecture_raw,
            name="target_architecture_raw",
        )
        connection = self._require_connection()
        mapping_row = connection.execute(
            "SELECT workspace_slug FROM workspaces WHERE architecture_id = ?",
            (target_value,),
        ).fetchone()
        if mapping_row is not None:
            workspace_slug = _required_text(
                mapping_row["workspace_slug"],
                name="workspace_slug",
            )
            self._stage_log(
                logging.INFO,
                "分类节点变更已复用目标 workspace 本地映射: operation_id=%s",
                request.operation_lease.operation_id,
            )
            return ReassignmentWorkspacePreparationClaimResult(
                ReassignmentWorkspacePreparationClaimOutcome.MAPPING_EXISTS,
                workspace_slug=workspace_slug,
            )

        claim_row = connection.execute(
            """
            SELECT operation_id, claim_owner, claim_token, fencing_token,
                   lease_expires_at, state
            FROM reassign_workspace_preparation_claims
            WHERE target_architecture_id = ?
            """,
            (target_value,),
        ).fetchone()
        claim_fencing_token = 1
        detail_code = "claim_acquired"
        if claim_row is None:
            connection.execute(
                """
                INSERT INTO reassign_workspace_preparation_claims (
                    target_architecture_id, operation_id, claim_owner, claim_token,
                    fencing_token, lease_expires_at, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    target_value,
                    request.operation_lease.operation_id,
                    request.operation_lease.owner,
                    request.claim_token,
                    claim_fencing_token,
                    claim_expiry,
                    _WORKSPACE_PREPARATION_CLAIM_ACTIVE,
                    now,
                    now,
                ),
            )
        else:
            current_state = _required_text(claim_row["state"], name="claim_state")
            current_expiry = self._repository._normalize_utc_text(
                claim_row["lease_expires_at"],
                name="claim_lease_expires_at",
            )
            current_fencing_token = int(claim_row["fencing_token"])
            if current_fencing_token < 1:
                raise ReassignmentContractError("目标 workspace 准备权 fencing_token 非法")
            if current_state == _WORKSPACE_PREPARATION_CLAIM_ACTIVE:
                same_owner = (
                    claim_row["operation_id"] == request.operation_lease.operation_id
                    and claim_row["claim_owner"] == request.operation_lease.owner
                    and claim_row["claim_token"] == request.claim_token
                )
                if same_owner and not self._repository._is_expired(
                    current_expiry,
                    now=now,
                ):
                    return ReassignmentWorkspacePreparationClaimResult(
                        ReassignmentWorkspacePreparationClaimOutcome.ACQUIRED,
                        claim=ReassignmentWorkspacePreparationClaim(
                            target_architecture_raw=request.target_architecture_raw,
                            operation_id=request.operation_lease.operation_id,
                            owner=request.operation_lease.owner,
                            token=request.claim_token,
                            fencing_token=current_fencing_token,
                            expires_at=current_expiry,
                        ),
                    )
                if not self._repository._is_expired(current_expiry, now=now):
                    self._append_event(
                        operation_id=request.operation_lease.operation_id,
                        event_type=ReassignmentEventType.WORKSPACE_PREPARATION_CLAIM_BLOCKED,
                        operation_status=owned.operation.status,
                        detail_code="active_target_preparation_claim",
                        reference_digest=self._repository._text_digest(
                            _raw_json_text(request.target_architecture_raw)
                        ),
                        fencing_token=request.operation_lease.fencing_token,
                    )
                    self._stage_log(
                        logging.WARNING,
                        "分类节点变更目标 workspace 准备权被活动 Operation 占用: operation_id=%s",
                        request.operation_lease.operation_id,
                    )
                    return ReassignmentWorkspacePreparationClaimResult(
                        ReassignmentWorkspacePreparationClaimOutcome.ACTIVE_CLAIM_EXISTS
                    )
                detail_code = "claim_taken_over_after_expiry"
            elif current_state == _WORKSPACE_PREPARATION_CLAIM_RELEASED:
                detail_code = "claim_reacquired_after_release"
            else:
                raise ReassignmentContractError("目标 workspace 准备权状态非法")

            claim_fencing_token = current_fencing_token + 1
            connection.execute(
                """
                UPDATE reassign_workspace_preparation_claims
                SET operation_id = ?, claim_owner = ?, claim_token = ?,
                    fencing_token = ?, lease_expires_at = ?, state = ?, updated_at = ?
                WHERE target_architecture_id = ?
                """,
                (
                    request.operation_lease.operation_id,
                    request.operation_lease.owner,
                    request.claim_token,
                    claim_fencing_token,
                    claim_expiry,
                    _WORKSPACE_PREPARATION_CLAIM_ACTIVE,
                    now,
                    target_value,
                ),
            )

        claim = ReassignmentWorkspacePreparationClaim(
            target_architecture_raw=request.target_architecture_raw,
            operation_id=request.operation_lease.operation_id,
            owner=request.operation_lease.owner,
            token=request.claim_token,
            fencing_token=claim_fencing_token,
            expires_at=claim_expiry,
        )
        self._append_event(
            operation_id=request.operation_lease.operation_id,
            event_type=ReassignmentEventType.WORKSPACE_PREPARATION_CLAIM_ACQUIRED,
            operation_status=owned.operation.status,
            detail_code=detail_code,
            reference_digest=self._repository._text_digest(
                _raw_json_text(request.target_architecture_raw)
            ),
            fencing_token=claim_fencing_token,
            actor_digest=self._repository._text_digest(request.operation_lease.owner),
        )
        self._stage_log(
            logging.INFO,
            "分类节点变更已取得目标 workspace 持久化准备权: operation_id=%s claim_fencing_token=%s",
            request.operation_lease.operation_id,
            claim_fencing_token,
        )
        return ReassignmentWorkspacePreparationClaimResult(
            ReassignmentWorkspacePreparationClaimOutcome.ACQUIRED,
            claim=claim,
        )

    def release_workspace_preparation_claim(
        self,
        claim: ReassignmentWorkspacePreparationClaim,
    ) -> ReassignmentWriteOutcome:
        """释放持久化准备权，但保留行与 fencing 计数以阻止 ABA 重用。"""

        self._require_writable()
        if not isinstance(claim, ReassignmentWorkspacePreparationClaim):
            raise TypeError("claim 必须是 ReassignmentWorkspacePreparationClaim")
        target_value = _sqlite_architecture_storage_value(
            claim.target_architecture_raw,
            name="claim.target_architecture_raw",
        )
        now = self._now()
        connection = self._require_connection()
        row = connection.execute(
            """
            SELECT operation_id, claim_owner, claim_token, fencing_token,
                   lease_expires_at, state
            FROM reassign_workspace_preparation_claims
            WHERE target_architecture_id = ?
            """,
            (target_value,),
        ).fetchone()
        if row is None:
            return ReassignmentWriteOutcome.STALE_LEASE
        if (
            row["operation_id"] != claim.operation_id
            or row["claim_owner"] != claim.owner
            or row["claim_token"] != claim.token
            or int(row["fencing_token"]) != claim.fencing_token
            or row["state"] != _WORKSPACE_PREPARATION_CLAIM_ACTIVE
        ):
            return ReassignmentWriteOutcome.STALE_LEASE
        current_expiry = self._repository._normalize_utc_text(
            row["lease_expires_at"],
            name="claim_lease_expires_at",
        )
        if self._repository._is_expired(current_expiry, now=now):
            return ReassignmentWriteOutcome.STALE_LEASE
        cursor = connection.execute(
            """
            UPDATE reassign_workspace_preparation_claims
            SET state = ?, updated_at = ?
            WHERE target_architecture_id = ?
              AND operation_id = ?
              AND claim_owner = ?
              AND claim_token = ?
              AND fencing_token = ?
              AND state = ?
            """,
            (
                _WORKSPACE_PREPARATION_CLAIM_RELEASED,
                now,
                target_value,
                claim.operation_id,
                claim.owner,
                claim.token,
                claim.fencing_token,
                _WORKSPACE_PREPARATION_CLAIM_ACTIVE,
            ),
        )
        if cursor.rowcount != 1:
            return ReassignmentWriteOutcome.STALE_LEASE
        self._append_event(
            operation_id=claim.operation_id,
            event_type=ReassignmentEventType.WORKSPACE_PREPARATION_CLAIM_RELEASED,
            detail_code="claim_released",
            reference_digest=self._repository._text_digest(
                _raw_json_text(claim.target_architecture_raw)
            ),
            fencing_token=claim.fencing_token,
            actor_digest=self._repository._text_digest(claim.owner),
        )
        self._stage_log(
            logging.INFO,
            "分类节点变更已释放目标 workspace 准备权: operation_id=%s claim_fencing_token=%s",
            claim.operation_id,
            claim.fencing_token,
        )
        return ReassignmentWriteOutcome.APPLIED

    def _owned_operation(
        self,
        lease: ReassignmentLease,
    ) -> ReassignmentOperationRecord | ReassignmentWriteOutcome:
        """读取并验证所有条件写共同使用的 lease/fencing 所有权。"""

        if not isinstance(lease, ReassignmentLease):
            raise TypeError("lease 必须是 ReassignmentLease")
        record = self._get_operation_record(lease.operation_id)
        if record is None:
            return ReassignmentWriteOutcome.OPERATION_NOT_FOUND
        if not operation_holds_document_protection(record.operation.status):
            # lease 字段需要作为历史审计保留，不能通过“字段仍存在”推断终态仍可写。
            return ReassignmentWriteOutcome.CONFLICT
        current_lease = record.lease
        if (
            current_lease.owner != lease.owner
            or current_lease.token != lease.token
            or current_lease.fencing_token != lease.fencing_token
            or current_lease.expires_at != lease.expires_at
        ):
            return ReassignmentWriteOutcome.STALE_LEASE
        if self._repository._is_expired(current_lease.expires_at, now=self._now()):
            return ReassignmentWriteOutcome.STALE_LEASE
        return record

    @staticmethod
    def _recovery_fencing_is_authorized(
        record: ReassignmentOperationRecord,
        *,
        lease: ReassignmentLease,
        recovery_authorized: bool,
    ) -> bool:
        """校验恢复隔离只能由新 fencing 的显式恢复路径解除或继续。

        ``recovery_required`` 是跨进程持久化隔离。仅持有原 lease 且把布尔参数设为 true
        不能继续写 Step；必须先完成过期接管，使 fencing 严格大于进入隔离时记录的值。
        """

        if record.operation.status is not ReassignmentOperationStatus.RECOVERY_REQUIRED:
            return True
        return (
            recovery_authorized
            and record.recovery_required_fencing_token is not None
            and lease.fencing_token > record.recovery_required_fencing_token
        )

    def renew_lease(
        self,
        *,
        lease: ReassignmentLease,
        lease_expires_at: str,
    ) -> ReassignmentLeaseUpdateResult:
        """使用 token 与 fencing 条件续租，过期 owner 不能重新取得写入权。"""

        self._require_writable()
        owned = self._owned_operation(lease)
        if isinstance(owned, ReassignmentWriteOutcome):
            return ReassignmentLeaseUpdateResult(owned)
        now = self._now()
        new_expiry = self._repository._normalize_utc_text(
            lease_expires_at,
            name="lease_expires_at",
        )
        if self._repository._is_expired(new_expiry, now=now):
            raise ReassignmentContractError("续租 lease_expires_at 必须晚于当前时间")
        connection = self._require_connection()
        cursor = connection.execute(
            """
            UPDATE reassign_operations
            SET lease_expires_at = ?, updated_at = ?
            WHERE operation_id = ?
              AND lease_owner = ?
              AND lease_token = ?
              AND fencing_token = ?
              AND lease_expires_at = ?
            """,
            (
                new_expiry,
                now,
                lease.operation_id,
                lease.owner,
                lease.token,
                lease.fencing_token,
                lease.expires_at,
            ),
        )
        if cursor.rowcount != 1:
            return ReassignmentLeaseUpdateResult(ReassignmentWriteOutcome.STALE_LEASE)
        renewed = ReassignmentLease(
            operation_id=lease.operation_id,
            owner=lease.owner,
            token=lease.token,
            fencing_token=lease.fencing_token,
            expires_at=new_expiry,
        )
        renewed_claim: ReassignmentWorkspacePreparationClaim | None = None
        claim_row = connection.execute(
            """
            SELECT claim_token, fencing_token, lease_expires_at
            FROM reassign_workspace_preparation_claims
            WHERE operation_id = ? AND claim_owner = ? AND state = ?
            """,
            (
                lease.operation_id,
                lease.owner,
                _WORKSPACE_PREPARATION_CLAIM_ACTIVE,
            ),
        ).fetchone()
        if claim_row is not None:
            claim_expiry = self._repository._normalize_utc_text(
                claim_row["lease_expires_at"],
                name="claim_lease_expires_at",
            )
            # 已经过期的准备权只能由后续 fencing 接管，续租 Operation 不能把它复活。
            if not self._repository._is_expired(claim_expiry, now=now):
                claim_cursor = connection.execute(
                    """
                    UPDATE reassign_workspace_preparation_claims
                    SET lease_expires_at = ?, updated_at = ?
                    WHERE operation_id = ? AND claim_owner = ?
                      AND claim_token = ? AND fencing_token = ?
                      AND lease_expires_at = ? AND state = ?
                    """,
                    (
                        new_expiry,
                        now,
                        lease.operation_id,
                        lease.owner,
                        claim_row["claim_token"],
                        int(claim_row["fencing_token"]),
                        claim_row["lease_expires_at"],
                        _WORKSPACE_PREPARATION_CLAIM_ACTIVE,
                    ),
                )
                if claim_cursor.rowcount != 1:
                    raise ReassignmentContractError(
                        "Operation 续租时目标 workspace 准备权发生并发变化"
                    )
                renewed_claim = ReassignmentWorkspacePreparationClaim(
                    target_architecture_raw=owned.operation.target_architecture_raw,
                    operation_id=lease.operation_id,
                    owner=lease.owner,
                    token=claim_row["claim_token"],
                    fencing_token=int(claim_row["fencing_token"]),
                    expires_at=new_expiry,
                )
        self._append_event(
            operation_id=lease.operation_id,
            event_type=ReassignmentEventType.LEASE_RENEWED,
            operation_status=owned.operation.status,
            detail_code="lease_renewed",
            fencing_token=lease.fencing_token,
            actor_digest=self._repository._text_digest(lease.owner),
        )
        self._stage_log(
            logging.DEBUG,
            "分类节点变更 lease 已续期: operation_id=%s fencing_token=%s",
            lease.operation_id,
            lease.fencing_token,
        )
        return ReassignmentLeaseUpdateResult(
            ReassignmentWriteOutcome.APPLIED,
            renewed,
            renewed_claim,
        )

    def take_over_expired_lease(
        self,
        request: ReassignmentExpiredLeaseTakeoverRequest,
    ) -> ReassignmentLeaseUpdateResult:
        """仅在 active/recovery Operation 已过期时原子接管并递增 fencing token。"""

        self._require_writable()
        if not isinstance(request, ReassignmentExpiredLeaseTakeoverRequest):
            raise TypeError("request 必须是 ReassignmentExpiredLeaseTakeoverRequest")
        record = self._get_operation_record(request.operation_id)
        if record is None:
            return ReassignmentLeaseUpdateResult(
                ReassignmentWriteOutcome.OPERATION_NOT_FOUND
            )
        if not operation_holds_document_protection(record.operation.status):
            return ReassignmentLeaseUpdateResult(ReassignmentWriteOutcome.CONFLICT)
        if record.operation.fencing_token != request.expected_fencing_token:
            return ReassignmentLeaseUpdateResult(ReassignmentWriteOutcome.STALE_LEASE)
        now = self._now()
        if not self._repository._is_expired(record.lease.expires_at, now=now):
            return ReassignmentLeaseUpdateResult(ReassignmentWriteOutcome.NOT_EXPIRED)
        new_expiry = self._repository._normalize_utc_text(
            request.lease_expires_at,
            name="lease_expires_at",
        )
        if self._repository._is_expired(new_expiry, now=now):
            raise ReassignmentContractError("接管后的 lease_expires_at 必须晚于当前时间")
        new_fencing = request.expected_fencing_token + 1
        connection = self._require_connection()
        cursor = connection.execute(
            f"""
            UPDATE reassign_operations
            SET lease_owner = ?, lease_token = ?, lease_expires_at = ?,
                fencing_token = ?, updated_at = ?
            WHERE operation_id = ?
              AND fencing_token = ?
              AND lease_expires_at <= ?
              AND status IN ({_ACTIVE_OPERATION_STATUS_SQL})
            """,
            (
                request.lease_owner,
                request.lease_token,
                new_expiry,
                new_fencing,
                now,
                request.operation_id,
                request.expected_fencing_token,
                now,
            ),
        )
        if cursor.rowcount != 1:
            return ReassignmentLeaseUpdateResult(ReassignmentWriteOutcome.STALE_LEASE)
        new_lease = ReassignmentLease(
            operation_id=request.operation_id,
            owner=request.lease_owner,
            token=request.lease_token,
            fencing_token=new_fencing,
            expires_at=new_expiry,
        )
        # 同一 Operation 可能在创建目标 workspace 前持有独立 claim。只有该 claim 同样过期，
        # 才能随 Operation lease 一起换 owner/token/fencing；绝不触碰属于其他 Operation 或仍
        # 有效的 claim，防止旧恢复器发生 ABA 释放新 owner 的目标准备权。
        recovered_claim: ReassignmentWorkspacePreparationClaim | None = None
        claim_row = connection.execute(
            """
            SELECT target_architecture_id, operation_id, claim_owner, claim_token,
                   fencing_token, lease_expires_at, state
            FROM reassign_workspace_preparation_claims
            WHERE operation_id = ? AND state = ?
            """,
            (
                request.operation_id,
                _WORKSPACE_PREPARATION_CLAIM_ACTIVE,
            ),
        ).fetchone()
        if claim_row is not None:
            claim_expiry = self._repository._normalize_utc_text(
                claim_row["lease_expires_at"],
                name="claim_lease_expires_at",
            )
            if self._repository._is_expired(claim_expiry, now=now):
                new_claim_token = (
                    request.workspace_claim_token or claim_row["claim_token"]
                )
                new_claim_fencing = int(claim_row["fencing_token"]) + 1
                claim_cursor = connection.execute(
                    """
                    UPDATE reassign_workspace_preparation_claims
                    SET claim_owner = ?, claim_token = ?, fencing_token = ?,
                        lease_expires_at = ?, updated_at = ?
                    WHERE target_architecture_id = ?
                      AND operation_id = ?
                      AND claim_owner = ?
                      AND claim_token = ?
                      AND fencing_token = ?
                      AND lease_expires_at <= ?
                      AND state = ?
                    """,
                    (
                        request.lease_owner,
                        new_claim_token,
                        new_claim_fencing,
                        new_expiry,
                        now,
                        int(claim_row["target_architecture_id"]),
                        request.operation_id,
                        claim_row["claim_owner"],
                        claim_row["claim_token"],
                        int(claim_row["fencing_token"]),
                        now,
                        _WORKSPACE_PREPARATION_CLAIM_ACTIVE,
                    ),
                )
                if claim_cursor.rowcount != 1:
                    raise ReassignmentContractError(
                        "接管 Operation lease 时目标 workspace 准备权发生并发变化"
                    )
                recovered_claim = ReassignmentWorkspacePreparationClaim(
                    target_architecture_raw=record.operation.target_architecture_raw,
                    operation_id=request.operation_id,
                    owner=request.lease_owner,
                    token=new_claim_token,
                    fencing_token=new_claim_fencing,
                    expires_at=new_expiry,
                )
                self._append_event(
                    operation_id=request.operation_id,
                    event_type=ReassignmentEventType.WORKSPACE_PREPARATION_CLAIM_TAKEN_OVER,
                    operation_status=record.operation.status,
                    detail_code="workspace_claim_taken_over",
                    reference_digest=self._repository._text_digest(
                        _raw_json_text(record.operation.target_architecture_raw)
                    ),
                    fencing_token=new_claim_fencing,
                    actor_digest=self._repository._text_digest(
                        request.actor or request.lease_owner
                    ),
                    reason_code=request.reason_code,
                )
            else:
                # Operation 与 claim 的租期理论上应在续租时同步延长。遇到不一致时保守保留
                # 旧 claim，恢复服务不把它当作自己可释放的资源，并在审计中留下可诊断事实。
                self._append_event(
                    operation_id=request.operation_id,
                    event_type=ReassignmentEventType.WORKSPACE_PREPARATION_CLAIM_BLOCKED,
                    operation_status=record.operation.status,
                    detail_code="workspace_claim_not_expired_during_takeover",
                    fencing_token=new_fencing,
                    actor_digest=self._repository._text_digest(
                        request.actor or request.lease_owner
                    ),
                    reason_code=request.reason_code,
                )
        self._append_event(
            operation_id=request.operation_id,
            event_type=ReassignmentEventType.LEASE_TAKEN_OVER,
            operation_status=record.operation.status,
            detail_code="lease_taken_over",
            fencing_token=new_fencing,
            actor_digest=self._repository._text_digest(
                request.actor or request.lease_owner
            ),
            reason_code=request.reason_code,
        )
        self._stage_log(
            logging.WARNING,
            "分类节点变更过期 lease 已接管: operation_id=%s fencing_token=%s",
            request.operation_id,
            new_fencing,
        )
        return ReassignmentLeaseUpdateResult(
            ReassignmentWriteOutcome.APPLIED,
            new_lease,
            recovered_claim,
        )

    def record_recovery_observation(
        self,
        observation: ReassignmentRecoveryObservation,
    ) -> ReassignmentRecoveryObservationRecord | ReassignmentWriteOutcome:
        """追加恢复前/后的探测快照，并把人工操作者与原因写入脱敏审计。

        外部成员关系探测必须在 UoW 外完成；本方法只把已经得到的枚举结论写入本地事实。每次
        观测均追加新行，而不是覆盖旧行，因此后续人工排障能区分“接管后初始现场”和“补偿后
        确认现场”。
        """

        self._require_writable()
        if not isinstance(observation, ReassignmentRecoveryObservation):
            raise TypeError("observation 必须是 ReassignmentRecoveryObservation")
        owned = self._owned_operation(observation.lease)
        if isinstance(owned, ReassignmentWriteOutcome):
            return owned
        if not self._recovery_fencing_is_authorized(
            owned,
            lease=observation.lease,
            recovery_authorized=True,
        ):
            return ReassignmentWriteOutcome.CONFLICT
        expected_remote = owned.operation.document.requires_remote_membership_change
        if observation.remote_membership_required != expected_remote:
            raise ReassignmentContractError(
                "恢复观测的远端成员关系标记与冻结文档不一致"
            )
        if not expected_remote and (
            observation.source_binding_state
            is not ReassignmentBindingState.NOT_APPLICABLE
            or observation.target_binding_state
            is not ReassignmentBindingState.NOT_APPLICABLE
        ):
            raise ReassignmentContractError(
                "本地-only 文档的恢复观测不能携带远端成员关系状态"
            )

        now = self._now()
        connection = self._require_connection()
        cursor = connection.execute(
            """
            INSERT INTO reassign_recovery_observations (
                operation_id, fencing_token, local_commit_state,
                source_binding_state, target_binding_state,
                remote_membership_required, actor_digest, reason_code, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation.lease.operation_id,
                observation.lease.fencing_token,
                observation.local_commit_state.value,
                observation.source_binding_state.value,
                observation.target_binding_state.value,
                1 if observation.remote_membership_required else 0,
                self._repository._text_digest(observation.actor),
                observation.reason_code,
                now,
            ),
        )
        observation_id = cursor.lastrowid
        if not isinstance(observation_id, int) or observation_id < 1:
            raise ReassignmentContractError("恢复观测写入后缺少 observation_id")
        detail_code = (
            f"local={observation.local_commit_state.value};"
            f"source={observation.source_binding_state.value};"
            f"target={observation.target_binding_state.value}"
        )
        self._append_event(
            operation_id=observation.lease.operation_id,
            event_type=ReassignmentEventType.RECOVERY_OBSERVATION_RECORDED,
            step_name=owned.operation.current_step,
            operation_status=owned.operation.status,
            detail_code=detail_code,
            fencing_token=observation.lease.fencing_token,
            actor_digest=self._repository._text_digest(observation.actor),
            reason_code=observation.reason_code,
        )
        self._stage_log(
            logging.INFO,
            "分类节点变更恢复观测已记录: operation_id=%s fencing_token=%s local=%s source=%s target=%s",
            observation.lease.operation_id,
            observation.lease.fencing_token,
            observation.local_commit_state.value,
            observation.source_binding_state.value,
            observation.target_binding_state.value,
        )
        return ReassignmentRecoveryObservationRecord(
            observation_id=observation_id,
            observation=observation,
            observed_at=now,
        )

    def _validate_recovery_terminal_observation(
        self,
        *,
        owned: ReassignmentOperationRecord,
        request: ReassignmentRecoveryFinalizationRequest,
    ) -> None:
        """核验恢复终态所引用的是当前 fencing 下最新且未篡改的观测事实。"""

        observation = request.observation.observation
        if observation.remote_membership_required != (
            owned.operation.document.requires_remote_membership_change
        ):
            raise ReassignmentContractError("恢复终态的远端成员关系标记不匹配")
        current_local_state = self._local_commit_state_for_record(owned)
        if current_local_state is not observation.local_commit_state:
            raise ReassignmentContractError(
                "恢复终态前本地文档状态已变化，禁止使用旧观测释放保护"
            )
        connection = self._require_connection()
        row = connection.execute(
            """
            SELECT observation_id, local_commit_state, source_binding_state,
                   target_binding_state, remote_membership_required,
                   actor_digest, reason_code
            FROM reassign_recovery_observations
            WHERE operation_id = ? AND fencing_token = ?
            ORDER BY observation_id DESC
            LIMIT 1
            """,
            (request.lease.operation_id, request.lease.fencing_token),
        ).fetchone()
        if row is None or int(row["observation_id"]) != request.observation.observation_id:
            raise ReassignmentContractError(
                "恢复终态必须引用当前 fencing 下最新的已持久化观测"
            )
        if (
            row["local_commit_state"] != observation.local_commit_state.value
            or row["source_binding_state"] != observation.source_binding_state.value
            or row["target_binding_state"] != observation.target_binding_state.value
            or int(row["remote_membership_required"])
            != int(observation.remote_membership_required)
            or row["actor_digest"] != self._repository._text_digest(observation.actor)
            or row["reason_code"] != observation.reason_code
        ):
            raise ReassignmentContractError("恢复终态引用的观测事实校验失败")

    @staticmethod
    def _validate_recovery_terminal_invariant(
        request: ReassignmentRecoveryFinalizationRequest,
    ) -> None:
        """以观测到的成员关系校验不同终态的最小跨系统不变量。"""

        observation = request.observation.observation
        if request.next_status is ReassignmentOperationStatus.SUCCEEDED:
            if observation.local_commit_state is not ReassignmentLocalCommitState.TARGET_COMMITTED:
                raise ReassignmentContractError("恢复成功终态要求本地分类已提交到目标")
            if observation.remote_membership_required:
                if (
                    observation.target_binding_state
                    is not ReassignmentBindingState.CONFIRMED_PRESENT
                    or observation.source_binding_state
                    not in {
                        ReassignmentBindingState.CONFIRMED_ABSENT,
                        ReassignmentBindingState.NOT_APPLICABLE,
                    }
                ):
                    raise ReassignmentContractError(
                        "恢复成功终态要求目标存在且来源已移除"
                    )
            elif (
                observation.source_binding_state
                is not ReassignmentBindingState.NOT_APPLICABLE
                or observation.target_binding_state
                is not ReassignmentBindingState.NOT_APPLICABLE
            ):
                raise ReassignmentContractError("本地-only 成功终态不应携带远端绑定状态")
            return

        if request.next_status is ReassignmentOperationStatus.COMPENSATED:
            if observation.local_commit_state is not ReassignmentLocalCommitState.SOURCE_UNCHANGED:
                raise ReassignmentContractError("补偿终态要求本地分类仍指向来源")
            if not observation.remote_membership_required:
                raise ReassignmentContractError("本地-only 文档不应进入补偿终态")
            if (
                observation.target_binding_state
                is not ReassignmentBindingState.CONFIRMED_ABSENT
                or observation.source_binding_state
                not in {
                    ReassignmentBindingState.CONFIRMED_PRESENT,
                    ReassignmentBindingState.NOT_APPLICABLE,
                }
            ):
                raise ReassignmentContractError(
                    "补偿终态要求目标已移除且来源已恢复"
                )
            return

        if request.next_status is ReassignmentOperationStatus.FAILED:
            if observation.local_commit_state is not ReassignmentLocalCommitState.SOURCE_UNCHANGED:
                raise ReassignmentContractError("无副作用失败终态要求本地分类未提交")
            if observation.remote_membership_required and (
                observation.target_binding_state
                is not ReassignmentBindingState.CONFIRMED_ABSENT
                or observation.source_binding_state
                not in {
                    ReassignmentBindingState.CONFIRMED_PRESENT,
                    ReassignmentBindingState.NOT_APPLICABLE,
                }
            ):
                raise ReassignmentContractError(
                    "无副作用失败终态要求远端仍保持来源绑定且不存在目标绑定"
                )
            if not observation.remote_membership_required and (
                observation.source_binding_state
                is not ReassignmentBindingState.NOT_APPLICABLE
                or observation.target_binding_state
                is not ReassignmentBindingState.NOT_APPLICABLE
            ):
                raise ReassignmentContractError("本地-only 失败终态不应携带远端绑定状态")
            return

        raise ReassignmentContractError("恢复终态类型不受支持")

    def _release_recovery_preparation_claim(
        self,
        *,
        claim: ReassignmentWorkspacePreparationClaim,
        lease: ReassignmentLease,
        now: str,
        actor: str,
        reason_code: str,
    ) -> ReassignmentWriteOutcome:
        """在恢复终态同一事务内释放精确接管的 claim，拒绝旧 token/fencing。"""

        if claim.operation_id != lease.operation_id or claim.owner != lease.owner:
            raise ReassignmentContractError("恢复终态使用的目标准备权与当前 lease 不匹配")
        target_value = _sqlite_architecture_storage_value(
            claim.target_architecture_raw,
            name="preparation_claim.target_architecture_raw",
        )
        connection = self._require_connection()
        cursor = connection.execute(
            """
            UPDATE reassign_workspace_preparation_claims
            SET state = ?, updated_at = ?
            WHERE target_architecture_id = ?
              AND operation_id = ?
              AND claim_owner = ?
              AND claim_token = ?
              AND fencing_token = ?
              AND lease_expires_at = ?
              AND state = ?
            """,
            (
                _WORKSPACE_PREPARATION_CLAIM_RELEASED,
                now,
                target_value,
                claim.operation_id,
                claim.owner,
                claim.token,
                claim.fencing_token,
                claim.expires_at,
                _WORKSPACE_PREPARATION_CLAIM_ACTIVE,
            ),
        )
        if cursor.rowcount != 1:
            return ReassignmentWriteOutcome.STALE_LEASE
        self._append_event(
            operation_id=lease.operation_id,
            event_type=ReassignmentEventType.WORKSPACE_PREPARATION_CLAIM_RELEASED,
            detail_code="recovery_claim_released",
            reference_digest=self._repository._text_digest(
                _raw_json_text(claim.target_architecture_raw)
            ),
            fencing_token=claim.fencing_token,
            actor_digest=self._repository._text_digest(actor),
            reason_code=reason_code,
        )
        return ReassignmentWriteOutcome.APPLIED

    def finalize_recovery_operation(
        self,
        request: ReassignmentRecoveryFinalizationRequest,
    ) -> ReassignmentOperationRecord | ReassignmentWriteOutcome:
        """依据最新恢复观测安全关闭 Operation，并可原子释放已接管 claim。"""

        self._require_writable()
        if not isinstance(request, ReassignmentRecoveryFinalizationRequest):
            raise TypeError("request 必须是 ReassignmentRecoveryFinalizationRequest")
        owned = self._owned_operation(request.lease)
        if isinstance(owned, ReassignmentWriteOutcome):
            return owned
        if not self._recovery_fencing_is_authorized(
            owned,
            lease=request.lease,
            recovery_authorized=True,
        ):
            return ReassignmentWriteOutcome.CONFLICT
        self._validate_recovery_terminal_observation(owned=owned, request=request)
        self._validate_recovery_terminal_invariant(request)
        if request.next_status is ReassignmentOperationStatus.SUCCEEDED:
            # 恢复入口同样必须满足前向成功的全部持久事实：仅凭一次远端探测结果，
            # 不能掩盖 target workspace 映射或前向 Step 检查点缺失的崩溃窗口。
            # 这样既避免将不完整现场伪造为成功，也保证恢复收口与正常本地 CAS
            # 使用同一套成功门禁。
            self._validate_forward_success_prerequisites(owned)
        if request.next_status is ReassignmentOperationStatus.FAILED:
            # ``failed`` 仍然严格保留 1E-4 的“没有待恢复副作用”门禁；不能因为走了人工
            # 恢复入口就把创建 workspace、未知步骤或已确认远端写伪装成普通失败。
            self._validate_no_side_effect_failure_prerequisites(owned)

        operation = replace(
            transition_operation_status(
                owned.operation,
                request.next_status,
                recovery_authorized=True,
                terminal_evidence=request.terminal_evidence,
            ),
            current_step=request.current_step,
        )
        now = self._now()
        observation = request.observation.observation
        if request.preparation_claim is not None:
            released = self._release_recovery_preparation_claim(
                claim=request.preparation_claim,
                lease=request.lease,
                now=now,
                actor=observation.actor,
                reason_code=observation.reason_code,
            )
            if released is not ReassignmentWriteOutcome.APPLIED:
                return released

        connection = self._require_connection()
        connection.execute(
            """
            UPDATE reassign_operations
            SET status = ?, current_step = ?, error_code = ?, error_summary = ?,
                updated_at = ?, finished_at = ?
            WHERE operation_id = ?
            """,
            (
                operation.status.value,
                operation.current_step.value,
                request.error_code,
                request.error_summary,
                now,
                now,
                request.lease.operation_id,
            ),
        )
        self._append_event(
            operation_id=request.lease.operation_id,
            event_type=ReassignmentEventType.RECOVERY_OPERATION_FINALIZED,
            step_name=operation.current_step,
            operation_status=operation.status,
            detail_code=operation.status.value,
            fencing_token=request.lease.fencing_token,
            actor_digest=self._repository._text_digest(observation.actor),
            reason_code=observation.reason_code,
        )
        self._stage_log(
            logging.INFO,
            "分类节点变更恢复终态已提交: operation_id=%s status=%s fencing_token=%s",
            request.lease.operation_id,
            operation.status.value,
            request.lease.fencing_token,
        )
        updated = self._get_operation_record(request.lease.operation_id)
        if updated is None:
            raise ReassignmentContractError("恢复终态提交后无法读取 Operation")
        return updated

    def begin_step_mutation(
        self,
        *,
        lease: ReassignmentLease,
        step_name: ReassignmentStepName,
        recovery_authorized: bool = False,
    ) -> ReassignmentStepRecord | ReassignmentWriteOutcome:
        """在外部写前同事务记录意图和 ``mutation_started`` 检查点。"""

        self._require_writable()
        if not isinstance(step_name, ReassignmentStepName):
            raise TypeError("step_name 必须是 ReassignmentStepName")
        if not isinstance(recovery_authorized, bool):
            raise TypeError("recovery_authorized 必须是 bool")
        owned = self._owned_operation(lease)
        if isinstance(owned, ReassignmentWriteOutcome):
            return owned
        if not self._recovery_fencing_is_authorized(
            owned,
            lease=lease,
            recovery_authorized=recovery_authorized,
        ):
            return ReassignmentWriteOutcome.CONFLICT
        step_record = self._get_step_record(
            operation_id=lease.operation_id,
            step_name=step_name,
        )
        if step_record is None:
            raise ReassignmentContractError("Operation 缺少固定 Step")
        if step_record.step.state is ReassignmentStepState.PENDING:
            step = record_step_write_intent(step_record.step)
            step = transition_step_state(step, ReassignmentStepState.MUTATION_STARTED)
        elif (
            step_record.step.state is ReassignmentStepState.KNOWN_FAILED
            and recovery_authorized
        ):
            if (
                step_record.last_attempt_fencing_token is None
                or lease.fencing_token
                <= step_record.last_attempt_fencing_token
            ):
                return ReassignmentWriteOutcome.CONFLICT
            step = transition_step_state(
                step_record.step,
                ReassignmentStepState.MUTATION_STARTED,
                recovery_authorized=True,
            )
            # 新一次受控尝试不能把前次失败的引用或错误作为本次结果继续传播。
            step = replace(
                step,
                external_reference=None,
                error_code=None,
                error_summary=None,
            )
        else:
            raise ReassignmentContractError(
                "Step 尚未处于允许发起外部写的状态"
            )
        now = self._now()
        connection = self._require_connection()
        connection.execute(
            """
            UPDATE reassign_steps
            SET state = ?, write_intent_recorded = 1, external_reference = ?,
                probe_outcome = NULL, error_code = ?, error_summary = ?,
                mutation_started_at = ?, attempt_count = ?,
                last_attempt_fencing_token = ?, updated_at = ?
            WHERE operation_id = ? AND step_name = ?
            """,
            (
                step.state.value,
                step.external_reference,
                step.error_code,
                step.error_summary,
                now,
                step_record.attempt_count + 1,
                lease.fencing_token,
                now,
                lease.operation_id,
                step_name.value,
            ),
        )
        connection.execute(
            """
            UPDATE reassign_operations
            SET current_step = ?, updated_at = ?
            WHERE operation_id = ?
            """,
            (step_name.value, now, lease.operation_id),
        )
        self._append_event(
            operation_id=lease.operation_id,
            event_type=ReassignmentEventType.STEP_MUTATION_STARTED,
            step_name=step_name,
            operation_status=owned.operation.status,
            detail_code="write_intent_recorded",
            fencing_token=lease.fencing_token,
            attempt_count=step_record.attempt_count + 1,
        )
        self._stage_log(
            logging.INFO,
            "分类节点变更步骤已记录外部写意图: operation_id=%s step=%s attempt=%s",
            lease.operation_id,
            step_name.value,
            step_record.attempt_count + 1,
        )
        updated = self._get_step_record(
            operation_id=lease.operation_id,
            step_name=step_name,
        )
        if updated is None:
            raise ReassignmentContractError("步骤写入后无法读取")
        return updated

    def complete_step(
        self,
        completion: ReassignmentStepCompletion,
    ) -> ReassignmentStepRecord | ReassignmentWriteOutcome:
        """持久化外部写或探测的确定结果；未知结果不会被改写为成功。"""

        self._require_writable()
        if not isinstance(completion, ReassignmentStepCompletion):
            raise TypeError("completion 必须是 ReassignmentStepCompletion")
        owned = self._owned_operation(completion.lease)
        if isinstance(owned, ReassignmentWriteOutcome):
            return owned
        if not self._recovery_fencing_is_authorized(
            owned,
            lease=completion.lease,
            recovery_authorized=completion.recovery_authorized,
        ):
            return ReassignmentWriteOutcome.CONFLICT
        step_record = self._get_step_record(
            operation_id=completion.lease.operation_id,
            step_name=completion.step_name,
        )
        if step_record is None:
            raise ReassignmentContractError("Operation 缺少固定 Step")
        step = transition_step_state(
            step_record.step,
            completion.next_state,
            recovery_authorized=completion.recovery_authorized,
        )
        step = replace(
            step,
            external_reference=completion.external_reference,
            error_code=completion.error_code,
            error_summary=completion.error_summary,
        )
        now = self._now()
        connection = self._require_connection()
        connection.execute(
            """
            UPDATE reassign_steps
            SET state = ?, external_reference = ?, probe_outcome = ?, error_code = ?,
                error_summary = ?, updated_at = ?
            WHERE operation_id = ? AND step_name = ?
            """,
            (
                step.state.value,
                step.external_reference,
                (
                    completion.probe_outcome.value
                    if completion.probe_outcome is not None
                    else None
                ),
                step.error_code,
                step.error_summary,
                now,
                completion.lease.operation_id,
                completion.step_name.value,
            ),
        )
        connection.execute(
            """
            UPDATE reassign_operations
            SET current_step = ?, updated_at = ?
            WHERE operation_id = ?
            """,
            (completion.step_name.value, now, completion.lease.operation_id),
        )
        self._append_event(
            operation_id=completion.lease.operation_id,
            event_type=ReassignmentEventType.STEP_COMPLETED,
            step_name=completion.step_name,
            operation_status=owned.operation.status,
            detail_code=step.state.value,
            fencing_token=completion.lease.fencing_token,
            attempt_count=step_record.attempt_count,
            probe_outcome=completion.probe_outcome,
        )
        level = (
            logging.INFO
            if step.state is ReassignmentStepState.SUCCEEDED
            else logging.WARNING
        )
        self._stage_log(
            level,
            "分类节点变更步骤已收口: operation_id=%s step=%s state=%s",
            completion.lease.operation_id,
            completion.step_name.value,
            step.state.value,
        )
        updated = self._get_step_record(
            operation_id=completion.lease.operation_id,
            step_name=completion.step_name,
        )
        if updated is None:
            raise ReassignmentContractError("步骤结果写入后无法读取")
        return updated

    def transition_operation(
        self,
        transition: ReassignmentOperationTransition,
    ) -> ReassignmentOperationRecord | ReassignmentWriteOutcome:
        """在当前 fencing 下改变 Operation 状态，并强制终态证据门禁。"""

        self._require_writable()
        if not isinstance(transition, ReassignmentOperationTransition):
            raise TypeError("transition 必须是 ReassignmentOperationTransition")
        if transition.next_status in _TERMINAL_OPERATION_STATUSES:
            raise ReassignmentContractError(
                "释放文档保护的终态必须使用具备持久事实校验的专用提交方法"
            )
        owned = self._owned_operation(transition.lease)
        if isinstance(owned, ReassignmentWriteOutcome):
            return owned
        if not self._recovery_fencing_is_authorized(
            owned,
            lease=transition.lease,
            recovery_authorized=transition.recovery_authorized,
        ):
            # recovery_required 是持久隔离，不允许原请求仅传一个布尔值就自行解封。
            return ReassignmentWriteOutcome.CONFLICT
        operation = transition_operation_status(
            owned.operation,
            transition.next_status,
            recovery_authorized=transition.recovery_authorized,
            terminal_evidence=transition.terminal_evidence,
        )
        operation = replace(operation, current_step=transition.current_step)
        now = self._now()
        finished_at = now if operation.status in _TERMINAL_OPERATION_STATUSES else None
        recovery_required_fencing_token = (
            transition.lease.fencing_token
            if operation.status is ReassignmentOperationStatus.RECOVERY_REQUIRED
            else owned.recovery_required_fencing_token
        )
        connection = self._require_connection()
        connection.execute(
            """
            UPDATE reassign_operations
            SET status = ?, current_step = ?, error_code = ?, error_summary = ?,
                recovery_required_fencing_token = ?, updated_at = ?, finished_at = ?
            WHERE operation_id = ?
            """,
            (
                operation.status.value,
                (
                    operation.current_step.value
                    if operation.current_step is not None
                    else None
                ),
                transition.error_code,
                transition.error_summary,
                recovery_required_fencing_token,
                now,
                finished_at,
                operation.operation_id,
            ),
        )
        self._append_event(
            operation_id=operation.operation_id,
            event_type=ReassignmentEventType.OPERATION_TRANSITIONED,
            step_name=operation.current_step,
            operation_status=operation.status,
            detail_code=operation.status.value,
            fencing_token=transition.lease.fencing_token,
        )
        self._stage_log(
            logging.INFO,
            "分类节点变更 Operation 状态已更新: operation_id=%s status=%s",
            operation.operation_id,
            operation.status.value,
        )
        updated = self._get_operation_record(operation.operation_id)
        if updated is None:
            raise ReassignmentContractError("Operation 状态写入后无法读取")
        return updated

    def record_workspace_mapping(
        self,
        request: ReassignmentWorkspaceMappingRequest,
    ) -> ReassignmentOperationRecord | ReassignmentWriteOutcome:
        """原子登记目标 mapping 与 prepare Step 成功，冲突绝不静默覆盖。"""

        self._require_writable()
        if not isinstance(request, ReassignmentWorkspaceMappingRequest):
            raise TypeError("request 必须是 ReassignmentWorkspaceMappingRequest")
        owned = self._owned_operation(request.lease)
        if isinstance(owned, ReassignmentWriteOutcome):
            return owned
        if (
            owned.operation.target_architecture_raw.canonical_json()
            != request.target_architecture_raw.canonical_json()
        ):
            raise ReassignmentContractError("目标 workspace mapping 与 Operation 目标分类不一致")
        step_record = self._get_step_record(
            operation_id=request.lease.operation_id,
            step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
        )
        if step_record is None:
            raise ReassignmentContractError("Operation 缺少 prepare_target_workspace Step")
        step = transition_step_state(
            step_record.step,
            ReassignmentStepState.SUCCEEDED,
        )
        target_value = _sqlite_architecture_storage_value(
            request.target_architecture_raw,
            name="target_architecture_raw",
        )
        connection = self._require_connection()
        claim = request.preparation_claim
        if claim is not None:
            if (
                claim.operation_id != request.lease.operation_id
                or claim.owner != request.lease.owner
                or claim.target_architecture_raw.canonical_json()
                != request.target_architecture_raw.canonical_json()
            ):
                raise ReassignmentContractError("目标 workspace mapping 使用了不匹配的准备权")
            claim_target_value = _sqlite_architecture_storage_value(
                claim.target_architecture_raw,
                name="preparation_claim.target_architecture_raw",
            )
            if claim_target_value != target_value:
                raise ReassignmentContractError("目标 workspace mapping 准备权的存储分类不一致")
            claim_row = connection.execute(
                """
                SELECT operation_id, claim_owner, claim_token, fencing_token,
                       lease_expires_at, state
                FROM reassign_workspace_preparation_claims
                WHERE target_architecture_id = ?
                """,
                (target_value,),
            ).fetchone()
            if claim_row is None:
                return ReassignmentWriteOutcome.STALE_LEASE
            if (
                claim_row["operation_id"] != claim.operation_id
                or claim_row["claim_owner"] != claim.owner
                or claim_row["claim_token"] != claim.token
                or int(claim_row["fencing_token"]) != claim.fencing_token
                or claim_row["state"] != _WORKSPACE_PREPARATION_CLAIM_ACTIVE
            ):
                return ReassignmentWriteOutcome.STALE_LEASE
            claim_expiry = self._repository._normalize_utc_text(
                claim_row["lease_expires_at"],
                name="claim_lease_expires_at",
            )
            if self._repository._is_expired(claim_expiry, now=self._now()):
                return ReassignmentWriteOutcome.STALE_LEASE
        architecture_row = connection.execute(
            """
            SELECT architecture_id, workspace_slug FROM workspaces
            WHERE architecture_id = ?
            """,
            (target_value,),
        ).fetchone()
        slug_rows = connection.execute(
            "SELECT architecture_id, workspace_slug FROM workspaces"
        ).fetchall()
        matching_slug_rows = [
            row
            for row in slug_rows
            if row["workspace_slug"].casefold() == request.workspace_slug.casefold()
        ]
        if len(matching_slug_rows) > 1:
            raise ReassignmentContractError(
                "本地 workspace mapping 已存在大小写无关的重复 slug"
            )
        slug_row = matching_slug_rows[0] if matching_slug_rows else None
        if architecture_row is not None and (
            architecture_row["workspace_slug"].casefold()
            != request.workspace_slug.casefold()
        ):
            self._append_event(
                operation_id=request.lease.operation_id,
                event_type=ReassignmentEventType.WORKSPACE_MAPPING_RECORDED,
                step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                operation_status=owned.operation.status,
                detail_code="workspace_mapping_conflict",
                fencing_token=request.lease.fencing_token,
                attempt_count=step_record.attempt_count,
            )
            self._stage_log(
                logging.WARNING,
                "分类节点变更 workspace 映射冲突: operation_id=%s",
                request.lease.operation_id,
            )
            return ReassignmentWriteOutcome.CONFLICT
        if architecture_row is None and claim is None:
            # 目标 mapping 尚不存在时，任何写入都必须证明自己持有按目标分类唯一的持久化
            # 准备权。进程内锁无法覆盖多实例部署；此处拒绝直接写入，防止绕过创建串行化。
            self._append_event(
                operation_id=request.lease.operation_id,
                event_type=ReassignmentEventType.WORKSPACE_MAPPING_RECORDED,
                step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                operation_status=owned.operation.status,
                detail_code="workspace_preparation_claim_required",
                fencing_token=request.lease.fencing_token,
                attempt_count=step_record.attempt_count,
            )
            self._stage_log(
                logging.WARNING,
                "分类节点变更拒绝无准备权的目标 workspace mapping 写入: operation_id=%s",
                request.lease.operation_id,
            )
            return ReassignmentWriteOutcome.CONFLICT
        if (
            architecture_row is not None
            and request.ownership
            is ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION
        ):
            # 本地映射已存在时，当前 Operation 不可能是该映射的创建者。若仍接受
            # ``true``，后续补偿可能把共享 workspace 误判为可删除的临时资源。
            self._append_event(
                operation_id=request.lease.operation_id,
                event_type=ReassignmentEventType.WORKSPACE_MAPPING_RECORDED,
                step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                operation_status=owned.operation.status,
                detail_code="workspace_creation_fact_conflict",
                fencing_token=request.lease.fencing_token,
                attempt_count=step_record.attempt_count,
            )
            self._stage_log(
                logging.WARNING,
                "分类节点变更 workspace 创建事实冲突: operation_id=%s",
                request.lease.operation_id,
            )
            return ReassignmentWriteOutcome.CONFLICT
        if (
            slug_row is not None
            and (
                architecture_row is None
                or slug_row["architecture_id"] != architecture_row["architecture_id"]
            )
        ):
            self._append_event(
                operation_id=request.lease.operation_id,
                event_type=ReassignmentEventType.WORKSPACE_MAPPING_RECORDED,
                step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                operation_status=owned.operation.status,
                detail_code="workspace_slug_conflict",
                fencing_token=request.lease.fencing_token,
                attempt_count=step_record.attempt_count,
            )
            self._stage_log(
                logging.WARNING,
                "分类节点变更 workspace slug 已被其他分类占用: operation_id=%s",
                request.lease.operation_id,
            )
            return ReassignmentWriteOutcome.CONFLICT
        if architecture_row is None:
            connection.execute(
                """
                INSERT INTO workspaces (architecture_id, workspace_slug)
                VALUES (?, ?)
                """,
                (target_value, request.workspace_slug),
            )
        persisted_ownership = (
            ReassignmentWorkspaceOwnership.PREEXISTING
            if architecture_row is not None
            else request.ownership
        )
        preparation_outcome = {
            ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION: (
                ReassignmentMutationOutcome.CONFIRMED_EFFECT
            ),
            ReassignmentWorkspaceOwnership.PREEXISTING: (
                ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT
            ),
            # UNKNOWN 只表示唯一资源可用但创建者不可证明，不能伪造“本次创建”
            # 或“明确复用”的写入效果，因此 Step 成功但探测效果保持为空。
            ReassignmentWorkspaceOwnership.UNKNOWN: None,
        }[persisted_ownership]
        now = self._now()
        connection.execute(
            """
            UPDATE reassign_steps
            SET state = ?, probe_outcome = ?, updated_at = ?
            WHERE operation_id = ? AND step_name = ?
            """,
            (
                step.state.value,
                (
                    None
                    if preparation_outcome is None
                    else preparation_outcome.value
                ),
                now,
                request.lease.operation_id,
                ReassignmentStepName.PREPARE_TARGET_WORKSPACE.value,
            ),
        )
        connection.execute(
            """
            UPDATE reassign_operations
            SET target_workspace_slug = ?, target_workspace_ownership = ?,
                current_step = ?, updated_at = ?
            WHERE operation_id = ?
            """,
            (
                request.workspace_slug,
                persisted_ownership.value,
                ReassignmentStepName.PREPARE_TARGET_WORKSPACE.value,
                now,
                request.lease.operation_id,
            ),
        )
        if claim is not None:
            # mapping、prepare Step 成功及 claim 释放在同一事务内提交。若 CAS 不匹配则抛出
            # 契约错误使整个事务回滚，绝不能留下“mapping 已写但准备权仍属于旧 owner”的中间态。
            cursor = connection.execute(
                """
                UPDATE reassign_workspace_preparation_claims
                SET state = ?, updated_at = ?
                WHERE target_architecture_id = ?
                  AND operation_id = ?
                  AND claim_owner = ?
                  AND claim_token = ?
                  AND fencing_token = ?
                  AND state = ?
                """,
                (
                    _WORKSPACE_PREPARATION_CLAIM_RELEASED,
                    now,
                    target_value,
                    claim.operation_id,
                    claim.owner,
                    claim.token,
                    claim.fencing_token,
                    _WORKSPACE_PREPARATION_CLAIM_ACTIVE,
                ),
            )
            if cursor.rowcount != 1:
                raise ReassignmentContractError(
                    "目标 workspace mapping 提交时准备权已失效"
                )
        self._append_event(
            operation_id=request.lease.operation_id,
            event_type=ReassignmentEventType.WORKSPACE_MAPPING_RECORDED,
            step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
            operation_status=owned.operation.status,
            detail_code="workspace_mapping_recorded",
            fencing_token=request.lease.fencing_token,
            attempt_count=step_record.attempt_count,
            probe_outcome=preparation_outcome,
        )
        self._stage_log(
            logging.INFO,
            "分类节点变更目标 workspace 映射已登记: operation_id=%s ownership=%s",
            request.lease.operation_id,
            persisted_ownership.value,
        )
        updated = self._get_operation_record(request.lease.operation_id)
        if updated is None:
            raise ReassignmentContractError("workspace mapping 写入后无法读取 Operation")
        return updated

    def record_workspace_preparation_fact(
        self,
        request: ReassignmentWorkspacePreparationFactRequest,
    ) -> ReassignmentOperationRecord | ReassignmentWriteOutcome:
        """保存已确认但尚未形成 mapping 的远端 workspace 身份。

        该入口故意不改变 Step 状态、不写 ``workspaces``、不释放 preparation claim。只有
        1E-5 在完成查回和冲突处置后，才能继续登记 mapping 或执行补偿。
        """

        self._require_writable()
        if not isinstance(request, ReassignmentWorkspacePreparationFactRequest):
            raise TypeError(
                "request 必须是 ReassignmentWorkspacePreparationFactRequest"
            )
        owned = self._owned_operation(request.lease)
        if isinstance(owned, ReassignmentWriteOutcome):
            return owned
        if not self._recovery_fencing_is_authorized(
            owned,
            lease=request.lease,
            recovery_authorized=request.recovery_authorized,
        ):
            return ReassignmentWriteOutcome.CONFLICT
        if owned.target_workspace_slug is not None and (
            owned.target_workspace_slug.casefold() != request.workspace_slug.casefold()
            or owned.target_workspace_ownership is not request.ownership
        ):
            return ReassignmentWriteOutcome.CONFLICT
        step_record = self._get_step_record(
            operation_id=request.lease.operation_id,
            step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
        )
        if step_record is None:
            raise ReassignmentContractError("Operation 缺少 prepare_target_workspace Step")
        allowed_recovery_unknown = (
            request.recovery_authorized
            and step_record.step.state is ReassignmentStepState.OUTCOME_UNKNOWN
        )
        if (
            step_record.step.state is not ReassignmentStepState.MUTATION_STARTED
            and not allowed_recovery_unknown
        ) or not step_record.step.write_intent_recorded:
            raise ReassignmentContractError(
                "只有已记录 prepare 写意图的 Operation 才能保存远端准备事实"
            )

        now = self._now()
        connection = self._require_connection()
        connection.execute(
            """
            UPDATE reassign_steps
            SET external_reference = ?, error_code = ?, updated_at = ?
            WHERE operation_id = ? AND step_name = ?
            """,
            (
                request.workspace_slug,
                request.error_code,
                now,
                request.lease.operation_id,
                ReassignmentStepName.PREPARE_TARGET_WORKSPACE.value,
            ),
        )
        connection.execute(
            """
            UPDATE reassign_operations
            SET target_workspace_slug = ?, target_workspace_ownership = ?,
                current_step = ?, error_code = ?, updated_at = ?
            WHERE operation_id = ?
            """,
            (
                request.workspace_slug,
                request.ownership.value,
                ReassignmentStepName.PREPARE_TARGET_WORKSPACE.value,
                request.error_code,
                now,
                request.lease.operation_id,
            ),
        )
        self._append_event(
            operation_id=request.lease.operation_id,
            event_type=ReassignmentEventType.WORKSPACE_PREPARATION_FACT_RECORDED,
            step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
            operation_status=owned.operation.status,
            detail_code=request.error_code,
            reference_digest=self._repository._text_digest(request.workspace_slug),
            fencing_token=request.lease.fencing_token,
            attempt_count=step_record.attempt_count,
            probe_outcome={
                ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION: (
                    ReassignmentMutationOutcome.CONFIRMED_EFFECT
                ),
                ReassignmentWorkspaceOwnership.PREEXISTING: (
                    ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT
                ),
                ReassignmentWorkspaceOwnership.UNKNOWN: (
                    ReassignmentMutationOutcome.OUTCOME_UNKNOWN
                ),
            }[request.ownership],
        )
        self._stage_log(
            logging.WARNING,
            "分类节点变更已保留待恢复 workspace 准备事实: "
            "operation_id=%s ownership=%s error_code=%s",
            request.lease.operation_id,
            request.ownership.value,
            request.error_code,
        )
        updated = self._get_operation_record(request.lease.operation_id)
        if updated is None:
            raise ReassignmentContractError("workspace 准备事实写入后无法读取 Operation")
        return updated

    def begin_best_effort_pin(
        self,
        *,
        lease: ReassignmentLease,
    ) -> ReassignmentWriteOutcome:
        """在非关键 Pin 外部写之前追加审计意图。"""

        self._require_writable()
        owned = self._owned_operation(lease)
        if isinstance(owned, ReassignmentWriteOutcome):
            return owned
        attach_step = self._get_step_record(
            operation_id=lease.operation_id,
            step_name=ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
        )
        if (
            attach_step is None
            or attach_step.step.state is not ReassignmentStepState.SUCCEEDED
        ):
            raise ReassignmentContractError("目标成员挂载尚未确认，禁止发起 Pin")
        self._append_event(
            operation_id=lease.operation_id,
            event_type=ReassignmentEventType.BEST_EFFORT_PIN_ATTEMPTED,
            step_name=ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
            operation_status=owned.operation.status,
            detail_code="pin_attempted",
            fencing_token=lease.fencing_token,
        )
        return ReassignmentWriteOutcome.APPLIED

    def complete_best_effort_pin(
        self,
        completion: ReassignmentBestEffortPinCompletion,
    ) -> ReassignmentWriteOutcome:
        """记录 Pin 的有界结果；不改变关键步骤和 Operation 状态。"""

        self._require_writable()
        if not isinstance(completion, ReassignmentBestEffortPinCompletion):
            raise TypeError("completion 必须是 ReassignmentBestEffortPinCompletion")
        owned = self._owned_operation(completion.lease)
        if isinstance(owned, ReassignmentWriteOutcome):
            return owned
        connection = self._require_connection()
        counts = connection.execute(
            """
            SELECT
                SUM(CASE WHEN event_type = ? THEN 1 ELSE 0 END) AS attempted,
                SUM(CASE WHEN event_type = ? THEN 1 ELSE 0 END) AS completed
            FROM reassign_events
            WHERE operation_id = ?
            """,
            (
                ReassignmentEventType.BEST_EFFORT_PIN_ATTEMPTED.value,
                ReassignmentEventType.BEST_EFFORT_PIN_COMPLETED.value,
                completion.lease.operation_id,
            ),
        ).fetchone()
        attempted = 0 if counts is None else int(counts["attempted"] or 0)
        completed = 0 if counts is None else int(counts["completed"] or 0)
        if attempted <= completed:
            raise ReassignmentContractError("Pin 完成事实缺少对应的持久化尝试意图")
        self._append_event(
            operation_id=completion.lease.operation_id,
            event_type=ReassignmentEventType.BEST_EFFORT_PIN_COMPLETED,
            step_name=ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
            operation_status=owned.operation.status,
            detail_code=completion.error_code or "pin_completed",
            fencing_token=completion.lease.fencing_token,
            probe_outcome=completion.mutation_outcome,
        )
        return ReassignmentWriteOutcome.APPLIED

    def _validate_forward_success_prerequisites(
        self,
        record: ReassignmentOperationRecord,
    ) -> None:
        """在本地 CAS 前核验远端必要步骤，避免伪造前向成功证据。"""

        remote_step_names = (
            ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
            ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
            ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
        )
        steps: dict[ReassignmentStepName, ReassignmentStepRecord] = {}
        for step_name in remote_step_names:
            step_record = self._get_step_record(
                operation_id=record.operation.operation_id,
                step_name=step_name,
            )
            if step_record is None:
                raise ReassignmentContractError(
                    f"Operation 缺少必要 Step: {step_name.value}"
                )
            steps[step_name] = step_record

        if not record.operation.document.requires_remote_membership_change:
            if any(
                item.step.state is not ReassignmentStepState.PENDING
                for item in steps.values()
            ):
                raise ReassignmentContractError(
                    "本地-only文档存在不应发生的远端步骤事实"
                )
            return

        if (
            record.target_workspace_slug is None
            or record.target_workspace_ownership is None
            or steps[
                ReassignmentStepName.PREPARE_TARGET_WORKSPACE
            ].step.state
            is not ReassignmentStepState.SUCCEEDED
            or steps[
                ReassignmentStepName.ATTACH_TARGET_DOCUMENT
            ].step.state
            is not ReassignmentStepState.SUCCEEDED
        ):
            raise ReassignmentContractError(
                "目标 workspace 与文档成员关系尚未确认，禁止提交成功终态"
            )
        if (
            record.source_workspace_slug is not None
            and steps[
                ReassignmentStepName.DETACH_SOURCE_DOCUMENT
            ].step.state
            is not ReassignmentStepState.SUCCEEDED
        ):
            raise ReassignmentContractError(
                "来源文档成员关系尚未确认移除，禁止提交成功终态"
            )

    def _validate_no_side_effect_failure_prerequisites(
        self,
        record: ReassignmentOperationRecord,
    ) -> None:
        """确认失败关闭不会掩盖任何可能需要恢复的远端或本地副作用。"""

        if record.target_workspace_ownership in {
            ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION,
            ReassignmentWorkspaceOwnership.UNKNOWN,
        }:
            raise ReassignmentContractError(
                "目标 workspace 存在本次创建或创建者未知事实，禁止无副作用失败收口"
            )
        for step_name in ReassignmentStepName:
            step_record = self._get_step_record(
                operation_id=record.operation.operation_id,
                step_name=step_name,
            )
            if step_record is None:
                raise ReassignmentContractError(
                    f"Operation 缺少固定 Step: {step_name.value}"
                )
            state = step_record.step.state
            outcome = step_record.probe_outcome
            if state in {
                ReassignmentStepState.MUTATION_STARTED,
                ReassignmentStepState.OUTCOME_UNKNOWN,
            }:
                raise ReassignmentContractError(
                    "存在正在执行或结果未知的 Step，禁止无副作用失败收口"
                )
            if outcome in {
                ReassignmentMutationOutcome.CONFIRMED_EFFECT,
                ReassignmentMutationOutcome.OUTCOME_UNKNOWN,
            }:
                raise ReassignmentContractError(
                    "存在已确认或未知外部副作用，禁止无副作用失败收口"
                )
            if (
                state is ReassignmentStepState.KNOWN_FAILED
                and outcome is not ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT
            ):
                raise ReassignmentContractError(
                    "已知失败 Step 缺少无副作用探测事实，禁止释放文档保护"
                )

    def finalize_no_side_effect_failure(
        self,
        request: ReassignmentNoSideEffectFailureRequest,
    ) -> ReassignmentOperationRecord | ReassignmentWriteOutcome:
        """核验无待恢复副作用后，原子写入失败终态并释放同文档保护。"""

        self._require_writable()
        if not isinstance(request, ReassignmentNoSideEffectFailureRequest):
            raise TypeError("request 必须是 ReassignmentNoSideEffectFailureRequest")
        if (
            request.terminal_evidence.kind
            is not ReassignmentTerminalEvidenceKind.NO_SIDE_EFFECT_FAILURE_CONFIRMED
        ):
            raise ReassignmentContractError("无副作用失败收口必须携带对应终态证据")
        owned = self._owned_operation(request.lease)
        if isinstance(owned, ReassignmentWriteOutcome):
            return owned
        self._validate_no_side_effect_failure_prerequisites(owned)
        operation = replace(
            transition_operation_status(
                owned.operation,
                ReassignmentOperationStatus.FAILED,
                terminal_evidence=request.terminal_evidence,
            ),
            current_step=request.current_step,
        )
        now = self._now()
        connection = self._require_connection()
        connection.execute(
            """
            UPDATE reassign_operations
            SET status = ?, current_step = ?, error_code = ?, error_summary = ?,
                updated_at = ?, finished_at = ?
            WHERE operation_id = ?
            """,
            (
                operation.status.value,
                (
                    operation.current_step.value
                    if operation.current_step is not None
                    else None
                ),
                request.error_code,
                request.error_summary,
                now,
                now,
                request.lease.operation_id,
            ),
        )
        self._append_event(
            operation_id=request.lease.operation_id,
            event_type=ReassignmentEventType.NO_SIDE_EFFECT_FAILURE_FINALIZED,
            step_name=operation.current_step,
            operation_status=operation.status,
            detail_code=request.error_code,
            fencing_token=request.lease.fencing_token,
        )
        self._stage_log(
            logging.INFO,
            "分类节点变更已按无副作用失败收口: operation_id=%s error_code=%s",
            request.lease.operation_id,
            request.error_code,
        )
        updated = self._get_operation_record(request.lease.operation_id)
        if updated is None:
            raise ReassignmentContractError("无副作用失败收口后无法读取 Operation")
        return updated

    def commit_local_architecture(
        self,
        request: ReassignmentLocalCommitRequest,
    ) -> ReassignmentOperationRecord | ReassignmentWriteOutcome:
        """将 documents 条件更新、commit Step 和成功 Operation 放进同一 SQLite 事务。"""

        self._require_writable()
        if not isinstance(request, ReassignmentLocalCommitRequest):
            raise TypeError("request 必须是 ReassignmentLocalCommitRequest")
        owned = self._owned_operation(request.lease)
        if isinstance(owned, ReassignmentWriteOutcome):
            return owned
        if owned.operation.document != request.expected_document:
            raise ReassignmentContractError("本地 CAS 文档快照与 Operation 冻结事实不一致")
        if (
            owned.operation.target_architecture_raw.canonical_json()
            != request.target_architecture_raw.canonical_json()
        ):
            raise ReassignmentContractError("本地 CAS 目标分类与 Operation 不一致")
        self._validate_forward_success_prerequisites(owned)
        step_record = self._get_step_record(
            operation_id=request.lease.operation_id,
            step_name=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
        )
        if step_record is None:
            raise ReassignmentContractError("Operation 缺少 commit_local_architecture Step")
        step = transition_step_state(
            step_record.step,
            ReassignmentStepState.SUCCEEDED,
        )
        known_failure_step = transition_step_state(
            step_record.step,
            ReassignmentStepState.KNOWN_FAILED,
        )
        now = self._now()
        document = request.expected_document
        connection = self._require_connection()
        try:
            cursor = connection.execute(
                """
                UPDATE documents
                SET architecture_id = ?
                WHERE id = ?
                  AND file_name = ?
                  AND architecture_id = ?
                  AND (
                    anything_doc_id = ?
                    OR (anything_doc_id IS NULL AND ? IS NULL)
                  )
                  AND (
                    doc_path = ?
                    OR (doc_path IS NULL AND ? IS NULL)
                  )
                """,
                (
                    _sqlite_architecture_storage_value(
                        request.target_architecture_raw,
                        name="target_architecture_raw",
                    ),
                    document.document_row_id,
                    document.file_name,
                    document.source_architecture_id,
                    document.anything_doc_id,
                    document.anything_doc_id,
                    document.doc_path,
                    document.doc_path,
                ),
            )
        except sqlite3.IntegrityError:
            connection.execute(
                """
                UPDATE reassign_steps
                SET state = ?, probe_outcome = ?, error_code = ?, updated_at = ?
                WHERE operation_id = ? AND step_name = ?
                """,
                (
                    known_failure_step.state.value,
                    ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT.value,
                    "local_unique_conflict",
                    now,
                    request.lease.operation_id,
                    ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE.value,
                ),
            )
            self._append_event(
                operation_id=request.lease.operation_id,
                event_type=ReassignmentEventType.LOCAL_ARCHITECTURE_CONFLICT,
                step_name=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
                operation_status=owned.operation.status,
                detail_code="local_unique_conflict",
                reference_digest=self._repository._document_digest(document),
                fencing_token=request.lease.fencing_token,
            )
            self._stage_log(
                logging.WARNING,
                "分类节点变更本地 CAS 唯一冲突: operation_id=%s document_digest=%s",
                request.lease.operation_id,
                self._repository._document_digest(document),
            )
            return ReassignmentWriteOutcome.CONFLICT
        if cursor.rowcount != 1:
            connection.execute(
                """
                UPDATE reassign_steps
                SET state = ?, probe_outcome = ?, error_code = ?, updated_at = ?
                WHERE operation_id = ? AND step_name = ?
                """,
                (
                    known_failure_step.state.value,
                    ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT.value,
                    "local_cas_not_one_row",
                    now,
                    request.lease.operation_id,
                    ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE.value,
                ),
            )
            self._append_event(
                operation_id=request.lease.operation_id,
                event_type=ReassignmentEventType.LOCAL_ARCHITECTURE_CONFLICT,
                step_name=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
                operation_status=owned.operation.status,
                detail_code="local_cas_not_one_row",
                reference_digest=self._repository._document_digest(document),
                fencing_token=request.lease.fencing_token,
            )
            self._stage_log(
                logging.WARNING,
                "分类节点变更本地 CAS 未命中唯一行: operation_id=%s document_digest=%s",
                request.lease.operation_id,
                self._repository._document_digest(document),
            )
            return ReassignmentWriteOutcome.CONFLICT

        operation = transition_operation_status(
            owned.operation,
            ReassignmentOperationStatus.SUCCEEDED,
            terminal_evidence=request.terminal_evidence,
        )
        operation = replace(
            operation,
            current_step=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
        )
        connection.execute(
            """
            UPDATE reassign_steps
            SET state = ?, updated_at = ?
            WHERE operation_id = ? AND step_name = ?
            """,
            (
                step.state.value,
                now,
                request.lease.operation_id,
                ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE.value,
            ),
        )
        connection.execute(
            """
            UPDATE reassign_operations
            SET status = ?, current_step = ?, updated_at = ?, finished_at = ?
            WHERE operation_id = ?
            """,
            (
                operation.status.value,
                operation.current_step.value,
                now,
                now,
                request.lease.operation_id,
            ),
        )
        self._append_event(
            operation_id=request.lease.operation_id,
            event_type=ReassignmentEventType.LOCAL_ARCHITECTURE_COMMITTED,
            step_name=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
            operation_status=operation.status,
            detail_code="local_cas_committed",
            reference_digest=self._repository._document_digest(document),
            fencing_token=request.lease.fencing_token,
        )
        self._stage_log(
            logging.INFO,
            "分类节点变更本地 CAS 与终态已原子提交: operation_id=%s document_digest=%s",
            request.lease.operation_id,
            self._repository._document_digest(document),
        )
        updated = self._get_operation_record(request.lease.operation_id)
        if updated is None:
            raise ReassignmentContractError("本地 CAS 提交后无法读取 Operation")
        return updated


__all__ = ["SQLiteReassignmentRepository", "SQLiteReassignmentUnitOfWork"]
