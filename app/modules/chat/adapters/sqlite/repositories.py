"""文件对话本地权威表的 SQLite 仓储实现。"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence

from app.modules.chat.domain.document_candidates import ChatDocumentCandidate
from app.modules.chat.domain.document_scope import (
    CHAT_SCOPE_MODE_ARCHITECTURE,
    CHAT_SCOPE_MODE_FILES,
    CHAT_SCOPE_MODES,
    CHAT_SCOPE_SELECTION_ARCHITECTURE_INITIAL,
    CHAT_SCOPE_SELECTION_ARCHITECTURE_REUSE,
    CHAT_SCOPE_SELECTION_MODES,
    CHAT_SCOPE_SOURCE_ARCHITECTURE_INITIAL,
    CHAT_SCOPE_SOURCE_MODES,
    ChatRequestedFile,
    ChatScopeHead,
    ChatScopeRevision,
    ChatSessionScopeBinding,
)
from app.modules.chat.domain.models import (
    CLEANUP_JOB_FAILED,
    CLEANUP_JOB_PENDING,
    CLEANUP_JOB_REASONS,
    CLEANUP_JOB_RUNNING,
    CLEANUP_JOB_STATUSES,
    CLEANUP_JOB_SUCCEEDED,
    MESSAGE_COMMITTED,
    MESSAGE_DISCARDED,
    MESSAGE_PENDING,
    MESSAGE_ROLES,
    MESSAGE_STATUSES,
    RUN_ABORTED,
    RUN_ACCEPTED,
    RUN_ACTIVE_STATUSES,
    RUN_FAILED,
    RUN_RUNNING,
    RUN_STATUSES,
    RUN_SUCCEEDED,
    RUN_TERMINAL_STATUSES,
    SESSION_ACTIVE,
    SESSION_DELETED,
    SESSION_DELETING,
    SESSION_ERROR,
    SESSION_STATUSES,
    ChatDocumentBinding,
    ChatCleanupJob,
    ChatMessage,
    ChatMessageFile,
    ChatMessageSourceChunk,
    ChatRun,
    ChatRunInput,
    ChatRunInputFile,
    ChatSession,
)
from app.modules.chat.domain.limits import MAX_CHAT_ARCHITECTURE_ID


logger = logging.getLogger(__name__)

# 文件对话完整执行会依次提交受理、资源租约、binding、事件和终态等多个短事务。
# 当进程内流容量显式提高到 50 时，不同会话仍会竞争 SQLite 的唯一写锁；5 秒默认值
# 不足以覆盖这组有界短事务的排队时间。这里保留 30 秒上限，避免无限等待，同时明确
# 这只是单实例 SQLite 的退避窗口，不代表并行写、多实例协调或可靠队列能力。
DEFAULT_CHAT_SQLITE_BUSY_TIMEOUT_SECONDS = 30.0
CHAT_SCHEMA_GENERATION = "conversation-v1"


class ChatSchemaGenerationError(RuntimeError):
    """数据库不属于当前 Conversation 身份世代时失败关闭。"""


_RUN_STATUS_TRANSITIONS = {
    RUN_ACCEPTED: frozenset({RUN_RUNNING, RUN_FAILED, RUN_ABORTED}),
    RUN_RUNNING: frozenset({RUN_SUCCEEDED, RUN_FAILED, RUN_ABORTED}),
    RUN_SUCCEEDED: frozenset(),
    RUN_FAILED: frozenset(),
    RUN_ABORTED: frozenset(),
}

_MESSAGE_STATUS_TRANSITIONS = {
    MESSAGE_PENDING: frozenset({MESSAGE_COMMITTED, MESSAGE_DISCARDED}),
    MESSAGE_COMMITTED: frozenset(),
    MESSAGE_DISCARDED: frozenset(),
}

_SESSION_STATUS_TRANSITIONS = {
    SESSION_ACTIVE: frozenset({SESSION_DELETING}),
    SESSION_DELETING: frozenset({SESSION_DELETED, SESSION_ERROR}),
    SESSION_ERROR: frozenset({SESSION_DELETING}),
    SESSION_DELETED: frozenset(),
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required_text(value: str, *, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


def _optional_text(value: str | None) -> str:
    return str(value or "").strip()


def _optional_canonical_text(value: Any, *, name: str) -> str:
    """读取允许为空、但禁止隐式字符串化或自动修剪的结构化身份字段。"""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    if value.strip() != value:
        raise ValueError(f"{name} must be normalized")
    return value


def _optional_architecture_id(value: Any, *, name: str) -> int | None:
    """严格读取 SQLite/Domain 中的可选 architecture ID。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be int or None")
    if value < 1 or value > MAX_CHAT_ARCHITECTURE_ID:
        raise ValueError(f"{name} is out of range")
    return value


def _validate_choice(value: str, *, name: str, allowed: frozenset[str]) -> str:
    normalized = _required_text(value, name=name)
    if normalized not in allowed:
        raise ValueError(f"{name} 不受支持: {normalized}")
    return normalized


def _json_dumps(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _json_loads_object(value: str) -> dict[str, Any]:
    loaded = json.loads(value or "{}")
    if not isinstance(loaded, dict):
        raise ValueError("metadata_json 必须是 JSON 对象")
    return loaded


def _json_loads_list(value: str) -> list[Any]:
    loaded = json.loads(value or "[]")
    if not isinstance(loaded, list):
        raise ValueError("files_json must be a JSON array")
    return loaded


def _connect(
    db_path: str,
    *,
    timeout_seconds: float = DEFAULT_CHAT_SQLITE_BUSY_TIMEOUT_SECONDS,
) -> sqlite3.Connection:
    normalized_timeout_seconds = max(0.0, float(timeout_seconds))
    connection = sqlite3.connect(
        db_path,
        timeout=normalized_timeout_seconds,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    # 有界忙等待超时可使受支持的单实例模式中短暂写入冲突的结果确定。它不能替代队列
    # 或分布式锁，但可避免相邻请求提交短事务时 SQLite 立即失败。
    busy_timeout_ms = round(normalized_timeout_seconds * 1000)
    connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    return connection


@contextmanager
def _connection_scope(db_path: str) -> Iterator[sqlite3.Connection]:
    connection = _connect(db_path)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def ensure_chat_schema(db_path: str) -> None:
    """按顺序应用对话模块的 SQLite 架构迁移。

    当前应用仅支持单个 SQLite 实例，但架构仍需要明确的版本历史。在启动时反复执行
    无名 ``CREATE TABLE`` 语句，会使人无法判断数据库实际包含哪个数据模型。因此每次
    新增结构性变更都必须以下方带编号的迁移形式加入。
    """
    normalized_path = _required_text(db_path, name="db_path")
    Path(normalized_path).parent.mkdir(parents=True, exist_ok=True)
    with _connection_scope(normalized_path) as connection:
        # WAL 在保留既定单写入者语义的同时改善并发读取。部署说明已禁止将该数据库文件
        # 放在网络共享目录中。
        connection.execute("PRAGMA journal_mode = WAL")
        existing_tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        has_migrations = "chat_schema_migrations" in existing_tables
        has_generation = "chat_schema_metadata" in existing_tables
        legacy_chat_tables = existing_tables.intersection({"chats", "chat_sessions"})
        if legacy_chat_tables and not has_generation:
            # 更早的开发库可能只有历史 ``chats``/``chat_sessions`` 表，甚至没有
            # migration ledger。它仍然属于旧世代，不能当作空库叠加新表。
            raise ChatSchemaGenerationError(
                "检测到旧 Chat Schema 世代；请停服清理并重建空 Chat 数据库，"
                "当前版本不会自动迁移或删除旧数据"
            )
        if has_migrations and not has_generation:
            # v1-v6 没有 generation 元数据。本需求已经明确不迁移也不清空旧数据，
            # 因此启动只能失败关闭，并要求执行停服清理流程后重建空库。
            raise ChatSchemaGenerationError(
                "检测到旧 Chat Schema 世代；请停服清理并重建空 Chat 数据库，"
                "当前版本不会自动迁移或删除旧数据"
            )
        if has_generation and not has_migrations:
            raise ChatSchemaGenerationError(
                "Chat Schema 元数据不完整，拒绝猜测或自动修复"
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_schema_metadata (
                singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                generation TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        metadata = connection.execute(
            "SELECT generation FROM chat_schema_metadata WHERE singleton_id = 1"
        ).fetchone()
        if metadata is None:
            connection.execute(
                """
                INSERT INTO chat_schema_metadata(singleton_id, generation, created_at)
                VALUES (1, ?, ?)
                """,
                (CHAT_SCHEMA_GENERATION, _utc_now_iso()),
            )
        elif str(metadata["generation"]) != CHAT_SCHEMA_GENERATION:
            raise ChatSchemaGenerationError(
                "Chat Schema generation 不受当前版本支持；拒绝自动迁移或清空"
            )
        applied_versions = {
            int(row["version"])
            for row in connection.execute(
                "SELECT version FROM chat_schema_migrations"
            ).fetchall()
        }
        # sqlite3 驱动不会保证第一条 DDL 自动开启可回滚事务。若不在迁移循环前显式
        # BEGIN，故障可能留下已创建的新表，却没有版本记录和后续数据复制。所有尚未应用
        # 的迁移及其版本登记必须作为一个写事务提交，失败时数据库停留在原完整版本。
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        for version, migration in _CHAT_SCHEMA_MIGRATIONS:
            if version in applied_versions:
                continue
            migration(connection)
            connection.execute(
                "INSERT INTO chat_schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, _utc_now_iso()),
            )
            logger.info(
                "已应用文件对话 SQLite 架构迁移: version=%s db_path=%s",
                version,
                normalized_path,
            )


def _create_chat_authority_schema(connection: sqlite3.Connection) -> None:
    """为全新的开发数据库创建首版权威对话架构。"""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            conversation_id TEXT PRIMARY KEY,
            workspace_ref TEXT NOT NULL DEFAULT '',
            thread_ref TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK (
                status IN ('active', 'deleting', 'deleted', 'error')
            ),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_runs (
            run_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('accepted', 'running', 'succeeded', 'failed', 'aborted')
            ),
            abort_requested INTEGER NOT NULL DEFAULT 0 CHECK (
                abort_requested IN (0, 1)
            ),
            owner_instance_id TEXT NOT NULL DEFAULT '',
            heartbeat_at TEXT,
            error_message TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_run_inputs (
            run_id TEXT PRIMARY KEY,
            message TEXT NOT NULL,
            files_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES chat_runs(run_id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_run_events (
            run_id TEXT NOT NULL,
            event_seq INTEGER NOT NULL CHECK (event_seq > 0),
            event_type TEXT NOT NULL,
            data_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            PRIMARY KEY(run_id, event_seq),
            FOREIGN KEY(run_id) REFERENCES chat_runs(run_id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_document_bindings (
            binding_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            file_name TEXT NOT NULL,
            original_name TEXT NOT NULL,
            document_ref TEXT NOT NULL,
            external_location TEXT NOT NULL DEFAULT '',
            added_by_run_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(conversation_id, file_name, document_ref),
            FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_document_heads (
            conversation_id TEXT NOT NULL,
            file_name TEXT NOT NULL,
            binding_id TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(conversation_id, file_name),
            FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id)
                ON DELETE CASCADE,
            FOREIGN KEY(binding_id) REFERENCES chat_document_bindings(binding_id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_messages (
            message_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'committed', 'discarded')
            ),
            sequence_no INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(conversation_id, sequence_no),
            FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id)
                ON DELETE CASCADE,
            FOREIGN KEY(run_id) REFERENCES chat_runs(run_id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_message_files (
            message_id TEXT NOT NULL,
            file_name TEXT NOT NULL,
            original_name TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(message_id, file_name),
            FOREIGN KEY(message_id) REFERENCES chat_messages(message_id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_resource_leases (
            lease_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            run_id TEXT NOT NULL DEFAULT '',
            resource_type TEXT NOT NULL CHECK (
                resource_type IN ('workspace', 'thread', 'document_binding')
            ),
            external_ref TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK (
                status IN (
                    'planned', 'active', 'cleanup_pending',
                    'closed', 'cleanup_failed'
                )
            ),
            error_message TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_cleanup_jobs (
            job_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            lease_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'running', 'succeeded', 'failed')
            ),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            next_attempt_at TEXT NOT NULL,
            error_message TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id)
                ON DELETE CASCADE
        )
        """
    )


def _add_chat_constraints_and_indexes(connection: sqlite3.Connection) -> None:
    """添加约束，防止绕过应用服务直接写入时破坏不变量。"""
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_runs_one_active_per_chat
        ON chat_runs (conversation_id)
        WHERE status IN ('accepted', 'running')
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chat_runs_chat_status
        ON chat_runs (conversation_id, status, updated_at)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chat_run_inputs_created_at
        ON chat_run_inputs (created_at)
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_run_events_terminal
        ON chat_run_events (run_id)
        WHERE event_type IN ('done', 'error', 'aborted')
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chat_document_bindings_chat
        ON chat_document_bindings (conversation_id, file_name, created_at)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chat_messages_chat_sequence
        ON chat_messages (conversation_id, sequence_no)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chat_resource_leases_status
        ON chat_resource_leases (status, updated_at)
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_cleanup_jobs_open
        ON chat_cleanup_jobs (conversation_id, reason)
        WHERE status IN ('pending', 'running')
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chat_cleanup_jobs_ready
        ON chat_cleanup_jobs (status, next_attempt_at)
        """
    )


def _add_integrity_triggers_and_refine_cleanup_job_identity(
    connection: sqlite3.Connection,
) -> None:
    """同时安装完整性触发器并细化清理任务身份。

    一个对话可能存在多个尚未清理的临时标题线程。原先的 ``(conversation_id, reason)`` 键会
    错误合并这些相互独立的清理任务。``lease_id`` 是持久化任务身份的一部分；而对话删除
    仍使用空租约 ID，因此每个对话仍保持唯一。
    """
    connection.execute("DROP INDEX IF EXISTS uq_chat_cleanup_jobs_open")
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_cleanup_jobs_open
        ON chat_cleanup_jobs (conversation_id, reason, lease_id)
        WHERE status IN ('pending', 'running')
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_chat_document_bindings_run_chat_insert
        BEFORE INSERT ON chat_document_bindings
        WHEN NEW.added_by_run_id != '' AND NOT EXISTS (
            SELECT 1 FROM chat_runs
            WHERE run_id = NEW.added_by_run_id AND conversation_id = NEW.conversation_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'chat_document_binding run_id does not belong to conversation_id');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_chat_document_bindings_run_chat_update
        BEFORE UPDATE OF conversation_id, added_by_run_id ON chat_document_bindings
        WHEN NEW.added_by_run_id != '' AND NOT EXISTS (
            SELECT 1 FROM chat_runs
            WHERE run_id = NEW.added_by_run_id AND conversation_id = NEW.conversation_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'chat_document_binding run_id does not belong to conversation_id');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_chat_document_heads_binding_chat
        BEFORE INSERT ON chat_document_heads
        WHEN NOT EXISTS (
            SELECT 1 FROM chat_document_bindings
            WHERE binding_id = NEW.binding_id AND conversation_id = NEW.conversation_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'chat_document_head binding_id does not belong to conversation_id');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_chat_document_heads_binding_chat_update
        BEFORE UPDATE OF conversation_id, binding_id ON chat_document_heads
        WHEN NOT EXISTS (
            SELECT 1 FROM chat_document_bindings
            WHERE binding_id = NEW.binding_id AND conversation_id = NEW.conversation_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'chat_document_head binding_id does not belong to conversation_id');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_chat_messages_run_chat_insert
        BEFORE INSERT ON chat_messages
        WHEN NOT EXISTS (
            SELECT 1 FROM chat_runs
            WHERE run_id = NEW.run_id AND conversation_id = NEW.conversation_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'chat_message run_id does not belong to conversation_id');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_chat_messages_run_chat_update
        BEFORE UPDATE OF conversation_id, run_id ON chat_messages
        WHEN NOT EXISTS (
            SELECT 1 FROM chat_runs
            WHERE run_id = NEW.run_id AND conversation_id = NEW.conversation_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'chat_message run_id does not belong to conversation_id');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_chat_resource_leases_run_chat_insert
        BEFORE INSERT ON chat_resource_leases
        WHEN NEW.run_id != '' AND NOT EXISTS (
            SELECT 1 FROM chat_runs
            WHERE run_id = NEW.run_id AND conversation_id = NEW.conversation_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'chat_resource_lease run_id does not belong to conversation_id');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_chat_resource_leases_run_chat_update
        BEFORE UPDATE OF conversation_id, run_id ON chat_resource_leases
        WHEN NEW.run_id != '' AND NOT EXISTS (
            SELECT 1 FROM chat_runs
            WHERE run_id = NEW.run_id AND conversation_id = NEW.conversation_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'chat_resource_lease run_id does not belong to conversation_id');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_chat_resource_leases_external_ref_immutable
        BEFORE UPDATE OF external_ref ON chat_resource_leases
        WHEN OLD.external_ref != '' AND NEW.external_ref != OLD.external_ref
        BEGIN
            SELECT RAISE(ABORT, 'chat_resource_lease external_ref is immutable');
        END
        """
    )


def _add_chat_scope_revisions(connection: sqlite3.Connection) -> None:
    """增加 Requested/Active/Effective Scope 所需的 Schema v4。

    本迁移必须先保持阶段 2 的旧生产链可运行，因此新增 run input 字段使用
    ``legacy_input``/空引用作为过渡默认值。阶段 3 切换后，新受理运行必须显式写入
   三种正式 selection mode 和 Scope Revision；阶段 6 会按已确认方案停服清理开发库。
    """
    connection.execute(
        """
        CREATE TABLE chat_scope_revisions (
            scope_revision_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            source_mode TEXT NOT NULL CHECK (
                source_mode IN ('automatic_initial', 'explicit')
            ),
            source_run_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE chat_scope_members (
            scope_revision_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            file_name TEXT NOT NULL,
            original_name TEXT NOT NULL,
            document_ref TEXT NOT NULL,
            external_location TEXT NOT NULL,
            PRIMARY KEY(scope_revision_id, ordinal),
            UNIQUE(scope_revision_id, file_name),
            UNIQUE(scope_revision_id, document_ref),
            UNIQUE(scope_revision_id, external_location),
            FOREIGN KEY(scope_revision_id)
                REFERENCES chat_scope_revisions(scope_revision_id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE chat_scope_heads (
            conversation_id TEXT PRIMARY KEY,
            scope_revision_id TEXT NOT NULL UNIQUE,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id)
                ON DELETE CASCADE,
            FOREIGN KEY(scope_revision_id)
                REFERENCES chat_scope_revisions(scope_revision_id)
        )
        """
    )
    connection.execute(
        """
        ALTER TABLE chat_run_inputs
        ADD COLUMN requested_files_json TEXT NOT NULL DEFAULT '[]'
        """
    )
    connection.execute(
        """
        ALTER TABLE chat_run_inputs
        ADD COLUMN effective_scope_revision_id TEXT NOT NULL DEFAULT ''
        """
    )
    connection.execute(
        """
        ALTER TABLE chat_run_inputs
        ADD COLUMN selection_mode TEXT NOT NULL DEFAULT 'legacy_input'
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_chat_scope_revisions_chat_created
        ON chat_scope_revisions(conversation_id, created_at, scope_revision_id)
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_chat_scope_members_revision_ordinal
        ON chat_scope_members(scope_revision_id, ordinal)
        """
    )
    connection.execute(
        """
        CREATE TRIGGER trg_chat_scope_revision_run_chat_insert
        BEFORE INSERT ON chat_scope_revisions
        WHEN NOT EXISTS (
            SELECT 1 FROM chat_runs
            WHERE run_id = NEW.source_run_id AND conversation_id = NEW.conversation_id
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'chat_scope_revision source_run_id does not belong to conversation_id'
            );
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER trg_chat_scope_revision_immutable
        BEFORE UPDATE ON chat_scope_revisions
        BEGIN
            SELECT RAISE(ABORT, 'chat_scope_revision is immutable');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER trg_chat_scope_member_immutable
        BEFORE UPDATE ON chat_scope_members
        BEGIN
            SELECT RAISE(ABORT, 'chat_scope_member is immutable');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER trg_chat_scope_head_revision_chat_insert
        BEFORE INSERT ON chat_scope_heads
        WHEN NOT EXISTS (
            SELECT 1 FROM chat_scope_revisions
            WHERE scope_revision_id = NEW.scope_revision_id
              AND conversation_id = NEW.conversation_id
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'chat_scope_head revision does not belong to conversation_id'
            );
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER trg_chat_scope_head_revision_chat_update
        BEFORE UPDATE OF conversation_id, scope_revision_id ON chat_scope_heads
        WHEN NOT EXISTS (
            SELECT 1 FROM chat_scope_revisions
            WHERE scope_revision_id = NEW.scope_revision_id
              AND conversation_id = NEW.conversation_id
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'chat_scope_head revision does not belong to conversation_id'
            );
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER trg_chat_run_input_scope_chat_insert
        BEFORE INSERT ON chat_run_inputs
        WHEN NEW.effective_scope_revision_id != '' AND NOT EXISTS (
            SELECT 1
            FROM chat_runs AS run
            JOIN chat_scope_revisions AS revision
              ON revision.scope_revision_id =
                 NEW.effective_scope_revision_id
            WHERE run.run_id = NEW.run_id
              AND run.conversation_id = revision.conversation_id
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'chat_run_input scope does not belong to run conversation_id'
            );
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER trg_chat_run_input_scope_chat_update
        BEFORE UPDATE OF run_id, effective_scope_revision_id
        ON chat_run_inputs
        WHEN NEW.effective_scope_revision_id != '' AND NOT EXISTS (
            SELECT 1
            FROM chat_runs AS run
            JOIN chat_scope_revisions AS revision
              ON revision.scope_revision_id =
                 NEW.effective_scope_revision_id
            WHERE run.run_id = NEW.run_id
              AND run.conversation_id = revision.conversation_id
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'chat_run_input scope does not belong to run conversation_id'
            );
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER trg_chat_run_input_scope_mode_insert
        BEFORE INSERT ON chat_run_inputs
        WHEN NOT (
            (
                NEW.effective_scope_revision_id = ''
                AND NEW.selection_mode = 'legacy_input'
            )
            OR
            (
                NEW.effective_scope_revision_id != ''
                AND NEW.selection_mode IN (
                    'automatic_initial', 'explicit', 'active_scope_reuse'
                )
                AND NEW.files_json = '[]'
            )
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'chat_run_input scope mode or legacy payload is invalid'
            );
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER trg_chat_run_input_scope_mode_update
        BEFORE UPDATE OF
            files_json, effective_scope_revision_id, selection_mode
        ON chat_run_inputs
        WHEN NOT (
            (
                NEW.effective_scope_revision_id = ''
                AND NEW.selection_mode = 'legacy_input'
            )
            OR
            (
                NEW.effective_scope_revision_id != ''
                AND NEW.selection_mode IN (
                    'automatic_initial', 'explicit', 'active_scope_reuse'
                )
                AND NEW.files_json = '[]'
            )
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'chat_run_input scope mode or legacy payload is invalid'
            );
        END
        """
    )


def _add_architecture_chat_scope(connection: sqlite3.Connection) -> None:
    """迁移到 Schema v5，并原子保留全部 v4 Scope 事实。

    v4 ``chat_scope_revisions.source_mode`` 使用表级 CHECK，SQLite 无法原位扩展，因此
    Revision/Member/Head 必须作为一组正式重建。迁移在外层同一事务内完成计数对账和
    ``foreign_key_check``；任一门禁失败都会回滚版本记录和全部 DDL/DML。
    """
    before_counts = {
        table: int(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )
        for table in (
            "chat_scope_revisions",
            "chat_scope_members",
            "chat_scope_heads",
        )
    }

    connection.execute(
        """
        CREATE TABLE chat_session_scope_bindings (
            conversation_id TEXT PRIMARY KEY,
            scope_mode TEXT NOT NULL CHECK (
                scope_mode IN ('files', 'architecture')
            ),
            architecture_id INTEGER,
            created_at TEXT NOT NULL,
            CHECK (
                (scope_mode = 'files' AND architecture_id IS NULL)
                OR
                (
                    scope_mode = 'architecture'
                    AND typeof(architecture_id) = 'integer'
                    AND architecture_id BETWEEN 1 AND 9223372036854775807
                )
            ),
            FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        INSERT INTO chat_session_scope_bindings (
            conversation_id, scope_mode, architecture_id, created_at
        )
        SELECT conversation_id, 'files', NULL, created_at
        FROM conversations
        """
    )

    connection.execute(
        """
        ALTER TABLE chat_run_inputs
        ADD COLUMN requested_architecture_id INTEGER CHECK (
            requested_architecture_id IS NULL
            OR (
                typeof(requested_architecture_id) = 'integer'
                AND requested_architecture_id
                    BETWEEN 1 AND 9223372036854775807
            )
        )
        """
    )
    connection.execute(
        """
        ALTER TABLE chat_messages
        ADD COLUMN architecture_id INTEGER CHECK (
            architecture_id IS NULL
            OR (
                typeof(architecture_id) = 'integer'
                AND architecture_id BETWEEN 1 AND 9223372036854775807
            )
        )
        """
    )

    for trigger_name in (
        "trg_chat_scope_revision_run_chat_insert",
        "trg_chat_scope_revision_immutable",
        "trg_chat_scope_member_immutable",
        "trg_chat_scope_head_revision_chat_insert",
        "trg_chat_scope_head_revision_chat_update",
        "trg_chat_run_input_scope_chat_insert",
        "trg_chat_run_input_scope_chat_update",
        "trg_chat_run_input_scope_mode_insert",
        "trg_chat_run_input_scope_mode_update",
    ):
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")

    connection.execute(
        """
        CREATE TABLE chat_scope_revisions_v5 (
            scope_revision_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            source_mode TEXT NOT NULL CHECK (
                source_mode IN (
                    'automatic_initial', 'explicit', 'architecture_initial'
                )
            ),
            source_run_id TEXT NOT NULL,
            source_architecture_id INTEGER,
            created_at TEXT NOT NULL,
            CHECK (
                (
                    source_mode = 'architecture_initial'
                    AND typeof(source_architecture_id) = 'integer'
                    AND source_architecture_id
                        BETWEEN 1 AND 9223372036854775807
                )
                OR
                (
                    source_mode IN ('automatic_initial', 'explicit')
                    AND source_architecture_id IS NULL
                )
            ),
            FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE chat_scope_members_v5 (
            scope_revision_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            file_name TEXT NOT NULL,
            original_name TEXT NOT NULL,
            document_ref TEXT NOT NULL,
            external_location TEXT NOT NULL,
            PRIMARY KEY(scope_revision_id, ordinal),
            UNIQUE(scope_revision_id, file_name),
            UNIQUE(scope_revision_id, document_ref),
            UNIQUE(scope_revision_id, external_location),
            FOREIGN KEY(scope_revision_id)
                REFERENCES chat_scope_revisions_v5(scope_revision_id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE chat_scope_heads_v5 (
            conversation_id TEXT PRIMARY KEY,
            scope_revision_id TEXT NOT NULL UNIQUE,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id)
                ON DELETE CASCADE,
            FOREIGN KEY(scope_revision_id)
                REFERENCES chat_scope_revisions_v5(scope_revision_id)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO chat_scope_revisions_v5 (
            scope_revision_id, conversation_id, source_mode, source_run_id,
            source_architecture_id, created_at
        )
        SELECT scope_revision_id, conversation_id, source_mode, source_run_id,
               NULL, created_at
        FROM chat_scope_revisions
        """
    )
    connection.execute(
        """
        INSERT INTO chat_scope_members_v5 (
            scope_revision_id, ordinal, file_name, original_name,
            document_ref, external_location
        )
        SELECT scope_revision_id, ordinal, file_name, original_name,
               document_ref, external_location
        FROM chat_scope_members
        """
    )
    connection.execute(
        """
        INSERT INTO chat_scope_heads_v5 (
            conversation_id, scope_revision_id, updated_at
        )
        SELECT conversation_id, scope_revision_id, updated_at
        FROM chat_scope_heads
        """
    )

    connection.execute("DROP TABLE chat_scope_heads")
    connection.execute("DROP TABLE chat_scope_members")
    connection.execute("DROP TABLE chat_scope_revisions")
    connection.execute(
        "ALTER TABLE chat_scope_revisions_v5 RENAME TO chat_scope_revisions"
    )
    connection.execute(
        "ALTER TABLE chat_scope_members_v5 RENAME TO chat_scope_members"
    )
    connection.execute(
        "ALTER TABLE chat_scope_heads_v5 RENAME TO chat_scope_heads"
    )

    connection.execute(
        """
        CREATE INDEX idx_chat_scope_revisions_chat_created
        ON chat_scope_revisions(conversation_id, created_at, scope_revision_id)
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_chat_scope_members_revision_ordinal
        ON chat_scope_members(scope_revision_id, ordinal)
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX uq_chat_scope_one_architecture_revision_per_chat
        ON chat_scope_revisions(conversation_id)
        WHERE source_mode = 'architecture_initial'
        """
    )
    connection.execute(
        """
        CREATE TRIGGER trg_chat_session_scope_binding_immutable
        BEFORE UPDATE ON chat_session_scope_bindings
        BEGIN
            SELECT RAISE(ABORT, 'chat_session_scope_binding is immutable');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER trg_chat_scope_revision_run_chat_insert
        BEFORE INSERT ON chat_scope_revisions
        WHEN NOT EXISTS (
            SELECT 1 FROM chat_runs
            WHERE run_id = NEW.source_run_id AND conversation_id = NEW.conversation_id
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'chat_scope_revision source_run_id does not belong to conversation_id'
            );
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER trg_chat_scope_revision_architecture_binding_insert
        BEFORE INSERT ON chat_scope_revisions
        WHEN NEW.source_mode = 'architecture_initial' AND NOT EXISTS (
            SELECT 1 FROM chat_session_scope_bindings
            WHERE conversation_id = NEW.conversation_id
              AND scope_mode = 'architecture'
              AND architecture_id = NEW.source_architecture_id
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'chat_scope_revision architecture binding is invalid'
            );
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER trg_chat_scope_revision_immutable
        BEFORE UPDATE ON chat_scope_revisions
        BEGIN
            SELECT RAISE(ABORT, 'chat_scope_revision is immutable');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER trg_chat_scope_member_immutable
        BEFORE UPDATE ON chat_scope_members
        BEGIN
            SELECT RAISE(ABORT, 'chat_scope_member is immutable');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER trg_chat_scope_head_revision_chat_insert
        BEFORE INSERT ON chat_scope_heads
        WHEN NOT EXISTS (
            SELECT 1 FROM chat_scope_revisions
            WHERE scope_revision_id = NEW.scope_revision_id
              AND conversation_id = NEW.conversation_id
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'chat_scope_head revision does not belong to conversation_id'
            );
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER trg_chat_scope_head_revision_chat_update
        BEFORE UPDATE OF conversation_id, scope_revision_id ON chat_scope_heads
        WHEN NOT EXISTS (
            SELECT 1 FROM chat_scope_revisions
            WHERE scope_revision_id = NEW.scope_revision_id
              AND conversation_id = NEW.conversation_id
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'chat_scope_head revision does not belong to conversation_id'
            );
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER trg_chat_run_input_scope_chat_insert
        BEFORE INSERT ON chat_run_inputs
        WHEN NEW.effective_scope_revision_id != '' AND NOT EXISTS (
            SELECT 1
            FROM chat_runs AS run
            JOIN chat_scope_revisions AS revision
              ON revision.scope_revision_id =
                 NEW.effective_scope_revision_id
            WHERE run.run_id = NEW.run_id
              AND run.conversation_id = revision.conversation_id
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'chat_run_input scope does not belong to run conversation_id'
            );
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER trg_chat_run_input_scope_chat_update
        BEFORE UPDATE OF run_id, effective_scope_revision_id
        ON chat_run_inputs
        WHEN NEW.effective_scope_revision_id != '' AND NOT EXISTS (
            SELECT 1
            FROM chat_runs AS run
            JOIN chat_scope_revisions AS revision
              ON revision.scope_revision_id =
                 NEW.effective_scope_revision_id
            WHERE run.run_id = NEW.run_id
              AND run.conversation_id = revision.conversation_id
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'chat_run_input scope does not belong to run conversation_id'
            );
        END
        """
    )

    for action in ("INSERT", "UPDATE"):
        update_clause = (
            " OF files_json, requested_files_json, "
            "effective_scope_revision_id, selection_mode, "
            "requested_architecture_id"
            if action == "UPDATE"
            else ""
        )
        connection.execute(
            f"""
            CREATE TRIGGER trg_chat_run_input_scope_mode_{action.lower()}
            BEFORE {action}{update_clause} ON chat_run_inputs
            WHEN NOT (
                (
                    NEW.effective_scope_revision_id = ''
                    AND NEW.selection_mode = 'legacy_input'
                    AND NEW.requested_architecture_id IS NULL
                )
                OR
                (
                    NEW.effective_scope_revision_id != ''
                    AND NEW.selection_mode IN (
                        'automatic_initial', 'explicit', 'active_scope_reuse'
                    )
                    AND NEW.files_json = '[]'
                    AND NEW.requested_architecture_id IS NULL
                )
                OR
                (
                    NEW.effective_scope_revision_id != ''
                    AND NEW.selection_mode IN (
                        'architecture_initial', 'architecture_reuse'
                    )
                    AND NEW.files_json = '[]'
                    AND NEW.requested_files_json = '[]'
                    AND NEW.requested_architecture_id
                        BETWEEN 1 AND 9223372036854775807
                    AND EXISTS (
                        SELECT 1
                        FROM chat_runs AS run
                        JOIN chat_session_scope_bindings AS binding
                          ON binding.conversation_id = run.conversation_id
                        JOIN chat_scope_revisions AS revision
                          ON revision.scope_revision_id =
                             NEW.effective_scope_revision_id
                         AND revision.conversation_id = run.conversation_id
                        WHERE run.run_id = NEW.run_id
                          AND binding.scope_mode = 'architecture'
                          AND binding.architecture_id =
                              NEW.requested_architecture_id
                          AND revision.source_mode = 'architecture_initial'
                          AND revision.source_architecture_id =
                              NEW.requested_architecture_id
                    )
                )
            )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'chat_run_input selector or scope mode is invalid'
                );
            END
            """
        )

    connection.execute(
        """
        CREATE TRIGGER trg_chat_message_architecture_insert
        BEFORE INSERT ON chat_messages
        WHEN
            (
                NEW.role = 'assistant'
                AND NEW.architecture_id IS NOT NULL
            )
            OR
            (
                NEW.role = 'user'
                AND EXISTS (
                    SELECT 1 FROM chat_session_scope_bindings
                    WHERE conversation_id = NEW.conversation_id
                      AND scope_mode = 'architecture'
                )
                AND NOT EXISTS (
                    SELECT 1 FROM chat_session_scope_bindings
                    WHERE conversation_id = NEW.conversation_id
                      AND scope_mode = 'architecture'
                      AND architecture_id = NEW.architecture_id
                )
            )
            OR
            (
                NEW.architecture_id IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM chat_session_scope_bindings
                    WHERE conversation_id = NEW.conversation_id
                      AND scope_mode = 'architecture'
                      AND architecture_id = NEW.architecture_id
                )
            )
        BEGIN
            SELECT RAISE(ABORT, 'chat_message architecture binding is invalid');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER trg_chat_message_architecture_immutable
        BEFORE UPDATE OF architecture_id ON chat_messages
        WHEN NEW.architecture_id IS NOT OLD.architecture_id
        BEGIN
            SELECT RAISE(ABORT, 'chat_message architecture_id is immutable');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER trg_chat_message_files_scope_insert
        BEFORE INSERT ON chat_message_files
        WHEN NOT EXISTS (
            SELECT 1
            FROM chat_messages AS message
            LEFT JOIN chat_session_scope_bindings AS binding
              ON binding.conversation_id = message.conversation_id
            WHERE message.message_id = NEW.message_id
              AND message.role = 'user'
              AND message.architecture_id IS NULL
              AND (
                  binding.conversation_id IS NULL
                  OR binding.scope_mode = 'files'
              )
        )
        BEGIN
            SELECT RAISE(ABORT, 'chat_message_files scope mode is invalid');
        END
        """
    )

    after_counts = {
        table: int(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )
        for table in before_counts
    }
    if after_counts != before_counts:
        raise sqlite3.IntegrityError(
            "chat scope v5 migration row count mismatch"
        )
    binding_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM chat_session_scope_bindings"
        ).fetchone()[0]
    )
    session_count = int(
        connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    )
    if binding_count != session_count:
        raise sqlite3.IntegrityError(
            "chat scope v5 migration binding count mismatch"
        )
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise sqlite3.IntegrityError(
            "chat scope v5 migration foreign_key_check failed"
        )


def _add_chat_admission_guards_and_scope_binding_constraints(
    connection: sqlite3.Connection,
) -> None:
    """迁移到 Schema v6：增加准入 Guard，并补齐 Binding/Scope 对称约束。

    Guard 是正式 Session/run 之前的短期协调事实，因此不能外键依赖尚不存在的
    ``conversations``。它只按 token 条件消费，并由过期清理兜底。迁移同时拒绝历史
    architecture Chat 中超过 JavaScript 安全整数的值，避免新合同上线后继续从 history
    输出无法被浏览器精确解析的数字。
    """

    invalid_id_queries = (
        (
            "chat_session_scope_bindings",
            """
            SELECT COUNT(*) FROM chat_session_scope_bindings
            WHERE architecture_id > ?
            """,
        ),
        (
            "chat_scope_revisions",
            """
            SELECT COUNT(*) FROM chat_scope_revisions
            WHERE source_architecture_id > ?
            """,
        ),
        (
            "chat_run_inputs",
            """
            SELECT COUNT(*) FROM chat_run_inputs
            WHERE requested_architecture_id > ?
            """,
        ),
        (
            "chat_messages",
            """
            SELECT COUNT(*) FROM chat_messages
            WHERE architecture_id > ?
            """,
        ),
    )
    for table_name, query in invalid_id_queries:
        invalid_count = int(
            connection.execute(
                query,
                (MAX_CHAT_ARCHITECTURE_ID,),
            ).fetchone()[0]
        )
        if invalid_count:
            raise sqlite3.IntegrityError(
                f"{table_name} contains architecture_id outside Chat safe range"
            )

    invalid_scope_binding_count = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM chat_scope_revisions AS revision
            LEFT JOIN chat_session_scope_bindings AS binding
              ON binding.conversation_id = revision.conversation_id
            WHERE binding.conversation_id IS NULL
               OR (
                    revision.source_mode = 'architecture_initial'
                    AND (
                        binding.scope_mode != 'architecture'
                        OR binding.architecture_id
                           != revision.source_architecture_id
                    )
               )
               OR (
                    revision.source_mode IN ('automatic_initial', 'explicit')
                    AND binding.scope_mode != 'files'
               )
            """
        ).fetchone()[0]
    )
    if invalid_scope_binding_count:
        raise sqlite3.IntegrityError(
            "chat scope revisions do not match immutable session bindings"
        )

    connection.execute(
        f"""
        CREATE TABLE chat_admission_guards (
            conversation_id TEXT PRIMARY KEY,
            admission_token TEXT NOT NULL UNIQUE,
            owner_instance_id TEXT NOT NULL,
            scope_mode TEXT NOT NULL CHECK (
                scope_mode IN ('files', 'architecture')
            ),
            architecture_id INTEGER,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            CHECK (
                (scope_mode = 'files' AND architecture_id IS NULL)
                OR
                (
                    scope_mode = 'architecture'
                    AND typeof(architecture_id) = 'integer'
                    AND architecture_id
                        BETWEEN 1 AND {MAX_CHAT_ARCHITECTURE_ID}
                )
            )
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_chat_admission_guards_expires
        ON chat_admission_guards(expires_at, conversation_id)
        """
    )

    connection.execute(
        "DROP TRIGGER IF EXISTS trg_chat_scope_revision_architecture_binding_insert"
    )
    connection.execute(
        """
        CREATE TRIGGER trg_chat_scope_revision_binding_insert
        BEFORE INSERT ON chat_scope_revisions
        WHEN NOT EXISTS (
            SELECT 1
            FROM chat_session_scope_bindings AS binding
            WHERE binding.conversation_id = NEW.conversation_id
              AND (
                  (
                      NEW.source_mode = 'architecture_initial'
                      AND binding.scope_mode = 'architecture'
                      AND binding.architecture_id =
                          NEW.source_architecture_id
                  )
                  OR
                  (
                      NEW.source_mode IN ('automatic_initial', 'explicit')
                      AND binding.scope_mode = 'files'
                      AND NEW.source_architecture_id IS NULL
                  )
              )
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'chat_scope_revision does not match immutable session binding'
            );
        END
        """
    )

    # v5 表级 CHECK 仍兼容 64 位整数；v6 用触发器收紧 Chat 专属范围，避免重建四张
    # 已经互相关联的权威表。现有数据已在本迁移开头完成对账。
    for trigger_name, table_name, column_name in (
        (
            "trg_chat_binding_safe_architecture_insert",
            "chat_session_scope_bindings",
            "architecture_id",
        ),
        (
            "trg_chat_revision_safe_architecture_insert",
            "chat_scope_revisions",
            "source_architecture_id",
        ),
        (
            "trg_chat_run_input_safe_architecture_insert",
            "chat_run_inputs",
            "requested_architecture_id",
        ),
        (
            "trg_chat_message_safe_architecture_insert",
            "chat_messages",
            "architecture_id",
        ),
    ):
        connection.execute(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE INSERT ON {table_name}
            WHEN NEW.{column_name} > {MAX_CHAT_ARCHITECTURE_ID}
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'Chat architecture_id exceeds JavaScript safe integer'
                );
            END
            """
        )
    connection.execute(
        f"""
        CREATE TRIGGER trg_chat_run_input_safe_architecture_update
        BEFORE UPDATE OF requested_architecture_id ON chat_run_inputs
        WHEN NEW.requested_architecture_id > {MAX_CHAT_ARCHITECTURE_ID}
        BEGIN
            SELECT RAISE(
                ABORT,
                'Chat architecture_id exceeds JavaScript safe integer'
            );
        END
        """
    )


def _add_conversation_identity_and_source_chunk_schema(
    connection: sqlite3.Connection,
) -> None:
    """建立当前世代的公开身份绑定、准入 Guard、删除审计和 Chunk 快照。"""

    connection.execute(
        f"""
        CREATE TABLE conversation_identities (
            conversation_id TEXT PRIMARY KEY,
            identity_kind TEXT NOT NULL CHECK (
                identity_kind IN ('file', 'weaponry')
            ),
            chat_id INTEGER,
            user_id INTEGER,
            architecture_id INTEGER,
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            created_at TEXT NOT NULL,
            released_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id),
            CHECK (
                (
                    identity_kind = 'file'
                    AND typeof(chat_id) = 'integer'
                    AND chat_id >= 1
                    AND user_id IS NULL
                    AND architecture_id IS NULL
                    AND active = 1
                    AND released_at = ''
                )
                OR
                (
                    identity_kind = 'weaponry'
                    AND chat_id IS NULL
                    AND typeof(user_id) = 'integer'
                    AND user_id BETWEEN 1 AND {MAX_CHAT_ARCHITECTURE_ID}
                    AND typeof(architecture_id) = 'integer'
                    AND architecture_id BETWEEN 1 AND {MAX_CHAT_ARCHITECTURE_ID}
                    AND (
                        (active = 1 AND released_at = '')
                        OR (active = 0 AND length(trim(released_at)) > 0)
                    )
                )
            )
        )
        """
    )
    # 文件 chatId 是全世代唯一墓碑；删除后 identity 行不会释放或删除。
    connection.execute(
        """
        CREATE UNIQUE INDEX uq_conversation_identity_file_chat
        ON conversation_identities(chat_id)
        WHERE identity_kind = 'file'
        """
    )
    # Weaponry 只限制活动世代。删除终态事务释放旧行后，复合身份才能创建新会话。
    connection.execute(
        """
        CREATE UNIQUE INDEX uq_conversation_identity_active_weaponry
        ON conversation_identities(user_id, architecture_id)
        WHERE identity_kind = 'weaponry' AND active = 1
        """
    )
    connection.execute(
        f"""
        CREATE TABLE conversation_admissions (
            identity_key TEXT PRIMARY KEY,
            identity_kind TEXT NOT NULL CHECK (
                identity_kind IN ('file', 'weaponry')
            ),
            chat_id INTEGER,
            user_id INTEGER,
            architecture_id INTEGER,
            admission_token TEXT NOT NULL UNIQUE,
            owner_instance_id TEXT NOT NULL,
            scope_mode TEXT NOT NULL CHECK (scope_mode IN ('files', 'architecture')),
            requested_scope_architecture_id INTEGER,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            CHECK (
                (
                    identity_kind = 'file'
                    AND typeof(chat_id) = 'integer'
                    AND chat_id >= 1
                    AND user_id IS NULL
                    AND architecture_id IS NULL
                )
                OR
                (
                    identity_kind = 'weaponry'
                    AND chat_id IS NULL
                    AND typeof(user_id) = 'integer'
                    AND user_id BETWEEN 1 AND {MAX_CHAT_ARCHITECTURE_ID}
                    AND typeof(architecture_id) = 'integer'
                    AND architecture_id BETWEEN 1 AND {MAX_CHAT_ARCHITECTURE_ID}
                )
            ),
            CHECK (
                (scope_mode = 'files' AND requested_scope_architecture_id IS NULL)
                OR
                (
                    scope_mode = 'architecture'
                    AND typeof(requested_scope_architecture_id) = 'integer'
                    AND requested_scope_architecture_id
                        BETWEEN 1 AND {MAX_CHAT_ARCHITECTURE_ID}
                )
            )
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_conversation_admissions_expiry
        ON conversation_admissions(expires_at, identity_key)
        """
    )
    connection.execute(
        """
        CREATE TABLE message_source_chunks (
            message_id TEXT NOT NULL,
            position INTEGER NOT NULL CHECK (position >= 0),
            content TEXT NOT NULL CHECK (typeof(content) = 'text'),
            file_name TEXT NOT NULL CHECK (length(trim(file_name)) > 0),
            original_file_name TEXT NOT NULL CHECK (
                length(trim(original_file_name)) > 0
            ),
            created_at TEXT NOT NULL,
            PRIMARY KEY(message_id, position),
            FOREIGN KEY(message_id) REFERENCES chat_messages(message_id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE conversation_deletion_audits (
            conversation_id TEXT PRIMARY KEY,
            identity_kind TEXT NOT NULL CHECK (
                identity_kind IN ('file', 'weaponry')
            ),
            deletion_status TEXT NOT NULL CHECK (
                deletion_status = 'deleted'
            ),
            cleanup_result TEXT NOT NULL CHECK (
                cleanup_result = 'succeeded'
            ),
            deleted_at TEXT NOT NULL,
            FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id)
        )
        """
    )


def _add_structured_source_keys(connection: sqlite3.Connection) -> None:
    """把 AnythingLLM 结构化来源键冻结到范围与绑定回执。

    空字符串只用于既有文件对话：文件对话不向公开响应暴露来源。知识谱系范围必须在
    Resolver/Application 层逐项提供非空键；数据库唯一索引用于阻止并发或实现缺陷把
    同一来源键错误绑定给同一会话内的多个文件。
    """
    connection.execute(
        """
        ALTER TABLE chat_scope_members
        ADD COLUMN structured_source_key TEXT NOT NULL DEFAULT ''
        """
    )
    connection.execute(
        """
        ALTER TABLE chat_document_bindings
        ADD COLUMN structured_source_key TEXT NOT NULL DEFAULT ''
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX uq_chat_scope_members_structured_source_key
        ON chat_scope_members(scope_revision_id, structured_source_key)
        WHERE structured_source_key <> ''
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX uq_chat_document_bindings_structured_source_key
        ON chat_document_bindings(conversation_id, structured_source_key)
        WHERE structured_source_key <> ''
        """
    )


def _make_deletion_audits_independent(connection: sqlite3.Connection) -> None:
    """把删除审计从在线 Conversation 聚合中解耦。

    Weaponry 删除成功后只允许保留不含正文的最小审计事实，不能为了维持外键而继续
    保存会话、运行、范围或供应商资源引用。审计行使用随机内部 Conversation ID 作为
    关联键，但它本身不再依赖已被物理清除的在线聚合。

    SQLite 不能直接删除既有外键，因此使用同事务建表、复制、替换。迁移期间任一语句
    失败都会整体回滚，避免出现审计表缺失或只复制部分数据的状态。
    """

    connection.execute(
        """
        CREATE TABLE conversation_deletion_audits_v9 (
            conversation_id TEXT PRIMARY KEY,
            identity_kind TEXT NOT NULL CHECK (
                identity_kind IN ('file', 'weaponry')
            ),
            deletion_status TEXT NOT NULL CHECK (
                deletion_status = 'deleted'
            ),
            cleanup_result TEXT NOT NULL CHECK (
                cleanup_result = 'succeeded'
            ),
            deleted_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO conversation_deletion_audits_v9(
            conversation_id, identity_kind, deletion_status,
            cleanup_result, deleted_at
        )
        SELECT conversation_id, identity_kind, deletion_status,
               cleanup_result, deleted_at
        FROM conversation_deletion_audits
        """
    )
    connection.execute("DROP TABLE conversation_deletion_audits")
    connection.execute(
        "ALTER TABLE conversation_deletion_audits_v9 "
        "RENAME TO conversation_deletion_audits"
    )


_CHAT_SCHEMA_MIGRATIONS = (
    (1, _create_chat_authority_schema),
    (2, _add_chat_constraints_and_indexes),
    (3, _add_integrity_triggers_and_refine_cleanup_job_identity),
    (4, _add_chat_scope_revisions),
    (5, _add_architecture_chat_scope),
    (6, _add_chat_admission_guards_and_scope_binding_constraints),
    (7, _add_conversation_identity_and_source_chunk_schema),
    (8, _add_structured_source_keys),
    (9, _make_deletion_audits_independent),
)


class _Repository:
    def __init__(self, db_path: str, *, initialize: bool = True) -> None:
        self.db_path = _required_text(db_path, name="db_path")
        if initialize:
            ensure_chat_schema(self.db_path)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with _connection_scope(self.db_path) as connection:
            yield connection


class ChatSessionRepository(_Repository):
    """`conversations` 表的仓储。"""

    def create_or_get(
        self,
        *,
        conversation_id: str,
        workspace_ref: str = "",
        thread_ref: str = "",
        status: str = SESSION_ACTIVE,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ChatSession:
        normalized_conversation_id = _required_text(conversation_id, name="conversation_id")
        normalized_status = _validate_choice(
            status,
            name="status",
            allowed=SESSION_STATUSES,
        )
        normalized_workspace = _optional_text(workspace_ref)
        normalized_thread = _optional_text(thread_ref)
        metadata_json = _json_dumps(metadata or {})
        now = _utc_now_iso()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id = ?",
                (normalized_conversation_id,),
            ).fetchone()
            if existing is not None:
                self._reject_ref_conflict(
                    existing,
                    workspace_ref=normalized_workspace,
                    thread_ref=normalized_thread,
                )
                if existing["status"] != normalized_status:
                    raise ValueError("chat_session status 冲突")
                if existing["status"] != SESSION_ACTIVE and (
                    (normalized_workspace and normalized_workspace != existing["workspace_ref"])
                    or (normalized_thread and normalized_thread != existing["thread_ref"])
                ):
                    raise ValueError(
                        "chat_session remote references can only change while active"
                    )
                existing_metadata = _json_loads_object(existing["metadata_json"])
                merged_metadata = dict(existing_metadata)
                for key, value in (metadata or {}).items():
                    if key in merged_metadata and merged_metadata[key] != value:
                        raise ValueError("chat_session metadata 冲突")
                    merged_metadata[key] = value
                resolved_workspace = existing["workspace_ref"] or normalized_workspace
                resolved_thread = existing["thread_ref"] or normalized_thread
                if (
                    resolved_workspace != existing["workspace_ref"]
                    or resolved_thread != existing["thread_ref"]
                    or merged_metadata != existing_metadata
                ):
                    logger.info(
                        "文件对话会话远端引用已更新: conversation_id=%s has_workspace_ref=%s "
                        "has_thread_ref=%s",
                        normalized_conversation_id,
                        bool(resolved_workspace),
                        bool(resolved_thread),
                    )
                    connection.execute(
                        """
                        UPDATE conversations
                        SET workspace_ref = ?, thread_ref = ?, metadata_json = ?,
                            updated_at = ?
                        WHERE conversation_id = ?
                        """,
                        (
                            resolved_workspace,
                            resolved_thread,
                            _json_dumps(merged_metadata),
                            now,
                            normalized_conversation_id,
                        ),
                    )
                    existing = connection.execute(
                        "SELECT * FROM conversations WHERE conversation_id = ?",
                        (normalized_conversation_id,),
                    ).fetchone()
                logger.debug(
                    "复用文件对话会话: conversation_id=%s status=%s",
                    normalized_conversation_id,
                    existing["status"],
                )
                return self._row(existing)
            connection.execute(
                """
                INSERT INTO conversations (
                    conversation_id, workspace_ref, thread_ref, status,
                    created_at, updated_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_conversation_id,
                    normalized_workspace,
                    normalized_thread,
                    normalized_status,
                    now,
                    now,
                    metadata_json,
                ),
            )
            row = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id = ?",
                (normalized_conversation_id,),
            ).fetchone()
            logger.info(
                "文件对话会话已创建: conversation_id=%s status=%s has_workspace_ref=%s "
                "has_thread_ref=%s",
                normalized_conversation_id,
                normalized_status,
                bool(normalized_workspace),
                bool(normalized_thread),
            )
            return self._row(row)

    def get(self, conversation_id: str) -> ChatSession | None:
        normalized_conversation_id = _required_text(conversation_id, name="conversation_id")
        with _connection_scope(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id = ?",
                (normalized_conversation_id,),
            ).fetchone()
        return self._row(row) if row is not None else None

    def list_all(self) -> tuple[ChatSession, ...]:
        with _connection_scope(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM conversations
                ORDER BY updated_at DESC, conversation_id ASC
                """
            ).fetchall()
        return tuple(self._row(row) for row in rows)

    def update_refs(
        self,
        *,
        conversation_id: str,
        workspace_ref: str | None = None,
        thread_ref: str | None = None,
    ) -> ChatSession:
        normalized_conversation_id = _required_text(conversation_id, name="conversation_id")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id = ?",
                (normalized_conversation_id,),
            ).fetchone()
            if row is None:
                raise ValueError("chat_session 不存在")
            if row["status"] != SESSION_ACTIVE:
                raise ValueError(
                    "chat_session remote references can only change while active"
                )
            connection.execute(
                """
                UPDATE conversations
                SET workspace_ref = ?, thread_ref = ?, updated_at = ?
                WHERE conversation_id = ?
                """,
                (
                    _optional_text(workspace_ref)
                    if workspace_ref is not None
                    else row["workspace_ref"],
                    _optional_text(thread_ref)
                    if thread_ref is not None
                    else row["thread_ref"],
                    _utc_now_iso(),
                    normalized_conversation_id,
                ),
            )
            logger.info(
                "文件对话会话远端引用已更新: conversation_id=%s has_workspace_ref=%s "
                "has_thread_ref=%s",
                normalized_conversation_id,
                bool(
                    _optional_text(workspace_ref)
                    if workspace_ref is not None
                    else row["workspace_ref"]
                ),
                bool(
                    _optional_text(thread_ref)
                    if thread_ref is not None
                    else row["thread_ref"]
                ),
            )
            return self._row(
                connection.execute(
                    "SELECT * FROM conversations WHERE conversation_id = ?",
                    (normalized_conversation_id,),
                ).fetchone()
            )

    def set_status(self, *, conversation_id: str, status: str) -> ChatSession:
        normalized_conversation_id = _required_text(conversation_id, name="conversation_id")
        normalized_status = _validate_choice(
            status,
            name="status",
            allowed=SESSION_STATUSES,
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id = ?",
                (normalized_conversation_id,),
            ).fetchone()
            if current is None:
                raise ValueError("chat_session does not exist")
            current_status = current["status"]
            if current_status == normalized_status:
                return self._row(current)
            if normalized_status not in _SESSION_STATUS_TRANSITIONS[current_status]:
                raise ValueError(
                    "illegal chat_session status transition: "
                    f"{current_status} -> {normalized_status}"
                )
            connection.execute(
                """
                UPDATE conversations
                SET status = ?, updated_at = ?
                WHERE conversation_id = ? AND status = ?
                """,
                (
                    normalized_status,
                    _utc_now_iso(),
                    normalized_conversation_id,
                    current_status,
                ),
            )
            logger.info(
                "更新文件对话会话状态: conversation_id=%s status=%s",
                normalized_conversation_id,
                normalized_status,
            )
            row = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id = ?",
                (normalized_conversation_id,),
            ).fetchone()
        if row is None:
            raise ValueError("chat_session 不存在")
        return self._row(row)

    @staticmethod
    def _reject_ref_conflict(
        row: sqlite3.Row,
        *,
        workspace_ref: str,
        thread_ref: str,
    ) -> None:
        if workspace_ref and row["workspace_ref"] and workspace_ref != row["workspace_ref"]:
            raise ValueError("chat_session workspace_ref 冲突")
        if thread_ref and row["thread_ref"] and thread_ref != row["thread_ref"]:
            raise ValueError("chat_session thread_ref 冲突")

    @staticmethod
    def _row(row: sqlite3.Row) -> ChatSession:
        return ChatSession(
            conversation_id=row["conversation_id"],
            workspace_ref=row["workspace_ref"],
            thread_ref=row["thread_ref"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=_json_loads_object(row["metadata_json"]),
        )


class ChatSessionScopeBindingRepository(_Repository):
    """会话范围模式的不可变权威 Repository。"""

    def get(self, conversation_id: str) -> ChatSessionScopeBinding | None:
        normalized_conversation_id = _required_text(conversation_id, name="conversation_id")
        with self._connection() as connection:
            return self.get_in_transaction(
                connection,
                conversation_id=normalized_conversation_id,
            )

    def create(self, binding: ChatSessionScopeBinding) -> ChatSessionScopeBinding:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self.create_in_transaction(connection, binding=binding)

    @staticmethod
    def create_in_transaction(
        connection: sqlite3.Connection,
        *,
        binding: ChatSessionScopeBinding,
    ) -> ChatSessionScopeBinding:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be sqlite3.Connection")
        if not isinstance(binding, ChatSessionScopeBinding):
            raise TypeError("binding must be ChatSessionScopeBinding")
        connection.execute(
            """
            INSERT INTO chat_session_scope_bindings (
                conversation_id, scope_mode, architecture_id, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                binding.conversation_id,
                binding.scope_mode,
                binding.architecture_id,
                binding.created_at,
            ),
        )
        logger.info(
            "文件对话会话范围绑定已创建: conversation_id=%s scope_mode=%s "
            "architecture_id=%s",
            binding.conversation_id,
            binding.scope_mode,
            binding.architecture_id,
        )
        return binding

    @staticmethod
    def get_in_transaction(
        connection: sqlite3.Connection,
        *,
        conversation_id: str,
    ) -> ChatSessionScopeBinding | None:
        normalized_conversation_id = _required_text(conversation_id, name="conversation_id")
        row = connection.execute(
            """
            SELECT conversation_id, scope_mode, architecture_id, created_at
            FROM chat_session_scope_bindings
            WHERE conversation_id = ?
            """,
            (normalized_conversation_id,),
        ).fetchone()
        if row is None:
            return None
        return ChatSessionScopeBinding(
            conversation_id=row["conversation_id"],
            scope_mode=_validate_choice(
                row["scope_mode"],
                name="scope_mode",
                allowed=CHAT_SCOPE_MODES,
            ),
            architecture_id=_optional_architecture_id(
                row["architecture_id"],
                name="architecture_id",
            ),
            created_at=_required_text(row["created_at"], name="created_at"),
        )

    @staticmethod
    def require_for_chat_in_transaction(
        connection: sqlite3.Connection,
        *,
        conversation_id: str,
    ) -> ChatSessionScopeBinding:
        binding = ChatSessionScopeBindingRepository.get_in_transaction(
            connection,
            conversation_id=conversation_id,
        )
        if binding is None:
            raise ValueError("chat session is missing immutable scope binding")
        return binding


class ChatDocumentBindingRepository(_Repository):
    """持久化不可变文档版本及当前版本投影。

    业务文件名在同一个对话中刻意不唯一：上传替换文档会生成不同的 ``document_ref``。
    历史绑定保持不可变，以供审计和清理；头表决定后续轮次默认选择哪个版本。
    """

    def add(
        self,
        *,
        conversation_id: str,
        file_name: str,
        original_name: str,
        document_ref: str,
        external_location: str = "",
        structured_source_key: str = "",
        added_by_run_id: str = "",
    ) -> ChatDocumentBinding:
        normalized_conversation_id = _required_text(conversation_id, name="conversation_id")
        normalized_file_name = _required_text(file_name, name="file_name")
        normalized_original_name = _required_text(
            original_name,
            name="original_name",
        )
        normalized_document_ref = _required_text(
            document_ref,
            name="document_ref",
        )
        normalized_location = _optional_text(external_location)
        normalized_source_key = _optional_canonical_text(
            structured_source_key,
            name="structured_source_key",
        )
        normalized_added_by_run_id = _optional_text(added_by_run_id)
        now = _utc_now_iso()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if normalized_added_by_run_id:
                run_row = connection.execute(
                    "SELECT conversation_id FROM chat_runs WHERE run_id = ?",
                    (normalized_added_by_run_id,),
                ).fetchone()
                if run_row is None:
                    raise ValueError("added_by_run_id 对应的 chat_run 不存在")
                if run_row["conversation_id"] != normalized_conversation_id:
                    raise ValueError(
                        "chat_document_binding run_id 不属于当前 conversation_id"
                    )
            existing = connection.execute(
                """
                SELECT * FROM chat_document_bindings
                WHERE conversation_id = ? AND file_name = ? AND document_ref = ?
                """,
                (
                    normalized_conversation_id,
                    normalized_file_name,
                    normalized_document_ref,
                ),
            ).fetchone()
            if existing is None:
                binding_id = uuid.uuid4().hex
                connection.execute(
                    """
                    INSERT INTO chat_document_bindings (
                        binding_id, conversation_id, file_name, original_name,
                        document_ref, external_location, structured_source_key,
                        added_by_run_id,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        binding_id,
                        normalized_conversation_id,
                        normalized_file_name,
                        normalized_original_name,
                        normalized_document_ref,
                        normalized_location,
                        normalized_source_key,
                        normalized_added_by_run_id,
                        now,
                    ),
                )
                binding = self._get_by_id_with_connection(
                    connection,
                    binding_id=binding_id,
                )
            else:
                binding = self._row(existing)
                self._reject_identity_conflict(
                    binding,
                    original_name=normalized_original_name,
                    external_location=normalized_location,
                    structured_source_key=normalized_source_key,
                )
            connection.execute(
                """
                INSERT INTO chat_document_heads (
                    conversation_id, file_name, binding_id, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(conversation_id, file_name) DO UPDATE SET
                    binding_id = excluded.binding_id,
                    updated_at = excluded.updated_at
                """,
                (
                    normalized_conversation_id,
                    normalized_file_name,
                    binding.binding_id,
                    now,
                ),
            )
            logger.info(
                "文件版本已绑定到本地对话: conversation_id=%s "
                "has_document_ref=%s binding_id=%s",
                normalized_conversation_id,
                bool(normalized_document_ref),
                binding.binding_id,
            )
            return binding

    def list_by_chat(self, conversation_id: str) -> tuple[ChatDocumentBinding, ...]:
        """返回全部历史绑定，供审计和清理使用。"""
        normalized_conversation_id = _required_text(conversation_id, name="conversation_id")
        with _connection_scope(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM chat_document_bindings
                WHERE conversation_id = ?
                ORDER BY created_at ASC, file_name ASC, binding_id ASC
                """,
                (normalized_conversation_id,),
            ).fetchall()
        return tuple(self._row(row) for row in rows)

    def list_current_by_chat(
        self,
        conversation_id: str,
    ) -> tuple[ChatDocumentBinding, ...]:
        """返回每个业务文件当前选中的最新版本。"""
        normalized_conversation_id = _required_text(conversation_id, name="conversation_id")
        with _connection_scope(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT binding.*
                FROM chat_document_heads AS head
                INNER JOIN chat_document_bindings AS binding
                    ON binding.binding_id = head.binding_id
                WHERE head.conversation_id = ?
                ORDER BY head.file_name ASC
                """,
                (normalized_conversation_id,),
            ).fetchall()
        return tuple(self._row(row) for row in rows)

    def _get_by_id_with_connection(
        self,
        connection: sqlite3.Connection,
        *,
        binding_id: str,
    ) -> ChatDocumentBinding:
        row = connection.execute(
            "SELECT * FROM chat_document_bindings WHERE binding_id = ?",
            (binding_id,),
        ).fetchone()
        if row is None:
            raise ValueError("chat_document_binding 不存在")
        return self._row(row)

    @staticmethod
    def _reject_identity_conflict(
        binding: ChatDocumentBinding,
        *,
        original_name: str,
        external_location: str,
        structured_source_key: str,
    ) -> None:
        if (
            binding.original_name != original_name
            or binding.external_location != external_location
            or binding.structured_source_key != structured_source_key
        ):
            raise ValueError(
                "chat_document_binding identity conflicts with an existing revision"
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> ChatDocumentBinding:
        return ChatDocumentBinding(
            binding_id=row["binding_id"],
            conversation_id=row["conversation_id"],
            file_name=row["file_name"],
            original_name=row["original_name"],
            document_ref=row["document_ref"],
            external_location=row["external_location"],
            structured_source_key=row["structured_source_key"],
            added_by_run_id=row["added_by_run_id"],
            created_at=row["created_at"],
        )


class ChatRunRepository(_Repository):
    """`chat_runs` 表的仓储。"""

    def create(
        self,
        *,
        run_id: str,
        conversation_id: str,
        status: str = RUN_ACCEPTED,
        owner_instance_id: str = "",
    ) -> ChatRun:
        normalized_run_id = _required_text(run_id, name="run_id")
        normalized_conversation_id = _required_text(conversation_id, name="conversation_id")
        normalized_status = _validate_choice(
            status,
            name="status",
            allowed=RUN_STATUSES,
        )
        if normalized_status != RUN_ACCEPTED:
            raise ValueError("chat_run 必须以 accepted 状态创建")
        now = _utc_now_iso()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM chat_runs WHERE run_id = ?",
                (normalized_run_id,),
            ).fetchone()
            if existing is not None:
                self._reject_identity_conflict(
                    existing,
                    conversation_id=normalized_conversation_id,
                    owner_instance_id=_optional_text(owner_instance_id),
                )
                logger.debug(
                    "复用已存在文件对话运行记录: conversation_id=%s run_id=%s status=%s",
                    normalized_conversation_id,
                    normalized_run_id,
                    existing["status"],
                )
                return self._row(existing)
            connection.execute(
                """
                INSERT INTO chat_runs (
                    run_id, conversation_id, status, owner_instance_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_run_id,
                    normalized_conversation_id,
                    normalized_status,
                    _optional_text(owner_instance_id),
                    now,
                    now,
                ),
            )
            logger.info(
                "文件对话运行记录已创建: conversation_id=%s run_id=%s "
                "has_owner_instance=%s status=%s",
                normalized_conversation_id,
                normalized_run_id,
                bool(_optional_text(owner_instance_id)),
                normalized_status,
            )
            return self._get_with_connection(connection, normalized_run_id)

    def get(self, run_id: str) -> ChatRun | None:
        normalized_run_id = _required_text(run_id, name="run_id")
        with _connection_scope(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM chat_runs WHERE run_id = ?",
                (normalized_run_id,),
            ).fetchone()
        return self._row(row) if row is not None else None

    def update_status(
        self,
        *,
        run_id: str,
        status: str,
        error_message: str = "",
    ) -> ChatRun:
        normalized_run_id = _required_text(run_id, name="run_id")
        normalized_status = _validate_choice(
            status,
            name="status",
            allowed=RUN_STATUSES,
        )
        now = _utc_now_iso()
        terminal = normalized_status in RUN_TERMINAL_STATUSES
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._get_with_connection(connection, normalized_run_id)
            self._ensure_transition(
                current_status=current.status,
                next_status=normalized_status,
            )
            if current.status == normalized_status:
                logger.debug(
                    "文件对话运行状态无需变更: conversation_id=%s run_id=%s status=%s",
                    current.conversation_id,
                    normalized_run_id,
                    normalized_status,
                )
                return current
            cursor = connection.execute(
                """
                UPDATE chat_runs
                SET status = ?,
                    error_message = ?,
                    started_at = CASE
                        WHEN ? = ? AND started_at IS NULL THEN ?
                        ELSE started_at
                    END,
                    completed_at = CASE
                        WHEN ? THEN ?
                        ELSE completed_at
                    END,
                    updated_at = ?
                WHERE run_id = ? AND status = ?
                """,
                (
                    normalized_status,
                    _optional_text(error_message),
                    normalized_status,
                    RUN_RUNNING,
                    now,
                    1 if terminal else 0,
                    now,
                    now,
                    normalized_run_id,
                    current.status,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("chat_run status was changed concurrently")
            logger.info(
                "文件对话运行状态已迁移: conversation_id=%s run_id=%s previous_status=%s "
                "target_status=%s terminal=%s error_chars=%d",
                current.conversation_id,
                normalized_run_id,
                current.status,
                normalized_status,
                terminal,
                len(_optional_text(error_message)),
            )
            return self._get_with_connection(connection, normalized_run_id)

    def mark_running(self, run_id: str) -> ChatRun:
        return self.update_status(run_id=run_id, status=RUN_RUNNING)

    def mark_succeeded(self, run_id: str) -> ChatRun:
        return self.update_status(run_id=run_id, status=RUN_SUCCEEDED)

    def mark_failed(self, run_id: str, *, error_message: str) -> ChatRun:
        return self.update_status(
            run_id=run_id,
            status=RUN_FAILED,
            error_message=_required_text(error_message, name="error_message"),
        )

    def mark_aborted(self, run_id: str, *, error_message: str = "") -> ChatRun:
        return self.update_status(
            run_id=run_id,
            status=RUN_ABORTED,
            error_message=_optional_text(error_message),
        )

    def request_abort(self, run_id: str) -> ChatRun:
        normalized_run_id = _required_text(run_id, name="run_id")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._get_with_connection(connection, normalized_run_id)
            if current.status not in RUN_ACTIVE_STATUSES:
                logger.info(
                    "拒绝为非活跃文件对话运行设置中断标记: "
                    "conversation_id=%s run_id=%s status=%s",
                    current.conversation_id,
                    normalized_run_id,
                    current.status,
                )
                raise ValueError("cannot request abort for inactive chat_run")
            cursor = connection.execute(
                """
                UPDATE chat_runs
                SET abort_requested = 1, updated_at = ?
                WHERE run_id = ? AND status IN (?, ?)
                """,
                (
                    _utc_now_iso(),
                    normalized_run_id,
                    *tuple(sorted(RUN_ACTIVE_STATUSES)),
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("chat_run status was changed concurrently")
            logger.info(
                "文件对话运行中断标记已持久化: conversation_id=%s run_id=%s previous_status=%s",
                current.conversation_id,
                normalized_run_id,
                current.status,
            )
            return self._get_with_connection(connection, normalized_run_id)

    def list_active(self, conversation_id: str) -> tuple[ChatRun, ...]:
        normalized_conversation_id = _required_text(conversation_id, name="conversation_id")
        placeholders = ",".join("?" for _ in RUN_ACTIVE_STATUSES)
        with _connection_scope(self.db_path) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM chat_runs
                WHERE conversation_id = ? AND status IN ({placeholders})
                ORDER BY created_at ASC
                """,
                (normalized_conversation_id, *tuple(sorted(RUN_ACTIVE_STATUSES))),
            ).fetchall()
        return tuple(self._row(row) for row in rows)

    def _get_with_connection(
        self,
        connection: sqlite3.Connection,
        run_id: str,
    ) -> ChatRun:
        row = connection.execute(
            "SELECT * FROM chat_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ValueError("chat_run 不存在")
        return self._row(row)

    @staticmethod
    def _reject_identity_conflict(
        row: sqlite3.Row,
        *,
        conversation_id: str,
        owner_instance_id: str,
    ) -> None:
        if row["conversation_id"] != conversation_id:
            raise ValueError("run_id is already bound to another conversation_id")
        if row["owner_instance_id"] != owner_instance_id:
            raise ValueError("run_id is already bound to another owner_instance_id")

    @staticmethod
    def _ensure_transition(*, current_status: str, next_status: str) -> None:
        if current_status == next_status:
            return
        allowed = _RUN_STATUS_TRANSITIONS[current_status]
        if next_status not in allowed:
            raise ValueError(
                f"illegal chat_run status transition: "
                f"{current_status} -> {next_status}"
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> ChatRun:
        return ChatRun(
            run_id=row["run_id"],
            conversation_id=row["conversation_id"],
            status=row["status"],
            abort_requested=bool(row["abort_requested"]),
            owner_instance_id=row["owner_instance_id"],
            heartbeat_at=row["heartbeat_at"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            updated_at=row["updated_at"],
        )


def chat_scope_revision_id_for_run(run_id: str) -> str:
    """由内部 run 身份生成稳定 Scope Revision 身份。"""
    return f"{_required_text(run_id, name='run_id')}:scope"


class ChatScopeRepository(_Repository):
    """不可变 Scope Revision、成员和当前 Head 的 SQLite Repository。"""

    def get_head(self, conversation_id: str) -> ChatScopeHead | None:
        normalized_conversation_id = _required_text(conversation_id, name="conversation_id")
        with self._connection() as connection:
            return self.get_head_in_transaction(
                connection,
                conversation_id=normalized_conversation_id,
            )

    def get_revision(
        self,
        scope_revision_id: str,
    ) -> ChatScopeRevision | None:
        normalized_revision_id = _required_text(
            scope_revision_id,
            name="scope_revision_id",
        )
        with self._connection() as connection:
            return self.get_revision_in_transaction(
                connection,
                scope_revision_id=normalized_revision_id,
            )

    def get_current_revision(self, conversation_id: str) -> ChatScopeRevision | None:
        normalized_conversation_id = _required_text(conversation_id, name="conversation_id")
        with self._connection() as connection:
            head = self.get_head_in_transaction(
                connection,
                conversation_id=normalized_conversation_id,
            )
            if head is None:
                return None
            revision = self.get_revision_in_transaction(
                connection,
                scope_revision_id=head.scope_revision_id,
            )
            if revision is None or revision.conversation_id != normalized_conversation_id:
                raise ValueError("chat scope head points to invalid revision")
            return revision

    def list_revisions_by_chat(
        self,
        conversation_id: str,
    ) -> tuple[ChatScopeRevision, ...]:
        normalized_conversation_id = _required_text(conversation_id, name="conversation_id")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT scope_revision_id
                FROM chat_scope_revisions
                WHERE conversation_id = ?
                ORDER BY created_at ASC, scope_revision_id ASC
                """,
                (normalized_conversation_id,),
            ).fetchall()
            revisions: list[ChatScopeRevision] = []
            for row in rows:
                revision = self.get_revision_in_transaction(
                    connection,
                    scope_revision_id=row["scope_revision_id"],
                )
                if revision is None:
                    raise ValueError("chat scope revision disappeared")
                revisions.append(revision)
            return tuple(revisions)

    def append_and_set_head(
        self,
        *,
        revision: ChatScopeRevision,
        expected_current_revision_id: str | None,
    ) -> ChatScopeHead:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self.append_and_set_head_in_transaction(
                connection,
                revision=revision,
                expected_current_revision_id=expected_current_revision_id,
            )

    @staticmethod
    def append_and_set_head_in_transaction(
        connection: sqlite3.Connection,
        *,
        revision: ChatScopeRevision,
        expected_current_revision_id: str | None,
    ) -> ChatScopeHead:
        """在调用方事务内追加 Revision，并以 CAS 方式切换 Head。"""
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be sqlite3.Connection")
        if not isinstance(revision, ChatScopeRevision):
            raise TypeError("revision must be ChatScopeRevision")
        expected_revision_id = (
            None
            if expected_current_revision_id is None
            else _required_text(
                expected_current_revision_id,
                name="expected_current_revision_id",
            )
        )
        connection.execute(
            """
            INSERT INTO chat_scope_revisions (
                scope_revision_id, conversation_id, source_mode,
                source_run_id, source_architecture_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                revision.scope_revision_id,
                revision.conversation_id,
                revision.source_mode,
                revision.source_run_id,
                revision.source_architecture_id,
                revision.created_at,
            ),
        )
        connection.executemany(
            """
            INSERT INTO chat_scope_members (
                scope_revision_id, ordinal, file_name, original_name,
                document_ref, external_location, structured_source_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    revision.scope_revision_id,
                    ordinal,
                    member.file_name,
                    member.original_name,
                    member.document_ref,
                    member.external_location,
                    member.structured_source_key,
                )
                for ordinal, member in enumerate(revision.members)
            ),
        )
        if expected_revision_id is None:
            connection.execute(
                """
                INSERT INTO chat_scope_heads (
                    conversation_id, scope_revision_id, updated_at
                ) VALUES (?, ?, ?)
                """,
                (
                    revision.conversation_id,
                    revision.scope_revision_id,
                    revision.created_at,
                ),
            )
        else:
            updated = connection.execute(
                """
                UPDATE chat_scope_heads
                SET scope_revision_id = ?, updated_at = ?
                WHERE conversation_id = ? AND scope_revision_id = ?
                """,
                (
                    revision.scope_revision_id,
                    revision.created_at,
                    revision.conversation_id,
                    expected_revision_id,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("chat scope head compare-and-set conflict")
        logger.info(
            "文件对话活动范围版本已追加并切换 Head: "
            "conversation_id=%s scope_revision_id=%s source_mode=%s member_count=%d",
            revision.conversation_id,
            revision.scope_revision_id,
            revision.source_mode,
            len(revision.members),
        )
        return ChatScopeHead(
            conversation_id=revision.conversation_id,
            scope_revision_id=revision.scope_revision_id,
            updated_at=revision.created_at,
        )

    @staticmethod
    def get_head_in_transaction(
        connection: sqlite3.Connection,
        *,
        conversation_id: str,
    ) -> ChatScopeHead | None:
        normalized_conversation_id = _required_text(conversation_id, name="conversation_id")
        row = connection.execute(
            "SELECT * FROM chat_scope_heads WHERE conversation_id = ?",
            (normalized_conversation_id,),
        ).fetchone()
        if row is None:
            return None
        return ChatScopeHead(
            conversation_id=row["conversation_id"],
            scope_revision_id=row["scope_revision_id"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def get_revision_in_transaction(
        connection: sqlite3.Connection,
        *,
        scope_revision_id: str,
    ) -> ChatScopeRevision | None:
        normalized_revision_id = _required_text(
            scope_revision_id,
            name="scope_revision_id",
        )
        row = connection.execute(
            """
            SELECT * FROM chat_scope_revisions
            WHERE scope_revision_id = ?
            """,
            (normalized_revision_id,),
        ).fetchone()
        if row is None:
            return None
        member_rows = connection.execute(
            """
            SELECT * FROM chat_scope_members
            WHERE scope_revision_id = ?
            ORDER BY ordinal ASC
            """,
            (normalized_revision_id,),
        ).fetchall()
        return ChatScopeRevision(
            scope_revision_id=row["scope_revision_id"],
            conversation_id=row["conversation_id"],
            source_mode=row["source_mode"],
            source_run_id=row["source_run_id"],
            members=tuple(
                ChatDocumentCandidate(
                    file_name=member["file_name"],
                    original_name=member["original_name"],
                    document_ref=member["document_ref"],
                    external_location=member["external_location"],
                    structured_source_key=member["structured_source_key"],
                )
                for member in member_rows
            ),
            created_at=row["created_at"],
            source_architecture_id=_optional_architecture_id(
                row["source_architecture_id"],
                name="source_architecture_id",
            ),
        )


class ChatRunInputRepository(_Repository):
    """不可变请求时 `chat_run_inputs` 快照的仓储。"""

    def get(self, run_id: str) -> ChatRunInput | None:
        normalized_run_id = _required_text(run_id, name="run_id")
        with _connection_scope(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM chat_run_inputs WHERE run_id = ?",
                (normalized_run_id,),
            ).fetchone()
            return self._row(connection, row) if row is not None else None

    @staticmethod
    def _row(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> ChatRunInput:
        files: list[ChatRunInputFile] = []
        effective_scope_revision_id = _optional_text(
            row["effective_scope_revision_id"]
        )
        if effective_scope_revision_id:
            revision = ChatScopeRepository.get_revision_in_transaction(
                connection,
                scope_revision_id=effective_scope_revision_id,
            )
            if revision is None:
                raise ValueError(
                    "chat_run_inputs references missing scope revision"
                )
            files.extend(
                ChatRunInputFile(
                    file_name=member.file_name,
                    original_name=member.original_name,
                    document_ref=member.document_ref,
                    external_location=member.external_location,
                    structured_source_key=member.structured_source_key,
                )
                for member in revision.members
            )
        else:
            for index, raw_file in enumerate(
                _json_loads_list(row["files_json"])
            ):
                if not isinstance(raw_file, Mapping):
                    raise ValueError(
                        f"chat_run_inputs.files_json[{index}] must be object"
                    )
                files.append(
                    ChatRunInputFile(
                        file_name=_required_text(
                            str(raw_file.get("file_name") or ""),
                            name="file_name",
                        ),
                        original_name=_required_text(
                            str(raw_file.get("original_name") or ""),
                            name="original_name",
                        ),
                        document_ref=_required_text(
                            str(raw_file.get("document_ref") or ""),
                            name="document_ref",
                        ),
                        external_location=_optional_text(
                            str(raw_file.get("external_location") or ""),
                        ),
                        structured_source_key=_optional_canonical_text(
                            raw_file.get("structured_source_key", ""),
                            name="structured_source_key",
                        ),
                    )
                )
        requested_files: list[ChatRequestedFile] = []
        for index, raw_file in enumerate(
            _json_loads_list(row["requested_files_json"])
        ):
            if not isinstance(raw_file, Mapping):
                raise ValueError(
                    "chat_run_inputs.requested_files_json"
                    f"[{index}] must be object"
                )
            if set(raw_file) != {"file_name", "original_name"}:
                raise ValueError(
                    "chat_run_inputs.requested_files_json"
                    f"[{index}] fields are invalid"
                )
            requested_files.append(
                ChatRequestedFile(
                    file_name=_required_text(
                        str(raw_file.get("file_name") or ""),
                        name="file_name",
                    ),
                    original_name=_required_text(
                        str(raw_file.get("original_name") or ""),
                        name="original_name",
                    ),
                )
            )
        if len({item.file_name for item in requested_files}) != len(
            requested_files
        ):
            raise ValueError(
                "chat_run_inputs.requested_files_json "
                "contains duplicate file_name"
            )
        selection_mode = _required_text(
            row["selection_mode"],
            name="selection_mode",
        )
        requested_architecture_id = _optional_architecture_id(
            row["requested_architecture_id"],
            name="requested_architecture_id",
        )
        if effective_scope_revision_id:
            if selection_mode not in CHAT_SCOPE_SELECTION_MODES:
                raise ValueError(
                    "chat_run_inputs selection_mode is invalid for scope input"
                )
        elif selection_mode != "legacy_input":
            raise ValueError(
                "chat_run_inputs without scope must use legacy_input"
            )
        if selection_mode in {
            CHAT_SCOPE_SELECTION_ARCHITECTURE_INITIAL,
            CHAT_SCOPE_SELECTION_ARCHITECTURE_REUSE,
        }:
            if requested_architecture_id is None or requested_files:
                raise ValueError(
                    "architecture run input selector facts are invalid"
                )
        elif requested_architecture_id is not None:
            raise ValueError(
                "file run input cannot contain requested_architecture_id"
            )
        return ChatRunInput(
            run_id=row["run_id"],
            message=row["message"],
            files=tuple(files),
            created_at=row["created_at"],
            requested_files=tuple(requested_files),
            effective_scope_revision_id=effective_scope_revision_id,
            selection_mode=selection_mode,
            requested_architecture_id=requested_architecture_id,
        )


class ChatCleanupJobRepository(_Repository):
    """独立于 HTTP 请求持久化可重试的清理任务。

    SQLite 实现刻意不宣称具备可靠的后台投递能力，但会保留足够状态，让未来调度器或
    工作进程仅凭 ``job_id`` 领取同一条任务，而无需依赖捕获的 Python 回调。
    """

    def enqueue(
        self,
        *,
        conversation_id: str,
        reason: str,
        lease_id: str = "",
    ) -> ChatCleanupJob:
        normalized_conversation_id = _required_text(conversation_id, name="conversation_id")
        normalized_reason = _required_text(reason, name="reason")
        if normalized_reason not in CLEANUP_JOB_REASONS:
            raise ValueError(f"cleanup reason is not supported: {normalized_reason}")
        normalized_lease_id = _optional_text(lease_id)
        now = _utc_now_iso()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM chat_cleanup_jobs
                WHERE conversation_id = ? AND reason = ? AND lease_id = ?
                  AND status IN (?, ?, ?)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (
                    normalized_conversation_id,
                    normalized_reason,
                    normalized_lease_id,
                    CLEANUP_JOB_PENDING,
                    CLEANUP_JOB_RUNNING,
                    CLEANUP_JOB_FAILED,
                ),
            ).fetchone()
            if existing is not None:
                job = self._row(existing)
                if job.lease_id != normalized_lease_id:
                    raise ValueError("cleanup job identity conflicts with an existing open job")
                if job.status == CLEANUP_JOB_FAILED:
                    connection.execute(
                        """
                        UPDATE chat_cleanup_jobs
                        SET status = ?, next_attempt_at = ?, error_message = '',
                            updated_at = ?
                        WHERE job_id = ? AND status = ?
                        """,
                        (
                            CLEANUP_JOB_PENDING,
                            now,
                            now,
                            job.job_id,
                            CLEANUP_JOB_FAILED,
                        ),
                    )
                    requeued = self._get_with_connection(
                        connection,
                        job_id=job.job_id,
                    )
                    logger.info(
                        "已重新激活文件对话清理任务: job_id=%s conversation_id=%s reason=%s previous_attempt_count=%d",
                        requeued.job_id,
                        requeued.conversation_id,
                        requeued.reason,
                        job.attempt_count,
                    )
                    return requeued
                logger.debug(
                    "复用已存在的文件对话清理任务: job_id=%s conversation_id=%s reason=%s status=%s",
                    job.job_id,
                    job.conversation_id,
                    job.reason,
                    job.status,
                )
                return job
            job_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO chat_cleanup_jobs (
                    job_id, conversation_id, reason, lease_id, status,
                    attempt_count, next_attempt_at, error_message,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, '', ?, ?)
                """,
                (
                    job_id,
                    normalized_conversation_id,
                    normalized_reason,
                    normalized_lease_id,
                    CLEANUP_JOB_PENDING,
                    now,
                    now,
                    now,
                ),
            )
            created = self._get_with_connection(connection, job_id=job_id)
            logger.info(
                "已创建文件对话清理任务: job_id=%s conversation_id=%s reason=%s has_lease=%s",
                created.job_id,
                created.conversation_id,
                created.reason,
                bool(created.lease_id),
            )
            return created

    def get(self, job_id: str) -> ChatCleanupJob | None:
        normalized_job_id = _required_text(job_id, name="job_id")
        with _connection_scope(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM chat_cleanup_jobs WHERE job_id = ?",
                (normalized_job_id,),
            ).fetchone()
        return self._row(row) if row is not None else None

    def claim(self, *, job_id: str) -> ChatCleanupJob:
        """在同一事务中领取就绪任务并计入本次尝试。"""
        normalized_job_id = _required_text(job_id, name="job_id")
        now = _utc_now_iso()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._get_with_connection(connection, job_id=normalized_job_id)
            if current.status == CLEANUP_JOB_SUCCEEDED:
                logger.debug(
                    "文件对话清理任务已完成，无需重复领取: job_id=%s conversation_id=%s",
                    current.job_id,
                    current.conversation_id,
                )
                return current
            if current.status == CLEANUP_JOB_RUNNING:
                raise ValueError("cleanup job is already running")
            if current.status not in {CLEANUP_JOB_PENDING, CLEANUP_JOB_FAILED}:
                raise ValueError("cleanup job has an unsupported status")
            if current.next_attempt_at > now:
                raise ValueError("cleanup job is not ready for another attempt")
            cursor = connection.execute(
                """
                UPDATE chat_cleanup_jobs
                SET status = ?, attempt_count = attempt_count + 1,
                    error_message = '', updated_at = ?
                WHERE job_id = ? AND status = ?
                """,
                (
                    CLEANUP_JOB_RUNNING,
                    now,
                    normalized_job_id,
                    current.status,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("cleanup job was changed concurrently")
            claimed = self._get_with_connection(connection, job_id=normalized_job_id)
            logger.info(
                "已领取文件对话清理任务: job_id=%s conversation_id=%s reason=%s attempt=%d",
                claimed.job_id,
                claimed.conversation_id,
                claimed.reason,
                claimed.attempt_count,
            )
            return claimed

    def mark_succeeded(self, *, job_id: str) -> ChatCleanupJob:
        return self._set_terminal_status(
            job_id=job_id,
            status=CLEANUP_JOB_SUCCEEDED,
            error_message="",
        )

    def mark_failed(
        self,
        *,
        job_id: str,
        error_message: str,
        next_attempt_at: str | None = None,
    ) -> ChatCleanupJob:
        return self._set_terminal_status(
            job_id=job_id,
            status=CLEANUP_JOB_FAILED,
            error_message=_required_text(error_message, name="error_message"),
            next_attempt_at=next_attempt_at,
        )

    def list_ready(self) -> tuple[ChatCleanupJob, ...]:
        """列出可由本地维护执行器处理的持久化任务。"""
        now = _utc_now_iso()
        with _connection_scope(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM chat_cleanup_jobs
                WHERE status IN (?, ?) AND next_attempt_at <= ?
                ORDER BY next_attempt_at ASC, created_at ASC
                """,
                (CLEANUP_JOB_PENDING, CLEANUP_JOB_FAILED, now),
            ).fetchall()
        jobs = tuple(self._row(row) for row in rows)
        logger.debug("已查询就绪文件对话清理任务: job_count=%d", len(jobs))
        return jobs

    def list_by_chat(self, conversation_id: str) -> tuple[ChatCleanupJob, ...]:
        """读取一个对话的清理审计轨迹，但不通过 HTTP 暴露。"""
        normalized_conversation_id = _required_text(conversation_id, name="conversation_id")
        with _connection_scope(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM chat_cleanup_jobs
                WHERE conversation_id = ?
                ORDER BY created_at ASC, job_id ASC
                """,
                (normalized_conversation_id,),
            ).fetchall()
        jobs = tuple(self._row(row) for row in rows)
        logger.debug(
            "已读取文件对话清理审计记录: conversation_id=%s job_count=%d",
            normalized_conversation_id,
            len(jobs),
        )
        return jobs

    def _set_terminal_status(
        self,
        *,
        job_id: str,
        status: str,
        error_message: str,
        next_attempt_at: str | None = None,
    ) -> ChatCleanupJob:
        normalized_job_id = _required_text(job_id, name="job_id")
        normalized_status = _validate_choice(
            status,
            name="status",
            allowed=CLEANUP_JOB_STATUSES,
        )
        if normalized_status not in {CLEANUP_JOB_SUCCEEDED, CLEANUP_JOB_FAILED}:
            raise ValueError("cleanup job terminal status is invalid")
        now = _utc_now_iso()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._get_with_connection(connection, job_id=normalized_job_id)
            if current.status == CLEANUP_JOB_SUCCEEDED:
                return current
            if current.status != CLEANUP_JOB_RUNNING:
                raise ValueError("cleanup job must be running before it can finish")
            cursor = connection.execute(
                """
                UPDATE chat_cleanup_jobs
                SET status = ?, error_message = ?, next_attempt_at = ?,
                    updated_at = ?
                WHERE job_id = ? AND status = ?
                """,
                (
                    normalized_status,
                    _optional_text(error_message),
                    _optional_text(next_attempt_at) or now,
                    now,
                    normalized_job_id,
                    CLEANUP_JOB_RUNNING,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("cleanup job was changed concurrently")
            completed = self._get_with_connection(connection, job_id=normalized_job_id)
            logger.info(
                "文件对话清理任务已进入终态: job_id=%s conversation_id=%s status=%s attempt=%d has_error=%s",
                completed.job_id,
                completed.conversation_id,
                completed.status,
                completed.attempt_count,
                bool(completed.error_message),
            )
            return completed

    def _get_with_connection(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
    ) -> ChatCleanupJob:
        row = connection.execute(
            "SELECT * FROM chat_cleanup_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise ValueError("chat_cleanup_job 不存在")
        return self._row(row)

    @staticmethod
    def _row(row: sqlite3.Row) -> ChatCleanupJob:
        return ChatCleanupJob(
            job_id=row["job_id"],
            conversation_id=row["conversation_id"],
            reason=row["reason"],
            lease_id=row["lease_id"],
            status=row["status"],
            attempt_count=int(row["attempt_count"]),
            next_attempt_at=row["next_attempt_at"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class ChatMessageSourceRepository(_Repository):
    """assistant 消息来源 Chunk 的无损、有序快照仓储。"""

    def list_by_message(
        self,
        message_id: str,
    ) -> tuple[ChatMessageSourceChunk, ...]:
        normalized_message_id = _required_text(message_id, name="message_id")
        with _connection_scope(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM message_source_chunks
                WHERE message_id = ?
                ORDER BY position ASC
                """,
                (normalized_message_id,),
            ).fetchall()
            return tuple(self._row(row) for row in rows)

    def list_by_conversation(
        self,
        conversation_id: str,
    ) -> tuple[ChatMessageSourceChunk, ...]:
        """一次查询读取 Conversation 的全部来源，避免历史投影产生 N+1。"""
        normalized_conversation_id = _required_text(
            conversation_id,
            name="conversation_id",
        )
        with _connection_scope(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT chunk.*
                FROM message_source_chunks AS chunk
                INNER JOIN chat_messages AS message
                  ON message.message_id = chunk.message_id
                WHERE message.conversation_id = ?
                ORDER BY message.sequence_no ASC, chunk.position ASC
                """,
                (normalized_conversation_id,),
            ).fetchall()
        return tuple(self._row(row) for row in rows)

    @staticmethod
    def append_many_in_transaction(
        *,
        connection: sqlite3.Connection,
        message_id: str,
        chunks: Sequence[ChatMessageSourceChunk],
    ) -> None:
        normalized_message_id = _required_text(message_id, name="message_id")
        normalized_chunks = tuple(chunks)
        for expected_position, chunk in enumerate(normalized_chunks):
            if not isinstance(chunk, ChatMessageSourceChunk):
                raise TypeError("chunks must contain ChatMessageSourceChunk")
            if chunk.message_id != normalized_message_id:
                raise ValueError("source chunk belongs to another message")
            if chunk.position != expected_position:
                raise ValueError("source chunk positions must be contiguous from zero")
            # content 必须直接绑定为原始 Python 字符串；此处禁止任何 strip/replace/str。
            connection.execute(
                """
                INSERT INTO message_source_chunks(
                    message_id, position, content, file_name,
                    original_file_name, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk.message_id,
                    chunk.position,
                    chunk.content,
                    chunk.file_name,
                    chunk.original_file_name,
                    chunk.created_at,
                ),
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> ChatMessageSourceChunk:
        return ChatMessageSourceChunk(
            message_id=row["message_id"],
            position=int(row["position"]),
            content=row["content"],
            file_name=row["file_name"],
            original_file_name=row["original_file_name"],
            created_at=row["created_at"],
        )


class ChatMessageRepository(_Repository):
    """`chat_messages` 与 `chat_message_files` 表的仓储。"""

    def append(
        self,
        *,
        message_id: str,
        conversation_id: str,
        run_id: str,
        role: str,
        content: str,
        status: str,
        sequence_no: int | None = None,
        files: Sequence[tuple[str, str]] = (),
        architecture_id: int | None = None,
    ) -> ChatMessage:
        normalized_message_id = _required_text(message_id, name="message_id")
        normalized_conversation_id = _required_text(conversation_id, name="conversation_id")
        normalized_run_id = _required_text(run_id, name="run_id")
        normalized_role = _validate_choice(role, name="role", allowed=MESSAGE_ROLES)
        normalized_status = _validate_choice(
            status,
            name="status",
            allowed=MESSAGE_STATUSES,
        )
        if not isinstance(content, str) or content == "":
            raise ValueError("content 不能为空")
        if sequence_no is not None and (
            isinstance(sequence_no, bool)
            or not isinstance(sequence_no, int)
            or sequence_no < 1
        ):
            raise ValueError("sequence_no 必须是正整数或 None")
        normalized_files = self._normalize_files(files)
        normalized_architecture_id = _optional_architecture_id(
            architecture_id,
            name="architecture_id",
        )
        if normalized_role == "assistant" and normalized_architecture_id is not None:
            raise ValueError("assistant message cannot contain architecture_id")
        if normalized_architecture_id is not None and normalized_files:
            raise ValueError(
                "architecture message cannot contain chat_message_files"
            )

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run_row = connection.execute(
                "SELECT conversation_id FROM chat_runs WHERE run_id = ?",
                (normalized_run_id,),
            ).fetchone()
            if run_row is None:
                raise ValueError("chat_run 不存在")
            if run_row["conversation_id"] != normalized_conversation_id:
                raise ValueError("chat_message run_id 不属于当前 conversation_id")
            existing = connection.execute(
                "SELECT * FROM chat_messages WHERE message_id = ?",
                (normalized_message_id,),
            ).fetchone()
            if existing is not None:
                existing_message = self._row(connection, existing)
                self._reject_identity_conflict(
                    existing_message,
                    conversation_id=normalized_conversation_id,
                    run_id=normalized_run_id,
                    role=normalized_role,
                    content=content,
                    status=normalized_status,
                    sequence_no=sequence_no,
                    files=normalized_files,
                    architecture_id=normalized_architecture_id,
                )
                return existing_message
            resolved_sequence = sequence_no
            if resolved_sequence is None:
                row = connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next_sequence
                    FROM chat_messages
                    WHERE conversation_id = ?
                    """,
                    (normalized_conversation_id,),
                ).fetchone()
                resolved_sequence = int(row["next_sequence"])
            connection.execute(
                """
                INSERT INTO chat_messages (
                    message_id, conversation_id, run_id, role, content,
                    status, sequence_no, created_at, architecture_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_message_id,
                    normalized_conversation_id,
                    normalized_run_id,
                    normalized_role,
                    content,
                    normalized_status,
                    resolved_sequence,
                    _utc_now_iso(),
                    normalized_architecture_id,
                ),
            )
            self._replace_files(
                connection,
                message_id=normalized_message_id,
                files=normalized_files,
            )
            logger.info(
                "写入文件对话消息: conversation_id=%s run_id=%s message_id=%s "
                "role=%s status=%s sequence_no=%s file_count=%d "
                "architecture_id=%s",
                normalized_conversation_id,
                normalized_run_id,
                normalized_message_id,
                normalized_role,
                normalized_status,
                resolved_sequence,
                len(normalized_files),
                normalized_architecture_id,
            )
            return self._get_with_connection(connection, normalized_message_id)

    def list_by_chat(self, conversation_id: str) -> tuple[ChatMessage, ...]:
        normalized_conversation_id = _required_text(conversation_id, name="conversation_id")
        with _connection_scope(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM chat_messages
                WHERE conversation_id = ?
                ORDER BY sequence_no ASC
                """,
                (normalized_conversation_id,),
            ).fetchall()
            file_rows = connection.execute(
                """
                SELECT message_files.*
                FROM chat_message_files AS message_files
                INNER JOIN chat_messages AS messages
                    ON messages.message_id = message_files.message_id
                WHERE messages.conversation_id = ?
                ORDER BY messages.sequence_no ASC, message_files.file_name ASC
                """,
                (normalized_conversation_id,),
            ).fetchall()
            files_by_message: dict[str, list[ChatMessageFile]] = {}
            for file_row in file_rows:
                files_by_message.setdefault(file_row["message_id"], []).append(
                    self._file_row(file_row)
                )
            return tuple(
                self._row(
                    connection,
                    row,
                    files=tuple(files_by_message.get(row["message_id"], ())),
                )
                for row in rows
            )

    def set_status(self, *, message_id: str, status: str) -> ChatMessage:
        normalized_message_id = _required_text(message_id, name="message_id")
        normalized_status = _validate_choice(
            status,
            name="status",
            allowed=MESSAGE_STATUSES,
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._get_with_connection(connection, normalized_message_id)
            self._ensure_status_transition(
                current_status=current.status,
                next_status=normalized_status,
            )
            if current.status == normalized_status:
                logger.debug(
                    "文件对话消息状态无需变更: message_id=%s status=%s",
                    normalized_message_id,
                    normalized_status,
                )
                return current
            cursor = connection.execute(
                """
                UPDATE chat_messages
                SET status = ?
                WHERE message_id = ? AND status = ?
                """,
                (
                    normalized_status,
                    normalized_message_id,
                    current.status,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("chat_message status was changed concurrently")
            logger.info(
                "文件对话消息状态变更: conversation_id=%s run_id=%s message_id=%s %s->%s",
                current.conversation_id,
                current.run_id,
                normalized_message_id,
                current.status,
                normalized_status,
            )
            return self._get_with_connection(connection, normalized_message_id)

    def _replace_files(
        self,
        connection: sqlite3.Connection,
        *,
        message_id: str,
        files: Sequence[tuple[str, str]],
    ) -> None:
        connection.execute(
            "DELETE FROM chat_message_files WHERE message_id = ?",
            (message_id,),
        )
        for file_name, original_name in files:
            connection.execute(
                """
                INSERT INTO chat_message_files (
                    message_id, file_name, original_name
                ) VALUES (?, ?, ?)
                """,
                (
                    message_id,
                    _required_text(file_name, name="file_name"),
                    _optional_text(original_name),
                ),
            )

    @staticmethod
    def _normalize_files(
        files: Sequence[tuple[str, str]],
    ) -> tuple[tuple[str, str], ...]:
        normalized: dict[str, str] = {}
        for file_name, original_name in files:
            normalized_file_name = _required_text(file_name, name="file_name")
            normalized_original_name = _optional_text(original_name)
            if normalized_file_name in normalized:
                raise ValueError("files 中存在重复 file_name")
            normalized[normalized_file_name] = normalized_original_name
        return tuple(sorted(normalized.items()))

    @staticmethod
    def _reject_identity_conflict(
        message: ChatMessage,
        *,
        conversation_id: str,
        run_id: str,
        role: str,
        content: str,
        status: str,
        sequence_no: int | None,
        files: tuple[tuple[str, str], ...],
        architecture_id: int | None,
    ) -> None:
        existing_files = tuple(
            (item.file_name, item.original_name) for item in message.files
        )
        status_conflicts = message.status != status and not (
            status == MESSAGE_PENDING and message.status == MESSAGE_COMMITTED
        )
        if (
            message.conversation_id != conversation_id
            or message.run_id != run_id
            or message.role != role
            or message.content != content
            or status_conflicts
            or (sequence_no is not None and message.sequence_no != sequence_no)
            or existing_files != files
            or message.architecture_id != architecture_id
        ):
            raise ValueError("message_id 对应的消息身份或内容冲突")

    @staticmethod
    def _ensure_status_transition(
        *,
        current_status: str,
        next_status: str,
    ) -> None:
        if current_status == next_status:
            return
        allowed = _MESSAGE_STATUS_TRANSITIONS[current_status]
        if next_status not in allowed:
            raise ValueError(
                f"illegal chat_message status transition: "
                f"{current_status} -> {next_status}"
            )

    def _get_with_connection(
        self,
        connection: sqlite3.Connection,
        message_id: str,
    ) -> ChatMessage:
        row = connection.execute(
            "SELECT * FROM chat_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if row is None:
            raise ValueError("chat_message 不存在")
        return self._row(connection, row)

    @staticmethod
    def _row(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        files: tuple[ChatMessageFile, ...] | None = None,
    ) -> ChatMessage:
        if files is None:
            file_rows = connection.execute(
                """
                SELECT * FROM chat_message_files
                WHERE message_id = ?
                ORDER BY file_name ASC
                """,
                (row["message_id"],),
            ).fetchall()
            files = tuple(ChatMessageRepository._file_row(item) for item in file_rows)
        return ChatMessage(
            message_id=row["message_id"],
            conversation_id=row["conversation_id"],
            run_id=row["run_id"],
            role=row["role"],
            content=row["content"],
            status=row["status"],
            sequence_no=row["sequence_no"],
            created_at=row["created_at"],
            files=files,
            architecture_id=_optional_architecture_id(
                row["architecture_id"],
                name="architecture_id",
            ),
        )

    @staticmethod
    def _file_row(row: sqlite3.Row) -> ChatMessageFile:
        return ChatMessageFile(
            message_id=row["message_id"],
            file_name=row["file_name"],
            original_name=row["original_name"],
        )
