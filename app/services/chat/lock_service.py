"""Durable ChatRun locking for file-chat request isolation."""

from __future__ import annotations

import os
import socket
import uuid

import sqlite3

from app.services.chat.models import (
    RUN_ACTIVE_STATUSES,
    RUN_FAILED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    ChatRun,
)
from app.services.chat.repositories import (
    ChatRunRepository,
    _connection_scope,
    _optional_text,
    _required_text,
    _utc_now_iso,
    ensure_chat_schema,
)


def _default_owner_instance_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class ChatRunBusyError(RuntimeError):
    """Raised when a chat already has an active run."""

    def __init__(self, *, chat_id: str, active_run_id: str) -> None:
        super().__init__("current chat already has an active run")
        self.chat_id = chat_id
        self.active_run_id = active_run_id


class ChatRunLockService:
    """Acquire and release durable per-chat run ownership."""

    def __init__(self, db_path: str, *, owner_instance_id: str | None = None) -> None:
        self.db_path = _required_text(db_path, name="db_path")
        self.owner_instance_id = _optional_text(
            owner_instance_id,
        ) or _default_owner_instance_id()
        ensure_chat_schema(self.db_path)
        self._runs = ChatRunRepository(self.db_path, initialize=False)

    def try_acquire_chat_run(
        self,
        *,
        chat_id: str,
        run_id: str | None = None,
        request_id: str | None = None,
    ) -> ChatRun:
        normalized_chat_id = _required_text(chat_id, name="chat_id")
        normalized_run_id = _optional_text(run_id) or uuid.uuid4().hex
        normalized_request_id = _optional_text(request_id) or normalized_run_id
        now = _utc_now_iso()
        active_statuses = tuple(sorted(RUN_ACTIVE_STATUSES))

        with _connection_scope(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO chat_sessions (
                    chat_id, workspace_ref, thread_ref, status,
                    created_at, updated_at, metadata_json
                ) VALUES (?, '', '', 'active', ?, ?, '{}')
                """,
                (normalized_chat_id, now, now),
            )
            active_row = connection.execute(
                f"""
                SELECT * FROM chat_runs
                WHERE chat_id = ? AND status IN ({",".join("?" for _ in active_statuses)})
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (normalized_chat_id, *active_statuses),
            ).fetchone()
            if active_row is not None:
                raise ChatRunBusyError(
                    chat_id=normalized_chat_id,
                    active_run_id=active_row["run_id"],
                )
            connection.execute(
                """
                INSERT INTO chat_runs (
                    run_id, chat_id, request_id, status, abort_requested,
                    owner_instance_id, heartbeat_at, error_message,
                    created_at, started_at, completed_at, updated_at
                ) VALUES (?, ?, ?, ?, 0, ?, ?, '', ?, ?, NULL, ?)
                """,
                (
                    normalized_run_id,
                    normalized_chat_id,
                    normalized_request_id,
                    RUN_RUNNING,
                    self.owner_instance_id,
                    now,
                    now,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM chat_runs WHERE run_id = ?",
                (normalized_run_id,),
            ).fetchone()
            if row is None:
                raise ValueError("chat_run was not created")
            return self._row(row)

    def complete_run(self, run_id: str) -> ChatRun:
        return self._runs.update_status(run_id=run_id, status=RUN_SUCCEEDED)

    def fail_run(self, run_id: str, *, error_message: str) -> ChatRun:
        return self._runs.update_status(
            run_id=run_id,
            status=RUN_FAILED,
            error_message=_required_text(error_message, name="error_message"),
        )

    def request_abort(self, run_id: str) -> ChatRun:
        return self._runs.request_abort(run_id)

    @staticmethod
    def _row(row: sqlite3.Row) -> ChatRun:
        return ChatRun(
            run_id=row["run_id"],
            chat_id=row["chat_id"],
            request_id=row["request_id"],
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


__all__ = [
    "ChatRunBusyError",
    "ChatRunLockService",
]
