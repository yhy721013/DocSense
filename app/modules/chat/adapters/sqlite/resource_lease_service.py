"""用于文件对话清理与恢复的持久化资源租约。"""

from __future__ import annotations

import hashlib
import logging
import sqlite3

from app.modules.chat.domain.models import (
    LEASE_ACTIVE,
    LEASE_CLEANUP_FAILED,
    LEASE_CLEANUP_PENDING,
    LEASE_CLOSED,
    LEASE_OPEN_STATUSES,
    LEASE_PLANNED,
    LEASE_STATUSES,
    RESOURCE_THREAD,
    RESOURCE_TYPES,
    RUN_ACTIVE_STATUSES,
    SESSION_ACTIVE,
    ChatResourceLease,
)
from app.modules.chat.adapters.sqlite.repositories import (
    _connection_scope,
    _optional_text,
    _required_text,
    _utc_now_iso,
    _validate_choice,
    ensure_chat_schema,
)
from app.modules.chat.ports.persistence import (
    ChatResourceLeaseSessionUnavailableError,
)


logger = logging.getLogger(__name__)


_LEASE_STATUS_TRANSITIONS = {
    LEASE_PLANNED: frozenset({LEASE_ACTIVE, LEASE_CLEANUP_PENDING, LEASE_CLOSED}),
    LEASE_ACTIVE: frozenset(
        {LEASE_CLEANUP_PENDING, LEASE_CLEANUP_FAILED, LEASE_CLOSED}
    ),
    LEASE_CLEANUP_PENDING: frozenset({LEASE_CLEANUP_FAILED, LEASE_CLOSED}),
    LEASE_CLEANUP_FAILED: frozenset({LEASE_CLEANUP_PENDING, LEASE_CLOSED}),
    LEASE_CLOSED: frozenset(),
}


def _lease_log_token(lease_id: str) -> str:
    """生成只用于日志关联的短摘要，避免租约结构泄漏文件或远端资源身份。

    文档绑定租约的持久化主键会包含业务文件名；该值必须继续原样参与数据库幂等，
    但不得直接进入日志。固定长度 SHA-256 摘要既能关联同一租约的多条状态日志，
    又不会暴露文件名、文档引用或供应商位置。
    """

    normalized_lease_id = _required_text(lease_id, name="lease_id")
    return hashlib.sha256(normalized_lease_id.encode("utf-8")).hexdigest()[:12]


class ChatResourceLeaseService:
    """管理对话工作区、线程与绑定的持久化租约。"""

    def __init__(self, db_path: str, *, initialize: bool = True) -> None:
        self.db_path = _required_text(db_path, name="db_path")
        if initialize:
            ensure_chat_schema(self.db_path)
        logger.info(
            "文件对话资源租约服务已初始化: db_path=%s initialize=%s",
            self.db_path,
            initialize,
        )

    def begin(
        self,
        *,
        lease_id: str,
        conversation_id: str,
        resource_type: str,
        run_id: str = "",
        external_ref: str = "",
        require_active_session: bool = False,
        require_exclusive_title: bool = False,
    ) -> ChatResourceLease:
        normalized_lease_id = _required_text(lease_id, name="lease_id")
        normalized_conversation_id = _required_text(conversation_id, name="conversation_id")
        normalized_type = _validate_choice(
            resource_type,
            name="resource_type",
            allowed=RESOURCE_TYPES,
        )
        normalized_run_id = _optional_text(run_id)
        normalized_external_ref = _optional_text(external_ref)
        if not isinstance(require_active_session, bool):
            raise TypeError("require_active_session must be bool")
        if not isinstance(require_exclusive_title, bool):
            raise TypeError("require_exclusive_title must be bool")
        if require_exclusive_title and (
            normalized_run_id or normalized_type != RESOURCE_THREAD
        ):
            raise ValueError(
                "exclusive title lease must be a session-scoped thread lease"
            )
        now = _utc_now_iso()
        with _connection_scope(self.db_path) as connection:
            # 租约用于记录远端 workspace/thread/document binding 等副作用资源。
            # 即使后续业务失败，cleanup worker 也可以根据租约表重试清理。
            connection.execute("BEGIN IMMEDIATE")
            if require_active_session:
                session_row = connection.execute(
                    "SELECT status FROM conversations WHERE conversation_id = ?",
                    (normalized_conversation_id,),
                ).fetchone()
                if session_row is None:
                    raise ChatResourceLeaseSessionUnavailableError(
                        conversation_id=normalized_conversation_id,
                        status="missing",
                    )
                if session_row["status"] != SESSION_ACTIVE:
                    raise ChatResourceLeaseSessionUnavailableError(
                        conversation_id=normalized_conversation_id,
                        status=session_row["status"],
                    )
            if require_exclusive_title:
                active_run = connection.execute(
                    """
                    SELECT 1
                    FROM chat_runs
                    WHERE conversation_id = ? AND status IN (?, ?)
                    LIMIT 1
                    """,
                    (
                        normalized_conversation_id,
                        *tuple(sorted(RUN_ACTIVE_STATUSES)),
                    ),
                ).fetchone()
                if active_run is not None:
                    raise ChatResourceLeaseSessionUnavailableError(
                        conversation_id=normalized_conversation_id,
                        status="run_active",
                    )
                title_prefix = (
                    f"chat:{normalized_conversation_id}:temporary_thread:%"
                )
                active_title = connection.execute(
                    """
                    SELECT 1
                    FROM chat_resource_leases
                    WHERE conversation_id = ?
                      AND run_id = ''
                      AND resource_type = ?
                      AND lease_id LIKE ?
                      AND lease_id != ?
                      AND status IN (?, ?, ?)
                    LIMIT 1
                    """,
                    (
                        normalized_conversation_id,
                        RESOURCE_THREAD,
                        title_prefix,
                        normalized_lease_id,
                        LEASE_PLANNED,
                        LEASE_ACTIVE,
                        LEASE_CLEANUP_PENDING,
                    ),
                ).fetchone()
                if active_title is not None:
                    raise ChatResourceLeaseSessionUnavailableError(
                        conversation_id=normalized_conversation_id,
                        status="title_active",
                    )
            if normalized_run_id:
                run_row = connection.execute(
                    "SELECT conversation_id FROM chat_runs WHERE run_id = ?",
                    (normalized_run_id,),
                ).fetchone()
                if run_row is None:
                    raise ValueError("run_id 对应的 chat_run 不存在")
                if run_row["conversation_id"] != normalized_conversation_id:
                    raise ValueError("chat_resource_lease run_id 不属于当前 conversation_id")
            existing = connection.execute(
                "SELECT * FROM chat_resource_leases WHERE lease_id = ?",
                (normalized_lease_id,),
            ).fetchone()
            if existing is not None:
                self._reject_identity_conflict(
                    existing,
                    conversation_id=normalized_conversation_id,
                    run_id=normalized_run_id,
                    resource_type=normalized_type,
                    external_ref=normalized_external_ref,
                )
                if not existing["external_ref"] and normalized_external_ref:
                    if existing["status"] != LEASE_PLANNED:
                        raise ValueError(
                            "非 planned chat_resource_lease 不能补写 external_ref"
                        )
                    logger.info(
                        "已补充文件对话资源租约的远端引用: lease_token=%s conversation_id=%s resource_type=%s has_external_ref=%s",
                        _lease_log_token(normalized_lease_id),
                        normalized_conversation_id,
                        normalized_type,
                        True,
                    )
                    cursor = connection.execute(
                        """
                        UPDATE chat_resource_leases
                        SET external_ref = ?, updated_at = ?
                        WHERE lease_id = ? AND status = ? AND external_ref = ''
                        """,
                        (
                            normalized_external_ref,
                            now,
                            normalized_lease_id,
                            LEASE_PLANNED,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError(
                            "chat_resource_lease was changed concurrently"
                        )
                    return self._get_with_connection(connection, normalized_lease_id)
                logger.debug(
                    "文件对话资源租约已存在，直接复用: lease_token=%s conversation_id=%s status=%s",
                    _lease_log_token(normalized_lease_id),
                    normalized_conversation_id,
                    existing["status"],
                )
                return self._row(existing)
            connection.execute(
                """
                INSERT INTO chat_resource_leases (
                    lease_id, conversation_id, run_id, resource_type, external_ref,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_lease_id,
                    normalized_conversation_id,
                    normalized_run_id,
                    normalized_type,
                    normalized_external_ref,
                    LEASE_PLANNED,
                    now,
                    now,
                ),
            )
            logger.info(
                "已创建文件对话资源租约: lease_token=%s conversation_id=%s run_id=%s resource_type=%s has_external_ref=%s",
                _lease_log_token(normalized_lease_id),
                normalized_conversation_id,
                normalized_run_id,
                normalized_type,
                bool(normalized_external_ref),
            )
            return self._get_with_connection(connection, normalized_lease_id)

    def activate(
        self,
        *,
        lease_id: str,
        external_ref: str = "",
    ) -> ChatResourceLease:
        normalized_lease_id = _required_text(lease_id, name="lease_id")
        normalized_external_ref = _optional_text(external_ref)
        with _connection_scope(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._get_with_connection(connection, normalized_lease_id)
            if current.status == LEASE_ACTIVE:
                if (
                    normalized_external_ref
                    and normalized_external_ref != current.external_ref
                ):
                    raise ValueError("chat_resource_lease external_ref 冲突")
                logger.debug(
                    "文件对话资源租约已处于活动状态: lease_token=%s has_external_ref=%s",
                    _lease_log_token(normalized_lease_id),
                    bool(current.external_ref),
                )
                return current
            if current.status != LEASE_PLANNED:
                raise ValueError(
                    f"illegal chat_resource_lease status transition: "
                    f"{current.status} -> {LEASE_ACTIVE}"
                )
            resolved_external_ref = normalized_external_ref or current.external_ref
            if not resolved_external_ref:
                raise ValueError("激活 chat_resource_lease 时 external_ref 不能为空")
            cursor = connection.execute(
                """
                UPDATE chat_resource_leases
                SET status = ?, external_ref = ?, error_message = '',
                    updated_at = ?
                WHERE lease_id = ? AND status = ?
                """,
                (
                    LEASE_ACTIVE,
                    resolved_external_ref,
                    _utc_now_iso(),
                    normalized_lease_id,
                    LEASE_PLANNED,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("chat_resource_lease status was changed concurrently")
            logger.info(
                "已激活文件对话资源租约: lease_token=%s has_external_ref=%s",
                _lease_log_token(normalized_lease_id),
                bool(resolved_external_ref),
            )
            return self._get_with_connection(connection, normalized_lease_id)

    def ensure_active(
        self,
        *,
        lease_id: str,
        conversation_id: str,
        resource_type: str,
        run_id: str = "",
        external_ref: str,
    ) -> ChatResourceLease:
        """创建或复用活动租约，且不掩盖清理失败。

        本方法刻意保守：只会将 planned 租约提升为 active。失败或待清理状态会原样返回，
        以防调用方在“确保”仍需补偿的资源时意外覆盖恢复依据。
        """
        lease = self.begin(
            lease_id=lease_id,
            conversation_id=conversation_id,
            run_id=run_id,
            resource_type=resource_type,
            external_ref=external_ref,
        )
        if lease.status == LEASE_PLANNED:
            return self.activate(
                lease_id=lease.lease_id,
                external_ref=external_ref,
            )
        return lease

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

    def list_by_chat(
        self,
        conversation_id: str,
        *,
        include_closed: bool = True,
    ) -> tuple[ChatResourceLease, ...]:
        normalized_conversation_id = _required_text(conversation_id, name="conversation_id")
        with _connection_scope(self.db_path) as connection:
            if include_closed:
                rows = connection.execute(
                    """
                    SELECT * FROM chat_resource_leases
                    WHERE conversation_id = ?
                    ORDER BY resource_type ASC, created_at ASC, lease_id ASC
                    """,
                    (normalized_conversation_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM chat_resource_leases
                    WHERE conversation_id = ? AND status != ?
                    ORDER BY resource_type ASC, created_at ASC, lease_id ASC
                    """,
                    (normalized_conversation_id, LEASE_CLOSED),
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
                logger.debug(
                    "文件对话资源租约状态无需变更: lease_token=%s status=%s",
                    _lease_log_token(normalized_lease_id),
                    normalized_status,
                )
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
            logger.info(
                "文件对话资源租约状态变更: lease_token=%s previous_status=%s current_status=%s has_error=%s",
                _lease_log_token(normalized_lease_id),
                current.status,
                normalized_status,
                bool(_optional_text(error_message)),
            )
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
        conversation_id: str,
        run_id: str,
        resource_type: str,
        external_ref: str,
    ) -> None:
        if (
            row["conversation_id"] != conversation_id
            or (
                run_id
                and row["run_id"]
                and row["run_id"] != run_id
            )
            or row["resource_type"] != resource_type
            or (
                row["external_ref"]
                and external_ref
                and row["external_ref"] != external_ref
            )
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
            conversation_id=row["conversation_id"],
            run_id=row["run_id"],
            resource_type=row["resource_type"],
            external_ref=row["external_ref"],
            status=row["status"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
