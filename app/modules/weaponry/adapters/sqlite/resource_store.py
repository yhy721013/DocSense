"""Weaponry 外部资源事实的 SQLite CAS Store。"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.tasks.adapters.sqlite.transaction import SQLiteTransactionManager
from app.modules.weaponry.ports import (
    AcquireWeaponryCleanupLease,
    CompleteWeaponryResourceCleanup,
    IdempotentOperationResult,
    PrepareWeaponryResourceCleanup,
    QuarantineWeaponryResources,
    RegisterWeaponryResource,
    ReleaseWeaponryCleanupLease,
    WeaponryCleanupLease,
    WeaponryCleanupLeaseAcquireOutcome,
    WeaponryCleanupLeaseAcquireResult,
    WeaponryPortStateError,
    WeaponryResourceCleanupOutcome,
    WeaponryResourceKind,
    WeaponryResourceOwnership,
    WeaponryResourceRecord,
    WeaponryResourceRecordState,
    WeaponryTrackedResource,
    WeaponryTrackedResourceState,
)


logger = logging.getLogger(__name__)


class SQLiteWeaponryResourceStoreAdapter:
    """按 task_id 保存资源快照，并以 ``version`` 实现条件更新。

    Store 只处理本地事实和清理租约，不调用 AnythingLLM。外部 delete 必须由资源恢复用例在
    事务外执行，然后带 lease/fencing 和期望版本调用 ``complete_cleanup``。这条边界可避免
    慢网络占用 SQLite 写锁，并为阶段 3 平移到 MySQL Repository 保留稳定 Port。
    """

    def __init__(
        self,
        db_path: str | None = None,
        *,
        transaction_manager: SQLiteTransactionManager | None = None,
        connection: sqlite3.Connection | None = None,
        cleanup_lease_seconds: float = 120.0,
        retry_delay_seconds: float = 30.0,
        busy_timeout_ms: int = 30_000,
    ) -> None:
        modes = sum(value is not None for value in (db_path, transaction_manager, connection))
        if modes != 1:
            raise ValueError("db_path、transaction_manager 与 connection 必须且只能提供一个")
        if db_path is not None and (not isinstance(db_path, str) or not db_path.strip()):
            raise ValueError("db_path 必须是非空 str")
        if connection is not None and not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection 必须是 sqlite3.Connection")
        if transaction_manager is not None and not isinstance(
            transaction_manager,
            SQLiteTransactionManager,
        ):
            raise TypeError("transaction_manager 必须是 SQLiteTransactionManager")
        for name, value in (
            ("cleanup_lease_seconds", cleanup_lease_seconds),
            ("retry_delay_seconds", retry_delay_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} 必须是正有限数字")
            normalized = float(value)
            if (
                normalized != normalized
                or normalized in (float("inf"), float("-inf"))
                or normalized <= 0.0
            ):
                raise ValueError(f"{name} 必须是正有限数字")
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or busy_timeout_ms < 1
        ):
            raise ValueError("busy_timeout_ms 必须是正整数")
        self._db_path = str(Path(db_path)) if db_path is not None else ""
        self._borrowed_connection = connection
        self._transactions = transaction_manager
        self._strict_control_mode = db_path is None
        self._cleanup_lease_seconds = float(cleanup_lease_seconds)
        self._retry_delay_seconds = float(retry_delay_seconds)
        self._busy_timeout_ms = busy_timeout_ms
        if db_path is not None:
            self._initialize_schema()

    @classmethod
    def from_connection(
        cls,
        connection: sqlite3.Connection,
        *,
        cleanup_lease_seconds: float = 120.0,
        retry_delay_seconds: float = 30.0,
    ) -> "SQLiteWeaponryResourceStoreAdapter":
        return cls(
            connection=connection,
            cleanup_lease_seconds=cleanup_lease_seconds,
            retry_delay_seconds=retry_delay_seconds,
        )

    def create(self, record: WeaponryResourceRecord) -> WeaponryResourceRecord:
        if not isinstance(record, WeaponryResourceRecord):
            raise TypeError("record 必须是 WeaponryResourceRecord")
        if record.version != 0 or record.state is not WeaponryResourceRecordState.TRACKING:
            raise ValueError("新资源记录必须从 tracking/version=0 开始")
        payload = self._encode_record(record)
        with self._transaction() as connection:
            if self._strict_control_mode:
                execution = connection.execute(
                    """
                    SELECT business_type, business_key FROM llm_task_executions
                    WHERE execution_id = ?
                    """,
                    (record.task_id.value,),
                ).fetchone()
                if (
                    execution is None
                    or execution["business_type"] != "weaponry"
                    or execution["business_key"] != record.business_ref.business_key
                ):
                    raise WeaponryPortStateError(
                        "resource_execution_identity_mismatch",
                        "资源记录与 Weaponry execution 身份不一致",
                    )
            existing = self._select(connection, record.task_id)
            if existing is not None:
                decoded = self._decode_row(existing)
                if decoded != record:
                    raise WeaponryPortStateError(
                        "resource_record_exists",
                        "task_id 已存在不同资源记录",
                    )
                return decoded
            connection.execute(
                """
                INSERT INTO weaponry_resource_records (
                    task_id, business_key, state, version, next_retry_at,
                    payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.task_id.value,
                    record.business_ref.business_key,
                    record.state.value,
                    record.version,
                    record.next_retry_at,
                    payload,
                    self._now_text(),
                    self._now_text(),
                ),
            )
        logger.info("武器谱资源记录已创建: task_id=%s", record.task_id.value)
        return record

    def get(self, task_id: TaskId) -> WeaponryResourceRecord | None:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        with self._read_connection() as connection:
            row = self._select(connection, task_id)
        return self._decode_row(row) if row is not None else None

    def register(self, command: RegisterWeaponryResource) -> WeaponryResourceRecord:
        if not isinstance(command, RegisterWeaponryResource):
            raise TypeError("command 必须是 RegisterWeaponryResource")
        with self._transaction() as connection:
            current = self._require_record(connection, command.task_id)
            resource = command.resource
            if resource.call_id and not resource.call_id.startswith(
                f"weaponry:{command.task_id.value}:"
            ):
                raise WeaponryPortStateError(
                    "resource_call_identity_mismatch",
                    "资源 call_id 不属于当前 task_id",
                )
            existing = next(
                (
                    item
                    for item in current.resources
                    if item.resource_id == resource.resource_id
                    or item.idempotency_key == resource.idempotency_key
                ),
                None,
            )
            if existing is not None:
                if existing != resource:
                    raise WeaponryPortStateError(
                        "resource_registration_conflict",
                        "资源 ID 或幂等键已绑定不同事实",
                    )
                return current
            self._require_version(current, command.expected_version)
            if current.state is not WeaponryResourceRecordState.TRACKING:
                raise WeaponryPortStateError(
                    "resource_record_not_tracking",
                    "清理开始后不得登记新资源",
                )
            updated = replace(
                current,
                resources=current.resources + (resource,),
                version=current.version + 1,
            )
            self._save(connection, updated, expected_version=current.version)
        logger.info(
            "武器谱资源已登记: task_id=%s kind=%s ownership=%s version=%d",
            command.task_id.value,
            resource.kind.value,
            resource.ownership.value,
            updated.version,
        )
        return updated

    def prepare_cleanup(
        self,
        command: PrepareWeaponryResourceCleanup,
    ) -> WeaponryResourceRecord:
        if not isinstance(command, PrepareWeaponryResourceCleanup):
            raise TypeError("command 必须是 PrepareWeaponryResourceCleanup")
        with self._transaction() as connection:
            current = self._require_record(connection, command.task_id)
            if current.state in {
                WeaponryResourceRecordState.CLEANUP_PENDING,
                WeaponryResourceRecordState.CLEANED,
            }:
                return current
            if current.state is WeaponryResourceRecordState.QUARANTINED:
                raise WeaponryPortStateError(
                    "resource_record_quarantined",
                    "隔离资源不得自动重新进入清理",
                )
            self._require_version(current, command.expected_version)
            resources = tuple(
                replace(item, state=WeaponryTrackedResourceState.CLEANUP_PENDING)
                if item.ownership is WeaponryResourceOwnership.OWNED
                and item.state is WeaponryTrackedResourceState.ACTIVE
                else item
                for item in current.resources
            )
            all_cleaned = self._all_owned_cleaned(resources)
            updated = replace(
                current,
                resources=resources,
                state=(
                    WeaponryResourceRecordState.CLEANED
                    if all_cleaned
                    else WeaponryResourceRecordState.CLEANUP_PENDING
                ),
                # 首次进入 cleanup_pending 必须立即可扫描；这里只冻结清理意图，
                # 尚未发生外部 DELETE，因此不存在失败退避或未知结果语义。
                next_retry_at=("" if all_cleaned else self._now_text()),
                version=current.version + 1,
            )
            self._save(connection, updated, expected_version=current.version)
        logger.info(
            "武器谱资源已准备清理: task_id=%s state=%s owned_pending=%d",
            command.task_id.value,
            updated.state.value,
            len(updated.owned_cleanup_candidates),
        )
        return updated

    def acquire_cleanup(
        self,
        command: AcquireWeaponryCleanupLease,
    ) -> WeaponryCleanupLeaseAcquireResult:
        if not isinstance(command, AcquireWeaponryCleanupLease):
            raise TypeError("command 必须是 AcquireWeaponryCleanupLease")
        with self._transaction() as connection:
            current = self._require_record(connection, command.task_id)
            if current.state is not WeaponryResourceRecordState.CLEANUP_PENDING:
                return WeaponryCleanupLeaseAcquireResult(
                    WeaponryCleanupLeaseAcquireOutcome.NOT_READY
                )
            now = self._now()
            if current.next_retry_at and self._parse_time(current.next_retry_at) > now:
                return WeaponryCleanupLeaseAcquireResult(
                    WeaponryCleanupLeaseAcquireOutcome.NOT_READY
                )
            if (
                current.cleanup_lease is not None
                and self._parse_time(current.cleanup_lease.deadline_at) > now
            ):
                return WeaponryCleanupLeaseAcquireResult(
                    WeaponryCleanupLeaseAcquireOutcome.BUSY
                )
            self._require_version(current, command.expected_version)
            fencing = current.cleanup_fencing_token + 1
            lease = WeaponryCleanupLease(
                task_id=current.task_id,
                token=secrets.token_hex(24),
                fencing_token=fencing,
                deadline_at=(
                    now + timedelta(seconds=self._cleanup_lease_seconds)
                ).isoformat(),
            )
            updated = replace(
                current,
                cleanup_lease=lease,
                cleanup_fencing_token=fencing,
                version=current.version + 1,
            )
            self._save(connection, updated, expected_version=current.version)
        logger.info(
            "武器谱资源清理租约已取得: task_id=%s fencing_token=%d",
            command.task_id.value,
            lease.fencing_token,
        )
        return WeaponryCleanupLeaseAcquireResult(
            WeaponryCleanupLeaseAcquireOutcome.ACQUIRED,
            lease,
        )

    def complete_cleanup(
        self,
        command: CompleteWeaponryResourceCleanup,
    ) -> WeaponryResourceRecord:
        if not isinstance(command, CompleteWeaponryResourceCleanup):
            raise TypeError("command 必须是 CompleteWeaponryResourceCleanup")
        with self._transaction() as connection:
            current = self._require_record(connection, command.task_id)
            resource = next(
                (
                    item
                    for item in current.resources
                    if item.resource_id == command.resource_id
                ),
                None,
            )
            if resource is None:
                raise WeaponryPortStateError(
                    "resource_not_found",
                    "待清理资源不存在",
                )
            if resource.ownership is WeaponryResourceOwnership.SHARED:
                raise WeaponryPortStateError(
                    "shared_resource_cleanup_forbidden",
                    "shared 资源禁止由任务清理",
                )
            if (
                resource.state is WeaponryTrackedResourceState.CLEANED
                and command.outcome is WeaponryResourceCleanupOutcome.SUCCEEDED
            ):
                return current
            self._require_version(current, command.expected_version)
            self._require_active_lease(
                current,
                command.lease,
                require_unexpired=True,
            )
            if resource.state is WeaponryTrackedResourceState.CLEANUP_UNKNOWN:
                raise WeaponryPortStateError(
                    "resource_cleanup_outcome_unknown",
                    "结果未知资源必须先对账或隔离，禁止直接重试",
                )
            target_state = {
                WeaponryResourceCleanupOutcome.SUCCEEDED: WeaponryTrackedResourceState.CLEANED,
                WeaponryResourceCleanupOutcome.FAILED: WeaponryTrackedResourceState.CLEANUP_PENDING,
                WeaponryResourceCleanupOutcome.OUTCOME_UNKNOWN: WeaponryTrackedResourceState.CLEANUP_UNKNOWN,
            }[command.outcome]
            resources = tuple(
                replace(item, state=target_state)
                if item.resource_id == command.resource_id
                else item
                for item in current.resources
            )
            all_cleaned = self._all_owned_cleaned(resources)
            succeeded = command.outcome is WeaponryResourceCleanupOutcome.SUCCEEDED
            updated = replace(
                current,
                resources=resources,
                state=(
                    WeaponryResourceRecordState.CLEANED
                    if all_cleaned
                    else WeaponryResourceRecordState.CLEANUP_PENDING
                ),
                cleanup_lease=(None if all_cleaned else current.cleanup_lease),
                retry_count=current.retry_count + (0 if succeeded else 1),
                # 成功清理一项后允许下一轮继续处理剩余资源；明确失败则写入持久
                # 冷却水位，避免进程重启或高频维护扫描造成热循环。结果未知会在
                # Application 紧接着隔离，仍按非成功分支保守记录。
                next_retry_at=(
                    ""
                    if all_cleaned
                    else (
                        self._now_text()
                        if succeeded
                        else self._after_seconds_text(self._retry_delay_seconds)
                    )
                ),
                last_error_code=("" if succeeded else command.error_code),
                last_error_message=(
                    "" if succeeded else "外部资源清理未成功，等待恢复处理"
                ),
                version=current.version + 1,
            )
            self._save(connection, updated, expected_version=current.version)
        logger.info(
            "武器谱单项资源清理事实已提交: task_id=%s outcome=%s state=%s",
            command.task_id.value,
            command.outcome.value,
            updated.state.value,
        )
        return updated

    def release_cleanup(
        self,
        command: ReleaseWeaponryCleanupLease,
    ) -> IdempotentOperationResult:
        if not isinstance(command, ReleaseWeaponryCleanupLease):
            raise TypeError("command 必须是 ReleaseWeaponryCleanupLease")
        with self._transaction() as connection:
            current = self._require_record(connection, command.lease.task_id)
            if current.cleanup_lease is None:
                return IdempotentOperationResult(success=True, already_applied=True)
            self._require_version(current, command.expected_version)
            self._require_active_lease(current, command.lease)
            updated = replace(
                current,
                cleanup_lease=None,
                version=current.version + 1,
            )
            self._save(connection, updated, expected_version=current.version)
        logger.info(
            "武器谱资源清理租约已释放: task_id=%s fencing_token=%d",
            command.lease.task_id.value,
            command.lease.fencing_token,
        )
        return IdempotentOperationResult(success=True)

    def quarantine(
        self,
        command: QuarantineWeaponryResources,
    ) -> WeaponryResourceRecord:
        if not isinstance(command, QuarantineWeaponryResources):
            raise TypeError("command 必须是 QuarantineWeaponryResources")
        with self._transaction() as connection:
            current = self._require_record(connection, command.task_id)
            if current.state is WeaponryResourceRecordState.QUARANTINED:
                if (
                    current.last_error_code == command.error_code
                    and current.last_error_message == command.reason
                ):
                    return current
                raise WeaponryPortStateError(
                    "resource_quarantine_conflict",
                    "资源已按不同原因隔离",
                )
            self._require_version(current, command.expected_version)
            updated = replace(
                current,
                state=WeaponryResourceRecordState.QUARANTINED,
                cleanup_lease=None,
                next_retry_at="",
                last_error_code=command.error_code,
                last_error_message=command.reason,
                version=current.version + 1,
            )
            self._save(connection, updated, expected_version=current.version)
        logger.warning(
            "武器谱资源已隔离: task_id=%s error_code=%s",
            command.task_id.value,
            command.error_code,
        )
        return updated

    def list_recoverable(self, *, limit: int) -> tuple[TaskId, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        now = self._now_text()
        with self._read_connection() as connection:
            # 资源 Store 在生产组合中与 ``llm_task_executions`` 共库。正常恢复扫描除了
            # cleanup_pending，还必须发现“业务终态已经提交、但进程在持久化清理意图前
            # 崩溃”的 tracking 记录。独立 Adapter 测试可以只建资源表，此时安全退化为
            # 原有 cleanup_pending 扫描，绝不能凭资源年龄猜测仍在运行的任务已经死亡。
            task_table_exists = connection.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table' AND name = 'llm_task_executions'
                """
            ).fetchone() is not None
            if task_table_exists:
                rows = connection.execute(
                    """
                    SELECT resources.task_id
                    FROM weaponry_resource_records AS resources
                    LEFT JOIN llm_task_executions AS execution
                      ON execution.execution_id = resources.task_id
                     AND execution.business_type = 'weaponry'
                    WHERE (
                        resources.state = 'cleanup_pending'
                        AND (
                            resources.next_retry_at = ''
                            OR resources.next_retry_at <= ?
                        )
                    ) OR (
                        resources.state = 'tracking'
                        AND (
                            execution.execution_id IS NULL
                            OR execution.execution_state IN ('succeeded', 'failed', 'stale')
                        )
                    )
                    ORDER BY resources.updated_at, resources.task_id
                    LIMIT ?
                    """,
                    (now, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT task_id
                    FROM weaponry_resource_records
                    WHERE state = 'cleanup_pending'
                      AND (next_retry_at = '' OR next_retry_at <= ?)
                    ORDER BY updated_at, task_id
                    LIMIT ?
                    """,
                    (now, limit),
                ).fetchall()
        return tuple(TaskId(str(row["task_id"])) for row in rows)

    def resolve_quarantine(
        self,
        task_id: TaskId,
        *,
        action: str,
        resolved_by: str,
        reason: str,
        external_state_confirmed: bool,
    ) -> WeaponryResourceRecord:
        """人工对账后解除资源隔离，并原子保存不可覆盖的操作审计。

        ``retry_cleanup`` 表示操作者确认远端资源仍存在且允许由恢复循环删除；
        ``confirmed_absent`` 表示操作者已经确认全部 owned 资源在远端不存在。两种动作都
        必须在 execution 非 accepted/running 时执行，避免与活跃 Worker 竞争资源所有权。
        """

        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        normalized_action = str(action or "").strip()
        if normalized_action not in {"retry_cleanup", "confirmed_absent"}:
            raise ValueError("action 必须是 retry_cleanup 或 confirmed_absent")
        normalized_by = self._operator_text(
            resolved_by,
            name="resolved_by",
            max_chars=128,
        )
        normalized_reason = self._operator_text(
            reason,
            name="reason",
            max_chars=512,
        )
        if not isinstance(external_state_confirmed, bool):
            raise TypeError("external_state_confirmed 必须是 bool")
        if not external_state_confirmed:
            raise ValueError("解除资源隔离前必须确认远端资源状态")

        with self._transaction() as connection:
            current = self._require_record(connection, task_id)
            if current.state is not WeaponryResourceRecordState.QUARANTINED:
                raise WeaponryPortStateError(
                    "resource_record_not_quarantined",
                    "只有 quarantined 资源记录可以人工解除",
                )
            self._require_execution_not_active(connection, task_id)
            if normalized_action == "confirmed_absent":
                resources = tuple(
                    replace(item, state=WeaponryTrackedResourceState.CLEANED)
                    if item.ownership is WeaponryResourceOwnership.OWNED
                    else item
                    for item in current.resources
                )
                target_state = WeaponryResourceRecordState.CLEANED
                next_retry_at = ""
            else:
                resources = tuple(
                    replace(
                        item,
                        state=WeaponryTrackedResourceState.CLEANUP_PENDING,
                    )
                    if item.ownership is WeaponryResourceOwnership.OWNED
                    and item.state is not WeaponryTrackedResourceState.CLEANED
                    else item
                    for item in current.resources
                )
                all_cleaned = self._all_owned_cleaned(resources)
                target_state = (
                    WeaponryResourceRecordState.CLEANED
                    if all_cleaned
                    else WeaponryResourceRecordState.CLEANUP_PENDING
                )
                next_retry_at = "" if all_cleaned else self._now_text()
            updated = replace(
                current,
                resources=resources,
                state=target_state,
                cleanup_lease=None,
                next_retry_at=next_retry_at,
                last_error_code="",
                last_error_message="",
                version=current.version + 1,
            )
            self._save(connection, updated, expected_version=current.version)
            connection.execute(
                """
                INSERT INTO weaponry_resource_operator_audits (
                    task_id, previous_version, new_version, action,
                    resolved_at, resolved_by, reason, external_state_confirmed,
                    previous_error_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    task_id.value,
                    current.version,
                    updated.version,
                    normalized_action,
                    self._now_text(),
                    normalized_by,
                    normalized_reason,
                    current.last_error_code,
                ),
            )
        logger.warning(
            "武器谱资源隔离已人工解除: task_id=%s action=%s resolved_by=%s "
            "target_state=%s",
            task_id.value,
            normalized_action,
            normalized_by,
            updated.state.value,
        )
        return updated

    def list_operator_audits(
        self,
        task_id: TaskId,
        *,
        limit: int = 100,
    ) -> tuple[dict[str, object], ...]:
        """读取资源人工处置审计；不返回远端引用、业务正文或凭据。"""

        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT previous_version, new_version, action, resolved_at,
                       resolved_by, reason, external_state_confirmed,
                       previous_error_code
                FROM weaponry_resource_operator_audits
                WHERE task_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (task_id.value, limit),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def _initialize_schema(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._read_connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS weaponry_resource_records (
                    task_id TEXT PRIMARY KEY,
                    business_key TEXT NOT NULL,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    next_retry_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_weaponry_resource_records_recovery
                ON weaponry_resource_records (state, next_retry_at, updated_at, task_id);
                CREATE TABLE IF NOT EXISTS weaponry_resource_operator_audits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    previous_version INTEGER NOT NULL,
                    new_version INTEGER NOT NULL,
                    action TEXT NOT NULL CHECK (
                        action IN ('retry_cleanup', 'confirmed_absent')
                    ),
                    resolved_at TEXT NOT NULL,
                    resolved_by TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    external_state_confirmed INTEGER NOT NULL CHECK (
                        external_state_confirmed = 1
                    ),
                    previous_error_code TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_weaponry_resource_operator_audits_task
                ON weaponry_resource_operator_audits (task_id, id DESC);
                """
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        if self._transactions is not None:
            with self._transactions.begin(read_only=False) as transaction:
                yield transaction.connection
                transaction.commit()
            return
        if self._borrowed_connection is not None:
            if not self._borrowed_connection.in_transaction:
                raise RuntimeError("Weaponry Resource Store 借用连接必须处于活动事务")
            yield self._borrowed_connection
            return
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

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        if self._transactions is not None:
            with self._transactions.begin(read_only=True) as transaction:
                yield transaction.connection
                transaction.commit()
            return
        if self._borrowed_connection is not None:
            if not self._borrowed_connection.in_transaction:
                raise RuntimeError("Weaponry Resource Store 借用连接必须处于活动事务")
            yield self._borrowed_connection
            return
        with closing(self._connect()) as connection:
            yield connection

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
        connection: sqlite3.Connection,
        task_id: TaskId,
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM weaponry_resource_records WHERE task_id = ?",
            (task_id.value,),
        ).fetchone()

    def _require_record(
        self,
        connection: sqlite3.Connection,
        task_id: TaskId,
    ) -> WeaponryResourceRecord:
        row = self._select(connection, task_id)
        if row is None:
            raise WeaponryPortStateError(
                "resource_record_not_found",
                "资源记录不存在",
            )
        return self._decode_row(row)

    @staticmethod
    def _require_execution_not_active(
        connection: sqlite3.Connection,
        task_id: TaskId,
    ) -> None:
        table_exists = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'llm_task_executions'
            """
        ).fetchone()
        if table_exists is None:
            # 人工解除属于生产运维动作。若误指向旧库/错误库，缺少 execution 权威表时
            # 不能把“无法核验活跃 Worker”当成“不活跃”，必须失败关闭。
            raise WeaponryPortStateError(
                "resource_execution_table_missing",
                "缺少 execution 权威表，禁止人工解除资源隔离",
            )
        row = connection.execute(
            """
            SELECT execution_state
            FROM llm_task_executions
            WHERE execution_id = ? AND business_type = 'weaponry'
            """,
            (task_id.value,),
        ).fetchone()
        if row is not None and str(row["execution_state"]) in {"accepted", "running"}:
            raise WeaponryPortStateError(
                "resource_execution_still_active",
                "活跃 execution 禁止人工解除资源隔离",
            )

    @staticmethod
    def _operator_text(value: object, *, name: str, max_chars: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} 必须是非空 str")
        normalized = value.strip()
        if len(normalized) > max_chars:
            raise ValueError(f"{name} 最多 {max_chars} 个字符")
        return normalized

    def _save(
        self,
        connection: sqlite3.Connection,
        record: WeaponryResourceRecord,
        *,
        expected_version: int,
    ) -> None:
        updated = connection.execute(
            """
            UPDATE weaponry_resource_records
            SET state = ?, version = ?, next_retry_at = ?,
                payload_json = ?, updated_at = ?
            WHERE task_id = ? AND version = ?
            """,
            (
                record.state.value,
                record.version,
                record.next_retry_at,
                self._encode_record(record),
                self._now_text(),
                record.task_id.value,
                expected_version,
            ),
        ).rowcount
        if updated != 1:
            raise WeaponryPortStateError(
                "resource_version_conflict",
                "资源记录版本不一致",
            )

    @staticmethod
    def _encode_record(record: WeaponryResourceRecord) -> str:
        lease = record.cleanup_lease
        value: dict[str, Any] = {
            "task_id": record.task_id.value,
            "business_key": record.business_ref.business_key,
            "resources": [
                {
                    "resource_id": item.resource_id,
                    "kind": item.kind.value,
                    "external_ref": item.external_ref,
                    "ownership": item.ownership.value,
                    "idempotency_key": item.idempotency_key,
                    "document_key": item.document_key,
                    "call_id": item.call_id,
                    "state": item.state.value,
                }
                for item in record.resources
            ],
            "state": record.state.value,
            "retry_count": record.retry_count,
            "next_retry_at": record.next_retry_at,
            "last_error_code": record.last_error_code,
            "last_error_message": record.last_error_message,
            "cleanup_lease": (
                {
                    "task_id": lease.task_id.value,
                    "token": lease.token,
                    "fencing_token": lease.fencing_token,
                    "deadline_at": lease.deadline_at,
                }
                if lease is not None
                else None
            ),
            "cleanup_fencing_token": record.cleanup_fencing_token,
            "version": record.version,
        }
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _decode_row(row: sqlite3.Row) -> WeaponryResourceRecord:
        try:
            return SQLiteWeaponryResourceStoreAdapter._decode_row_unchecked(row)
        except WeaponryPortStateError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            # 数据库内容属于内部事实，损坏时必须转换为稳定状态错误，不能把 KeyError 等
            # 实现细节一路传播成不可诊断的 Worker 500。
            raise WeaponryPortStateError(
                "resource_payload_invalid",
                "资源记录字段集合、类型或枚举值无效",
            ) from exc

    @staticmethod
    def _decode_row_unchecked(row: sqlite3.Row) -> WeaponryResourceRecord:
        try:
            value = json.loads(str(row["payload_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise WeaponryPortStateError(
                "resource_payload_invalid",
                "资源记录 JSON 无法解码",
            ) from exc
        if not isinstance(value, Mapping):
            raise WeaponryPortStateError(
                "resource_payload_invalid",
                "资源记录 JSON 必须是对象",
            )
        resources_value = value.get("resources")
        if not isinstance(resources_value, list):
            raise WeaponryPortStateError(
                "resource_payload_invalid",
                "资源记录 resources 必须是数组",
            )
        resources = tuple(
            WeaponryTrackedResource(
                resource_id=item["resource_id"],
                kind=WeaponryResourceKind(item["kind"]),
                external_ref=item["external_ref"],
                ownership=WeaponryResourceOwnership(item["ownership"]),
                idempotency_key=item["idempotency_key"],
                document_key=item["document_key"],
                call_id=item["call_id"],
                state=WeaponryTrackedResourceState(item["state"]),
            )
            for item in resources_value
        )
        lease_value = value.get("cleanup_lease")
        lease = None
        if lease_value is not None:
            if not isinstance(lease_value, Mapping):
                raise WeaponryPortStateError(
                    "resource_payload_invalid",
                    "资源清理租约必须是对象或 null",
                )
            lease = WeaponryCleanupLease(
                task_id=TaskId(str(lease_value["task_id"])),
                token=lease_value["token"],
                fencing_token=lease_value["fencing_token"],
                deadline_at=lease_value["deadline_at"],
            )
        record = WeaponryResourceRecord(
            task_id=TaskId(str(value["task_id"])),
            business_ref=TaskBusinessRef("weaponry", str(value["business_key"])),
            resources=resources,
            state=WeaponryResourceRecordState(value["state"]),
            retry_count=value["retry_count"],
            next_retry_at=value["next_retry_at"],
            last_error_code=value["last_error_code"],
            last_error_message=value["last_error_message"],
            cleanup_lease=lease,
            cleanup_fencing_token=value["cleanup_fencing_token"],
            version=value["version"],
        )
        # 冗余列用于有界扫描，必须与 JSON 权威快照一致；任何分叉都按持久化损坏处理。
        if (
            row["task_id"] != record.task_id.value
            or row["business_key"] != record.business_ref.business_key
            or row["state"] != record.state.value
            or int(row["version"]) != record.version
            or row["next_retry_at"] != record.next_retry_at
        ):
            raise WeaponryPortStateError(
                "resource_record_projection_mismatch",
                "资源记录索引列与 JSON 快照不一致",
            )
        return record

    @staticmethod
    def _require_version(record: WeaponryResourceRecord, expected: int) -> None:
        if record.version != expected:
            raise WeaponryPortStateError(
                "resource_version_conflict",
                "资源记录版本不一致",
            )

    @staticmethod
    def _require_active_lease(
        record: WeaponryResourceRecord,
        lease: WeaponryCleanupLease,
        *,
        require_unexpired: bool = False,
    ) -> None:
        if record.cleanup_lease != lease:
            raise WeaponryPortStateError(
                "resource_cleanup_lease_mismatch",
                "资源清理租约不存在或已经失权",
            )
        if require_unexpired and SQLiteWeaponryResourceStoreAdapter._parse_time(
            lease.deadline_at
        ) <= SQLiteWeaponryResourceStoreAdapter._now():
            raise WeaponryPortStateError(
                "resource_cleanup_lease_expired",
                "资源清理租约已经过期",
            )

    @staticmethod
    def _all_owned_cleaned(resources: tuple[WeaponryTrackedResource, ...]) -> bool:
        return all(
            item.ownership is WeaponryResourceOwnership.SHARED
            or item.state is WeaponryTrackedResourceState.CLEANED
            for item in resources
        )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @classmethod
    def _now_text(cls) -> str:
        return cls._now().strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    @classmethod
    def _after_seconds_text(cls, seconds: float) -> str:
        """生成持久冷却水位；进程重启后仍不会立即热循环失败资源。"""

        return (cls._now() + timedelta(seconds=seconds)).isoformat()

    @staticmethod
    def _parse_time(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise WeaponryPortStateError(
                "resource_timestamp_invalid",
                "资源记录时间戳无效",
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)


__all__ = ["SQLiteWeaponryResourceStoreAdapter"]
