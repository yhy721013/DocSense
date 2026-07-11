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

from app.services.chat.domain.models import (
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
    ChatRun,
    ChatRunInput,
    ChatRunInputFile,
    ChatSession,
)


logger = logging.getLogger(__name__)


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


def _connect(db_path: str, *, timeout_seconds: float = 5.0) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=max(0.0, timeout_seconds))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    # 有界忙等待超时可使受支持的单实例模式中短暂写入冲突的结果确定。它不能替代队列
    # 或分布式锁，但可避免相邻请求提交短事务时 SQLite 立即失败。
    connection.execute("PRAGMA busy_timeout = 5000")
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
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        applied_versions = {
            int(row["version"])
            for row in connection.execute(
                "SELECT version FROM chat_schema_migrations"
            ).fetchall()
        }
        for version, migration in _CHAT_SCHEMA_MIGRATIONS:
            if version in applied_versions:
                continue
            migration(connection)
            connection.execute(
                "INSERT INTO chat_schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, _utc_now_iso()),
            )
            logger.info(
                "已应用文件对话SQLite schema migration: version=%s db_path=%s",
                version,
                normalized_path,
            )


def _create_chat_authority_schema(connection: sqlite3.Connection) -> None:
    """为全新的开发数据库创建首版权威对话架构。"""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_sessions (
            chat_id TEXT PRIMARY KEY,
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
            chat_id TEXT NOT NULL,
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
            FOREIGN KEY(chat_id) REFERENCES chat_sessions(chat_id)
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
            chat_id TEXT NOT NULL,
            file_name TEXT NOT NULL,
            original_name TEXT NOT NULL,
            document_ref TEXT NOT NULL,
            external_location TEXT NOT NULL DEFAULT '',
            added_by_run_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(chat_id, file_name, document_ref),
            FOREIGN KEY(chat_id) REFERENCES chat_sessions(chat_id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_document_heads (
            chat_id TEXT NOT NULL,
            file_name TEXT NOT NULL,
            binding_id TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(chat_id, file_name),
            FOREIGN KEY(chat_id) REFERENCES chat_sessions(chat_id)
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
            chat_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'committed', 'discarded')
            ),
            sequence_no INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(chat_id, sequence_no),
            FOREIGN KEY(chat_id) REFERENCES chat_sessions(chat_id)
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
            chat_id TEXT NOT NULL,
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
            FOREIGN KEY(chat_id) REFERENCES chat_sessions(chat_id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_cleanup_jobs (
            job_id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
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
            FOREIGN KEY(chat_id) REFERENCES chat_sessions(chat_id)
                ON DELETE CASCADE
        )
        """
    )


def _add_chat_constraints_and_indexes(connection: sqlite3.Connection) -> None:
    """添加约束，防止绕过应用服务直接写入时破坏不变量。"""
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_runs_one_active_per_chat
        ON chat_runs (chat_id)
        WHERE status IN ('accepted', 'running')
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chat_runs_chat_status
        ON chat_runs (chat_id, status, updated_at)
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
        ON chat_document_bindings (chat_id, file_name, created_at)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chat_messages_chat_sequence
        ON chat_messages (chat_id, sequence_no)
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
        ON chat_cleanup_jobs (chat_id, reason)
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

    一个对话可能存在多个尚未清理的临时标题线程。原先的 ``(chat_id, reason)`` 键会
    错误合并这些相互独立的清理任务。``lease_id`` 是持久化任务身份的一部分；而对话删除
    仍使用空租约 ID，因此每个对话仍保持唯一。
    """
    connection.execute("DROP INDEX IF EXISTS uq_chat_cleanup_jobs_open")
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_cleanup_jobs_open
        ON chat_cleanup_jobs (chat_id, reason, lease_id)
        WHERE status IN ('pending', 'running')
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_chat_document_bindings_run_chat_insert
        BEFORE INSERT ON chat_document_bindings
        WHEN NEW.added_by_run_id != '' AND NOT EXISTS (
            SELECT 1 FROM chat_runs
            WHERE run_id = NEW.added_by_run_id AND chat_id = NEW.chat_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'chat_document_binding run_id does not belong to chat_id');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_chat_document_bindings_run_chat_update
        BEFORE UPDATE OF chat_id, added_by_run_id ON chat_document_bindings
        WHEN NEW.added_by_run_id != '' AND NOT EXISTS (
            SELECT 1 FROM chat_runs
            WHERE run_id = NEW.added_by_run_id AND chat_id = NEW.chat_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'chat_document_binding run_id does not belong to chat_id');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_chat_document_heads_binding_chat
        BEFORE INSERT ON chat_document_heads
        WHEN NOT EXISTS (
            SELECT 1 FROM chat_document_bindings
            WHERE binding_id = NEW.binding_id AND chat_id = NEW.chat_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'chat_document_head binding_id does not belong to chat_id');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_chat_document_heads_binding_chat_update
        BEFORE UPDATE OF chat_id, binding_id ON chat_document_heads
        WHEN NOT EXISTS (
            SELECT 1 FROM chat_document_bindings
            WHERE binding_id = NEW.binding_id AND chat_id = NEW.chat_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'chat_document_head binding_id does not belong to chat_id');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_chat_messages_run_chat_insert
        BEFORE INSERT ON chat_messages
        WHEN NOT EXISTS (
            SELECT 1 FROM chat_runs
            WHERE run_id = NEW.run_id AND chat_id = NEW.chat_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'chat_message run_id does not belong to chat_id');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_chat_messages_run_chat_update
        BEFORE UPDATE OF chat_id, run_id ON chat_messages
        WHEN NOT EXISTS (
            SELECT 1 FROM chat_runs
            WHERE run_id = NEW.run_id AND chat_id = NEW.chat_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'chat_message run_id does not belong to chat_id');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_chat_resource_leases_run_chat_insert
        BEFORE INSERT ON chat_resource_leases
        WHEN NEW.run_id != '' AND NOT EXISTS (
            SELECT 1 FROM chat_runs
            WHERE run_id = NEW.run_id AND chat_id = NEW.chat_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'chat_resource_lease run_id does not belong to chat_id');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_chat_resource_leases_run_chat_update
        BEFORE UPDATE OF chat_id, run_id ON chat_resource_leases
        WHEN NEW.run_id != '' AND NOT EXISTS (
            SELECT 1 FROM chat_runs
            WHERE run_id = NEW.run_id AND chat_id = NEW.chat_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'chat_resource_lease run_id does not belong to chat_id');
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


_CHAT_SCHEMA_MIGRATIONS = (
    (1, _create_chat_authority_schema),
    (2, _add_chat_constraints_and_indexes),
    (3, _add_integrity_triggers_and_refine_cleanup_job_identity),
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
    """`chat_sessions` 表的仓储。"""

    def create_or_get(
        self,
        *,
        chat_id: str,
        workspace_ref: str = "",
        thread_ref: str = "",
        status: str = SESSION_ACTIVE,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ChatSession:
        normalized_chat_id = _required_text(chat_id, name="chat_id")
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
                "SELECT * FROM chat_sessions WHERE chat_id = ?",
                (normalized_chat_id,),
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
                        "更新文件对话会话引用: chat_id=%s workspace_ref=%s thread_ref=%s",
                        normalized_chat_id,
                        resolved_workspace,
                        resolved_thread,
                    )
                    connection.execute(
                        """
                        UPDATE chat_sessions
                        SET workspace_ref = ?, thread_ref = ?, metadata_json = ?,
                            updated_at = ?
                        WHERE chat_id = ?
                        """,
                        (
                            resolved_workspace,
                            resolved_thread,
                            _json_dumps(merged_metadata),
                            now,
                            normalized_chat_id,
                        ),
                    )
                    existing = connection.execute(
                        "SELECT * FROM chat_sessions WHERE chat_id = ?",
                        (normalized_chat_id,),
                    ).fetchone()
                logger.debug(
                    "复用文件对话会话: chat_id=%s status=%s",
                    normalized_chat_id,
                    existing["status"],
                )
                return self._row(existing)
            connection.execute(
                """
                INSERT INTO chat_sessions (
                    chat_id, workspace_ref, thread_ref, status,
                    created_at, updated_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_chat_id,
                    normalized_workspace,
                    normalized_thread,
                    normalized_status,
                    now,
                    now,
                    metadata_json,
                ),
            )
            row = connection.execute(
                "SELECT * FROM chat_sessions WHERE chat_id = ?",
                (normalized_chat_id,),
            ).fetchone()
            logger.info(
                "创建文件对话会话: chat_id=%s status=%s workspace_ref=%s thread_ref=%s",
                normalized_chat_id,
                normalized_status,
                normalized_workspace,
                normalized_thread,
            )
            return self._row(row)

    def get(self, chat_id: str) -> ChatSession | None:
        normalized_chat_id = _required_text(chat_id, name="chat_id")
        with _connection_scope(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM chat_sessions WHERE chat_id = ?",
                (normalized_chat_id,),
            ).fetchone()
        return self._row(row) if row is not None else None

    def list_all(self) -> tuple[ChatSession, ...]:
        with _connection_scope(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM chat_sessions
                ORDER BY updated_at DESC, chat_id ASC
                """
            ).fetchall()
        return tuple(self._row(row) for row in rows)

    def update_refs(
        self,
        *,
        chat_id: str,
        workspace_ref: str | None = None,
        thread_ref: str | None = None,
    ) -> ChatSession:
        normalized_chat_id = _required_text(chat_id, name="chat_id")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM chat_sessions WHERE chat_id = ?",
                (normalized_chat_id,),
            ).fetchone()
            if row is None:
                raise ValueError("chat_session 不存在")
            if row["status"] != SESSION_ACTIVE:
                raise ValueError(
                    "chat_session remote references can only change while active"
                )
            connection.execute(
                """
                UPDATE chat_sessions
                SET workspace_ref = ?, thread_ref = ?, updated_at = ?
                WHERE chat_id = ?
                """,
                (
                    _optional_text(workspace_ref)
                    if workspace_ref is not None
                    else row["workspace_ref"],
                    _optional_text(thread_ref)
                    if thread_ref is not None
                    else row["thread_ref"],
                    _utc_now_iso(),
                    normalized_chat_id,
                ),
            )
            logger.info(
                "更新文件对话会话远端引用: chat_id=%s workspace_ref=%s thread_ref=%s",
                normalized_chat_id,
                _optional_text(workspace_ref)
                if workspace_ref is not None
                else row["workspace_ref"],
                _optional_text(thread_ref)
                if thread_ref is not None
                else row["thread_ref"],
            )
            return self._row(
                connection.execute(
                    "SELECT * FROM chat_sessions WHERE chat_id = ?",
                    (normalized_chat_id,),
                ).fetchone()
            )

    def set_status(self, *, chat_id: str, status: str) -> ChatSession:
        normalized_chat_id = _required_text(chat_id, name="chat_id")
        normalized_status = _validate_choice(
            status,
            name="status",
            allowed=SESSION_STATUSES,
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM chat_sessions WHERE chat_id = ?",
                (normalized_chat_id,),
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
                UPDATE chat_sessions
                SET status = ?, updated_at = ?
                WHERE chat_id = ? AND status = ?
                """,
                (
                    normalized_status,
                    _utc_now_iso(),
                    normalized_chat_id,
                    current_status,
                ),
            )
            logger.info(
                "更新文件对话会话状态: chat_id=%s status=%s",
                normalized_chat_id,
                normalized_status,
            )
            row = connection.execute(
                "SELECT * FROM chat_sessions WHERE chat_id = ?",
                (normalized_chat_id,),
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
            chat_id=row["chat_id"],
            workspace_ref=row["workspace_ref"],
            thread_ref=row["thread_ref"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=_json_loads_object(row["metadata_json"]),
        )


class ChatDocumentBindingRepository(_Repository):
    """持久化不可变文档版本及当前版本投影。

    业务文件名在同一个对话中刻意不唯一：上传替换文档会生成不同的 ``document_ref``。
    历史绑定保持不可变，以供审计和清理；头表决定后续轮次默认选择哪个版本。
    """

    def add(
        self,
        *,
        chat_id: str,
        file_name: str,
        original_name: str,
        document_ref: str,
        external_location: str = "",
        added_by_run_id: str = "",
    ) -> ChatDocumentBinding:
        normalized_chat_id = _required_text(chat_id, name="chat_id")
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
        normalized_added_by_run_id = _optional_text(added_by_run_id)
        now = _utc_now_iso()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if normalized_added_by_run_id:
                run_row = connection.execute(
                    "SELECT chat_id FROM chat_runs WHERE run_id = ?",
                    (normalized_added_by_run_id,),
                ).fetchone()
                if run_row is None:
                    raise ValueError("added_by_run_id 对应的 chat_run 不存在")
                if run_row["chat_id"] != normalized_chat_id:
                    raise ValueError(
                        "chat_document_binding run_id 不属于当前 chat_id"
                    )
            existing = connection.execute(
                """
                SELECT * FROM chat_document_bindings
                WHERE chat_id = ? AND file_name = ? AND document_ref = ?
                """,
                (
                    normalized_chat_id,
                    normalized_file_name,
                    normalized_document_ref,
                ),
            ).fetchone()
            if existing is None:
                binding_id = uuid.uuid4().hex
                connection.execute(
                    """
                    INSERT INTO chat_document_bindings (
                        binding_id, chat_id, file_name, original_name,
                        document_ref, external_location, added_by_run_id,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        binding_id,
                        normalized_chat_id,
                        normalized_file_name,
                        normalized_original_name,
                        normalized_document_ref,
                        normalized_location,
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
                )
            connection.execute(
                """
                INSERT INTO chat_document_heads (
                    chat_id, file_name, binding_id, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, file_name) DO UPDATE SET
                    binding_id = excluded.binding_id,
                    updated_at = excluded.updated_at
                """,
                (
                    normalized_chat_id,
                    normalized_file_name,
                    binding.binding_id,
                    now,
                ),
            )
            logger.info(
                "绑定文件revision到本地对话: chat_id=%s file_name=%s document_ref=%s binding_id=%s",
                normalized_chat_id,
                normalized_file_name,
                normalized_document_ref,
                binding.binding_id,
            )
            return binding

    def list_by_chat(self, chat_id: str) -> tuple[ChatDocumentBinding, ...]:
        """返回全部历史绑定，供审计和清理使用。"""
        normalized_chat_id = _required_text(chat_id, name="chat_id")
        with _connection_scope(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM chat_document_bindings
                WHERE chat_id = ?
                ORDER BY created_at ASC, file_name ASC, binding_id ASC
                """,
                (normalized_chat_id,),
            ).fetchall()
        return tuple(self._row(row) for row in rows)

    def list_current_by_chat(
        self,
        chat_id: str,
    ) -> tuple[ChatDocumentBinding, ...]:
        """返回每个业务文件当前选中的最新版本。"""
        normalized_chat_id = _required_text(chat_id, name="chat_id")
        with _connection_scope(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT binding.*
                FROM chat_document_heads AS head
                INNER JOIN chat_document_bindings AS binding
                    ON binding.binding_id = head.binding_id
                WHERE head.chat_id = ?
                ORDER BY head.file_name ASC
                """,
                (normalized_chat_id,),
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
    ) -> None:
        if (
            binding.original_name != original_name
            or binding.external_location != external_location
        ):
            raise ValueError(
                "chat_document_binding identity conflicts with an existing revision"
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> ChatDocumentBinding:
        return ChatDocumentBinding(
            binding_id=row["binding_id"],
            chat_id=row["chat_id"],
            file_name=row["file_name"],
            original_name=row["original_name"],
            document_ref=row["document_ref"],
            external_location=row["external_location"],
            added_by_run_id=row["added_by_run_id"],
            created_at=row["created_at"],
        )


class ChatRunRepository(_Repository):
    """`chat_runs` 表的仓储。"""

    def create(
        self,
        *,
        run_id: str,
        chat_id: str,
        status: str = RUN_ACCEPTED,
        owner_instance_id: str = "",
    ) -> ChatRun:
        normalized_run_id = _required_text(run_id, name="run_id")
        normalized_chat_id = _required_text(chat_id, name="chat_id")
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
                    chat_id=normalized_chat_id,
                    owner_instance_id=_optional_text(owner_instance_id),
                )
                logger.debug(
                    "复用已存在文件对话run: chat_id=%s run_id=%s status=%s",
                    normalized_chat_id,
                    normalized_run_id,
                    existing["status"],
                )
                return self._row(existing)
            connection.execute(
                """
                INSERT INTO chat_runs (
                    run_id, chat_id, status, owner_instance_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_run_id,
                    normalized_chat_id,
                    normalized_status,
                    _optional_text(owner_instance_id),
                    now,
                    now,
                ),
            )
            logger.info(
                "创建文件对话run记录: chat_id=%s run_id=%s owner=%s status=%s",
                normalized_chat_id,
                normalized_run_id,
                _optional_text(owner_instance_id),
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
                    "文件对话run状态无需变更: chat_id=%s run_id=%s status=%s",
                    current.chat_id,
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
                "文件对话run状态迁移: chat_id=%s run_id=%s %s->%s terminal=%s error=%s",
                current.chat_id,
                normalized_run_id,
                current.status,
                normalized_status,
                terminal,
                _optional_text(error_message),
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
                    "拒绝为非活跃run设置中断标记: chat_id=%s run_id=%s status=%s",
                    current.chat_id,
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
                "文件对话run中断标记已持久化: chat_id=%s run_id=%s previous_status=%s",
                current.chat_id,
                normalized_run_id,
                current.status,
            )
            return self._get_with_connection(connection, normalized_run_id)

    def list_active(self, chat_id: str) -> tuple[ChatRun, ...]:
        normalized_chat_id = _required_text(chat_id, name="chat_id")
        placeholders = ",".join("?" for _ in RUN_ACTIVE_STATUSES)
        with _connection_scope(self.db_path) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM chat_runs
                WHERE chat_id = ? AND status IN ({placeholders})
                ORDER BY created_at ASC
                """,
                (normalized_chat_id, *tuple(sorted(RUN_ACTIVE_STATUSES))),
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
        chat_id: str,
        owner_instance_id: str,
    ) -> None:
        if row["chat_id"] != chat_id:
            raise ValueError("run_id is already bound to another chat_id")
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
            chat_id=row["chat_id"],
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


class ChatRunInputRepository(_Repository):
    """不可变请求时 `chat_run_inputs` 快照的仓储。"""

    def get(self, run_id: str) -> ChatRunInput | None:
        normalized_run_id = _required_text(run_id, name="run_id")
        with _connection_scope(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM chat_run_inputs WHERE run_id = ?",
                (normalized_run_id,),
            ).fetchone()
        return self._row(row) if row is not None else None

    @staticmethod
    def _row(row: sqlite3.Row) -> ChatRunInput:
        files: list[ChatRunInputFile] = []
        for index, raw_file in enumerate(_json_loads_list(row["files_json"])):
            if not isinstance(raw_file, Mapping):
                raise ValueError(f"chat_run_inputs.files_json[{index}] must be object")
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
                )
            )
        return ChatRunInput(
            run_id=row["run_id"],
            message=row["message"],
            files=tuple(files),
            created_at=row["created_at"],
        )


class ChatCleanupJobRepository(_Repository):
    """独立于 HTTP 请求持久化可重试的清理任务。

    SQLite 实现刻意不宣称具备可靠的后台投递能力，但会保留足够状态，让未来调度器或
    工作进程仅凭 ``job_id`` 领取同一条任务，而无需依赖捕获的 Python 回调。
    """

    def enqueue(
        self,
        *,
        chat_id: str,
        reason: str,
        lease_id: str = "",
    ) -> ChatCleanupJob:
        normalized_chat_id = _required_text(chat_id, name="chat_id")
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
                WHERE chat_id = ? AND reason = ? AND lease_id = ?
                  AND status IN (?, ?, ?)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (
                    normalized_chat_id,
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
                    return self._get_with_connection(
                        connection,
                        job_id=job.job_id,
                    )
                return job
            job_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO chat_cleanup_jobs (
                    job_id, chat_id, reason, lease_id, status,
                    attempt_count, next_attempt_at, error_message,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, '', ?, ?)
                """,
                (
                    job_id,
                    normalized_chat_id,
                    normalized_reason,
                    normalized_lease_id,
                    CLEANUP_JOB_PENDING,
                    now,
                    now,
                    now,
                ),
            )
            return self._get_with_connection(connection, job_id=job_id)

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
            return self._get_with_connection(connection, job_id=normalized_job_id)

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
        return tuple(self._row(row) for row in rows)

    def list_by_chat(self, chat_id: str) -> tuple[ChatCleanupJob, ...]:
        """读取一个对话的清理审计轨迹，但不通过 HTTP 暴露。"""
        normalized_chat_id = _required_text(chat_id, name="chat_id")
        with _connection_scope(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM chat_cleanup_jobs
                WHERE chat_id = ?
                ORDER BY created_at ASC, job_id ASC
                """,
                (normalized_chat_id,),
            ).fetchall()
        return tuple(self._row(row) for row in rows)

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
            return self._get_with_connection(connection, job_id=normalized_job_id)

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
            chat_id=row["chat_id"],
            reason=row["reason"],
            lease_id=row["lease_id"],
            status=row["status"],
            attempt_count=int(row["attempt_count"]),
            next_attempt_at=row["next_attempt_at"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class ChatMessageRepository(_Repository):
    """`chat_messages` 与 `chat_message_files` 表的仓储。"""

    def append(
        self,
        *,
        message_id: str,
        chat_id: str,
        run_id: str,
        role: str,
        content: str,
        status: str,
        sequence_no: int | None = None,
        files: Sequence[tuple[str, str]] = (),
    ) -> ChatMessage:
        normalized_message_id = _required_text(message_id, name="message_id")
        normalized_chat_id = _required_text(chat_id, name="chat_id")
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

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run_row = connection.execute(
                "SELECT chat_id FROM chat_runs WHERE run_id = ?",
                (normalized_run_id,),
            ).fetchone()
            if run_row is None:
                raise ValueError("chat_run 不存在")
            if run_row["chat_id"] != normalized_chat_id:
                raise ValueError("chat_message run_id 不属于当前 chat_id")
            existing = connection.execute(
                "SELECT * FROM chat_messages WHERE message_id = ?",
                (normalized_message_id,),
            ).fetchone()
            if existing is not None:
                existing_message = self._row(connection, existing)
                self._reject_identity_conflict(
                    existing_message,
                    chat_id=normalized_chat_id,
                    run_id=normalized_run_id,
                    role=normalized_role,
                    content=content,
                    status=normalized_status,
                    sequence_no=sequence_no,
                    files=normalized_files,
                )
                return existing_message
            resolved_sequence = sequence_no
            if resolved_sequence is None:
                row = connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next_sequence
                    FROM chat_messages
                    WHERE chat_id = ?
                    """,
                    (normalized_chat_id,),
                ).fetchone()
                resolved_sequence = int(row["next_sequence"])
            connection.execute(
                """
                INSERT INTO chat_messages (
                    message_id, chat_id, run_id, role, content,
                    status, sequence_no, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_message_id,
                    normalized_chat_id,
                    normalized_run_id,
                    normalized_role,
                    content,
                    normalized_status,
                    resolved_sequence,
                    _utc_now_iso(),
                ),
            )
            self._replace_files(
                connection,
                message_id=normalized_message_id,
                files=normalized_files,
            )
            logger.info(
                "写入文件对话消息: chat_id=%s run_id=%s message_id=%s role=%s status=%s sequence_no=%s file_count=%d",
                normalized_chat_id,
                normalized_run_id,
                normalized_message_id,
                normalized_role,
                normalized_status,
                resolved_sequence,
                len(normalized_files),
            )
            return self._get_with_connection(connection, normalized_message_id)

    def list_by_chat(self, chat_id: str) -> tuple[ChatMessage, ...]:
        normalized_chat_id = _required_text(chat_id, name="chat_id")
        with _connection_scope(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM chat_messages
                WHERE chat_id = ?
                ORDER BY sequence_no ASC
                """,
                (normalized_chat_id,),
            ).fetchall()
            file_rows = connection.execute(
                """
                SELECT message_files.*
                FROM chat_message_files AS message_files
                INNER JOIN chat_messages AS messages
                    ON messages.message_id = message_files.message_id
                WHERE messages.chat_id = ?
                ORDER BY messages.sequence_no ASC, message_files.file_name ASC
                """,
                (normalized_chat_id,),
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
                "文件对话消息状态变更: chat_id=%s run_id=%s message_id=%s %s->%s",
                current.chat_id,
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
        chat_id: str,
        run_id: str,
        role: str,
        content: str,
        status: str,
        sequence_no: int | None,
        files: tuple[tuple[str, str], ...],
    ) -> None:
        existing_files = tuple(
            (item.file_name, item.original_name) for item in message.files
        )
        status_conflicts = message.status != status and not (
            status == MESSAGE_PENDING and message.status == MESSAGE_COMMITTED
        )
        if (
            message.chat_id != chat_id
            or message.run_id != run_id
            or message.role != role
            or message.content != content
            or status_conflicts
            or (sequence_no is not None and message.sequence_no != sequence_no)
            or existing_files != files
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
            chat_id=row["chat_id"],
            run_id=row["run_id"],
            role=row["role"],
            content=row["content"],
            status=row["status"],
            sequence_no=row["sequence_no"],
            created_at=row["created_at"],
            files=files,
        )

    @staticmethod
    def _file_row(row: sqlite3.Row) -> ChatMessageFile:
        return ChatMessageFile(
            message_id=row["message_id"],
            file_name=row["file_name"],
            original_name=row["original_name"],
        )
