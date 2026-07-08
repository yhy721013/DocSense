"""Persistent resource leases for file-chat cleanup and recovery."""

from __future__ import annotations

import sqlite3

from app.services.chat.models import (
    LEASE_ACTIVE,
    LEASE_CLEANUP_FAILED,
    LEASE_CLEANUP_PENDING,
    LEASE_CLOSED,
    LEASE_OPEN_STATUSES,
    LEASE_PLANNED,
    LEASE_STATUSES,
    RESOURCE_TYPES,
    ChatResourceLease,
)
from app.services.chat.repositories import (
    _connection_scope,
    _optional_text,
    _required_text,
    _utc_now_iso,
    _validate_choice,
    ensure_chat_schema,
)


_LEASE_STATUS_TRANSITIONS = {
    LEASE_PLANNED: frozenset({LEASE_ACTIVE, LEASE_CLEANUP_PENDING, LEASE_CLOSED}),
    LEASE_ACTIVE: frozenset(
        {LEASE_CLEANUP_PENDING, LEASE_CLEANUP_FAILED, LEASE_CLOSED}
    ),
    LEASE_CLEANUP_PENDING: frozenset({LEASE_CLEANUP_FAILED, LEASE_CLOSED}),
    LEASE_CLEANUP_FAILED: frozenset({LEASE_CLEANUP_PENDING, LEASE_CLOSED}),
    LEASE_CLOSED: frozenset(),
}


class ChatResourceLeaseService:
    """Manage durable leases for chat workspaces, threads and bindings."""

    def __init__(self, db_path: str, *, initialize: bool = True) -> None:
        self.db_path = _required_text(db_path, name="db_path")
        if initialize:
            ensure_chat_schema(self.db_path)

    def begin(
        self,
        *,
        lease_id: str,
        chat_id: str,
        resource_type: str,
        run_id: str = "",
        external_ref: str = "",
    ) -> ChatResourceLease:
        normalized_lease_id = _required_text(lease_id, name="lease_id")
        normalized_chat_id = _required_text(chat_id, name="chat_id")
        normalized_type = _validate_choice(
            resource_type,
            name="resource_type",
            allowed=RESOURCE_TYPES,
        )
        now = _utc_now_iso()
        with _connection_scope(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM chat_resource_leases WHERE lease_id = ?",
                (normalized_lease_id,),
            ).fetchone()
            if existing is not None:
                self._reject_identity_conflict(
                    existing,
                    chat_id=normalized_chat_id,
                    run_id=_optional_text(run_id),
                    resource_type=normalized_type,
                )
                return self._row(existing)
            connection.execute(
                """
                INSERT INTO chat_resource_leases (
                    lease_id, chat_id, run_id, resource_type, external_ref,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_lease_id,
                    normalized_chat_id,
                    _optional_text(run_id),
                    normalized_type,
                    _optional_text(external_ref),
                    LEASE_PLANNED,
                    now,
                    now,
                ),
            )
            return self._get_with_connection(connection, normalized_lease_id)

    def activate(
        self,
        *,
        lease_id: str,
        external_ref: str = "",
    ) -> ChatResourceLease:
        normalized_lease_id = _required_text(lease_id, name="lease_id")
        with _connection_scope(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._get_with_connection(connection, normalized_lease_id)
            if current.status not in {LEASE_PLANNED, LEASE_ACTIVE}:
                raise ValueError(
                    f"illegal chat_resource_lease status transition: "
                    f"{current.status} -> {LEASE_ACTIVE}"
                )
            cursor = connection.execute(
                """
                UPDATE chat_resource_leases
                SET status = ?, external_ref = CASE
                        WHEN ? != '' THEN ? ELSE external_ref
                    END,
                    error_message = '',
                    updated_at = ?
                WHERE lease_id = ? AND status IN (?, ?)
                """,
                (
                    LEASE_ACTIVE,
                    _optional_text(external_ref),
                    _optional_text(external_ref),
                    _utc_now_iso(),
                    normalized_lease_id,
                    LEASE_PLANNED,
                    LEASE_ACTIVE,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("chat_resource_lease status was changed concurrently")
            return self._get_with_connection(connection, normalized_lease_id)

    def mark_cleanup_pending(self, lease_id: str) -> ChatResourceLease:
        return self._set_status(lease_id, status=LEASE_CLEANUP_PENDING)

    def mark_closed(self, lease_id: str) -> ChatResourceLease:
        return self._set_status(
            lease_id,
            status=LEASE_CLOSED,
            error_message="",
        )

    def record_cleanup_failure(
        self,
        *,
        lease_id: str,
        error_message: str,
    ) -> ChatResourceLease:
        return self._set_status(
            lease_id,
            status=LEASE_CLEANUP_FAILED,
            error_message=_required_text(error_message, name="error_message"),
        )

    def get(self, lease_id: str) -> ChatResourceLease | None:
        normalized_lease_id = _required_text(lease_id, name="lease_id")
        with _connection_scope(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM chat_resource_leases WHERE lease_id = ?",
                (normalized_lease_id,),
            ).fetchone()
        return self._row(row) if row is not None else None

    def list_open(self) -> tuple[ChatResourceLease, ...]:
        placeholders = ",".join("?" for _ in LEASE_OPEN_STATUSES)
        with _connection_scope(self.db_path) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM chat_resource_leases
                WHERE status IN ({placeholders})
                ORDER BY updated_at ASC
                """,
                tuple(sorted(LEASE_OPEN_STATUSES)),
            ).fetchall()
        return tuple(self._row(row) for row in rows)

    def _set_status(
        self,
        lease_id: str,
        *,
        status: str,
        error_message: str = "",
    ) -> ChatResourceLease:
        normalized_lease_id = _required_text(lease_id, name="lease_id")
        normalized_status = _validate_choice(
            status,
            name="status",
            allowed=LEASE_STATUSES,
        )
        with _connection_scope(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._get_with_connection(connection, normalized_lease_id)
            self._ensure_transition(
                current_status=current.status,
                next_status=normalized_status,
            )
            if current.status == normalized_status:
                return current
            cursor = connection.execute(
                """
                UPDATE chat_resource_leases
                SET status = ?, error_message = ?, updated_at = ?
                WHERE lease_id = ? AND status = ?
                """,
                (
                    normalized_status,
                    _optional_text(error_message),
                    _utc_now_iso(),
                    normalized_lease_id,
                    current.status,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("chat_resource_lease status was changed concurrently")
            return self._get_with_connection(connection, normalized_lease_id)

    def _get_with_connection(
        self,
        connection: sqlite3.Connection,
        lease_id: str,
    ) -> ChatResourceLease:
        row = connection.execute(
            "SELECT * FROM chat_resource_leases WHERE lease_id = ?",
            (lease_id,),
        ).fetchone()
        if row is None:
            raise ValueError("chat_resource_lease 不存在")
        return self._row(row)

    @staticmethod
    def _reject_identity_conflict(
        row: sqlite3.Row,
        *,
        chat_id: str,
        run_id: str,
        resource_type: str,
    ) -> None:
        if (
            row["chat_id"] != chat_id
            or row["run_id"] != run_id
            or row["resource_type"] != resource_type
        ):
            raise ValueError("lease_id 对应的资源租约身份冲突")

    @staticmethod
    def _ensure_transition(*, current_status: str, next_status: str) -> None:
        if current_status == next_status:
            return
        allowed = _LEASE_STATUS_TRANSITIONS[current_status]
        if next_status not in allowed:
            raise ValueError(
                f"illegal chat_resource_lease status transition: "
                f"{current_status} -> {next_status}"
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> ChatResourceLease:
        return ChatResourceLease(
            lease_id=row["lease_id"],
            chat_id=row["chat_id"],
            run_id=row["run_id"],
            resource_type=row["resource_type"],
            external_ref=row["external_ref"],
            status=row["status"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
