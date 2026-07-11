"""Durable ChatRun locking for file-chat request isolation."""

from __future__ import annotations

import os
import socket
import uuid
import json

import logging
import sqlite3
from datetime import datetime, timezone

from app.services.chat.domain.models import (
    MESSAGE_COMMITTED,
    MESSAGE_PENDING,
    MESSAGE_ROLE_ASSISTANT,
    RUN_ABORTED,
    RUN_ACCEPTED,
    RUN_ACTIVE_STATUSES,
    RUN_FAILED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    SESSION_ACTIVE,
    SESSION_DELETED,
    SESSION_DELETING,
    SESSION_ERROR,
    ChatRun,
)
from app.services.chat.domain.events import ChatStreamEvent
from app.services.chat.locking.lease import (
    ChatRunLease,
    ChatRunLeaseCapabilities,
    ChatRunLeaseLostError,
    SINGLE_INSTANCE_CHAT_RUN_LEASE_CAPABILITIES,
)
from app.services.chat.persistence.event_repository import ChatRunEventRepository
from app.services.chat.persistence.repositories import (
    ChatRunRepository,
    _connection_scope,
    _optional_text,
    _required_text,
    _utc_now_iso,
    ensure_chat_schema,
)


DEFAULT_STALE_RUN_SECONDS = 6 * 60 * 60
logger = logging.getLogger(__name__)


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


class ChatRunInactiveError(RuntimeError):
    """Raised when an abort request races with a terminal chat run."""

    def __init__(self, *, run_id: str, status: str) -> None:
        super().__init__("chat run is no longer active")
        self.run_id = run_id
        self.status = status


class ChatSessionUnavailableError(RuntimeError):
    """Raised when a chat session is not allowed to accept a new run."""

    def __init__(self, *, chat_id: str, status: str) -> None:
        super().__init__("chat session is not active")
        self.chat_id = chat_id
        self.status = status


class ChatSessionDeleteBusyError(RuntimeError):
    """Raised when deletion races with an active run or another deletion."""

    def __init__(self, *, chat_id: str, reason: str) -> None:
        super().__init__(reason)
        self.chat_id = chat_id
        self.reason = reason


class ChatRunLockService:
    """SQLite 单实例 run 协调器及其兼容锁服务入口。

    名称保留是为了兼容已存在的应用装配与测试；它并不是分布式锁实现。共享
    持久化落地后，应由新的 ``ChatRunCoordinator`` 适配器替换本类，并以真实
    lease/fencing token 把心跳和终态提交收敛为条件更新。
    """

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
        logger.info(
            "文件对话SQLite单实例run协调器已初始化: db_path=%s owner=%s stale_after_seconds=%s",
            self.db_path,
            self.owner_instance_id,
            self.stale_after_seconds,
        )

    @property
    def lease_capabilities(self) -> ChatRunLeaseCapabilities:
        """返回当前 SQLite 实现的真实租约能力，明确不含跨实例 fencing。"""
        return SINGLE_INSTANCE_CHAT_RUN_LEASE_CAPABILITIES

    def try_acquire_chat_run(
        self,
        *,
        chat_id: str,
        run_id: str | None = None,
        user_message: str | None = None,
        user_files: tuple[tuple[str, str], ...] = (),
        input_documents: tuple[tuple[str, str, str, str], ...] = (),
    ) -> ChatRun:
        normalized_chat_id = _required_text(chat_id, name="chat_id")
        normalized_run_id = _optional_text(run_id) or uuid.uuid4().hex
        now = _utc_now_iso()
        active_statuses = tuple(sorted(RUN_ACTIVE_STATUSES))

        with _connection_scope(self.db_path) as connection:
            # BEGIN IMMEDIATE 在 SQLite 中会立即取得写锁，保证“检查活跃 run”和
            # “插入新 run”处于同一个临界区。后续替换为 PostgreSQL/Redis 时，
            # 这里就是分布式互斥语义的边界。
            connection.execute("BEGIN IMMEDIATE")
            logger.info(
                "尝试获取文件对话run锁: chat_id=%s run_id=%s owner=%s",
                normalized_chat_id,
                normalized_run_id,
                self.owner_instance_id,
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO chat_sessions (
                    chat_id, workspace_ref, thread_ref, status,
                    created_at, updated_at, metadata_json
                ) VALUES (?, '', '', 'active', ?, ?, '{}')
                """,
                (normalized_chat_id, now, now),
            )
            session_row = connection.execute(
                "SELECT status FROM chat_sessions WHERE chat_id = ?",
                (normalized_chat_id,),
            ).fetchone()
            if session_row is None:
                raise ValueError("chat_session was not created")
            if session_row["status"] != SESSION_ACTIVE:
                raise ChatSessionUnavailableError(
                    chat_id=normalized_chat_id,
                    status=session_row["status"],
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
                logger.warning(
                    "文件对话run锁获取失败，存在活跃run: chat_id=%s active_run_id=%s active_status=%s",
                    normalized_chat_id,
                    active_row["run_id"],
                    active_row["status"],
                )
                raise ChatRunBusyError(
                    chat_id=normalized_chat_id,
                    active_run_id=active_row["run_id"],
                )
            connection.execute(
                """
                INSERT INTO chat_runs (
                    run_id, chat_id, status, abort_requested,
                    owner_instance_id, heartbeat_at, error_message,
                    created_at, started_at, completed_at, updated_at
                ) VALUES (?, ?, ?, 0, ?, NULL, '', ?, NULL, NULL, ?)
                """,
                (
                    normalized_run_id,
                    normalized_chat_id,
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
            if user_message is not None:
                self._append_run_input(
                    connection,
                    run_id=normalized_run_id,
                    message=user_message,
                    documents=input_documents,
                    created_at=now,
                )
                self._append_user_pending(
                    connection,
                    chat_id=normalized_chat_id,
                    run_id=normalized_run_id,
                    message=user_message,
                    files=user_files,
                    created_at=now,
                )
            logger.info(
                "文件对话run锁获取成功: chat_id=%s run_id=%s owner=%s",
                normalized_chat_id,
                normalized_run_id,
                self.owner_instance_id,
            )
            return self._row(row)

    def begin_chat_deletion(self, *, chat_id: str) -> None:
        """Atomically block new runs and reject deletion while a run is active."""
        normalized_chat_id = _required_text(chat_id, name="chat_id")
        active_statuses = tuple(sorted(RUN_ACTIVE_STATUSES))
        now = _utc_now_iso()
        with _connection_scope(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                "SELECT status FROM chat_sessions WHERE chat_id = ?",
                (normalized_chat_id,),
            ).fetchone()
            if session is None:
                raise ValueError("chat_session 不存在")
            status = session["status"]
            if status == SESSION_DELETED:
                return
            if status == SESSION_DELETING:
                raise ChatSessionDeleteBusyError(
                    chat_id=normalized_chat_id,
                    reason="当前对话正在删除",
                )
            self._expire_stale_runs(
                connection,
                chat_id=normalized_chat_id,
                now=now,
                active_statuses=active_statuses,
            )
            active = connection.execute(
                f"""
                SELECT run_id FROM chat_runs
                WHERE chat_id = ? AND status IN ({",".join("?" for _ in active_statuses)})
                LIMIT 1
                """,
                (normalized_chat_id, *active_statuses),
            ).fetchone()
            if active is not None:
                raise ChatSessionDeleteBusyError(
                    chat_id=normalized_chat_id,
                    reason="当前对话存在进行中的流式响应，请先中断后删除",
                )
            if status not in {SESSION_ACTIVE, SESSION_ERROR}:
                raise ChatSessionUnavailableError(
                    chat_id=normalized_chat_id,
                    status=status,
                )
            cursor = connection.execute(
                """
                UPDATE chat_sessions
                SET status = ?, updated_at = ?
                WHERE chat_id = ? AND status = ?
                """,
                (SESSION_DELETING, now, normalized_chat_id, status),
            )
            if cursor.rowcount != 1:
                raise ValueError("chat_session status was changed concurrently")

    def complete_run_with_messages(
        self,
        *,
        run_id: str,
        user_message_id: str,
        assistant_message_id: str,
        assistant_content: str,
        terminal_event: ChatStreamEvent | None = None,
    ) -> ChatRun:
        """Commit one successful turn and its terminal run state atomically."""
        return self._finish_run_with_user(
            run_id=run_id,
            user_message_id=user_message_id,
            terminal_status=RUN_SUCCEEDED,
            error_message="",
            assistant_message_id=assistant_message_id,
            assistant_content=assistant_content,
            terminal_event=terminal_event,
        )

    def fail_run_with_user(
        self,
        *,
        run_id: str,
        user_message_id: str,
        error_message: str,
        terminal_event: ChatStreamEvent | None = None,
    ) -> ChatRun:
        return self._finish_run_with_user(
            run_id=run_id,
            user_message_id=user_message_id,
            terminal_status=RUN_FAILED,
            error_message=_required_text(error_message, name="error_message"),
            terminal_event=terminal_event,
        )

    def abort_run_with_user(
        self,
        *,
        run_id: str,
        user_message_id: str,
        terminal_event: ChatStreamEvent | None = None,
    ) -> ChatRun:
        return self._finish_run_with_user(
            run_id=run_id,
            user_message_id=user_message_id,
            terminal_status=RUN_ABORTED,
            error_message="",
            terminal_event=terminal_event,
        )

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
        normalized_run_id = _required_text(run_id, name="run_id")
        try:
            return self._runs.request_abort(normalized_run_id)
        except ValueError as exc:
            current = self._runs.get(normalized_run_id)
            if current is not None and current.status not in RUN_ACTIVE_STATUSES:
                raise ChatRunInactiveError(
                    run_id=normalized_run_id,
                    status=current.status,
                ) from exc
            raise

    def issue_execution_lease(self, *, run_id: str) -> ChatRunLease:
        """为当前单实例执行器生成内部运行权证明。

        SQLite 适配器不会生成可跨进程校验的 token；返回空 token/fencing 字段是
        有意为之，调用方必须结合 ``lease_capabilities`` 判断部署能力。这里仍
        校验 run 归属，避免错误实例误用同一个内部 run 标识。
        """
        run = self._get_run_for_execution_lease(run_id)
        return ChatRunLease(
            run_id=run.run_id,
            chat_id=run.chat_id,
            owner_instance_id=run.owner_instance_id,
        )

    def validate_execution_lease(self, *, lease: ChatRunLease) -> ChatRun:
        """校验一个内部执行租约仍对应本实例的活跃 run。

        当前校验与后续写入之间无法构成跨实例 fencing 原子条件，因此只可用于
        ``single_instance`` 模式。未来适配器必须把 lease/fencing 条件合并到
        heartbeat 和终态 UPDATE 中。
        """
        if not isinstance(lease, ChatRunLease):
            raise TypeError("lease must be ChatRunLease")
        run = self._runs.get(lease.run_id)
        if run is None:
            raise ChatRunLeaseLostError(
                run_id=lease.run_id,
                reason="run does not exist",
            )
        if run.chat_id != lease.chat_id:
            raise ChatRunLeaseLostError(
                run_id=lease.run_id,
                reason="chat_id does not match",
            )
        if run.owner_instance_id != lease.owner_instance_id:
            raise ChatRunLeaseLostError(
                run_id=lease.run_id,
                reason="owner_instance_id does not match",
            )
        if run.owner_instance_id != self.owner_instance_id:
            raise ChatRunLeaseLostError(
                run_id=lease.run_id,
                reason="run is owned by another instance",
            )
        if run.status not in RUN_ACTIVE_STATUSES:
            raise ChatRunLeaseLostError(
                run_id=lease.run_id,
                reason=f"run is no longer active: {run.status}",
            )
        return run

    def heartbeat_execution_lease(self, *, lease: ChatRunLease) -> ChatRun:
        """通过执行租约刷新心跳，保留未来 fencing 条件更新的稳定签名。"""
        self.validate_execution_lease(lease=lease)
        return self.heartbeat_run(lease.run_id)

    def complete_run_with_execution_lease(
        self,
        *,
        lease: ChatRunLease,
        user_message_id: str,
        assistant_message_id: str,
        assistant_content: str,
        terminal_event: ChatStreamEvent | None = None,
    ) -> ChatRun:
        """通过执行租约提交成功终态及完整助手消息。"""
        self.validate_execution_lease(lease=lease)
        return self.complete_run_with_messages(
            run_id=lease.run_id,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            assistant_content=assistant_content,
            terminal_event=terminal_event,
        )

    def fail_run_with_execution_lease(
        self,
        *,
        lease: ChatRunLease,
        user_message_id: str,
        error_message: str,
        terminal_event: ChatStreamEvent | None = None,
    ) -> ChatRun:
        """通过执行租约提交失败终态，并按既有规则保留 user 消息。"""
        self.validate_execution_lease(lease=lease)
        return self.fail_run_with_user(
            run_id=lease.run_id,
            user_message_id=user_message_id,
            error_message=error_message,
            terminal_event=terminal_event,
        )

    def abort_run_with_execution_lease(
        self,
        *,
        lease: ChatRunLease,
        user_message_id: str,
        terminal_event: ChatStreamEvent | None = None,
    ) -> ChatRun:
        """通过执行租约提交中断终态，并丢弃未完成助手输出。"""
        self.validate_execution_lease(lease=lease)
        return self.abort_run_with_user(
            run_id=lease.run_id,
            user_message_id=user_message_id,
            terminal_event=terminal_event,
        )

    def expire_stale_runs_for_chat(self, *, chat_id: str) -> tuple[ChatRun, ...]:
        """Mark stale active runs for one chat as failed and return them.

        `/llm/chat` already expires stale runs before acquiring a new lock. Abort
        must apply the same rule before reporting that an active stream can be
        interrupted; otherwise a crashed worker would receive `aborted: true`
        even though no executor remains to observe the abort flag.
        """
        normalized_chat_id = _required_text(chat_id, name="chat_id")
        if self.stale_after_seconds is None:
            return ()
        now = _utc_now_iso()
        active_statuses = tuple(sorted(RUN_ACTIVE_STATUSES))
        with _connection_scope(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            stale_run_ids = self._expire_stale_runs(
                connection,
                chat_id=normalized_chat_id,
                now=now,
                active_statuses=active_statuses,
            )
            if not stale_run_ids:
                return ()
            placeholders = ",".join("?" for _ in stale_run_ids)
            rows = connection.execute(
                f"""
                SELECT * FROM chat_runs
                WHERE run_id IN ({placeholders})
                ORDER BY created_at ASC
                """,
                stale_run_ids,
            ).fetchall()
        return tuple(self._row(row) for row in rows)

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
                logger.debug(
                    "跳过非活跃run心跳: run_id=%s status=%s",
                    normalized_run_id,
                    current["status"],
                )
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
            logger.debug(
                "文件对话run心跳写入成功: run_id=%s status=%s",
                normalized_run_id,
                row["status"],
            )
            return self._row(row)

    def _expire_stale_runs(
        self,
        connection: sqlite3.Connection,
        *,
        chat_id: str,
        now: str,
        active_statuses: tuple[str, ...],
    ) -> tuple[str, ...]:
        if self.stale_after_seconds is None:
            return ()
        now_dt = _parse_utc(now)
        if now_dt is None:
            return ()
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
            logger.warning(
                "文件对话run心跳超时，标记失败释放锁: chat_id=%s run_id=%s stale_after_seconds=%s",
                chat_id,
                stale_run_id,
                self.stale_after_seconds,
            )
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
        return tuple(stale_run_ids)

    def _get_run_for_execution_lease(self, run_id: str) -> ChatRun:
        """读取并校验当前实例可以为其签发内部执行租约的活跃 run。"""
        normalized_run_id = _required_text(run_id, name="run_id")
        run = self._runs.get(normalized_run_id)
        if run is None:
            raise ChatRunLeaseLostError(
                run_id=normalized_run_id,
                reason="run does not exist",
            )
        if run.owner_instance_id != self.owner_instance_id:
            raise ChatRunLeaseLostError(
                run_id=normalized_run_id,
                reason="run is owned by another instance",
            )
        if run.status not in RUN_ACTIVE_STATUSES:
            raise ChatRunLeaseLostError(
                run_id=normalized_run_id,
                reason=f"run is no longer active: {run.status}",
            )
        return run

    @staticmethod
    def _append_run_input(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        message: str,
        documents: tuple[tuple[str, str, str, str], ...],
        created_at: str,
    ) -> None:
        normalized_message = _required_text(message, name="user_message")
        normalized_documents = tuple(
            {
                "file_name": _required_text(file_name, name="file_name"),
                "original_name": _required_text(
                    original_name,
                    name="original_name",
                ),
                "document_ref": _required_text(
                    document_ref,
                    name="document_ref",
                ),
                "external_location": _optional_text(external_location),
            }
            for file_name, original_name, document_ref, external_location in documents
        )
        if len({item["file_name"] for item in normalized_documents}) != len(
            normalized_documents
        ):
            raise ValueError("input_documents contains duplicate file_name")
        connection.execute(
            """
            INSERT INTO chat_run_inputs (run_id, message, files_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                run_id,
                normalized_message,
                json.dumps(
                    normalized_documents,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                created_at,
            ),
        )

    @staticmethod
    def _append_user_pending(
        connection: sqlite3.Connection,
        *,
        chat_id: str,
        run_id: str,
        message: str,
        files: tuple[tuple[str, str], ...],
        created_at: str,
    ) -> None:
        normalized_message = _required_text(message, name="user_message")
        normalized_files = tuple(
            (_required_text(file_name, name="file_name"), _optional_text(original_name))
            for file_name, original_name in files
        )
        if len({file_name for file_name, _ in normalized_files}) != len(normalized_files):
            raise ValueError("user_files contains duplicate file_name")
        sequence_row = connection.execute(
            """
            SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next_sequence
            FROM chat_messages WHERE chat_id = ?
            """,
            (chat_id,),
        ).fetchone()
        message_id = f"{run_id}:user"
        connection.execute(
            """
            INSERT INTO chat_messages (
                message_id, chat_id, run_id, role, content,
                status, sequence_no, created_at
            ) VALUES (?, ?, ?, 'user', ?, ?, ?, ?)
            """,
            (
                message_id,
                chat_id,
                run_id,
                normalized_message,
                MESSAGE_PENDING,
                int(sequence_row["next_sequence"]),
                created_at,
            ),
        )
        for file_name, original_name in normalized_files:
            connection.execute(
                """
                INSERT INTO chat_message_files (message_id, file_name, original_name)
                VALUES (?, ?, ?)
                """,
                (message_id, file_name, original_name),
            )

    def _finish_run_with_user(
        self,
        *,
        run_id: str,
        user_message_id: str,
        terminal_status: str,
        error_message: str,
        assistant_message_id: str = "",
        assistant_content: str = "",
        terminal_event: ChatStreamEvent | None = None,
    ) -> ChatRun:
        normalized_run_id = _required_text(run_id, name="run_id")
        normalized_user_message_id = _required_text(
            user_message_id,
            name="user_message_id",
        )
        normalized_terminal_event = self._validate_terminal_event(
            terminal_event=terminal_event,
            terminal_status=terminal_status,
        )
        now = _utc_now_iso()
        with _connection_scope(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._get_run_with_connection(connection, normalized_run_id)
            if run.status not in RUN_ACTIVE_STATUSES:
                if run.status == terminal_status:
                    return run
                raise ChatRunInactiveError(
                    run_id=normalized_run_id,
                    status=run.status,
                )
            user = connection.execute(
                """
                SELECT status FROM chat_messages
                WHERE message_id = ? AND chat_id = ? AND run_id = ? AND role = 'user'
                """,
                (normalized_user_message_id, run.chat_id, normalized_run_id),
            ).fetchone()
            if user is None:
                raise ValueError("chat run user message 不存在")
            if user["status"] == MESSAGE_PENDING:
                cursor = connection.execute(
                    """
                    UPDATE chat_messages SET status = ?
                    WHERE message_id = ? AND status = ?
                    """,
                    (MESSAGE_COMMITTED, normalized_user_message_id, MESSAGE_PENDING),
                )
                if cursor.rowcount != 1:
                    raise ValueError("chat user message was changed concurrently")
            elif user["status"] != MESSAGE_COMMITTED:
                raise ValueError("chat run user message is not committable")
            if terminal_status == RUN_SUCCEEDED and assistant_content:
                existing = connection.execute(
                    "SELECT message_id FROM chat_messages WHERE message_id = ?",
                    (assistant_message_id,),
                ).fetchone()
                if existing is None:
                    sequence_row = connection.execute(
                        """
                        SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next_sequence
                        FROM chat_messages WHERE chat_id = ?
                        """,
                        (run.chat_id,),
                    ).fetchone()
                    connection.execute(
                        """
                        INSERT INTO chat_messages (
                            message_id, chat_id, run_id, role, content,
                            status, sequence_no, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            _required_text(assistant_message_id, name="assistant_message_id"),
                            run.chat_id,
                            normalized_run_id,
                            MESSAGE_ROLE_ASSISTANT,
                            assistant_content,
                            MESSAGE_COMMITTED,
                            int(sequence_row["next_sequence"]),
                            now,
                        ),
                    )
            cursor = connection.execute(
                """
                UPDATE chat_runs
                SET status = ?, error_message = ?, completed_at = ?, updated_at = ?
                WHERE run_id = ? AND status = ?
                """,
                (
                    terminal_status,
                    _optional_text(error_message),
                    now,
                    now,
                    normalized_run_id,
                    run.status,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("chat_run status was changed concurrently")
            if normalized_terminal_event is not None:
                ChatRunEventRepository.append_in_transaction(
                    connection=connection,
                    run_id=normalized_run_id,
                    event=normalized_terminal_event,
                )
            row = connection.execute(
                "SELECT * FROM chat_runs WHERE run_id = ?",
                (normalized_run_id,),
            ).fetchone()
            if row is None:
                raise ValueError("chat_run 不存在")
            return self._row(row)

    @staticmethod
    def _validate_terminal_event(
        *,
        terminal_event: ChatStreamEvent | None,
        terminal_status: str,
    ) -> ChatStreamEvent | None:
        if terminal_event is None:
            return None
        if not isinstance(terminal_event, ChatStreamEvent):
            raise TypeError("terminal_event must be ChatStreamEvent")
        expected_type = {
            RUN_SUCCEEDED: "done",
            RUN_FAILED: "error",
            RUN_ABORTED: "aborted",
        }.get(terminal_status)
        if expected_type is None:
            raise ValueError("terminal_status does not support a stream event")
        if terminal_event.event_type != expected_type:
            raise ValueError(
                "terminal_event type does not match the requested run terminal status"
            )
        return terminal_event

    @staticmethod
    def _get_run_with_connection(
        connection: sqlite3.Connection,
        run_id: str,
    ) -> ChatRun:
        row = connection.execute(
            "SELECT * FROM chat_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ValueError("chat_run 不存在")
        return ChatRunLockService._row(row)

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


__all__ = [
    "ChatRunBusyError",
    "ChatRunInactiveError",
    "ChatRunLockService",
    "ChatSessionDeleteBusyError",
    "ChatSessionUnavailableError",
    "DEFAULT_STALE_RUN_SECONDS",
]
