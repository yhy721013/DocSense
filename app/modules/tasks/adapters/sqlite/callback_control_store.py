"""SQLite Callback Guard/Delivery 控制事实的唯一 v2 Store。

Store 只使用调用方 Unit of Work 提供的活动 Connection，不自行提交、重试或执行网络 I/O。
所有发送权都由业务键 Guard、Task latest owner、随机 lease token 和单调 fencing token
共同约束；租约过期只会保守冻结为 ``outcome_unknown``，绝不自动重抢。
"""

from __future__ import annotations

import logging
import sqlite3

from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.tasks.ports.callback_delivery_control import (
    CallbackAcquireCommand,
    CallbackAcquireOutcome,
    CallbackAcquireResult,
    CallbackAdmissionConflict,
    CallbackCompleteCommand,
    CallbackControlMutationOutcome,
    CallbackDeliveryLease,
    CallbackDeliveryOutcome,
    CallbackEligibilityCommand,
    CallbackGuardObservation,
    CallbackGuardState,
    CallbackGuardSweepCommand,
    CallbackGuardSweepResult,
    CallbackHeartbeatCommand,
    CallbackHeartbeatResult,
    CallbackReleaseOutcome,
    CallbackReleaseUnknownCommand,
    CallbackValidationCommand,
    CallbackValidationOutcome,
)
from app.modules.tasks.ports.clock import require_persisted_utc


logger = logging.getLogger(__name__)

_TERMINAL_TASK_STATES = frozenset({"succeeded", "failed"})
_TERMINAL_ATTEMPT_STATES = frozenset({"succeeded", "failed"})


class SQLiteCallbackControlStore:
    """实现完整 Callback Control Port；Connection 生命周期归 UoW。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection 必须是 sqlite3.Connection")
        self._connection = connection
        self._connection.row_factory = sqlite3.Row

    def _require_write_transaction(self) -> None:
        if not self._connection.in_transaction:
            raise RuntimeError("Callback Control Store 写入必须位于活动事务")
        query_only = int(self._connection.execute("PRAGMA query_only").fetchone()[0])
        if query_only:
            raise RuntimeError("Callback Control Store 不能在只读事务中写入")

    def _guard(self, business_ref: TaskBusinessRef) -> sqlite3.Row | None:
        return self._connection.execute(
            """
            SELECT business_type, business_key, owner_execution_id, state,
                   lease_token, lease_version, lease_started_at, deadline_at,
                   last_outcome, error_stage, released_at, released_by,
                   release_reason, updated_at
            FROM callback_delivery_guards
            WHERE business_type = ? AND business_key = ?
            """,
            (business_ref.business_type, business_ref.business_key),
        ).fetchone()

    def _execution_view(
        self,
        task_id: TaskId,
        business_ref: TaskBusinessRef,
    ) -> sqlite3.Row | None:
        return self._connection.execute(
            """
            SELECT e.execution_id, e.business_type, e.business_key,
                   e.execution_state, e.current_attempt_no, e.fencing_token,
                   e.callback_status AS execution_callback_status,
                   e.callback_outcome, l.execution_id AS latest_execution_id,
                   l.status AS latest_status,
                   l.callback_status AS latest_callback_status,
                   l.callback_attempts,
                   a.state AS attempt_state, a.lease_token AS attempt_lease_token,
                   a.fencing_token AS attempt_fencing_token,
                   a.lease_expires_at AS attempt_lease_expires_at
            FROM llm_task_executions AS e
            LEFT JOIN llm_tasks AS l
              ON l.business_type = e.business_type
             AND l.business_key = e.business_key
            LEFT JOIN task_attempts AS a
              ON a.task_id = e.execution_id
             AND a.attempt_no = e.current_attempt_no
            WHERE e.execution_id = ?
              AND e.business_type = ?
              AND e.business_key = ?
            """,
            (task_id.value, business_ref.business_type, business_ref.business_key),
        ).fetchone()

    @staticmethod
    def _is_latest(row: sqlite3.Row) -> bool:
        return str(row["latest_execution_id"] or "") == str(row["execution_id"])

    @staticmethod
    def _lease_matches(guard: sqlite3.Row, lease: CallbackDeliveryLease) -> bool:
        return bool(
            str(guard["owner_execution_id"] or "") == lease.task_id.value
            and str(guard["state"]) == CallbackGuardState.SENDING.value
            and str(guard["lease_token"]) == lease.lease_token
            and int(guard["lease_version"]) == lease.fencing_token
            and str(guard["deadline_at"] or "") == lease.lease_expires_at
        )

    def _append_attempt_event(
        self,
        *,
        business_ref: TaskBusinessRef,
        task_id: TaskId,
        callback_attempt: int,
        lease_version: int,
        trigger: str,
        event_type: str,
        delivery_outcome: str,
        request_trace_id: str,
        occurred_at: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO callback_delivery_attempt_events (
                business_type, business_key, owner_execution_id,
                callback_attempt, lease_version, trigger, event_type,
                delivery_outcome, request_trace_id, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                business_ref.business_type,
                business_ref.business_key,
                task_id.value,
                callback_attempt,
                lease_version,
                trigger,
                event_type,
                delivery_outcome,
                request_trace_id,
                occurred_at,
            ),
        )

    def _authorization_event(self, guard: sqlite3.Row) -> sqlite3.Row | None:
        return self._connection.execute(
            """
            SELECT callback_attempt, trigger, request_trace_id
            FROM callback_delivery_attempt_events
            WHERE business_type = ? AND business_key = ?
              AND owner_execution_id = ? AND lease_version = ?
              AND event_type = 'authorized'
            """,
            (
                str(guard["business_type"]),
                str(guard["business_key"]),
                str(guard["owner_execution_id"]),
                int(guard["lease_version"]),
            ),
        ).fetchone()

    def _transition_to_unknown(
        self,
        guard: sqlite3.Row,
        *,
        observed_at: str,
        event_type: str,
        reason: str,
    ) -> bool:
        """把仍由同一 fencing owner 持有的 sending 原子冻结为 unknown。"""

        owner_text = str(guard["owner_execution_id"] or "").strip()
        if not owner_text:
            raise RuntimeError("sending Callback Guard 缺少 owner_execution_id")
        cursor = self._connection.execute(
            """
            UPDATE callback_delivery_guards
            SET state = 'outcome_unknown', lease_token = '', deadline_at = NULL,
                last_outcome = 'delivery_outcome_unknown', error_stage = ?,
                updated_at = ?
            WHERE business_type = ? AND business_key = ?
              AND owner_execution_id = ? AND state = 'sending'
              AND lease_version = ? AND lease_token = ?
            """,
            (
                reason,
                observed_at,
                str(guard["business_type"]),
                str(guard["business_key"]),
                owner_text,
                int(guard["lease_version"]),
                str(guard["lease_token"]),
            ),
        )
        if cursor.rowcount != 1:
            return False
        self._connection.execute(
            """
            UPDATE llm_task_executions
            SET callback_status = 'outcome_unknown',
                callback_outcome = 'delivery_outcome_unknown', updated_at = ?
            WHERE execution_id = ?
              AND business_type = ? AND business_key = ?
            """,
            (
                observed_at,
                owner_text,
                str(guard["business_type"]),
                str(guard["business_key"]),
            ),
        )
        self._connection.execute(
            """
            UPDATE llm_tasks
            SET callback_status = 'outcome_unknown',
                last_callback_error = ?, updated_at = ?
            WHERE business_type = ? AND business_key = ?
              AND execution_id = ?
            """,
            (
                reason,
                observed_at,
                str(guard["business_type"]),
                str(guard["business_key"]),
                owner_text,
            ),
        )
        authorization = self._authorization_event(guard)
        if authorization is None:
            raise RuntimeError("sending Callback Guard 缺少 authorized 审计事件")
        self._append_attempt_event(
            business_ref=TaskBusinessRef(
                str(guard["business_type"]),
                str(guard["business_key"]),
            ),
            task_id=TaskId(owner_text),
            callback_attempt=int(authorization["callback_attempt"]),
            lease_version=int(guard["lease_version"]),
            trigger=str(authorization["trigger"]),
            event_type=event_type,
            delivery_outcome=CallbackDeliveryOutcome.DELIVERY_OUTCOME_UNKNOWN.value,
            request_trace_id=str(authorization["request_trace_id"]),
            occurred_at=observed_at,
        )
        logger.warning(
            "Callback 发送租约已冻结为结果未知: business_type=%s task_id=%s "
            "fencing_token=%s reason=%s",
            str(guard["business_type"]),
            owner_text,
            int(guard["lease_version"]),
            reason,
        )
        return True

    def get_admission_conflict(
        self,
        business_ref: TaskBusinessRef,
    ) -> CallbackAdmissionConflict:
        if not isinstance(business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        guard = self._guard(business_ref)
        if guard is None or str(guard["state"]) == CallbackGuardState.IDLE.value:
            return CallbackAdmissionConflict.NONE
        if str(guard["state"]) == CallbackGuardState.SENDING.value:
            return CallbackAdmissionConflict.SENDING
        if str(guard["state"]) == CallbackGuardState.OUTCOME_UNKNOWN.value:
            return CallbackAdmissionConflict.OUTCOME_UNKNOWN
        raise RuntimeError("Callback Guard 存在未知状态")

    def mark_eligible(
        self,
        command: CallbackEligibilityCommand,
    ) -> CallbackControlMutationOutcome:
        self._require_write_transaction()
        if not isinstance(command, CallbackEligibilityCommand):
            raise TypeError("command 必须是 CallbackEligibilityCommand")
        row = self._execution_view(command.authority.task_id, command.business_ref)
        if row is None:
            return CallbackControlMutationOutcome.MISSING
        if not self._is_latest(row):
            return CallbackControlMutationOutcome.STALE
        authority = command.authority
        if (
            int(row["current_attempt_no"]) != authority.attempt_no
            or int(row["fencing_token"]) != authority.fencing_token
            or int(row["attempt_fencing_token"] or 0) != authority.fencing_token
            or str(row["attempt_lease_token"] or "") != authority.lease_token
        ):
            return CallbackControlMutationOutcome.AUTHORITY_LOST
        if (
            str(row["execution_state"]) not in _TERMINAL_TASK_STATES
            or str(row["attempt_state"]) not in _TERMINAL_ATTEMPT_STATES
            or str(row["execution_callback_status"]) != "pending"
            or str(row["latest_callback_status"]) != "pending"
        ):
            return CallbackControlMutationOutcome.INVALID_STATE
        guard = self._guard(command.business_ref)
        if guard is None:
            self._connection.execute(
                """
                INSERT INTO callback_delivery_guards (
                    business_type, business_key, owner_execution_id, state,
                    lease_token, lease_version, lease_started_at, deadline_at,
                    last_outcome, error_stage, released_at, released_by,
                    release_reason, updated_at
                ) VALUES (?, ?, ?, 'idle', '', 0, NULL, NULL, '', '',
                          NULL, '', '', ?)
                """,
                (
                    command.business_ref.business_type,
                    command.business_ref.business_key,
                    authority.task_id.value,
                    command.eligible_at,
                ),
            )
            return CallbackControlMutationOutcome.APPLIED
        if str(guard["state"]) != CallbackGuardState.IDLE.value:
            return CallbackControlMutationOutcome.INVALID_STATE
        if str(guard["owner_execution_id"] or "") == authority.task_id.value:
            return CallbackControlMutationOutcome.DUPLICATE
        cursor = self._connection.execute(
            """
            UPDATE callback_delivery_guards
            SET owner_execution_id = ?, lease_token = '',
                lease_started_at = NULL, deadline_at = NULL,
                last_outcome = '', error_stage = '',
                released_at = NULL, released_by = '', release_reason = '',
                updated_at = ?
            WHERE business_type = ? AND business_key = ? AND state = 'idle'
              AND lease_version = ?
            """,
            (
                authority.task_id.value,
                command.eligible_at,
                command.business_ref.business_type,
                command.business_ref.business_key,
                int(guard["lease_version"]),
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Callback eligibility fencing 条件写未命中")
        return CallbackControlMutationOutcome.APPLIED

    def acquire(self, command: CallbackAcquireCommand) -> CallbackAcquireResult:
        self._require_write_transaction()
        if not isinstance(command, CallbackAcquireCommand):
            raise TypeError("command 必须是 CallbackAcquireCommand")
        row = self._execution_view(command.task_id, command.business_ref)
        if row is None or not self._is_latest(row):
            return CallbackAcquireResult(CallbackAcquireOutcome.STALE)
        if (
            str(row["execution_state"]) not in _TERMINAL_TASK_STATES
            or str(row["attempt_state"]) not in _TERMINAL_ATTEMPT_STATES
        ):
            return CallbackAcquireResult(CallbackAcquireOutcome.INVALID_STATE)
        attempts = int(row["callback_attempts"])
        if (
            command.expected_callback_attempts is not None
            and attempts != command.expected_callback_attempts
        ):
            return CallbackAcquireResult(CallbackAcquireOutcome.STALE)
        execution_status = str(row["execution_callback_status"])
        latest_status = str(row["latest_callback_status"])
        if execution_status != latest_status:
            return CallbackAcquireResult(CallbackAcquireOutcome.OUTCOME_UNKNOWN)
        if execution_status in {"success", "skipped"}:
            return CallbackAcquireResult(CallbackAcquireOutcome.ALREADY_COMPLETED)
        explicit = command.expected_callback_attempts is not None
        if execution_status == "failed" and not explicit:
            return CallbackAcquireResult(CallbackAcquireOutcome.ALREADY_COMPLETED)
        if execution_status == "outcome_unknown" and not explicit:
            return CallbackAcquireResult(CallbackAcquireOutcome.OUTCOME_UNKNOWN)
        if execution_status not in {"pending", "failed", "sending", "outcome_unknown"}:
            return CallbackAcquireResult(CallbackAcquireOutcome.INVALID_STATE)
        guard = self._guard(command.business_ref)
        if guard is None:
            logger.critical(
                "Callback eligible Task 缺少 Guard，拒绝发送: business_type=%s task_id=%s",
                command.business_ref.business_type,
                command.task_id,
            )
            return CallbackAcquireResult(CallbackAcquireOutcome.INVALID_STATE)
        guard_state = str(guard["state"])
        if guard_state == CallbackGuardState.SENDING.value:
            deadline = str(guard["deadline_at"] or "")
            if not deadline:
                raise RuntimeError("sending Callback Guard 缺少 deadline_at")
            if deadline <= command.acquired_at:
                if not self._transition_to_unknown(
                    guard,
                    observed_at=command.acquired_at,
                    event_type="lease_expired_unknown",
                    reason="callback lease expired before completion",
                ):
                    raise RuntimeError("过期 Callback Guard 未能冻结")
                return CallbackAcquireResult(CallbackAcquireOutcome.OUTCOME_UNKNOWN)
            return CallbackAcquireResult(CallbackAcquireOutcome.BUSY)
        explicit_unknown = bool(
            explicit
            and execution_status == "outcome_unknown"
            and guard_state == CallbackGuardState.OUTCOME_UNKNOWN.value
            and str(guard["owner_execution_id"] or "") == command.task_id.value
        )
        if guard_state == CallbackGuardState.OUTCOME_UNKNOWN.value and not explicit_unknown:
            return CallbackAcquireResult(CallbackAcquireOutcome.OUTCOME_UNKNOWN)
        if execution_status == "sending":
            # 投影声称正在发送而 Guard 没有相同租约，任何重发都可能重复通知接收方。
            logger.critical(
                "Callback execution 与 Guard 状态不一致，拒绝重发: business_type=%s task_id=%s",
                command.business_ref.business_type,
                command.task_id,
            )
            return CallbackAcquireResult(CallbackAcquireOutcome.OUTCOME_UNKNOWN)
        if execution_status == "outcome_unknown" and not explicit_unknown:
            return CallbackAcquireResult(CallbackAcquireOutcome.OUTCOME_UNKNOWN)
        expected_guard_state = (
            CallbackGuardState.OUTCOME_UNKNOWN.value
            if explicit_unknown
            else CallbackGuardState.IDLE.value
        )
        previous_version = int(guard["lease_version"])
        fencing_token = previous_version + 1
        cursor = self._connection.execute(
            """
            UPDATE callback_delivery_guards
            SET owner_execution_id = ?, state = 'sending', lease_token = ?,
                lease_version = ?, lease_started_at = ?, deadline_at = ?,
                last_outcome = '', error_stage = '', released_at = NULL,
                released_by = '', release_reason = '', updated_at = ?
            WHERE business_type = ? AND business_key = ?
              AND state = ? AND lease_version = ?
              AND (? = 'idle' OR owner_execution_id = ?)
            """,
            (
                command.task_id.value,
                command.lease_token,
                fencing_token,
                command.acquired_at,
                command.lease_expires_at,
                command.acquired_at,
                command.business_ref.business_type,
                command.business_ref.business_key,
                expected_guard_state,
                previous_version,
                expected_guard_state,
                command.task_id.value,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Callback Guard fencing 条件写未命中")
        execution_cursor = self._connection.execute(
            """
            UPDATE llm_task_executions
            SET callback_status = 'sending', callback_outcome = '', updated_at = ?
            WHERE execution_id = ? AND callback_status = ?
            """,
            (command.acquired_at, command.task_id.value, execution_status),
        )
        latest_cursor = self._connection.execute(
            """
            UPDATE llm_tasks
            SET callback_status = 'sending', callback_attempts = callback_attempts + 1,
                last_callback_error = '', updated_at = ?
            WHERE business_type = ? AND business_key = ? AND execution_id = ?
              AND callback_status = ? AND callback_attempts = ?
            """,
            (
                command.acquired_at,
                command.business_ref.business_type,
                command.business_ref.business_key,
                command.task_id.value,
                latest_status,
                attempts,
            ),
        )
        if execution_cursor.rowcount != 1 or latest_cursor.rowcount != 1:
            raise RuntimeError("Callback Guard 与 Task/latest 投影未能原子取得发送权")
        self._append_attempt_event(
            business_ref=command.business_ref,
            task_id=command.task_id,
            callback_attempt=attempts + 1,
            lease_version=fencing_token,
            trigger=command.trigger.value,
            event_type="authorized",
            delivery_outcome="",
            request_trace_id=command.request_trace_id,
            occurred_at=command.acquired_at,
        )
        lease = CallbackDeliveryLease(
            task_id=command.task_id,
            business_ref=command.business_ref,
            lease_token=command.lease_token,
            fencing_token=fencing_token,
            lease_expires_at=command.lease_expires_at,
        )
        logger.info(
            "Callback 发送权获取完成: business_type=%s task_id=%s outcome=acquired "
            "fencing_token=%s trigger=%s",
            command.business_ref.business_type,
            command.task_id,
            fencing_token,
            command.trigger.value,
        )
        return CallbackAcquireResult(CallbackAcquireOutcome.ACQUIRED, lease)

    def heartbeat(self, command: CallbackHeartbeatCommand) -> CallbackHeartbeatResult:
        self._require_write_transaction()
        if not isinstance(command, CallbackHeartbeatCommand):
            raise TypeError("command 必须是 CallbackHeartbeatCommand")
        guard = self._guard(command.lease.business_ref)
        if guard is None or not self._lease_matches(guard, command.lease):
            return CallbackHeartbeatResult(CallbackControlMutationOutcome.AUTHORITY_LOST)
        if command.heartbeat_at >= command.lease.lease_expires_at:
            if not self._transition_to_unknown(
                guard,
                observed_at=command.heartbeat_at,
                event_type="lease_expired_unknown",
                reason="callback heartbeat observed expired lease",
            ):
                return CallbackHeartbeatResult(CallbackControlMutationOutcome.AUTHORITY_LOST)
            return CallbackHeartbeatResult(CallbackControlMutationOutcome.LEASE_EXPIRED)
        row = self._execution_view(command.lease.task_id, command.lease.business_ref)
        if row is None or not self._is_latest(row):
            return CallbackHeartbeatResult(CallbackControlMutationOutcome.STALE)
        if str(row["execution_callback_status"]) != "sending":
            return CallbackHeartbeatResult(CallbackControlMutationOutcome.INVALID_STATE)
        cursor = self._connection.execute(
            """
            UPDATE callback_delivery_guards
            SET deadline_at = ?, updated_at = ?
            WHERE business_type = ? AND business_key = ?
              AND owner_execution_id = ? AND state = 'sending'
              AND lease_token = ? AND lease_version = ? AND deadline_at = ?
            """,
            (
                command.lease_expires_at,
                command.heartbeat_at,
                command.lease.business_ref.business_type,
                command.lease.business_ref.business_key,
                command.lease.task_id.value,
                command.lease.lease_token,
                command.lease.fencing_token,
                command.lease.lease_expires_at,
            ),
        )
        if cursor.rowcount != 1:
            return CallbackHeartbeatResult(CallbackControlMutationOutcome.AUTHORITY_LOST)
        renewed = CallbackDeliveryLease(
            task_id=command.lease.task_id,
            business_ref=command.lease.business_ref,
            lease_token=command.lease.lease_token,
            fencing_token=command.lease.fencing_token,
            lease_expires_at=command.lease_expires_at,
        )
        return CallbackHeartbeatResult(CallbackControlMutationOutcome.APPLIED, renewed)

    def validate(self, command: CallbackValidationCommand) -> CallbackValidationOutcome:
        self._require_write_transaction()
        if not isinstance(command, CallbackValidationCommand):
            raise TypeError("command 必须是 CallbackValidationCommand")
        row = self._execution_view(command.lease.task_id, command.lease.business_ref)
        if row is None or not self._is_latest(row):
            return CallbackValidationOutcome.STALE
        guard = self._guard(command.lease.business_ref)
        if guard is None or not self._lease_matches(guard, command.lease):
            if guard is not None and str(guard["state"]) == "outcome_unknown":
                return CallbackValidationOutcome.OUTCOME_UNKNOWN
            return CallbackValidationOutcome.AUTHORITY_LOST
        if command.observed_at >= command.lease.lease_expires_at:
            if not self._transition_to_unknown(
                guard,
                observed_at=command.observed_at,
                event_type="lease_expired_unknown",
                reason="callback lease expired before HTTP validation",
            ):
                return CallbackValidationOutcome.AUTHORITY_LOST
            return CallbackValidationOutcome.LEASE_EXPIRED
        if (
            str(row["execution_callback_status"]) != "sending"
            or str(row["latest_callback_status"]) != "sending"
        ):
            return CallbackValidationOutcome.AUTHORITY_LOST
        return CallbackValidationOutcome.VALID

    def complete(self, command: CallbackCompleteCommand) -> CallbackControlMutationOutcome:
        self._require_write_transaction()
        if not isinstance(command, CallbackCompleteCommand):
            raise TypeError("command 必须是 CallbackCompleteCommand")
        guard = self._guard(command.lease.business_ref)
        if guard is None or not self._lease_matches(guard, command.lease):
            if guard is not None and str(guard["state"]) == "outcome_unknown":
                return CallbackControlMutationOutcome.OUTCOME_UNKNOWN
            return CallbackControlMutationOutcome.AUTHORITY_LOST
        row = self._execution_view(command.lease.task_id, command.lease.business_ref)
        if row is None or not self._is_latest(row):
            return CallbackControlMutationOutcome.STALE
        if (
            str(row["execution_callback_status"]) != "sending"
            or str(row["latest_callback_status"]) != "sending"
        ):
            return CallbackControlMutationOutcome.INVALID_STATE
        if command.outcome is CallbackDeliveryOutcome.DELIVERY_OUTCOME_UNKNOWN:
            guard_state = "outcome_unknown"
            callback_status = "outcome_unknown"
        elif command.outcome is CallbackDeliveryOutcome.SUCCESS:
            guard_state = "idle"
            callback_status = "success"
        elif command.outcome is CallbackDeliveryOutcome.SKIPPED:
            guard_state = "idle"
            callback_status = "skipped"
        else:
            guard_state = "idle"
            callback_status = "failed"
        error_stage = command.detail if callback_status in {"failed", "outcome_unknown"} else ""
        guard_cursor = self._connection.execute(
            """
            UPDATE callback_delivery_guards
            SET state = ?, lease_token = '', deadline_at = NULL,
                last_outcome = ?, error_stage = ?, updated_at = ?
            WHERE business_type = ? AND business_key = ?
              AND owner_execution_id = ? AND state = 'sending'
              AND lease_token = ? AND lease_version = ? AND deadline_at = ?
            """,
            (
                guard_state,
                command.outcome.value,
                error_stage,
                command.completed_at,
                command.lease.business_ref.business_type,
                command.lease.business_ref.business_key,
                command.lease.task_id.value,
                command.lease.lease_token,
                command.lease.fencing_token,
                command.lease.lease_expires_at,
            ),
        )
        execution_cursor = self._connection.execute(
            """
            UPDATE llm_task_executions
            SET callback_status = ?, callback_outcome = ?, updated_at = ?
            WHERE execution_id = ? AND callback_status = 'sending'
            """,
            (
                callback_status,
                command.outcome.value,
                command.completed_at,
                command.lease.task_id.value,
            ),
        )
        latest_cursor = self._connection.execute(
            """
            UPDATE llm_tasks
            SET callback_status = ?, last_callback_error = ?, updated_at = ?
            WHERE business_type = ? AND business_key = ?
              AND execution_id = ? AND callback_status = 'sending'
            """,
            (
                callback_status,
                error_stage,
                command.completed_at,
                command.lease.business_ref.business_type,
                command.lease.business_ref.business_key,
                command.lease.task_id.value,
            ),
        )
        if guard_cursor.rowcount != 1:
            return CallbackControlMutationOutcome.AUTHORITY_LOST
        if execution_cursor.rowcount != 1 or latest_cursor.rowcount != 1:
            raise RuntimeError("Callback 完成未能与 Task/latest 投影原子收敛")
        authorization = self._authorization_event(guard)
        if authorization is None:
            raise RuntimeError("Callback 完成缺少 authorized 审计事件")
        self._append_attempt_event(
            business_ref=command.lease.business_ref,
            task_id=command.lease.task_id,
            callback_attempt=int(row["callback_attempts"]),
            lease_version=command.lease.fencing_token,
            trigger=str(authorization["trigger"]),
            event_type="completed",
            delivery_outcome=command.outcome.value,
            request_trace_id=str(authorization["request_trace_id"]),
            occurred_at=command.completed_at,
        )
        logger.info(
            "Callback 控制事实完成: business_type=%s task_id=%s outcome=%s "
            "fencing_token=%s",
            command.lease.business_ref.business_type,
            command.lease.task_id,
            command.outcome.value,
            command.lease.fencing_token,
        )
        return CallbackControlMutationOutcome.APPLIED

    def observe(
        self,
        business_ref: TaskBusinessRef,
        *,
        observed_at: str,
    ) -> CallbackGuardObservation:
        self._require_write_transaction()
        if not isinstance(business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        observed_at = require_persisted_utc(observed_at, name="observed_at")
        guard = self._guard(business_ref)
        if guard is None:
            return CallbackGuardObservation(CallbackGuardState.IDLE)
        state = CallbackGuardState(str(guard["state"]))
        if state is CallbackGuardState.SENDING:
            deadline = str(guard["deadline_at"] or "")
            if not deadline:
                raise RuntimeError("sending Callback Guard 缺少 deadline_at")
            if deadline <= observed_at:
                if not self._transition_to_unknown(
                    guard,
                    observed_at=observed_at,
                    event_type="lease_expired_unknown",
                    reason="callback lease expired while observing guard",
                ):
                    raise RuntimeError("Callback Guard 观察冻结条件写未命中")
                return CallbackGuardObservation(
                    CallbackGuardState.OUTCOME_UNKNOWN,
                    TaskId(str(guard["owner_execution_id"])),
                )
        owner = (
            TaskId(str(guard["owner_execution_id"]))
            if guard["owner_execution_id"] is not None
            else None
        )
        return CallbackGuardObservation(
            state,
            owner,
            str(guard["deadline_at"] or ""),
        )

    def freeze_expired(
        self,
        command: CallbackGuardSweepCommand,
    ) -> CallbackGuardSweepResult:
        self._require_write_transaction()
        if not isinstance(command, CallbackGuardSweepCommand):
            raise TypeError("command 必须是 CallbackGuardSweepCommand")
        rows = self._connection.execute(
            """
            SELECT * FROM callback_delivery_guards
            WHERE business_type = ? AND state = 'sending'
              AND deadline_at <= ?
            ORDER BY deadline_at, business_key
            LIMIT ?
            """,
            (command.business_type, command.observed_at, command.limit),
        ).fetchall()
        frozen = 0
        for guard in rows:
            if self._transition_to_unknown(
                guard,
                observed_at=command.observed_at,
                event_type="lease_expired_unknown",
                reason="callback lease expired during bounded sweep",
            ):
                frozen += 1
        return CallbackGuardSweepResult(len(rows), frozen)

    def release_unknown(
        self,
        command: CallbackReleaseUnknownCommand,
    ) -> CallbackReleaseOutcome:
        self._require_write_transaction()
        if not isinstance(command, CallbackReleaseUnknownCommand):
            raise TypeError("command 必须是 CallbackReleaseUnknownCommand")
        guard = self._guard(command.business_ref)
        if guard is None:
            return CallbackReleaseOutcome.NOT_FROZEN
        if str(guard["state"]) == CallbackGuardState.IDLE.value:
            if guard["released_at"] is not None:
                return CallbackReleaseOutcome.ALREADY_RELEASED
            return CallbackReleaseOutcome.NOT_FROZEN
        if str(guard["state"]) != CallbackGuardState.OUTCOME_UNKNOWN.value:
            return CallbackReleaseOutcome.NOT_FROZEN
        owner_text = str(guard["owner_execution_id"] or "").strip()
        if not owner_text:
            raise RuntimeError("outcome_unknown Callback Guard 缺少 owner")
        try:
            self._connection.execute(
                """
                INSERT INTO callback_guard_release_audits (
                    business_type, business_key, owner_execution_id,
                    lease_version, released_at, released_by, release_reason,
                    worker_stopped_confirmed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    command.business_ref.business_type,
                    command.business_ref.business_key,
                    owner_text,
                    int(guard["lease_version"]),
                    command.released_at,
                    command.released_by,
                    command.reason,
                ),
            )
        except sqlite3.IntegrityError:
            return CallbackReleaseOutcome.ALREADY_RELEASED
        cursor = self._connection.execute(
            """
            UPDATE callback_delivery_guards
            SET state = 'idle', lease_token = '', deadline_at = NULL,
                released_at = ?, released_by = ?, release_reason = ?,
                updated_at = ?
            WHERE business_type = ? AND business_key = ?
              AND owner_execution_id = ? AND state = 'outcome_unknown'
              AND lease_version = ?
            """,
            (
                command.released_at,
                command.released_by,
                command.reason,
                command.released_at,
                command.business_ref.business_type,
                command.business_ref.business_key,
                owner_text,
                int(guard["lease_version"]),
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Callback unknown 解除审计与 Guard 未能原子收敛")
        logger.warning(
            "Callback outcome_unknown 已人工解除: business_type=%s task_id=%s "
            "fencing_token=%s released_by=%s",
            command.business_ref.business_type,
            owner_text,
            int(guard["lease_version"]),
            command.released_by,
        )
        return CallbackReleaseOutcome.RELEASED


__all__ = ["SQLiteCallbackControlStore"]
