"""Durable ChatRun locking for file-chat request isolation."""

from __future__ import annotations

import os
import socket
import uuid

import sqlite3
from datetime import datetime, timezone

from app.services.chat.domain.models import (
    RUN_ACCEPTED,
    RUN_ACTIVE_STATUSES,
    RUN_FAILED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    ChatRun,
)
from app.services.chat.persistence.repositories import (
    ChatRunRepository,
    _connection_scope,
    _optional_text,
    _required_text,
    _utc_now_iso,
    ensure_chat_schema,
)


DEFAULT_STALE_RUN_SECONDS = 6 * 60 * 60


def _default_owner_instance_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class ChatRunBusyError(RuntimeError):
    """Raised when a chat already has an active run."""

    def __init__(self, *, chat_id: str, active_run_id: str) -> None:
        super().__init__("current chat already has an active run")
        self.chat_id = chat_id
        self.active_run_id = active_run_id


class ChatRunLockService:
    """Acquire and release durable per-chat run ownership."""

    def __init__(
        self,
        db_path: str,
        *,
        owner_instance_id: str | None = None,
        stale_after_seconds: float | None = DEFAULT_STALE_RUN_SECONDS,
    ) -> None:
        self.db_path = _required_text(db_path, name="db_path")
        self.owner_instance_id = _optional_text(
            owner_instance_id,
        ) or _default_owner_instance_id()
        if stale_after_seconds is not None and stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive or None")
        self.stale_after_seconds = stale_after_seconds
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
            self._expire_stale_runs(
                connection,
                chat_id=normalized_chat_id,
                now=now,
                active_statuses=active_statuses,
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
                ) VALUES (?, ?, ?, ?, 0, ?, NULL, '', ?, NULL, NULL, ?)
                """,
                (
                    normalized_run_id,
                    normalized_chat_id,
                    normalized_request_id,
                    RUN_ACCEPTED,
                    self.owner_instance_id,
                    now,
                    now,
                ),
            )
            cursor = connection.execute(
                """
                UPDATE chat_runs
                SET status = ?,
                    heartbeat_at = ?,
                    started_at = ?,
                    updated_at = ?
                WHERE run_id = ? AND status = ?
                """,
                (
                    RUN_RUNNING,
                    now,
                    now,
                    now,
                    normalized_run_id,
                    RUN_ACCEPTED,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("chat_run status was changed concurrently")
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

    def abort_run(self, run_id: str) -> ChatRun:
        return self._runs.mark_aborted(run_id)

    def request_abort(self, run_id: str) -> ChatRun:
        return self._runs.request_abort(run_id)

    def heartbeat_run(self, run_id: str) -> ChatRun:
        normalized_run_id = _required_text(run_id, name="run_id")
        now = _utc_now_iso()
        active_statuses = tuple(sorted(RUN_ACTIVE_STATUSES))
        with _connection_scope(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM chat_runs WHERE run_id = ?",
                (normalized_run_id,),
            ).fetchone()
            if current is None:
                raise ValueError("chat_run 不存在")
            if current["status"] not in RUN_ACTIVE_STATUSES:
                return self._row(current)
            cursor = connection.execute(
                f"""
                UPDATE chat_runs
                SET heartbeat_at = ?, updated_at = ?
                WHERE run_id = ? AND status IN ({",".join("?" for _ in active_statuses)})
                """,
                (now, now, normalized_run_id, *active_statuses),
            )
            if cursor.rowcount != 1:
                raise ValueError("chat_run status was changed concurrently")
            row = connection.execute(
                "SELECT * FROM chat_runs WHERE run_id = ?",
                (normalized_run_id,),
            ).fetchone()
            if row is None:
                raise ValueError("chat_run 不存在")
            return self._row(row)

    def _expire_stale_runs(
        self,
        connection: sqlite3.Connection,
        *,
        chat_id: str,
        now: str,
        active_statuses: tuple[str, ...],
    ) -> None:
        if self.stale_after_seconds is None:
            return
        now_dt = _parse_utc(now)
        if now_dt is None:
            return
        rows = connection.execute(
            f"""
            SELECT * FROM chat_runs
            WHERE chat_id = ? AND status IN ({",".join("?" for _ in active_statuses)})
            """,
            (chat_id, *active_statuses),
        ).fetchall()
        stale_run_ids: list[str] = []
        for row in rows:
            last_seen = (
                _parse_utc(row["heartbeat_at"])
                or _parse_utc(row["updated_at"])
                or _parse_utc(row["created_at"])
            )
            if last_seen is None:
                continue
            age_seconds = (now_dt - last_seen).total_seconds()
            if age_seconds > self.stale_after_seconds:
                stale_run_ids.append(row["run_id"])

        for stale_run_id in stale_run_ids:
            connection.execute(
                f"""
                UPDATE chat_runs
                SET status = ?,
                    error_message = ?,
                    completed_at = ?,
                    updated_at = ?
                WHERE run_id = ? AND status IN ({",".join("?" for _ in active_statuses)})
                """,
                (
                    RUN_FAILED,
                    "chat run heartbeat expired",
                    now,
                    now,
                    stale_run_id,
                    *active_statuses,
                ),
            )

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
    "DEFAULT_STALE_RUN_SECONDS",
]
