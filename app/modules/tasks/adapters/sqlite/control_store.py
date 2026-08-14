"""阶段 2 Task Control 的唯一 SQLite 业务事实写入实现。

本 Store 只使用调用方 UoW 提供的同一条活动连接：不创建连接、不提交、不回滚、不关闭，
也不在内部隐藏重试。Admission、Execution 与 Recovery 的每个公开写方法都要求连接已经处于
显式事务中；网络、模型、文件转换和外部删除不得从本模块调用。

所有条件写均以 Task/Attempt/Step/Recovery Authority 重新核对数据库当前事实。日志只包含内部
Task/Case/Operation ID 和有限原因码，绝不记录 lease token、输入正文、供应商响应或凭据。
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import logging
import sqlite3
from typing import Any, Mapping
from uuid import uuid4

from app.modules.tasks.domain import (
    RecoveryAuthority,
    RecoveryCaseState,
    RecoveryClassification,
    RecoveryDecisionKind,
    RecoveryObservationKind,
    RecoveryOperationKind,
    RecoveryOperationState,
    StepEffectKind,
    StepReplayPolicy,
    TaskAttempt,
    TaskAttemptState,
    TaskAttemptTransition,
    TaskBusinessRef,
    TaskEvent,
    TaskExecutionAuthority,
    TaskId,
    TaskRecord,
    TaskRecoveryCandidate,
    TaskRecoveryCase,
    TaskRecoveryDecision,
    TaskRecoveryObservation,
    TaskRecoveryOperation,
    TaskRecoveryStepResolution,
    TaskRecoveryTerminalProjection,
    TaskSnapshot,
    TaskState,
    TaskStep,
    TaskStepAttempt,
    TaskStepCheckpoint,
    TaskStepState,
    TaskStepTransition,
    TaskTransition,
    apply_recovery_decision,
    apply_recovery_step_resolution,
    converge_recovery_operation,
    create_recovery_case,
    transition_attempt_state,
    transition_step_state,
    transition_task_state,
)
from app.modules.tasks.ports import (
    CallbackAdmissionConflict,
    TaskAdmissionOutcome,
    TaskAdmissionRequest,
    TaskAdmissionResult,
    TaskClaimRequest,
    TaskExecutionClaimResult,
    TaskExecutionMutationOutcome,
    TaskDispatchDeferralCommand,
    TaskHeartbeatCommand,
    TaskHeartbeatResult,
    TaskProgressCommand,
    PersistedTaskExecutionInput,
    TaskRecoveryClaimRequest,
    TaskRecoveryClaimResult,
    TaskRecoveryClassificationCommand,
    TaskRecoveryClassificationResult,
    TaskRecoveryHeartbeatCommand,
    TaskRecoveryHeartbeatResult,
    TaskRecoveryMutationOutcome,
    TaskRecoveryOperationIntentCommand,
    TaskStepCompletionCommand,
    TaskStepIntentCommand,
    TaskStepSkipCommand,
    TaskTerminalCommand,
    require_persisted_utc,
    validate_task_admission_batch,
)


logger = logging.getLogger(__name__)

_ACTIVE_TASK_STATES = (
    TaskState.ACCEPTED.value,
    TaskState.RUNNING.value,
    TaskState.RECOVERY_REQUIRED.value,
)
_SAFE_RETRY_OBSERVATIONS = {
    RecoveryObservationKind.DEFINITELY_NOT_SENT.value,
    RecoveryObservationKind.NO_EFFECT_CONFIRMED.value,
    RecoveryObservationKind.COMPENSATION_CONFIRMED.value,
}


def _canonical_json(value: object) -> str:
    """生成稳定 JSON；拒绝 NaN/Infinity，避免数据库中出现不可移植载荷。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _reject_json_constant(value: str) -> None:
    """拒绝 Python JSON 解码器默认接受的 NaN/Infinity 扩展值。"""

    raise ValueError(f"input_payload 包含非法 JSON 常量: {value}")


def _optional_checkpoint(row: sqlite3.Row) -> TaskStepCheckpoint | None:
    code = str(row["checkpoint_code"])
    if not code:
        return None
    return TaskStepCheckpoint(
        code=code,
        result_ref=str(row["result_ref"]),
        result_digest=str(row["result_digest"]),
        external_ref=str(row["external_ref"]),
        observation_ref=str(row["observation_ref"]),
    )


def _checkpoint_columns(
    checkpoint: TaskStepCheckpoint | None,
) -> tuple[str, str, str, str, str]:
    if checkpoint is None:
        return "", "", "", "", ""
    return (
        checkpoint.code,
        checkpoint.result_ref,
        checkpoint.result_digest,
        checkpoint.external_ref,
        checkpoint.observation_ref,
    )


class SQLiteTaskControlStore:
    """实现 Task Admission、Execution、Recovery、Event 与 runnable 查询 Port。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection 必须是 sqlite3.Connection")
        self._connection = connection

    def _require_write_transaction(self) -> None:
        if not self._connection.in_transaction:
            raise RuntimeError("Task Control 写入必须位于显式 UnitOfWork 事务中")

    def _task_row(self, task_id: TaskId | str) -> sqlite3.Row | None:
        value = task_id.value if isinstance(task_id, TaskId) else task_id
        return self._connection.execute(
            "SELECT * FROM llm_task_executions WHERE execution_id = ?",
            (value,),
        ).fetchone()

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=TaskId(str(row["execution_id"])),
            task_type=str(row["business_type"]),
            business_ref=TaskBusinessRef(
                str(row["business_type"]),
                str(row["business_key"]),
            ),
            state=TaskState(str(row["execution_state"])),
            current_attempt_no=int(row["current_attempt_no"]),
            fencing_token=int(row["fencing_token"]),
            row_version=int(row["row_version"]),
            recovery_generation=int(row["recovery_generation"]),
            current_recovery_case_id=str(row["current_recovery_case_id"] or ""),
            recovery_reason_code=str(row["recovery_reason_code"]),
            retry_from_step_key=str(row["retry_from_step_key"] or ""),
        )

    def get_task(self, task_id: TaskId) -> TaskRecord | None:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        row = self._task_row(task_id)
        return self._task_from_row(row) if row is not None else None

    @staticmethod
    def _snapshot_from_read_row(row: sqlite3.Row) -> TaskSnapshot:
        """把 execution 与可选 latest 投影转换为只读快照。

        ``llm_task_executions`` 保存每次执行自己的状态；Callback attempt 则按 execution
        从追加审计事件恢复。这样即使同一业务键已经产生新的 latest，旧任务按 TaskId
        回读时也不会把已发生的投递次数错误回落为 0，更不会借用其他 execution 的计数。
        """

        return TaskSnapshot(
            task_id=TaskId(str(row["execution_id"])),
            task_type=str(row["business_type"]),
            business_ref=TaskBusinessRef(
                str(row["business_type"]),
                str(row["business_key"]),
            ),
            execution_state=str(row["execution_state"]),
            public_status=str(row["public_status"]),
            progress=float(row["progress"]),
            message=str(row["message"]),
            callback_status=str(row["callback_status"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            callback_attempts=int(row["callback_attempts"]),
        )

    def read_snapshot_by_id(self, task_id: TaskId) -> TaskSnapshot | None:
        """按不可变 TaskId 读取同一次执行，不修改任何控制事实。"""

        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        row = self._connection.execute(
            """
            SELECT e.execution_id, e.business_type, e.business_key,
                   e.execution_state, e.public_status, e.progress, e.message,
                   e.callback_status, e.created_at, e.updated_at,
                   COALESCE((
                       SELECT MAX(a.callback_attempt)
                       FROM callback_delivery_attempt_events AS a
                       WHERE a.owner_execution_id = e.execution_id
                   ), 0) AS callback_attempts
            FROM llm_task_executions AS e
            WHERE e.execution_id = ?
            """,
            (task_id.value,),
        ).fetchone()
        return self._snapshot_from_read_row(row) if row is not None else None

    def read_latest_snapshot(
        self,
        business_ref: TaskBusinessRef,
    ) -> TaskSnapshot | None:
        """按业务键读取 v2 latest；公开投影与 execution 身份必须来自同一行连接。"""

        if not isinstance(business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        row = self._connection.execute(
            """
            SELECT e.execution_id, e.business_type, e.business_key,
                   e.execution_state, l.status AS public_status,
                   l.progress, l.message, l.callback_status,
                   l.created_at, l.updated_at, l.callback_attempts
            FROM llm_tasks AS l
            JOIN llm_task_executions AS e ON e.execution_id = l.execution_id
            WHERE l.business_type = ? AND l.business_key = ?
            """,
            (business_ref.business_type, business_ref.business_key),
        ).fetchone()
        return self._snapshot_from_read_row(row) if row is not None else None

    def _step_row(self, task_id: TaskId, step_key: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM task_steps WHERE task_id = ? AND step_key = ?",
            (task_id.value, step_key),
        ).fetchone()

    @staticmethod
    def _step_from_row(row: sqlite3.Row) -> TaskStep:
        return TaskStep(
            task_id=TaskId(str(row["task_id"])),
            step_key=str(row["step_key"]),
            definition_version=int(row["definition_version"]),
            effect_kind=StepEffectKind(str(row["effect_kind"])),
            replay_policy=StepReplayPolicy(str(row["replay_policy"])),
            state=TaskStepState(str(row["state"])),
            current_step_attempt_no=int(row["current_step_attempt_no"]),
            idempotency_key=str(row["idempotency_key"]),
            checkpoint=_optional_checkpoint(row),
            row_version=int(row["row_version"]),
        )

    def get_step(self, task_id: TaskId, step_key: str) -> TaskStep | None:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(step_key, str) or not step_key.strip():
            raise ValueError("step_key 必须是非空 str")
        row = self._step_row(task_id, step_key.strip())
        return self._step_from_row(row) if row is not None else None

    def _step_attempt_row(
        self,
        task_id: TaskId,
        step_key: str,
        step_attempt_no: int,
    ) -> sqlite3.Row | None:
        return self._connection.execute(
            """
            SELECT * FROM task_step_attempts
            WHERE task_id = ? AND step_key = ? AND step_attempt_no = ?
            """,
            (task_id.value, step_key, step_attempt_no),
        ).fetchone()

    @staticmethod
    def _step_attempt_from_row(row: sqlite3.Row) -> TaskStepAttempt:
        return TaskStepAttempt(
            task_id=TaskId(str(row["task_id"])),
            step_key=str(row["step_key"]),
            step_attempt_no=int(row["step_attempt_no"]),
            task_attempt_no=int(row["task_attempt_no"]),
            fencing_token=int(row["fencing_token"]),
            state=TaskStepState(str(row["state"])),
            idempotency_key=str(row["idempotency_key"]),
            intent_at=str(row["intent_at"]),
            result_at=str(row["result_at"] or ""),
            checkpoint=_optional_checkpoint(row),
            error_code=str(row["error_code"]),
        )

    def get_step_attempt(
        self,
        task_id: TaskId,
        step_key: str,
        step_attempt_no: int,
    ) -> TaskStepAttempt | None:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(step_key, str) or not step_key.strip():
            raise ValueError("step_key 必须是非空 str")
        if type(step_attempt_no) is not int or step_attempt_no <= 0:
            raise ValueError("step_attempt_no 必须是正整数")
        row = self._step_attempt_row(task_id, step_key.strip(), step_attempt_no)
        return self._step_attempt_from_row(row) if row is not None else None

    def get_admission_conflict(
        self,
        business_ref: TaskBusinessRef,
    ) -> CallbackAdmissionConflict:
        if not isinstance(business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        guard = self._connection.execute(
            """
            SELECT state FROM callback_delivery_guards
            WHERE business_type = ? AND business_key = ?
            """,
            (business_ref.business_type, business_ref.business_key),
        ).fetchone()
        state = str(guard[0]) if guard is not None else ""
        if state == "sending":
            return CallbackAdmissionConflict.SENDING
        if state == "outcome_unknown":
            return CallbackAdmissionConflict.OUTCOME_UNKNOWN
        # Guard 是受理冲突的唯一权威。人工解除只把 Guard 释放为 idle，旧 execution/latest
        # 的 outcome_unknown 仍作为历史事实保留；若继续读取旧投影，新任务会被永久阻塞，
        # 与既有 Report “保留 unknown 事实但允许重新受理”合同相冲突。
        return CallbackAdmissionConflict.NONE

    def _classify_admission(
        self,
        request: TaskAdmissionRequest[Any],
    ) -> TaskAdmissionOutcome:
        callback = self.get_admission_conflict(request.business_ref)
        if callback is CallbackAdmissionConflict.SENDING:
            return TaskAdmissionOutcome.CALLBACK_SENDING
        if callback is CallbackAdmissionConflict.OUTCOME_UNKNOWN:
            return TaskAdmissionOutcome.CALLBACK_OUTCOME_UNKNOWN
        duplicate_id = self._connection.execute(
            "SELECT 1 FROM llm_task_executions WHERE execution_id = ?",
            (request.task_id.value,),
        ).fetchone()
        if duplicate_id is not None:
            return TaskAdmissionOutcome.ACTIVE_TASK_CONFLICT
        latest = self._connection.execute(
            """
            SELECT e.execution_state
            FROM llm_tasks AS l
            JOIN llm_task_executions AS e ON e.execution_id = l.execution_id
            WHERE l.business_type = ? AND l.business_key = ?
            """,
            (request.business_ref.business_type, request.business_ref.business_key),
        ).fetchone()
        if latest is not None and str(latest[0]) in _ACTIVE_TASK_STATES:
            return TaskAdmissionOutcome.ACTIVE_TASK_CONFLICT
        return TaskAdmissionOutcome.ACCEPTED

    def _next_dispatch_sequence(self) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(dispatch_sequence), 0) + 1 FROM llm_task_executions"
        ).fetchone()
        return int(row[0])

    def _append_event(
        self,
        task_id: TaskId,
        *,
        event_type: str,
        created_at: str,
        attempt_no: int = 0,
        step_key: str = "",
        reason_code: str = "",
        metadata: Mapping[str, str | int | bool] | None = None,
    ) -> None:
        """在当前状态事务中追加有界事件；不得由调用方单独提交。"""

        task_row = self._task_row(task_id)
        if task_row is None:
            raise RuntimeError("追加 Task Event 时 Task 不存在")
        sequence_row = self._connection.execute(
            "SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM task_events WHERE task_id = ?",
            (task_id.value,),
        ).fetchone()
        self._connection.execute(
            """
            INSERT INTO task_events (
                event_id, task_id, sequence_no, event_type, attempt_no,
                step_key, reason_code, metadata_json, trace_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid4().hex,
                task_id.value,
                int(sequence_row[0]),
                event_type,
                attempt_no,
                step_key,
                reason_code,
                _canonical_json(dict(metadata or {})),
                str(task_row["trace_id"]),
                created_at,
            ),
        )

    def _insert_admission(
        self,
        request: TaskAdmissionRequest[Any],
        dispatch_sequence: int,
    ) -> TaskRecord:
        batch_id = request.batch.batch_id if request.batch is not None else None
        batch_sequence = request.batch.sequence if request.batch is not None else None
        input_payload = _canonical_json(dict(request.input_payload))
        request_payload = _canonical_json(dict(request.public_request_payload))
        values = (
            request.task_id.value,
            request.business_ref.business_type,
            request.business_ref.business_key,
            request.input_schema_version,
            input_payload,
            batch_id,
            batch_sequence,
            dispatch_sequence,
            TaskState.ACCEPTED.value,
            request.initial_public_status,
            request.trace_id,
            request.accepted_at,
            request.accepted_at,
        )
        self._connection.execute(
            """
            INSERT INTO llm_task_executions (
                execution_id, business_type, business_key, input_schema_version,
                input_payload, batch_id, batch_sequence, dispatch_sequence,
                execution_state, public_status, trace_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        self._connection.execute(
            """
            INSERT INTO llm_tasks (
                business_type, business_key, execution_id, request_payload,
                status, progress, message, callback_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, '', 'pending', ?, ?)
            ON CONFLICT (business_type, business_key) DO UPDATE SET
                execution_id = excluded.execution_id,
                request_payload = excluded.request_payload,
                status = excluded.status,
                progress = 0,
                message = '',
                result_payload = NULL,
                callback_status = 'pending',
                callback_attempts = 0,
                last_callback_error = '',
                callback_claim_id = '',
                callback_claim_expires_at = NULL,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at
            """,
            (
                request.business_ref.business_type,
                request.business_ref.business_key,
                request.task_id.value,
                request_payload,
                request.initial_public_status,
                request.accepted_at,
                request.accepted_at,
            ),
        )
        task = TaskRecord(
            task_id=request.task_id,
            task_type=request.business_ref.business_type,
            business_ref=request.business_ref,
            state=TaskState.ACCEPTED,
            current_attempt_no=0,
            fencing_token=0,
            row_version=1,
            recovery_generation=0,
        )
        self._append_event(
            request.task_id,
            event_type="task.accepted",
            created_at=request.accepted_at,
            metadata={"dispatch_sequence": dispatch_sequence},
        )
        return task

    def admit_one(self, request: TaskAdmissionRequest[Any]) -> TaskAdmissionResult:
        return self.admit_many((request,))[0]

    def admit_many(
        self,
        requests: tuple[TaskAdmissionRequest[Any], ...],
    ) -> tuple[TaskAdmissionResult, ...]:
        self._require_write_transaction()
        validate_task_admission_batch(requests)
        if len({item.task_id for item in requests}) != len(requests):
            raise ValueError("同一批量受理不得包含重复 task_id")
        if len({item.business_ref for item in requests}) != len(requests):
            raise ValueError("同一批量受理不得包含重复业务键")
        outcomes = tuple(self._classify_admission(item) for item in requests)
        if any(item is not TaskAdmissionOutcome.ACCEPTED for item in outcomes):
            return tuple(
                TaskAdmissionResult(
                    task_id=request.task_id,
                    business_ref=request.business_ref,
                    outcome=(
                        outcome
                        if outcome is not TaskAdmissionOutcome.ACCEPTED
                        else TaskAdmissionOutcome.BATCH_REJECTED
                    ),
                )
                for request, outcome in zip(requests, outcomes, strict=True)
            )
        first_sequence = self._next_dispatch_sequence()
        results: list[TaskAdmissionResult] = []
        for offset, request in enumerate(requests):
            task = self._insert_admission(request, first_sequence + offset)
            results.append(
                TaskAdmissionResult(
                    task_id=request.task_id,
                    business_ref=request.business_ref,
                    outcome=TaskAdmissionOutcome.ACCEPTED,
                    task=task,
                )
            )
        logger.info(
            "Task Control 受理事务已写入: task_type=%s count=%d first_dispatch_sequence=%d",
            requests[0].task_type,
            len(requests),
            first_sequence,
        )
        return tuple(results)

    def list_for_task(
        self,
        task_id: TaskId,
        *,
        after_sequence_no: int = 0,
        limit: int = 100,
    ) -> tuple[TaskEvent, ...]:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if type(after_sequence_no) is not int or after_sequence_no < 0:
            raise ValueError("after_sequence_no 必须是非负整数")
        self._validate_limit(limit)
        rows = self._connection.execute(
            """
            SELECT * FROM task_events
            WHERE task_id = ? AND sequence_no > ?
            ORDER BY sequence_no ASC LIMIT ?
            """,
            (task_id.value, after_sequence_no, limit),
        ).fetchall()
        return tuple(self._event_from_row(row) for row in rows)

    def list_by_type(
        self,
        event_type: str,
        *,
        created_at_or_after: str,
        limit: int = 100,
    ) -> tuple[TaskEvent, ...]:
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("event_type 必须是非空 str")
        created_at_or_after = require_persisted_utc(
            created_at_or_after,
            name="created_at_or_after",
        )
        self._validate_limit(limit)
        rows = self._connection.execute(
            """
            SELECT * FROM task_events
            WHERE event_type = ? AND created_at >= ?
            ORDER BY created_at ASC, event_id ASC LIMIT ?
            """,
            (event_type.strip(), created_at_or_after, limit),
        ).fetchall()
        return tuple(self._event_from_row(row) for row in rows)

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if type(limit) is not int or limit < 1 or limit > 1000:
            raise ValueError("limit 必须位于 1..1000")

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> TaskEvent:
        metadata = json.loads(str(row["metadata_json"]))
        if not isinstance(metadata, dict):
            raise ValueError("Task Event metadata_json 必须是对象")
        return TaskEvent.create(
            task_id=TaskId(str(row["task_id"])),
            sequence_no=int(row["sequence_no"]),
            event_type=str(row["event_type"]),
            attempt_no=int(row["attempt_no"]),
            step_key=str(row["step_key"]),
            reason_code=str(row["reason_code"]),
            metadata=metadata,
            trace_id=str(row["trace_id"]),
            created_at=str(row["created_at"]),
        )

    def _attempt_row(self, task_id: TaskId, attempt_no: int) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM task_attempts WHERE task_id = ? AND attempt_no = ?",
            (task_id.value, attempt_no),
        ).fetchone()

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> TaskAttempt:
        authority = TaskExecutionAuthority(
            task_id=TaskId(str(row["task_id"])),
            attempt_no=int(row["attempt_no"]),
            owner_id=str(row["owner_id"]),
            lease_token=str(row["lease_token"]),
            fencing_token=int(row["fencing_token"]),
            lease_expires_at=str(row["lease_expires_at"]),
        )
        return TaskAttempt(
            authority=authority,
            state=TaskAttemptState(str(row["state"])),
            claimed_at=str(row["claimed_at"]),
            heartbeat_at=str(row["heartbeat_at"]),
            started_at=str(row["started_at"] or ""),
            completed_at=str(row["completed_at"] or ""),
            error_code=str(row["error_code"]),
        )

    def _execution_authority_outcome(
        self,
        authority: TaskExecutionAuthority,
        *,
        observed_at: str,
    ) -> tuple[TaskExecutionMutationOutcome, sqlite3.Row | None, sqlite3.Row | None]:
        """在每条执行写路径统一复核完整 Authority 与数据库租约。"""

        task_row = self._task_row(authority.task_id)
        attempt_row = self._attempt_row(authority.task_id, authority.attempt_no)
        if task_row is None or attempt_row is None:
            return TaskExecutionMutationOutcome.MISSING, task_row, attempt_row
        matches = (
            int(task_row["current_attempt_no"]) == authority.attempt_no
            and int(task_row["fencing_token"]) == authority.fencing_token
            and str(attempt_row["owner_id"]) == authority.owner_id
            and str(attempt_row["lease_token"]) == authority.lease_token
            and int(attempt_row["fencing_token"]) == authority.fencing_token
            and str(attempt_row["lease_expires_at"])
            == authority.lease_expires_at
        )
        if not matches:
            return TaskExecutionMutationOutcome.AUTHORITY_LOST, task_row, attempt_row
        if observed_at >= str(attempt_row["lease_expires_at"]):
            return TaskExecutionMutationOutcome.LEASE_EXPIRED, task_row, attempt_row
        return TaskExecutionMutationOutcome.APPLIED, task_row, attempt_row

    def _is_latest(self, row: sqlite3.Row) -> bool:
        latest = self._connection.execute(
            """
            SELECT execution_id FROM llm_tasks
            WHERE business_type = ? AND business_key = ?
            """,
            (str(row["business_type"]), str(row["business_key"])),
        ).fetchone()
        return latest is not None and str(latest[0]) == str(row["execution_id"])

    def _analysis_predecessors_terminal(self, row: sqlite3.Row) -> bool:
        if str(row["business_type"]) != "file":
            return True
        batch_id = row["batch_id"]
        batch_sequence = row["batch_sequence"]
        if batch_id is None or batch_sequence is None:
            return False
        predecessor = self._connection.execute(
            """
            SELECT 1 FROM llm_task_executions
            WHERE business_type = 'file' AND batch_id = ?
              AND batch_sequence < ?
              AND execution_state IN ('accepted','running','recovery_required')
            LIMIT 1
            """,
            (str(batch_id), int(batch_sequence)),
        ).fetchone()
        return predecessor is None

    def claim(self, request: TaskClaimRequest) -> TaskExecutionClaimResult:
        self._require_write_transaction()
        row = self._task_row(request.task_id)
        if row is None:
            return TaskExecutionClaimResult(TaskExecutionMutationOutcome.MISSING)
        if (
            str(row["business_type"]) != request.task_type
            or str(row["execution_state"]) != TaskState.ACCEPTED.value
            or not self._is_latest(row)
            or not self._analysis_predecessors_terminal(row)
            or (
                row["next_dispatch_at"] is not None
                and str(row["next_dispatch_at"]) > request.claimed_at
            )
        ):
            return TaskExecutionClaimResult(TaskExecutionMutationOutcome.NOT_RUNNABLE)
        if request.lease_expires_at <= request.claimed_at:
            return TaskExecutionClaimResult(TaskExecutionMutationOutcome.LEASE_EXPIRED)

        old_version = int(row["row_version"])
        attempt_no = int(row["current_attempt_no"]) + 1
        fencing_token = int(row["fencing_token"]) + 1
        cursor = self._connection.execute(
            """
            UPDATE llm_task_executions
            SET execution_state = 'running', current_attempt_no = ?,
                fencing_token = ?, row_version = row_version + 1,
                next_dispatch_at = NULL, last_dispatch_error = '', updated_at = ?
            WHERE execution_id = ? AND execution_state = 'accepted'
              AND row_version = ? AND current_attempt_no = ? AND fencing_token = ?
            """,
            (
                attempt_no,
                fencing_token,
                request.claimed_at,
                request.task_id.value,
                old_version,
                int(row["current_attempt_no"]),
                int(row["fencing_token"]),
            ),
        )
        if cursor.rowcount != 1:
            return TaskExecutionClaimResult(TaskExecutionMutationOutcome.NOT_RUNNABLE)
        self._connection.execute(
            """
            INSERT INTO task_attempts (
                task_id, attempt_no, state, owner_id, instance_start_id,
                process_id, executor_name, worker_slot, lease_token,
                fencing_token, claimed_at, heartbeat_at, lease_expires_at
            ) VALUES (?, ?, 'leased', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request.task_id.value,
                attempt_no,
                request.owner_id,
                request.owner.instance_start_id,
                request.owner.process_id,
                request.owner.executor_name,
                request.owner.worker_slot,
                request.lease_token,
                fencing_token,
                request.claimed_at,
                request.claimed_at,
                request.lease_expires_at,
            ),
        )
        self._append_event(
            request.task_id,
            event_type="task.claimed",
            created_at=request.claimed_at,
            attempt_no=attempt_no,
        )
        updated_row = self._task_row(request.task_id)
        attempt_row = self._attempt_row(request.task_id, attempt_no)
        assert updated_row is not None and attempt_row is not None
        logger.info(
            "Task Control 执行权已领取: task_id=%s task_type=%s attempt_no=%d fencing=%d",
            request.task_id,
            request.task_type,
            attempt_no,
            fencing_token,
        )
        return TaskExecutionClaimResult(
            TaskExecutionMutationOutcome.APPLIED,
            self._task_from_row(updated_row),
            self._attempt_from_row(attempt_row),
        )

    def start(
        self,
        authority: TaskExecutionAuthority,
        *,
        started_at: str,
    ) -> TaskExecutionMutationOutcome:
        self._require_write_transaction()
        started_at = require_persisted_utc(started_at, name="started_at")
        outcome, task_row, attempt_row = self._execution_authority_outcome(
            authority,
            observed_at=started_at,
        )
        if outcome is not TaskExecutionMutationOutcome.APPLIED:
            return outcome
        assert task_row is not None and attempt_row is not None
        if (
            str(task_row["execution_state"]) != TaskState.RUNNING.value
            or str(attempt_row["state"]) != TaskAttemptState.LEASED.value
        ):
            return TaskExecutionMutationOutcome.INVALID_STATE
        cursor = self._connection.execute(
            """
            UPDATE task_attempts SET state = 'running', started_at = ?
            WHERE task_id = ? AND attempt_no = ? AND state = 'leased'
              AND lease_token = ? AND fencing_token = ?
            """,
            (
                started_at,
                authority.task_id.value,
                authority.attempt_no,
                authority.lease_token,
                authority.fencing_token,
            ),
        )
        if cursor.rowcount != 1:
            return TaskExecutionMutationOutcome.AUTHORITY_LOST
        self._connection.execute(
            """
            UPDATE llm_task_executions
            SET started_at = COALESCE(started_at, ?), updated_at = ?,
                row_version = row_version + 1
            WHERE execution_id = ? AND current_attempt_no = ? AND fencing_token = ?
            """,
            (
                started_at,
                started_at,
                authority.task_id.value,
                authority.attempt_no,
                authority.fencing_token,
            ),
        )
        self._append_event(
            authority.task_id,
            event_type="task.started",
            created_at=started_at,
            attempt_no=authority.attempt_no,
        )
        return TaskExecutionMutationOutcome.APPLIED

    def heartbeat(self, command: TaskHeartbeatCommand) -> TaskHeartbeatResult:
        self._require_write_transaction()
        # DTO 已拒绝非单调续租；Store 仍做最后一道防御，避免被绕过构造器的对象缩短租约。
        if command.lease_expires_at <= command.authority.lease_expires_at:
            return TaskHeartbeatResult(TaskExecutionMutationOutcome.INVALID_STATE)
        outcome, task_row, attempt_row = self._execution_authority_outcome(
            command.authority,
            observed_at=command.heartbeat_at,
        )
        if outcome is not TaskExecutionMutationOutcome.APPLIED:
            return TaskHeartbeatResult(outcome)
        assert task_row is not None and attempt_row is not None
        if (
            str(task_row["execution_state"]) != TaskState.RUNNING.value
            or str(attempt_row["state"])
            not in {TaskAttemptState.LEASED.value, TaskAttemptState.RUNNING.value}
        ):
            return TaskHeartbeatResult(TaskExecutionMutationOutcome.INVALID_STATE)
        cursor = self._connection.execute(
            """
            UPDATE task_attempts
            SET heartbeat_at = ?, lease_expires_at = ?
            WHERE task_id = ? AND attempt_no = ? AND lease_token = ?
              AND fencing_token = ? AND lease_expires_at = ?
              AND state IN ('leased','running')
            """,
            (
                command.heartbeat_at,
                command.lease_expires_at,
                command.authority.task_id.value,
                command.authority.attempt_no,
                command.authority.lease_token,
                command.authority.fencing_token,
                command.authority.lease_expires_at,
            ),
        )
        if cursor.rowcount != 1:
            return TaskHeartbeatResult(TaskExecutionMutationOutcome.AUTHORITY_LOST)
        renewed = replace(
            command.authority,
            lease_expires_at=command.lease_expires_at,
        )
        return TaskHeartbeatResult(TaskExecutionMutationOutcome.APPLIED, renewed)

    def defer_dispatch(
        self,
        command: TaskDispatchDeferralCommand,
    ) -> TaskExecutionMutationOutcome:
        self._require_write_transaction()
        task_row = self._task_row(command.task_id)
        if task_row is None:
            return TaskExecutionMutationOutcome.MISSING
        if str(task_row["business_type"]) != command.task_type:
            return TaskExecutionMutationOutcome.NOT_RUNNABLE
        if (
            str(task_row["execution_state"]) != TaskState.ACCEPTED.value
            or not self._is_latest(task_row)
        ):
            return TaskExecutionMutationOutcome.NOT_RUNNABLE
        cursor = self._connection.execute(
            """
            UPDATE llm_task_executions
            SET next_dispatch_at = ?, last_dispatch_error = ?, updated_at = ?,
                row_version = row_version + 1
            WHERE execution_id = ? AND business_type = ?
              AND execution_state = 'accepted' AND row_version = ?
            """,
            (
                command.next_dispatch_at,
                command.reason_code[:512],
                command.deferred_at,
                command.task_id.value,
                command.task_type,
                int(task_row["row_version"]),
            ),
        )
        if cursor.rowcount != 1:
            return TaskExecutionMutationOutcome.STALE_LATEST
        self._append_event(
            command.task_id,
            event_type="task.dispatch_deferred",
            created_at=command.deferred_at,
            reason_code=command.reason_code[:128],
        )
        return TaskExecutionMutationOutcome.APPLIED

    @staticmethod
    def _same_step_intent(
        current: TaskStep,
        attempt: TaskStepAttempt | None,
        command: TaskStepIntentCommand,
    ) -> bool:
        candidate = command.step
        return bool(
            attempt is not None
            and current.state is TaskStepState.RUNNING
            and attempt.state is TaskStepState.RUNNING
            and current.task_id == candidate.task_id
            and current.step_key == candidate.step_key
            and current.definition_version == candidate.definition_version
            and current.effect_kind is candidate.effect_kind
            and current.replay_policy is candidate.replay_policy
            and current.idempotency_key == candidate.idempotency_key
            and current.checkpoint == candidate.checkpoint
            and current.current_step_attempt_no
            == candidate.current_step_attempt_no + 1
            and current.row_version == candidate.row_version + 1
            and attempt.task_attempt_no == command.authority.attempt_no
            and attempt.fencing_token == command.authority.fencing_token
            and attempt.idempotency_key == candidate.idempotency_key
            and attempt.intent_at == command.intent_at
        )

    def begin_step(self, command: TaskStepIntentCommand) -> TaskExecutionMutationOutcome:
        self._require_write_transaction()
        outcome, task_row, attempt_row = self._execution_authority_outcome(
            command.authority,
            observed_at=command.intent_at,
        )
        if outcome is not TaskExecutionMutationOutcome.APPLIED:
            return outcome
        assert task_row is not None and attempt_row is not None
        if (
            str(task_row["execution_state"]) != TaskState.RUNNING.value
            or str(attempt_row["state"]) != TaskAttemptState.RUNNING.value
            or command.step.state is not TaskStepState.PENDING
        ):
            return TaskExecutionMutationOutcome.INVALID_STATE

        current_row = self._step_row(command.authority.task_id, command.step.step_key)
        if current_row is not None:
            current = self._step_from_row(current_row)
            if current.state is TaskStepState.RUNNING:
                current_attempt = self.get_step_attempt(
                    current.task_id,
                    current.step_key,
                    current.current_step_attempt_no,
                )
                return (
                    TaskExecutionMutationOutcome.DUPLICATE_STEP_INTENT
                    if self._same_step_intent(current, current_attempt, command)
                    else TaskExecutionMutationOutcome.INVALID_STATE
                )
            if current != command.step:
                return TaskExecutionMutationOutcome.INVALID_STATE
            base = current
        else:
            if command.step.current_step_attempt_no != 0 or command.step.row_version != 0:
                return TaskExecutionMutationOutcome.INVALID_STATE
            base = command.step

        next_attempt_no = base.current_step_attempt_no + 1
        next_row_version = base.row_version + 1
        checkpoint = _checkpoint_columns(base.checkpoint)
        if current_row is None:
            self._connection.execute(
                """
                INSERT INTO task_steps (
                    task_id, step_key, definition_version, effect_kind,
                    replay_policy, state, checkpoint_code, result_ref,
                    result_digest, external_ref, observation_ref,
                    current_step_attempt_no, idempotency_key, row_version
                ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    base.task_id.value,
                    base.step_key,
                    base.definition_version,
                    base.effect_kind.value,
                    base.replay_policy.value,
                    *checkpoint,
                    next_attempt_no,
                    base.idempotency_key,
                    next_row_version,
                ),
            )
        else:
            cursor = self._connection.execute(
                """
                UPDATE task_steps
                SET state = 'running', current_step_attempt_no = ?,
                    row_version = row_version + 1
                WHERE task_id = ? AND step_key = ? AND state = 'pending'
                  AND current_step_attempt_no = ? AND row_version = ?
                """,
                (
                    next_attempt_no,
                    base.task_id.value,
                    base.step_key,
                    base.current_step_attempt_no,
                    base.row_version,
                ),
            )
            if cursor.rowcount != 1:
                return TaskExecutionMutationOutcome.INVALID_STATE
        self._connection.execute(
            """
            INSERT INTO task_step_attempts (
                task_id, step_key, step_attempt_no, task_attempt_no,
                fencing_token, state, idempotency_key, intent_at
            ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?)
            """,
            (
                base.task_id.value,
                base.step_key,
                next_attempt_no,
                command.authority.attempt_no,
                command.authority.fencing_token,
                base.idempotency_key,
                command.intent_at,
            ),
        )
        self._append_event(
            command.authority.task_id,
            event_type="task.step_intent_recorded",
            created_at=command.intent_at,
            attempt_no=command.authority.attempt_no,
            step_key=base.step_key,
            metadata={"step_attempt_no": next_attempt_no},
        )
        return TaskExecutionMutationOutcome.APPLIED

    def complete_step(
        self,
        command: TaskStepCompletionCommand,
    ) -> TaskExecutionMutationOutcome:
        self._require_write_transaction()
        outcome, task_row, task_attempt_row = self._execution_authority_outcome(
            command.authority,
            observed_at=command.completed_at,
        )
        if outcome is not TaskExecutionMutationOutcome.APPLIED:
            return outcome
        assert task_row is not None and task_attempt_row is not None
        if (
            str(task_row["execution_state"]) != TaskState.RUNNING.value
            or str(task_attempt_row["state"]) != TaskAttemptState.RUNNING.value
        ):
            return TaskExecutionMutationOutcome.INVALID_STATE
        step_row = self._step_row(command.authority.task_id, command.step_key)
        step_attempt_row = self._step_attempt_row(
            command.authority.task_id,
            command.step_key,
            command.step_attempt_no,
        )
        if step_row is None or step_attempt_row is None:
            return TaskExecutionMutationOutcome.MISSING
        step = self._step_from_row(step_row)
        if (
            step.state is not TaskStepState.RUNNING
            or step.current_step_attempt_no != command.step_attempt_no
            or str(step_attempt_row["state"]) != TaskStepState.RUNNING.value
            or int(step_attempt_row["task_attempt_no"])
            != command.authority.attempt_no
            or int(step_attempt_row["fencing_token"])
            != command.authority.fencing_token
        ):
            return TaskExecutionMutationOutcome.INVALID_STATE
        target = transition_step_state(step.state, command.transition)
        checkpoint = _checkpoint_columns(command.checkpoint)
        attempt_cursor = self._connection.execute(
            """
            UPDATE task_step_attempts
            SET state = ?, result_at = ?, checkpoint_code = ?, result_ref = ?,
                result_digest = ?, external_ref = ?, observation_ref = ?,
                error_code = ?
            WHERE task_id = ? AND step_key = ? AND step_attempt_no = ?
              AND state = 'running' AND task_attempt_no = ? AND fencing_token = ?
            """,
            (
                target.value,
                command.completed_at,
                *checkpoint,
                command.error_code,
                command.authority.task_id.value,
                command.step_key,
                command.step_attempt_no,
                command.authority.attempt_no,
                command.authority.fencing_token,
            ),
        )
        step_cursor = self._connection.execute(
            """
            UPDATE task_steps
            SET state = ?, checkpoint_code = ?, result_ref = ?,
                result_digest = ?, external_ref = ?, observation_ref = ?,
                row_version = row_version + 1
            WHERE task_id = ? AND step_key = ? AND state = 'running'
              AND current_step_attempt_no = ? AND row_version = ?
            """,
            (
                target.value,
                *checkpoint,
                command.authority.task_id.value,
                command.step_key,
                command.step_attempt_no,
                step.row_version,
            ),
        )
        if attempt_cursor.rowcount != 1 or step_cursor.rowcount != 1:
            raise RuntimeError("Step 完成条件写在同一事务内失去一致性")

        event_type = f"task.step_{target.value}"
        reason_code = command.error_code
        if command.transition is TaskStepTransition.MARK_OUTCOME_UNKNOWN:
            isolation = command.recovery_isolation
            assert isolation is not None
            current_task = self._task_from_row(task_row)
            isolated, recovery_case = create_recovery_case(
                current_task,
                case_id=isolation.case_id,
                source_attempt_no=command.authority.attempt_no,
                source_fencing_token=command.authority.fencing_token,
                reason_code=isolation.reason_code,
                policy_version=isolation.policy_version,
                created_at=command.completed_at,
            )
            task_cursor = self._connection.execute(
                """
                UPDATE llm_task_executions
                SET execution_state = 'recovery_required',
                    recovery_generation = ?, current_recovery_case_id = ?,
                    recovery_reason_code = ?, retry_from_step_key = NULL,
                    next_recovery_at = NULL, row_version = row_version + 1,
                    updated_at = ?
                WHERE execution_id = ? AND execution_state = 'running'
                  AND current_attempt_no = ? AND fencing_token = ?
                  AND row_version = ? AND current_recovery_case_id IS NULL
                """,
                (
                    isolated.recovery_generation,
                    recovery_case.case_id,
                    recovery_case.reason_code,
                    command.completed_at,
                    command.authority.task_id.value,
                    command.authority.attempt_no,
                    command.authority.fencing_token,
                    current_task.row_version,
                ),
            )
            attempt_cursor = self._connection.execute(
                """
                UPDATE task_attempts
                SET state = 'abandoned', completed_at = ?, error_code = ?,
                    recovery_reason_code = ?
                WHERE task_id = ? AND attempt_no = ? AND state = 'running'
                  AND lease_token = ? AND fencing_token = ?
                """,
                (
                    command.completed_at,
                    command.error_code,
                    recovery_case.reason_code,
                    command.authority.task_id.value,
                    command.authority.attempt_no,
                    command.authority.lease_token,
                    command.authority.fencing_token,
                ),
            )
            if task_cursor.rowcount != 1 or attempt_cursor.rowcount != 1:
                raise RuntimeError("unknown 隔离未能原子撤销旧执行权")
            self._connection.execute(
                """
                INSERT INTO task_recovery_cases (
                    case_id, task_id, recovery_generation, state,
                    source_attempt_no, source_fencing_token, reason_code,
                    policy_version, created_at
                ) VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?)
                """,
                (
                    recovery_case.case_id,
                    recovery_case.task_id.value,
                    recovery_case.generation,
                    recovery_case.source_attempt_no,
                    recovery_case.source_fencing_token,
                    recovery_case.reason_code,
                    recovery_case.policy_version,
                    recovery_case.created_at,
                ),
            )
            event_type = "task.recovery_isolated"
            reason_code = recovery_case.reason_code
            logger.warning(
                "Task Step 结果未知并已隔离: task_id=%s case_id=%s step_key=%s attempt_no=%d",
                command.authority.task_id,
                recovery_case.case_id,
                command.step_key,
                command.authority.attempt_no,
            )
        self._append_event(
            command.authority.task_id,
            event_type=event_type,
            created_at=command.completed_at,
            attempt_no=command.authority.attempt_no,
            step_key=command.step_key,
            reason_code=reason_code,
            metadata={"step_attempt_no": command.step_attempt_no},
        )
        return TaskExecutionMutationOutcome.APPLIED

    def skip_step(self, command: TaskStepSkipCommand) -> TaskExecutionMutationOutcome:
        self._require_write_transaction()
        outcome, task_row, task_attempt_row = self._execution_authority_outcome(
            command.authority,
            observed_at=command.skipped_at,
        )
        if outcome is not TaskExecutionMutationOutcome.APPLIED:
            return outcome
        assert task_row is not None and task_attempt_row is not None
        if (
            str(task_row["execution_state"]) != TaskState.RUNNING.value
            or str(task_attempt_row["state"]) != TaskAttemptState.RUNNING.value
        ):
            return TaskExecutionMutationOutcome.INVALID_STATE
        current_row = self._step_row(command.authority.task_id, command.step.step_key)
        if current_row is None:
            if command.step.current_step_attempt_no != 0 or command.step.row_version != 0:
                return TaskExecutionMutationOutcome.INVALID_STATE
            current = command.step
            next_attempt_no = 1
            self._connection.execute(
                """
                INSERT INTO task_steps (
                    task_id, step_key, definition_version, effect_kind,
                    replay_policy, state, current_step_attempt_no,
                    idempotency_key, row_version
                ) VALUES (?, ?, ?, ?, ?, 'skipped', 1, ?, 1)
                """,
                (
                    current.task_id.value,
                    current.step_key,
                    current.definition_version,
                    current.effect_kind.value,
                    current.replay_policy.value,
                    current.idempotency_key,
                ),
            )
        else:
            current = self._step_from_row(current_row)
            if current != command.step or current.state is not TaskStepState.PENDING:
                return TaskExecutionMutationOutcome.INVALID_STATE
            next_attempt_no = current.current_step_attempt_no + 1
            cursor = self._connection.execute(
                """
                UPDATE task_steps SET state = 'skipped',
                    current_step_attempt_no = ?, row_version = row_version + 1
                WHERE task_id = ? AND step_key = ? AND state = 'pending'
                  AND current_step_attempt_no = ? AND row_version = ?
                """,
                (
                    next_attempt_no,
                    current.task_id.value,
                    current.step_key,
                    current.current_step_attempt_no,
                    current.row_version,
                ),
            )
            if cursor.rowcount != 1:
                return TaskExecutionMutationOutcome.INVALID_STATE
        self._connection.execute(
            """
            INSERT INTO task_step_attempts (
                task_id, step_key, step_attempt_no, task_attempt_no,
                fencing_token, state, idempotency_key, intent_at,
                result_at, error_code
            ) VALUES (?, ?, ?, ?, ?, 'skipped', ?, ?, ?, ?)
            """,
            (
                current.task_id.value,
                current.step_key,
                next_attempt_no,
                command.authority.attempt_no,
                command.authority.fencing_token,
                current.idempotency_key,
                command.skipped_at,
                command.skipped_at,
                command.reason_code,
            ),
        )
        self._append_event(
            command.authority.task_id,
            event_type="task.step_skipped",
            created_at=command.skipped_at,
            attempt_no=command.authority.attempt_no,
            step_key=current.step_key,
            reason_code=command.reason_code,
            metadata={"step_attempt_no": next_attempt_no},
        )
        return TaskExecutionMutationOutcome.APPLIED

    def update_progress(self, command: TaskProgressCommand) -> TaskExecutionMutationOutcome:
        self._require_write_transaction()
        outcome, task_row, attempt_row = self._execution_authority_outcome(
            command.authority,
            observed_at=command.updated_at,
        )
        if outcome is not TaskExecutionMutationOutcome.APPLIED:
            return outcome
        assert task_row is not None and attempt_row is not None
        if (
            str(task_row["execution_state"]) != TaskState.RUNNING.value
            or str(attempt_row["state"]) != TaskAttemptState.RUNNING.value
        ):
            return TaskExecutionMutationOutcome.INVALID_STATE
        if not self._is_latest(task_row):
            return TaskExecutionMutationOutcome.STALE_LATEST
        execution_cursor = self._connection.execute(
            """
            UPDATE llm_task_executions
            SET progress = ?, message = ?, public_status = ?, updated_at = ?,
                row_version = row_version + 1
            WHERE execution_id = ? AND execution_state = 'running'
              AND current_attempt_no = ? AND fencing_token = ?
            """,
            (
                command.progress,
                command.message,
                command.public_status,
                command.updated_at,
                command.authority.task_id.value,
                command.authority.attempt_no,
                command.authority.fencing_token,
            ),
        )
        latest_cursor = self._connection.execute(
            """
            UPDATE llm_tasks
            SET progress = ?, message = ?, status = ?, updated_at = ?
            WHERE business_type = ? AND business_key = ? AND execution_id = ?
            """,
            (
                command.progress,
                command.message,
                command.public_status,
                command.updated_at,
                str(task_row["business_type"]),
                str(task_row["business_key"]),
                command.authority.task_id.value,
            ),
        )
        if execution_cursor.rowcount != 1 or latest_cursor.rowcount != 1:
            return TaskExecutionMutationOutcome.STALE_LATEST
        self._append_event(
            command.authority.task_id,
            event_type="task.progress_updated",
            created_at=command.updated_at,
            attempt_no=command.authority.attempt_no,
        )
        return TaskExecutionMutationOutcome.APPLIED

    def finish(self, command: TaskTerminalCommand) -> TaskExecutionMutationOutcome:
        self._require_write_transaction()
        outcome, task_row, attempt_row = self._execution_authority_outcome(
            command.authority,
            observed_at=command.completed_at,
        )
        if outcome is not TaskExecutionMutationOutcome.APPLIED:
            return outcome
        assert task_row is not None and attempt_row is not None
        if str(task_row["execution_state"]) in {
            TaskState.SUCCEEDED.value,
            TaskState.FAILED.value,
            TaskState.STALE.value,
        }:
            return TaskExecutionMutationOutcome.DUPLICATE_TERMINAL
        if (
            str(task_row["execution_state"]) != TaskState.RUNNING.value
            or str(attempt_row["state"]) != TaskAttemptState.RUNNING.value
        ):
            return TaskExecutionMutationOutcome.INVALID_STATE
        if not self._is_latest(task_row):
            return TaskExecutionMutationOutcome.STALE_LATEST
        target = transition_task_state(
            TaskState(str(task_row["execution_state"])),
            command.transition,
        )
        attempt_target = (
            TaskAttemptState.SUCCEEDED
            if target is TaskState.SUCCEEDED
            else TaskAttemptState.FAILED
        )
        result_payload = (
            _canonical_json({"result_ref": command.result_ref})
            if command.result_ref
            else None
        )
        execution_cursor = self._connection.execute(
            """
            UPDATE llm_task_executions
            SET execution_state = ?, public_status = ?, message = ?,
                result_payload = ?, completed_at = ?, updated_at = ?,
                row_version = row_version + 1
            WHERE execution_id = ? AND execution_state = 'running'
              AND current_attempt_no = ? AND fencing_token = ?
            """,
            (
                target.value,
                command.public_status,
                command.message,
                result_payload,
                command.completed_at,
                command.completed_at,
                command.authority.task_id.value,
                command.authority.attempt_no,
                command.authority.fencing_token,
            ),
        )
        attempt_cursor = self._connection.execute(
            """
            UPDATE task_attempts SET state = ?, completed_at = ?
            WHERE task_id = ? AND attempt_no = ? AND state = 'running'
              AND lease_token = ? AND fencing_token = ?
            """,
            (
                attempt_target.value,
                command.completed_at,
                command.authority.task_id.value,
                command.authority.attempt_no,
                command.authority.lease_token,
                command.authority.fencing_token,
            ),
        )
        latest_cursor = self._connection.execute(
            """
            UPDATE llm_tasks SET status = ?, message = ?, result_payload = ?,
                updated_at = ?
            WHERE business_type = ? AND business_key = ? AND execution_id = ?
            """,
            (
                command.public_status,
                command.message,
                result_payload,
                command.completed_at,
                str(task_row["business_type"]),
                str(task_row["business_key"]),
                command.authority.task_id.value,
            ),
        )
        if (
            execution_cursor.rowcount != 1
            or attempt_cursor.rowcount != 1
            or latest_cursor.rowcount != 1
        ):
            raise RuntimeError("Task 终态与 latest 投影未能在同一事务收敛")
        # Callback Delivery 事实由后续专用 Callback Control Store 独占。阶段 2-2 只在受理时
        # 读取既有 Guard 冲突，不能为了“提前准备”在这里写 idle，否则会形成第二个 Writer。
        self._append_event(
            command.authority.task_id,
            event_type=f"task.{target.value}",
            created_at=command.completed_at,
            attempt_no=command.authority.attempt_no,
        )
        return TaskExecutionMutationOutcome.APPLIED

    def scan_runnable(
        self,
        task_type: str,
        *,
        not_after: str,
        limit: int,
    ) -> tuple[TaskId, ...]:
        if not isinstance(task_type, str) or not task_type.strip():
            raise ValueError("task_type 必须是非空 str")
        not_after = require_persisted_utc(not_after, name="not_after")
        self._validate_limit(limit)
        rows = self._connection.execute(
            """
            SELECT e.execution_id
            FROM llm_task_executions AS e
            JOIN llm_tasks AS l
              ON l.business_type = e.business_type
             AND l.business_key = e.business_key
             AND l.execution_id = e.execution_id
            WHERE e.business_type = ? AND e.execution_state = 'accepted'
              AND (e.next_dispatch_at IS NULL OR e.next_dispatch_at <= ?)
              AND (
                e.business_type <> 'file'
                OR NOT EXISTS (
                  SELECT 1 FROM llm_task_executions AS p
                  WHERE p.business_type = 'file'
                    AND p.batch_id = e.batch_id
                    AND p.batch_sequence < e.batch_sequence
                    AND p.execution_state IN ('accepted','running','recovery_required')
                )
              )
            ORDER BY e.dispatch_sequence ASC LIMIT ?
            """,
            (task_type.strip(), not_after, limit),
        ).fetchall()
        return tuple(TaskId(str(row[0])) for row in rows)

    def load_execution_input(
        self,
        task_id: TaskId,
    ) -> PersistedTaskExecutionInput | None:
        """在独立只读 UoW 中加载受理时冻结输入，不构造或补齐业务默认值。"""

        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        row = self._connection.execute(
            """
            SELECT e.execution_id, e.business_type, e.business_key,
                   e.execution_state, e.input_schema_version, e.input_payload,
                   e.trace_id, e.created_at,
                   l.status AS public_status, l.progress, l.message
            FROM llm_task_executions AS e
            JOIN llm_tasks AS l
              ON l.business_type = e.business_type
             AND l.business_key = e.business_key
             AND l.execution_id = e.execution_id
            WHERE e.execution_id = ?
            """,
            (task_id.value,),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(
            str(row["input_payload"]),
            parse_constant=_reject_json_constant,
        )
        if not isinstance(payload, dict):
            raise ValueError("持久化 input_payload 必须是 JSON 对象")
        return PersistedTaskExecutionInput(
            task_id=TaskId(str(row["execution_id"])),
            task_type=str(row["business_type"]),
            business_ref=TaskBusinessRef(
                str(row["business_type"]),
                str(row["business_key"]),
            ),
            execution_state=str(row["execution_state"]),
            public_status=str(row["public_status"]),
            progress=float(row["progress"]),
            message=str(row["message"]),
            input_schema_version=int(row["input_schema_version"]),
            input_payload=payload,
            accepted_at=str(row["created_at"]),
            trace_id=str(row["trace_id"]),
        )

    def scan_expired_attempts(
        self,
        *,
        expired_before: str,
        limit: int,
    ) -> tuple[TaskId, ...]:
        expired_before = require_persisted_utc(
            expired_before,
            name="expired_before",
        )
        self._validate_limit(limit)
        rows = self._connection.execute(
            """
            SELECT e.execution_id
            FROM llm_task_executions AS e
            JOIN task_attempts AS a
              ON a.task_id = e.execution_id
             AND a.attempt_no = e.current_attempt_no
             AND a.fencing_token = e.fencing_token
            WHERE e.execution_state = 'running'
              AND a.state IN ('leased','running')
              AND a.lease_expires_at <= ?
              AND (e.next_recovery_at IS NULL OR e.next_recovery_at <= ?)
            ORDER BY a.lease_expires_at ASC, e.execution_id ASC LIMIT ?
            """,
            (expired_before, expired_before, limit),
        ).fetchall()
        return tuple(TaskId(str(row[0])) for row in rows)

    def _candidate_from_current(self, task_id: TaskId) -> TaskRecoveryCandidate | None:
        task_row = self._task_row(task_id)
        if task_row is None or str(task_row["execution_state"]) != TaskState.RUNNING.value:
            return None
        attempt_row = self._attempt_row(task_id, int(task_row["current_attempt_no"]))
        if attempt_row is None:
            return None
        step_rows = self._connection.execute(
            """
            SELECT step_key, definition_version, effect_kind, replay_policy,
                   state, current_step_attempt_no, idempotency_key,
                   checkpoint_code, result_digest, external_ref,
                   observation_ref, row_version
            FROM task_steps WHERE task_id = ? ORDER BY step_key ASC
            """,
            (task_id.value,),
        ).fetchall()
        evidence_payload = {
            "task_id": task_id.value,
            "task_row_version": int(task_row["row_version"]),
            "attempt_no": int(attempt_row["attempt_no"]),
            "fencing_token": int(attempt_row["fencing_token"]),
            "attempt_state": str(attempt_row["state"]),
            "lease_expires_at": str(attempt_row["lease_expires_at"]),
            "steps": [dict(row) for row in step_rows],
        }
        digest = hashlib.sha256(
            _canonical_json(evidence_payload).encode("utf-8")
        ).hexdigest()
        return TaskRecoveryCandidate(
            task=self._task_from_row(task_row),
            source_attempt_no=int(attempt_row["attempt_no"]),
            source_fencing_token=int(attempt_row["fencing_token"]),
            reason_code="lease_expired",
            latest_is_current=self._is_latest(task_row),
            evidence_digest=digest,
        )

    def load_candidate(self, task_id: TaskId) -> TaskRecoveryCandidate | None:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        return self._candidate_from_current(task_id)

    def _abandon_expired_attempt(
        self,
        candidate: TaskRecoveryCandidate,
        *,
        completed_at: str,
        reason_code: str,
    ) -> bool:
        """把已由 Policy 分类的过期 Attempt 直接收敛为 abandoned。"""

        cursor = self._connection.execute(
            """
            UPDATE task_attempts
            SET state = 'abandoned', completed_at = ?,
                recovery_reason_code = ?
            WHERE task_id = ? AND attempt_no = ? AND fencing_token = ?
              AND state IN ('leased','running') AND lease_expires_at <= ?
            """,
            (
                completed_at,
                reason_code,
                candidate.task.task_id.value,
                candidate.source_attempt_no,
                candidate.source_fencing_token,
                completed_at,
            ),
        )
        return cursor.rowcount == 1

    def classify_candidate_if_current(
        self,
        command: TaskRecoveryClassificationCommand,
    ) -> TaskRecoveryClassificationResult:
        self._require_write_transaction()
        current = self._candidate_from_current(command.candidate.task.task_id)
        if current is None:
            return TaskRecoveryClassificationResult(
                TaskRecoveryMutationOutcome.MISSING,
                command.classification,
            )
        attempt_row = self._attempt_row(
            current.task.task_id,
            current.source_attempt_no,
        )
        assert attempt_row is not None
        if (
            current != command.candidate
            or str(attempt_row["lease_expires_at"]) > command.classified_at
        ):
            return TaskRecoveryClassificationResult(
                TaskRecoveryMutationOutcome.SOURCE_CHANGED,
                command.classification,
            )
        task = current.task
        classification = command.classification
        recovery_case: TaskRecoveryCase | None = None

        if classification is RecoveryClassification.DEFER:
            cursor = self._connection.execute(
                """
                UPDATE llm_task_executions
                SET next_recovery_at = ?, updated_at = ?,
                    row_version = row_version + 1
                WHERE execution_id = ? AND execution_state = 'running'
                  AND current_attempt_no = ? AND fencing_token = ?
                  AND row_version = ?
                """,
                (
                    command.next_action_at,
                    command.classified_at,
                    task.task_id.value,
                    current.source_attempt_no,
                    current.source_fencing_token,
                    task.row_version,
                ),
            )
            if cursor.rowcount != 1:
                return TaskRecoveryClassificationResult(
                    TaskRecoveryMutationOutcome.SOURCE_CHANGED,
                    classification,
                )
            event_type = "task.recovery_deferred"
        elif classification is RecoveryClassification.RETRY_SAFE:
            if not self._abandon_expired_attempt(
                current,
                completed_at=command.classified_at,
                reason_code=classification.value,
            ):
                return TaskRecoveryClassificationResult(
                    TaskRecoveryMutationOutcome.SOURCE_CHANGED,
                    classification,
                )
            cursor = self._connection.execute(
                """
                UPDATE llm_task_executions
                SET execution_state = 'accepted', next_dispatch_at = ?,
                    next_recovery_at = NULL, retry_from_step_key = NULL,
                    row_version = row_version + 1, updated_at = ?
                WHERE execution_id = ? AND execution_state = 'running'
                  AND current_attempt_no = ? AND fencing_token = ?
                  AND row_version = ?
                """,
                (
                    command.next_action_at,
                    command.classified_at,
                    task.task_id.value,
                    current.source_attempt_no,
                    current.source_fencing_token,
                    task.row_version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("retry_safe 未能与旧 Attempt 原子收敛")
            event_type = "task.retry_safe_classified"
        elif classification is RecoveryClassification.MARK_STALE:
            if not self._abandon_expired_attempt(
                current,
                completed_at=command.classified_at,
                reason_code=classification.value,
            ):
                return TaskRecoveryClassificationResult(
                    TaskRecoveryMutationOutcome.SOURCE_CHANGED,
                    classification,
                )
            cursor = self._connection.execute(
                """
                UPDATE llm_task_executions
                SET execution_state = 'stale', completed_at = ?,
                    next_recovery_at = NULL, row_version = row_version + 1,
                    updated_at = ?
                WHERE execution_id = ? AND execution_state = 'running'
                  AND current_attempt_no = ? AND fencing_token = ?
                  AND row_version = ?
                """,
                (
                    command.classified_at,
                    command.classified_at,
                    task.task_id.value,
                    current.source_attempt_no,
                    current.source_fencing_token,
                    task.row_version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("mark_stale 未能与旧 Attempt 原子收敛")
            event_type = "task.stale_classified"
        else:
            if not self._abandon_expired_attempt(
                current,
                completed_at=command.classified_at,
                reason_code=classification.value,
            ):
                return TaskRecoveryClassificationResult(
                    TaskRecoveryMutationOutcome.SOURCE_CHANGED,
                    classification,
                )
            isolated, recovery_case = create_recovery_case(
                task,
                case_id=command.case_id,
                source_attempt_no=current.source_attempt_no,
                source_fencing_token=current.source_fencing_token,
                reason_code=classification.value,
                policy_version=command.policy_version,
                created_at=command.classified_at,
            )
            cursor = self._connection.execute(
                """
                UPDATE llm_task_executions
                SET execution_state = 'recovery_required',
                    recovery_generation = ?, current_recovery_case_id = ?,
                    recovery_reason_code = ?, next_recovery_at = NULL,
                    retry_from_step_key = NULL, row_version = row_version + 1,
                    updated_at = ?
                WHERE execution_id = ? AND execution_state = 'running'
                  AND current_attempt_no = ? AND fencing_token = ?
                  AND row_version = ? AND current_recovery_case_id IS NULL
                """,
                (
                    isolated.recovery_generation,
                    recovery_case.case_id,
                    recovery_case.reason_code,
                    command.classified_at,
                    task.task_id.value,
                    current.source_attempt_no,
                    current.source_fencing_token,
                    task.row_version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Recovery Case 未能与旧 Attempt 原子建立")
            self._connection.execute(
                """
                INSERT INTO task_recovery_cases (
                    case_id, task_id, recovery_generation, state,
                    source_attempt_no, source_fencing_token, reason_code,
                    policy_version, created_at
                ) VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?)
                """,
                (
                    recovery_case.case_id,
                    recovery_case.task_id.value,
                    recovery_case.generation,
                    recovery_case.source_attempt_no,
                    recovery_case.source_fencing_token,
                    recovery_case.reason_code,
                    recovery_case.policy_version,
                    recovery_case.created_at,
                ),
            )
            event_type = "task.recovery_case_created"

        self._append_event(
            task.task_id,
            event_type=event_type,
            created_at=command.classified_at,
            attempt_no=current.source_attempt_no,
            reason_code=classification.value,
        )
        return TaskRecoveryClassificationResult(
            TaskRecoveryMutationOutcome.APPLIED,
            classification,
            recovery_case,
        )

    def _case_row(self, case_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM task_recovery_cases WHERE case_id = ?",
            (case_id,),
        ).fetchone()

    @staticmethod
    def _case_from_row(row: sqlite3.Row) -> TaskRecoveryCase:
        return TaskRecoveryCase(
            case_id=str(row["case_id"]),
            task_id=TaskId(str(row["task_id"])),
            generation=int(row["recovery_generation"]),
            state=RecoveryCaseState(str(row["state"])),
            source_attempt_no=int(row["source_attempt_no"]),
            source_fencing_token=int(row["source_fencing_token"]),
            reason_code=str(row["reason_code"]),
            policy_version=str(row["policy_version"]),
            created_at=str(row["created_at"]),
            recovery_fencing_token=int(row["recovery_fencing_token"]),
            current_decision_id=str(row["current_decision_id"]),
            next_observation_at=str(row["next_observation_at"] or ""),
        )

    def get_case(self, case_id: str) -> TaskRecoveryCase | None:
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError("case_id 必须是非空 str")
        row = self._case_row(case_id.strip())
        return self._case_from_row(row) if row is not None else None

    @staticmethod
    def _authority_from_case_row(row: sqlite3.Row) -> RecoveryAuthority | None:
        if not str(row["recovery_owner_id"]):
            return None
        expiry = row["recovery_lease_expires_at"]
        if expiry is None:
            return None
        return RecoveryAuthority(
            case_id=str(row["case_id"]),
            generation=int(row["recovery_generation"]),
            owner_id=str(row["recovery_owner_id"]),
            lease_token=str(row["recovery_lease_token"]),
            fencing_token=int(row["recovery_fencing_token"]),
            lease_expires_at=str(expiry),
        )

    def _recovery_authority_outcome(
        self,
        authority: RecoveryAuthority,
        *,
        observed_at: str,
    ) -> tuple[TaskRecoveryMutationOutcome, sqlite3.Row | None]:
        row = self._case_row(authority.case_id)
        if row is None:
            return TaskRecoveryMutationOutcome.MISSING, None
        current = self._authority_from_case_row(row)
        if (
            current is None
            or current != authority
            or int(row["recovery_generation"]) != authority.generation
        ):
            return TaskRecoveryMutationOutcome.AUTHORITY_LOST, row
        if observed_at >= authority.lease_expires_at:
            return TaskRecoveryMutationOutcome.LEASE_EXPIRED, row
        if str(row["state"]) != RecoveryCaseState.OBSERVING.value:
            return TaskRecoveryMutationOutcome.INVALID_STATE, row
        return TaskRecoveryMutationOutcome.APPLIED, row

    def claim_case(self, request: TaskRecoveryClaimRequest) -> TaskRecoveryClaimResult:
        self._require_write_transaction()
        row = self._case_row(request.case_id)
        if row is None:
            return TaskRecoveryClaimResult(TaskRecoveryMutationOutcome.MISSING)
        if int(row["recovery_generation"]) != request.generation:
            return TaskRecoveryClaimResult(TaskRecoveryMutationOutcome.SOURCE_CHANGED)
        state = RecoveryCaseState(str(row["state"]))
        if state in {RecoveryCaseState.RESOLVED, RecoveryCaseState.SUPERSEDED}:
            return TaskRecoveryClaimResult(TaskRecoveryMutationOutcome.INVALID_STATE)
        if state is RecoveryCaseState.AWAITING_EVIDENCE:
            next_observation = row["next_observation_at"]
            if next_observation is not None and str(next_observation) > request.claimed_at:
                return TaskRecoveryClaimResult(TaskRecoveryMutationOutcome.INVALID_STATE)
        if state is RecoveryCaseState.OBSERVING:
            current_expiry = row["recovery_lease_expires_at"]
            if current_expiry is None or str(current_expiry) > request.claimed_at:
                return TaskRecoveryClaimResult(TaskRecoveryMutationOutcome.INVALID_STATE)
        elif state not in {RecoveryCaseState.OPEN, RecoveryCaseState.AWAITING_EVIDENCE}:
            return TaskRecoveryClaimResult(TaskRecoveryMutationOutcome.INVALID_STATE)
        next_fencing = int(row["recovery_fencing_token"]) + 1
        cursor = self._connection.execute(
            """
            UPDATE task_recovery_cases
            SET state = 'observing', recovery_owner_id = ?,
                recovery_lease_token = ?, recovery_fencing_token = ?,
                recovery_lease_expires_at = ?, next_observation_at = NULL
            WHERE case_id = ? AND recovery_generation = ?
              AND recovery_fencing_token = ?
              AND (
                state IN ('open','awaiting_evidence')
                OR (state = 'observing' AND recovery_lease_expires_at <= ?)
              )
            """,
            (
                request.owner_id,
                request.lease_token,
                next_fencing,
                request.lease_expires_at,
                request.case_id,
                request.generation,
                int(row["recovery_fencing_token"]),
                request.claimed_at,
            ),
        )
        if cursor.rowcount != 1:
            return TaskRecoveryClaimResult(TaskRecoveryMutationOutcome.SOURCE_CHANGED)
        updated_row = self._case_row(request.case_id)
        assert updated_row is not None
        authority = self._authority_from_case_row(updated_row)
        assert authority is not None
        case = self._case_from_row(updated_row)
        self._append_event(
            case.task_id,
            event_type="task.recovery_claimed",
            created_at=request.claimed_at,
            attempt_no=case.source_attempt_no,
            reason_code=case.reason_code,
            metadata={"recovery_generation": case.generation, "recovery_fencing": next_fencing},
        )
        logger.info(
            "Recovery Case 已领取: case_id=%s task_id=%s generation=%d fencing=%d",
            case.case_id,
            case.task_id,
            case.generation,
            next_fencing,
        )
        return TaskRecoveryClaimResult(
            TaskRecoveryMutationOutcome.APPLIED,
            case,
            authority,
        )

    def heartbeat_case(
        self,
        command: TaskRecoveryHeartbeatCommand,
    ) -> TaskRecoveryHeartbeatResult:
        self._require_write_transaction()
        if command.lease_expires_at <= command.authority.lease_expires_at:
            return TaskRecoveryHeartbeatResult(TaskRecoveryMutationOutcome.INVALID_STATE)
        outcome, _row = self._recovery_authority_outcome(
            command.authority,
            observed_at=command.heartbeat_at,
        )
        if outcome is not TaskRecoveryMutationOutcome.APPLIED:
            return TaskRecoveryHeartbeatResult(outcome)
        cursor = self._connection.execute(
            """
            UPDATE task_recovery_cases SET recovery_lease_expires_at = ?
            WHERE case_id = ? AND recovery_generation = ? AND state = 'observing'
              AND recovery_owner_id = ? AND recovery_lease_token = ?
              AND recovery_fencing_token = ? AND recovery_lease_expires_at = ?
            """,
            (
                command.lease_expires_at,
                command.authority.case_id,
                command.authority.generation,
                command.authority.owner_id,
                command.authority.lease_token,
                command.authority.fencing_token,
                command.authority.lease_expires_at,
            ),
        )
        if cursor.rowcount != 1:
            return TaskRecoveryHeartbeatResult(TaskRecoveryMutationOutcome.AUTHORITY_LOST)
        renewed = replace(
            command.authority,
            lease_expires_at=command.lease_expires_at,
        )
        return TaskRecoveryHeartbeatResult(TaskRecoveryMutationOutcome.APPLIED, renewed)

    # ------------------------------------------------------------------
    # Recovery Operation / Observation
    # ------------------------------------------------------------------

    def _operation_row(self, operation_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM task_recovery_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()

    @staticmethod
    def _operation_from_row(row: sqlite3.Row) -> TaskRecoveryOperation:
        return TaskRecoveryOperation(
            operation_id=str(row["operation_id"]),
            case_id=str(row["case_id"]),
            generation=int(row["recovery_generation"]),
            recovery_fencing_token=int(row["recovery_fencing_token"]),
            kind=RecoveryOperationKind(str(row["operation_kind"])),
            step_key=str(row["step_key"]),
            idempotency_key=str(row["idempotency_key"]),
            intent_digest=str(row["intent_digest"]),
            external_ref=str(row["external_ref"]),
            state=RecoveryOperationState(str(row["state"])),
            intent_at=str(row["intent_at"]),
            result_at=str(row["result_at"] or ""),
        )

    def begin_operation(
        self,
        command: TaskRecoveryOperationIntentCommand,
    ) -> TaskRecoveryMutationOutcome:
        """先提交稳定恢复 Intent，提交事务后调用方才可执行外部 I/O。"""

        self._require_write_transaction()
        outcome, case_row = self._recovery_authority_outcome(
            command.authority,
            observed_at=command.operation.intent_at,
        )
        if outcome is not TaskRecoveryMutationOutcome.APPLIED:
            return outcome
        assert case_row is not None

        existing_row = self._operation_row(command.operation.operation_id)
        if existing_row is not None:
            existing = self._operation_from_row(existing_row)
            return (
                TaskRecoveryMutationOutcome.DUPLICATE_OPERATION
                if existing == command.operation
                else TaskRecoveryMutationOutcome.SOURCE_CHANGED
            )
        idempotency_row = self._connection.execute(
            """
            SELECT operation_id FROM task_recovery_operations
            WHERE case_id = ? AND idempotency_key = ?
            """,
            (command.operation.case_id, command.operation.idempotency_key),
        ).fetchone()
        if idempotency_row is not None:
            return TaskRecoveryMutationOutcome.SOURCE_CHANGED

        operation = command.operation
        try:
            self._connection.execute(
                """
                INSERT INTO task_recovery_operations (
                    operation_id, case_id, recovery_generation,
                    recovery_fencing_token, operation_kind, step_key,
                    idempotency_key, intent_digest, external_ref, state,
                    intent_at, result_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'intent_recorded', ?, NULL)
                """,
                (
                    operation.operation_id,
                    operation.case_id,
                    operation.generation,
                    operation.recovery_fencing_token,
                    operation.kind.value,
                    operation.step_key,
                    operation.idempotency_key,
                    operation.intent_digest,
                    operation.external_ref,
                    operation.intent_at,
                ),
            )
        except sqlite3.IntegrityError:
            # 多连接竞争时，稳定 ID 或 Case 内幂等键只有一个提交者可以成功。
            return TaskRecoveryMutationOutcome.SOURCE_CHANGED
        task_id = TaskId(str(case_row["task_id"]))
        self._append_event(
            task_id,
            event_type="task.recovery_operation_intent_recorded",
            created_at=operation.intent_at,
            attempt_no=int(case_row["source_attempt_no"]),
            metadata={
                "operation_id": operation.operation_id,
                "operation_kind": operation.kind.value,
                "recovery_generation": operation.generation,
            },
        )
        logger.info(
            "Recovery Operation Intent 已记录: operation_id=%s case_id=%s generation=%d",
            operation.operation_id,
            operation.case_id,
            operation.generation,
        )
        return TaskRecoveryMutationOutcome.APPLIED

    @staticmethod
    def _observation_from_row(row: sqlite3.Row) -> TaskRecoveryObservation:
        return TaskRecoveryObservation(
            observation_id=str(row["observation_id"]),
            operation_id=str(row["operation_id"]),
            case_id=str(row["case_id"]),
            generation=int(row["recovery_generation"]),
            recovery_fencing_token=int(row["recovery_fencing_token"]),
            kind=RecoveryObservationKind(str(row["observation_kind"])),
            evidence_digest=str(row["evidence_digest"]),
            observed_at=str(row["observed_at"]),
            step_key=str(row["step_key"]),
            external_ref=str(row["external_ref"]),
            reason_code=str(row["reason_code"]),
        )

    def append_observation(
        self,
        authority: RecoveryAuthority,
        observation: TaskRecoveryObservation,
    ) -> TaskRecoveryMutationOutcome:
        """追加唯一证据，并在同一事务收敛对应的已提交 Operation。"""

        self._require_write_transaction()
        outcome, case_row = self._recovery_authority_outcome(
            authority,
            observed_at=observation.observed_at,
        )
        if outcome is not TaskRecoveryMutationOutcome.APPLIED:
            return outcome
        assert case_row is not None
        if (
            observation.case_id != authority.case_id
            or observation.generation != authority.generation
            or observation.recovery_fencing_token != authority.fencing_token
        ):
            return TaskRecoveryMutationOutcome.AUTHORITY_LOST

        existing_row = self._connection.execute(
            "SELECT * FROM task_recovery_observations WHERE observation_id = ?",
            (observation.observation_id,),
        ).fetchone()
        if existing_row is not None:
            existing = self._observation_from_row(existing_row)
            return (
                TaskRecoveryMutationOutcome.DUPLICATE_OBSERVATION
                if existing == observation
                else TaskRecoveryMutationOutcome.SOURCE_CHANGED
            )
        operation_row = self._operation_row(observation.operation_id)
        if operation_row is None:
            return TaskRecoveryMutationOutcome.MISSING
        operation = self._operation_from_row(operation_row)
        try:
            converged = converge_recovery_operation(operation, observation)
        except ValueError:
            return TaskRecoveryMutationOutcome.SOURCE_CHANGED

        # Operation 可以来自接管前的旧 recovery fencing，但 Observation 必须由当前 fencing 写入。
        try:
            self._connection.execute(
                """
                INSERT INTO task_recovery_observations (
                    observation_id, operation_id, case_id,
                    recovery_generation, recovery_fencing_token,
                    observation_kind, evidence_digest, observed_at,
                    step_key, external_ref, reason_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.operation_id,
                    observation.case_id,
                    observation.generation,
                    observation.recovery_fencing_token,
                    observation.kind.value,
                    observation.evidence_digest,
                    observation.observed_at,
                    observation.step_key,
                    observation.external_ref,
                    observation.reason_code,
                ),
            )
        except sqlite3.IntegrityError:
            return TaskRecoveryMutationOutcome.SOURCE_CHANGED
        cursor = self._connection.execute(
            """
            UPDATE task_recovery_operations
            SET state = 'observation_recorded', result_at = ?
            WHERE operation_id = ? AND case_id = ?
              AND recovery_generation = ? AND state = 'intent_recorded'
              AND result_at IS NULL
            """,
            (
                converged.result_at,
                operation.operation_id,
                operation.case_id,
                operation.generation,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Observation 已写入但 Recovery Operation 未能原子收敛")
        self._append_event(
            TaskId(str(case_row["task_id"])),
            event_type="task.recovery_observation_recorded",
            created_at=observation.observed_at,
            attempt_no=int(case_row["source_attempt_no"]),
            reason_code=observation.reason_code,
            metadata={
                "operation_id": observation.operation_id,
                "observation_id": observation.observation_id,
                "observation_kind": observation.kind.value,
            },
        )
        return TaskRecoveryMutationOutcome.APPLIED

    def list_observations(
        self,
        case_id: str,
    ) -> tuple[TaskRecoveryObservation, ...]:
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError("case_id 必须是非空 str")
        rows = self._connection.execute(
            """
            SELECT * FROM task_recovery_observations
            WHERE case_id = ? ORDER BY observed_at ASC, observation_id ASC
            """,
            (case_id.strip(),),
        ).fetchall()
        return tuple(self._observation_from_row(row) for row in rows)

    def list_operations(
        self,
        case_id: str,
    ) -> tuple[TaskRecoveryOperation, ...]:
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError("case_id 必须是非空 str")
        rows = self._connection.execute(
            """
            SELECT * FROM task_recovery_operations
            WHERE case_id = ? ORDER BY intent_at ASC, operation_id ASC
            """,
            (case_id.strip(),),
        ).fetchall()
        return tuple(self._operation_from_row(row) for row in rows)

    # ------------------------------------------------------------------
    # Recovery Decision
    # ------------------------------------------------------------------

    @staticmethod
    def _step_resolution_payload(
        resolution: TaskRecoveryStepResolution | None,
    ) -> str | None:
        if resolution is None:
            return None
        return _canonical_json(
            {
                "evidence_digest": resolution.evidence_digest,
                "expected_step_row_version": resolution.expected_step_row_version,
                "observation_id": resolution.observation_id,
                "operation_id": resolution.operation_id,
                "source_step_attempt_no": resolution.source_step_attempt_no,
                "source_step_key": resolution.source_step_key,
                "target_transition": resolution.target_transition.value,
            }
        )

    @staticmethod
    def _terminal_projection_payload(
        projection: TaskRecoveryTerminalProjection | None,
    ) -> str | None:
        if projection is None:
            return None
        return _canonical_json(
            {
                "checkpoint_code": projection.checkpoint_code,
                "checkpoint_digest": projection.checkpoint_digest,
                "message": projection.message,
                "public_status": projection.public_status,
                "result_ref": projection.result_ref,
                "source_step_attempt_no": projection.source_step_attempt_no,
                "source_step_key": projection.source_step_key,
            }
        )

    @classmethod
    def _decision_from_row(cls, row: sqlite3.Row) -> TaskRecoveryDecision:
        resolution_payload = (
            json.loads(str(row["step_resolution_payload"]))
            if row["step_resolution_payload"] is not None
            else None
        )
        terminal_payload = (
            json.loads(str(row["terminal_projection_payload"]))
            if row["terminal_projection_payload"] is not None
            else None
        )
        resolution = None
        if resolution_payload is not None:
            resolution = TaskRecoveryStepResolution(
                source_step_key=str(resolution_payload["source_step_key"]),
                source_step_attempt_no=int(
                    resolution_payload["source_step_attempt_no"]
                ),
                expected_step_row_version=int(
                    resolution_payload["expected_step_row_version"]
                ),
                operation_id=str(resolution_payload["operation_id"]),
                observation_id=str(resolution_payload["observation_id"]),
                evidence_digest=str(resolution_payload["evidence_digest"]),
                target_transition=TaskStepTransition(
                    str(resolution_payload["target_transition"])
                ),
            )
        projection = None
        if terminal_payload is not None:
            projection = TaskRecoveryTerminalProjection(
                source_step_key=str(terminal_payload["source_step_key"]),
                source_step_attempt_no=int(
                    terminal_payload["source_step_attempt_no"]
                ),
                checkpoint_code=str(terminal_payload["checkpoint_code"]),
                checkpoint_digest=str(terminal_payload["checkpoint_digest"]),
                public_status=str(terminal_payload["public_status"]),
                message=str(terminal_payload["message"]),
                result_ref=str(terminal_payload["result_ref"]),
            )
        terminal_state = (
            TaskState(str(row["terminal_state"]))
            if str(row["terminal_state"])
            else None
        )
        return TaskRecoveryDecision(
            decision_id=str(row["decision_id"]),
            task_id=TaskId(str(row["task_id"])),
            case_id=str(row["case_id"]),
            generation=int(row["recovery_generation"]),
            recovery_fencing_token=int(row["recovery_fencing_token"]),
            expected_task_row_version=int(row["expected_task_row_version"]),
            source_attempt_no=int(row["source_attempt_no"]),
            source_fencing_token=int(row["source_fencing_token"]),
            kind=RecoveryDecisionKind(str(row["decision_kind"])),
            evidence_digest=str(row["evidence_digest"]),
            reason_code=str(row["reason_code"]),
            policy_version=str(row["policy_version"]),
            actor_marker=str(row["actor_marker"]),
            decided_at=str(row["decided_at"]),
            retry_from_step_key=str(row["retry_from_step_key"]),
            terminal_state=terminal_state,
            next_observation_at=str(row["next_observation_at"] or ""),
            terminal_projection=projection,
            step_resolution=resolution,
        )

    def _validated_recovery_step(
        self,
        decision: TaskRecoveryDecision,
    ) -> TaskStep | None | bool:
        """校验 retry Step 及其已收敛证据；False 表示缺失，None 表示漂移。"""

        resolution = decision.step_resolution
        if resolution is None:
            return False
        operation_row = self._operation_row(resolution.operation_id)
        observation_row = self._connection.execute(
            "SELECT * FROM task_recovery_observations WHERE observation_id = ?",
            (resolution.observation_id,),
        ).fetchone()
        if operation_row is None or observation_row is None:
            return False
        operation = self._operation_from_row(operation_row)
        observation = self._observation_from_row(observation_row)
        if (
            operation.case_id != decision.case_id
            or operation.generation != decision.generation
            or operation.state is not RecoveryOperationState.OBSERVATION_RECORDED
            or observation.case_id != decision.case_id
            or observation.generation != decision.generation
            or observation.operation_id != operation.operation_id
            or observation.evidence_digest != resolution.evidence_digest
            or observation.kind.value not in _SAFE_RETRY_OBSERVATIONS
        ):
            return None
        step_row = self._step_row(decision.task_id, resolution.source_step_key)
        if step_row is None:
            return False
        current_step = self._step_from_row(step_row)
        try:
            return apply_recovery_step_resolution(current_step, resolution)
        except ValueError:
            return None

    def _terminal_projection_is_current(
        self,
        decision: TaskRecoveryDecision,
    ) -> bool | None:
        projection = decision.terminal_projection
        if projection is None:
            return False
        step_row = self._step_row(decision.task_id, projection.source_step_key)
        if step_row is None:
            return None
        step = self._step_from_row(step_row)
        return bool(
            step.current_step_attempt_no == projection.source_step_attempt_no
            and step.checkpoint is not None
            and step.checkpoint.code == projection.checkpoint_code
            and step.checkpoint.result_digest == projection.checkpoint_digest
        )

    def _insert_recovery_decision(self, decision: TaskRecoveryDecision) -> bool:
        try:
            self._connection.execute(
                """
                INSERT INTO task_recovery_decisions (
                    decision_id, task_id, case_id, recovery_generation,
                    recovery_fencing_token, expected_task_row_version,
                    source_attempt_no, source_fencing_token, decision_kind,
                    evidence_digest, reason_code, policy_version, actor_marker,
                    decided_at, next_observation_at, retry_from_step_key,
                    step_resolution_payload, terminal_state,
                    terminal_projection_payload, closes_case
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.decision_id,
                    decision.task_id.value,
                    decision.case_id,
                    decision.generation,
                    decision.recovery_fencing_token,
                    decision.expected_task_row_version,
                    decision.source_attempt_no,
                    decision.source_fencing_token,
                    decision.kind.value,
                    decision.evidence_digest,
                    decision.reason_code,
                    decision.policy_version,
                    decision.actor_marker,
                    decision.decided_at,
                    decision.next_observation_at or None,
                    decision.retry_from_step_key,
                    self._step_resolution_payload(decision.step_resolution),
                    decision.terminal_state.value if decision.terminal_state else "",
                    self._terminal_projection_payload(decision.terminal_projection),
                    int(decision.closes_case),
                ),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def decide_if_current(
        self,
        authority: RecoveryAuthority,
        decision: TaskRecoveryDecision,
    ) -> TaskRecoveryMutationOutcome:
        """以 Case/Task/Step/证据四重 CAS 原子提交恢复决定。"""

        self._require_write_transaction()
        existing_row = self._connection.execute(
            "SELECT * FROM task_recovery_decisions WHERE decision_id = ?",
            (decision.decision_id,),
        ).fetchone()
        if existing_row is not None:
            existing = self._decision_from_row(existing_row)
            return (
                TaskRecoveryMutationOutcome.DUPLICATE_DECISION
                if existing == decision
                else TaskRecoveryMutationOutcome.SOURCE_CHANGED
            )

        outcome, case_row = self._recovery_authority_outcome(
            authority,
            observed_at=decision.decided_at,
        )
        if outcome is not TaskRecoveryMutationOutcome.APPLIED:
            return outcome
        assert case_row is not None
        if (
            decision.case_id != authority.case_id
            or decision.generation != authority.generation
            or decision.recovery_fencing_token != authority.fencing_token
        ):
            return TaskRecoveryMutationOutcome.AUTHORITY_LOST
        task_row = self._task_row(decision.task_id)
        if task_row is None:
            return TaskRecoveryMutationOutcome.MISSING
        task = self._task_from_row(task_row)
        case = self._case_from_row(case_row)

        updated_step: TaskStep | None = None
        if decision.kind is RecoveryDecisionKind.RETRY_AUTHORIZED:
            step_result = self._validated_recovery_step(decision)
            if step_result is False:
                return TaskRecoveryMutationOutcome.MISSING
            if step_result is None:
                return TaskRecoveryMutationOutcome.SOURCE_CHANGED
            assert isinstance(step_result, TaskStep)
            updated_step = step_result
            # 回到 accepted 后必须能由标准 runnable/latest 查询重新领取。
            if not self._is_latest(task_row):
                return TaskRecoveryMutationOutcome.SOURCE_CHANGED
        elif decision.kind is RecoveryDecisionKind.FINALIZE_FROM_CHECKPOINT:
            checkpoint_current = self._terminal_projection_is_current(decision)
            if checkpoint_current is None:
                return TaskRecoveryMutationOutcome.MISSING
            if not checkpoint_current or not self._is_latest(task_row):
                return TaskRecoveryMutationOutcome.SOURCE_CHANGED
        elif (
            decision.kind is RecoveryDecisionKind.MARK_STALE
            and self._is_latest(task_row)
        ):
            # latest 仍指向本 Task 时不能把公开有效任务误判为 stale。
            return TaskRecoveryMutationOutcome.SOURCE_CHANGED

        try:
            updated_task, updated_case = apply_recovery_decision(
                task,
                case,
                decision,
            )
        except ValueError:
            return TaskRecoveryMutationOutcome.SOURCE_CHANGED
        if not self._insert_recovery_decision(decision):
            return TaskRecoveryMutationOutcome.SOURCE_CHANGED

        if updated_step is not None:
            resolution = decision.step_resolution
            assert resolution is not None
            cursor = self._connection.execute(
                """
                UPDATE task_steps
                SET state = 'pending', checkpoint_code = '', result_ref = '',
                    result_digest = '', external_ref = '', observation_ref = '',
                    row_version = row_version + 1
                WHERE task_id = ? AND step_key = ? AND state = 'outcome_unknown'
                  AND current_step_attempt_no = ? AND row_version = ?
                """,
                (
                    decision.task_id.value,
                    resolution.source_step_key,
                    resolution.source_step_attempt_no,
                    resolution.expected_step_row_version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Recovery Decision 写入后 Step CAS 未能原子收敛")

        result_payload: str | None = None
        if decision.kind is RecoveryDecisionKind.KEEP_QUARANTINED:
            pass
        elif decision.kind is RecoveryDecisionKind.RETRY_AUTHORIZED:
            cursor = self._connection.execute(
                """
                UPDATE llm_task_executions
                SET execution_state = 'accepted', current_recovery_case_id = NULL,
                    recovery_reason_code = '', next_recovery_at = NULL,
                    next_dispatch_at = ?, retry_from_step_key = ?,
                    row_version = row_version + 1, updated_at = ?
                WHERE execution_id = ? AND execution_state = 'recovery_required'
                  AND current_recovery_case_id = ? AND recovery_generation = ?
                  AND row_version = ?
                """,
                (
                    decision.decided_at,
                    decision.retry_from_step_key,
                    decision.decided_at,
                    decision.task_id.value,
                    decision.case_id,
                    decision.generation,
                    decision.expected_task_row_version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Recovery retry Decision 写入后 Task CAS 未能原子收敛")
        elif decision.kind is RecoveryDecisionKind.FINALIZE_FROM_CHECKPOINT:
            projection = decision.terminal_projection
            assert projection is not None and decision.terminal_state is not None
            result_payload = (
                _canonical_json({"result_ref": projection.result_ref})
                if projection.result_ref
                else None
            )
            cursor = self._connection.execute(
                """
                UPDATE llm_task_executions
                SET execution_state = ?, public_status = ?, message = ?,
                    result_payload = ?, completed_at = ?, updated_at = ?,
                    current_recovery_case_id = NULL, recovery_reason_code = '',
                    next_recovery_at = NULL, retry_from_step_key = NULL,
                    row_version = row_version + 1
                WHERE execution_id = ? AND execution_state = 'recovery_required'
                  AND current_recovery_case_id = ? AND recovery_generation = ?
                  AND row_version = ?
                """,
                (
                    decision.terminal_state.value,
                    projection.public_status,
                    projection.message,
                    result_payload,
                    decision.decided_at,
                    decision.decided_at,
                    decision.task_id.value,
                    decision.case_id,
                    decision.generation,
                    decision.expected_task_row_version,
                ),
            )
            latest_cursor = self._connection.execute(
                """
                UPDATE llm_tasks
                SET status = ?, message = ?, result_payload = ?, updated_at = ?
                WHERE business_type = ? AND business_key = ? AND execution_id = ?
                """,
                (
                    projection.public_status,
                    projection.message,
                    result_payload,
                    decision.decided_at,
                    str(task_row["business_type"]),
                    str(task_row["business_key"]),
                    decision.task_id.value,
                ),
            )
            if cursor.rowcount != 1 or latest_cursor.rowcount != 1:
                raise RuntimeError("Recovery 终态与 latest 投影未能在同一事务收敛")
            # 与普通业务终态相同，Recovery 终态也不越权写 Callback Delivery 表；对应业务
            # 切换波次必须把终态与 Callback eligibility/Guard 通过专用 Store 纳入同一 UoW。
        else:
            cursor = self._connection.execute(
                """
                UPDATE llm_task_executions
                SET execution_state = 'stale', completed_at = ?, updated_at = ?,
                    current_recovery_case_id = NULL, recovery_reason_code = '',
                    next_recovery_at = NULL, retry_from_step_key = NULL,
                    row_version = row_version + 1
                WHERE execution_id = ? AND execution_state = 'recovery_required'
                  AND current_recovery_case_id = ? AND recovery_generation = ?
                  AND row_version = ?
                """,
                (
                    decision.decided_at,
                    decision.decided_at,
                    decision.task_id.value,
                    decision.case_id,
                    decision.generation,
                    decision.expected_task_row_version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Recovery stale Decision 写入后 Task CAS 未能原子收敛")

        case_cursor = self._connection.execute(
            """
            UPDATE task_recovery_cases
            SET state = ?, current_decision_id = ?, next_observation_at = ?,
                recovery_owner_id = '', recovery_lease_token = '',
                recovery_lease_expires_at = NULL
            WHERE case_id = ? AND recovery_generation = ? AND state = 'observing'
              AND recovery_owner_id = ? AND recovery_lease_token = ?
              AND recovery_fencing_token = ? AND recovery_lease_expires_at = ?
            """,
            (
                updated_case.state.value,
                decision.decision_id,
                updated_case.next_observation_at or None,
                authority.case_id,
                authority.generation,
                authority.owner_id,
                authority.lease_token,
                authority.fencing_token,
                authority.lease_expires_at,
            ),
        )
        if case_cursor.rowcount != 1:
            raise RuntimeError("Recovery Decision 写入后 Case fencing CAS 未能原子收敛")
        self._append_event(
            decision.task_id,
            event_type=f"task.recovery_{decision.kind.value}",
            created_at=decision.decided_at,
            attempt_no=decision.source_attempt_no,
            reason_code=decision.reason_code,
            metadata={
                "case_id": decision.case_id,
                "decision_id": decision.decision_id,
                "recovery_generation": decision.generation,
            },
        )
        logger.info(
            "Recovery Decision 已提交: decision_id=%s case_id=%s kind=%s",
            decision.decision_id,
            decision.case_id,
            decision.kind.value,
        )
        return TaskRecoveryMutationOutcome.APPLIED


__all__ = ["SQLiteTaskControlStore"]
