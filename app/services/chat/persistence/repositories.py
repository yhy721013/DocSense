"""SQLite repositories for the file-chat local authority tables."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence

from app.services.chat.domain.models import (
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
    ChatDocument,
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
    """Create all stage-3 chat tables and indexes without mutating old `chats`."""
    normalized_path = _required_text(db_path, name="db_path")
    Path(normalized_path).parent.mkdir(parents=True, exist_ok=True)
    with _connection_scope(normalized_path) as connection:
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
                    status IN (
                        'accepted', 'running', 'succeeded', 'failed', 'aborted'
                    )
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
            CREATE TABLE IF NOT EXISTS chat_documents (
                chat_id TEXT NOT NULL,
                file_name TEXT NOT NULL,
                original_name TEXT NOT NULL DEFAULT '',
                document_ref TEXT NOT NULL DEFAULT '',
                external_location TEXT NOT NULL DEFAULT '',
                added_by_run_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(chat_id, file_name),
                FOREIGN KEY(chat_id) REFERENCES chat_sessions(chat_id)
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
            CREATE INDEX IF NOT EXISTS idx_chat_documents_chat
            ON chat_documents (chat_id, created_at)
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
            CREATE TRIGGER IF NOT EXISTS trg_chat_documents_run_chat_insert
            BEFORE INSERT ON chat_documents
            WHEN NEW.added_by_run_id != '' AND NOT EXISTS (
                SELECT 1 FROM chat_runs
                WHERE run_id = NEW.added_by_run_id
                  AND chat_id = NEW.chat_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'chat_document run_id does not belong to chat_id');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_chat_documents_run_chat_update
            BEFORE UPDATE OF chat_id, added_by_run_id ON chat_documents
            WHEN NEW.added_by_run_id != '' AND NOT EXISTS (
                SELECT 1 FROM chat_runs
                WHERE run_id = NEW.added_by_run_id
                  AND chat_id = NEW.chat_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'chat_document run_id does not belong to chat_id');
            END
            """
        )
        logger.debug("文件对话本地权威表结构已确认: db_path=%s", normalized_path)
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
    """Repository for `chat_sessions`."""

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


class ChatDocumentRepository(_Repository):
    """Repository for `chat_documents`."""

    def add(
        self,
        *,
        chat_id: str,
        file_name: str,
        original_name: str = "",
        document_ref: str = "",
        external_location: str = "",
        added_by_run_id: str = "",
    ) -> ChatDocument:
        normalized_chat_id = _required_text(chat_id, name="chat_id")
        normalized_file_name = _required_text(file_name, name="file_name")
        normalized_added_by_run_id = _optional_text(added_by_run_id)
        now = _utc_now_iso()
        with self._connection() as connection:
            if normalized_added_by_run_id:
                run_row = connection.execute(
                    "SELECT chat_id FROM chat_runs WHERE run_id = ?",
                    (normalized_added_by_run_id,),
                ).fetchone()
                if run_row is None:
                    raise ValueError("added_by_run_id 对应的 chat_run 不存在")
                if run_row["chat_id"] != normalized_chat_id:
                    raise ValueError("chat_document run_id 不属于当前 chat_id")
            connection.execute(
                """
                INSERT INTO chat_documents (
                    chat_id, file_name, original_name, document_ref,
                    external_location, added_by_run_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, file_name) DO UPDATE SET
                    original_name = CASE
                        WHEN excluded.original_name != ''
                        THEN excluded.original_name
                        ELSE chat_documents.original_name
                    END,
                    document_ref = CASE
                        WHEN excluded.document_ref != ''
                        THEN excluded.document_ref
                        ELSE chat_documents.document_ref
                    END,
                    external_location = CASE
                        WHEN excluded.external_location != ''
                        THEN excluded.external_location
                        ELSE chat_documents.external_location
                    END,
                    added_by_run_id = CASE
                        WHEN excluded.added_by_run_id != ''
                        THEN excluded.added_by_run_id
                        ELSE chat_documents.added_by_run_id
                    END
                """,
                (
                    normalized_chat_id,
                    normalized_file_name,
                    _optional_text(original_name),
                    _optional_text(document_ref),
                    _optional_text(external_location),
                    normalized_added_by_run_id,
                    now,
                ),
            )
            logger.info(
                "绑定文件到本地对话: chat_id=%s file_name=%s original_name=%s added_by_run_id=%s",
                normalized_chat_id,
                normalized_file_name,
                _optional_text(original_name),
                normalized_added_by_run_id,
            )
            return self._get_with_connection(
                connection,
                chat_id=normalized_chat_id,
                file_name=normalized_file_name,
            )

    def list_by_chat(self, chat_id: str) -> tuple[ChatDocument, ...]:
        normalized_chat_id = _required_text(chat_id, name="chat_id")
        with _connection_scope(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM chat_documents
                WHERE chat_id = ?
                ORDER BY created_at ASC, file_name ASC
                """,
                (normalized_chat_id,),
            ).fetchall()
        return tuple(self._row(row) for row in rows)

    def _get_with_connection(
        self,
        connection: sqlite3.Connection,
        *,
        chat_id: str,
        file_name: str,
    ) -> ChatDocument:
        row = connection.execute(
            "SELECT * FROM chat_documents WHERE chat_id = ? AND file_name = ?",
            (chat_id, file_name),
        ).fetchone()
        if row is None:
            raise ValueError("chat_document 不存在")
        return self._row(row)

    @staticmethod
    def _row(row: sqlite3.Row) -> ChatDocument:
        return ChatDocument(
            chat_id=row["chat_id"],
            file_name=row["file_name"],
            original_name=row["original_name"],
            document_ref=row["document_ref"],
            external_location=row["external_location"],
            added_by_run_id=row["added_by_run_id"],
            created_at=row["created_at"],
        )


class ChatRunRepository(_Repository):
    """Repository for `chat_runs`."""

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
    """Repository for immutable request-time `chat_run_inputs` snapshots."""

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


class ChatMessageRepository(_Repository):
    """Repository for `chat_messages` and `chat_message_files`."""

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
