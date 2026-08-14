"""阶段 2-2 SQLite Control Store 的原子写、fencing 与恢复闭环测试。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.modules.tasks.adapters.sqlite import (
    SQLiteConnectionFactory,
    SQLiteTaskControlStore,
    SQLiteTransactionManager,
    bootstrap_task_control_database,
    build_sqlite_task_control_uow_factories,
)
from app.modules.tasks.domain import (
    RecoveryClassification,
    RecoveryDecisionKind,
    RecoveryObservationKind,
    RecoveryOperationKind,
    RecoveryOperationState,
    StepEffectKind,
    StepReplayPolicy,
    TaskBusinessRef,
    TaskBatchRef,
    TaskId,
    TaskOwnerIdentity,
    TaskRecoveryDecision,
    TaskRecoveryIsolation,
    TaskRecoveryObservation,
    TaskRecoveryOperation,
    TaskRecoveryStepResolution,
    TaskRecoveryTerminalProjection,
    TaskState,
    TaskStep,
    TaskStepCheckpoint,
    TaskStepState,
    TaskStepTransition,
    TaskTransition,
)
from app.modules.tasks.ports import (
    TaskAdmissionOutcome,
    TaskAdmissionRequest,
    TaskClaimRequest,
    TaskExecutionMutationOutcome,
    TaskProgressCommand,
    TaskRecoveryClaimRequest,
    TaskRecoveryClassificationCommand,
    TaskRecoveryMutationOutcome,
    TaskRecoveryOperationIntentCommand,
    TaskStepCompletionCommand,
    TaskStepIntentCommand,
    TaskTerminalCommand,
)


_T0 = "2026-08-12T00:00:00.000000Z"
_T1 = "2026-08-12T00:00:01.000000Z"
_T2 = "2026-08-12T00:00:02.000000Z"
_T3 = "2026-08-12T00:00:03.000000Z"
_T4 = "2026-08-12T00:00:04.000000Z"
_T5 = "2026-08-12T00:00:05.000000Z"
_T6 = "2026-08-12T00:00:06.000000Z"
_T10 = "2026-08-12T00:00:10.000000Z"
_T11 = "2026-08-12T00:00:11.000000Z"
_T12 = "2026-08-12T00:00:12.000000Z"
_T13 = "2026-08-12T00:00:13.000000Z"
_T30 = "2026-08-12T00:00:30.000000Z"
_T31 = "2026-08-12T00:00:31.000000Z"
_T32 = "2026-08-12T00:00:32.000000Z"
_T33 = "2026-08-12T00:00:33.000000Z"
_T40 = "2026-08-12T00:00:40.000000Z"
_T50 = "2026-08-12T00:00:50.000000Z"


def _request(task_id: str, business_key: str) -> TaskAdmissionRequest[tuple[str, ...]]:
    return TaskAdmissionRequest(
        task_id=TaskId(task_id),
        task_type="report",
        business_ref=TaskBusinessRef("report", business_key),
        input_schema_version=1,
        input_snapshot=(business_key,),
        input_payload={"business_key": business_key},
        public_request_payload={"reportId": business_key},
        initial_public_status="waiting",
        trace_id=f"trace-{task_id}",
        accepted_at=_T0,
    )


def _file_request(
    task_id: str,
    business_key: str,
) -> TaskAdmissionRequest[tuple[str, ...]]:
    """构造采用已确认 ``file`` 路由名的 Analysis 批次任务。"""

    return TaskAdmissionRequest(
        task_id=TaskId(task_id),
        task_type="file",
        business_ref=TaskBusinessRef("file", business_key),
        input_schema_version=1,
        input_snapshot=(business_key,),
        input_payload={"file_id": business_key},
        public_request_payload={"fileId": business_key},
        initial_public_status="waiting",
        trace_id=f"trace-{task_id}",
        accepted_at=_T0,
        batch=TaskBatchRef(batch_id=f"batch-{task_id}", sequence=1),
    )


def _owner(slot: str) -> TaskOwnerIdentity:
    return TaskOwnerIdentity(
        instance_start_id="12345678-1234-4234-8234-123456789abc",
        process_id=100,
        executor_name="report",
        worker_slot=slot,
    )


def _pending_step(task_id: TaskId) -> TaskStep:
    return TaskStep(
        task_id=task_id,
        step_key="rag.generate",
        definition_version=1,
        effect_kind=StepEffectKind.EXTERNAL_WRITE,
        replay_policy=StepReplayPolicy.RECONCILE_ONLY,
        state=TaskStepState.PENDING,
        current_step_attempt_no=0,
        idempotency_key=f"{task_id}:rag.generate",
        checkpoint=None,
        row_version=0,
    )


class SQLiteTaskControlStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        root = Path(self._directory.name)
        old_path = root / "old.sqlite3"
        self.database_path = root / "task-control.sqlite3"
        sqlite3.connect(old_path).close()
        bootstrap = bootstrap_task_control_database(old_path, self.database_path)
        connection_factory = SQLiteConnectionFactory(bootstrap, busy_timeout_ms=100)
        self.transaction_manager = SQLiteTransactionManager(connection_factory)
        self.factories = build_sqlite_task_control_uow_factories(
            self.transaction_manager
        )

    def tearDown(self) -> None:
        self._directory.cleanup()

    def _admit(self, request: TaskAdmissionRequest[object]) -> None:
        with self.factories.admission() as unit_of_work:
            result = unit_of_work.admission.admit_one(request)
            self.assertIs(TaskAdmissionOutcome.ACCEPTED, result.outcome)
            unit_of_work.commit()

    def _claim(self, request: TaskAdmissionRequest[object], *, slot: str = "worker-0"):
        with self.factories.execution() as unit_of_work:
            result = unit_of_work.execution.claim(
                TaskClaimRequest(
                    task_id=request.task_id,
                    task_type=request.task_type,
                    owner=_owner(slot),
                    lease_token=f"lease-{request.task_id}-{slot}",
                    claimed_at=_T1 if slot == "worker-0" else _T13,
                    lease_expires_at=_T30 if slot == "worker-0" else _T50,
                )
            )
            self.assertIs(TaskExecutionMutationOutcome.APPLIED, result.outcome)
            assert result.attempt is not None
            authority = result.attempt.authority
            unit_of_work.commit()
            return authority


class SQLiteAdmissionAndExecutionTests(SQLiteTaskControlStoreTestCase):
    def test_malformed_analysis_batch_identity_fails_scan_and_claim_closed(self) -> None:
        """即使维护操作绕过 DTO 写入脏行，Store 也不得领取该 Analysis Task。"""

        request = _file_request("task-file-malformed", "file-malformed")
        self._admit(request)
        with self.transaction_manager.begin() as transaction:
            transaction.connection.execute(
                """
                UPDATE llm_task_executions
                SET batch_id = NULL, batch_sequence = NULL
                WHERE execution_id = ?
                """,
                (request.task_id.value,),
            )
            transaction.commit()

        with self.factories.queries() as unit_of_work:
            with self.assertRaisesRegex(RuntimeError, "批次身份不变量损坏"):
                unit_of_work.queries.scan_runnable(
                    "file",
                    not_after=_T1,
                    limit=10,
                )
        with self.factories.execution() as unit_of_work:
            with self.assertRaisesRegex(RuntimeError, "批次身份不变量损坏"):
                unit_of_work.execution.claim(
                    TaskClaimRequest(
                        task_id=request.task_id,
                        task_type="file",
                        owner=_owner("worker-file"),
                        lease_token="lease-file-malformed",
                        claimed_at=_T1,
                        lease_expires_at=_T30,
                    )
                )

    def test_uow_default_rollback_and_committed_admission_conflict(self) -> None:
        request = _request("task-sqlite-admission", "business-admission")
        with self.factories.admission() as unit_of_work:
            self.assertIs(
                TaskAdmissionOutcome.ACCEPTED,
                unit_of_work.admission.admit_one(request).outcome,
            )
            # 不 commit：正常离开也必须回滚整组 Task/latest/Event。

        with self.factories.execution() as unit_of_work:
            self.assertIsNone(unit_of_work.execution.get_task(request.task_id))
            unit_of_work.rollback()

        self._admit(request)
        conflict = replace(
            request,
            task_id=TaskId("task-sqlite-admission-duplicate"),
            trace_id="trace-task-sqlite-admission-duplicate",
        )
        with self.factories.admission() as unit_of_work:
            result = unit_of_work.admission.admit_one(conflict)
            self.assertIs(TaskAdmissionOutcome.ACTIVE_TASK_CONFLICT, result.outcome)
            unit_of_work.rollback()

    def test_execution_step_progress_terminal_and_events_commit_atomically(self) -> None:
        request = _request("task-sqlite-terminal", "business-terminal")
        self._admit(request)
        authority = self._claim(request)
        step = _pending_step(request.task_id)
        checkpoint = TaskStepCheckpoint(
            code="result_committed",
            result_ref="report-result:1",
            result_digest="a" * 64,
            external_ref="provider:request-1",
            observation_ref="observation:1",
        )
        with self.factories.execution() as unit_of_work:
            execution = unit_of_work.execution
            self.assertIs(
                TaskExecutionMutationOutcome.APPLIED,
                execution.start(authority, started_at=_T2),
            )
            command = TaskStepIntentCommand(
                authority=authority,
                step=step,
                intent_at=_T3,
            )
            self.assertIs(TaskExecutionMutationOutcome.APPLIED, execution.begin_step(command))
            self.assertIs(
                TaskExecutionMutationOutcome.DUPLICATE_STEP_INTENT,
                execution.begin_step(command),
            )
            self.assertIs(
                TaskExecutionMutationOutcome.APPLIED,
                execution.complete_step(
                    TaskStepCompletionCommand(
                        authority=authority,
                        step_key=step.step_key,
                        step_attempt_no=1,
                        transition=TaskStepTransition.SUCCEED,
                        checkpoint=checkpoint,
                        error_code="",
                        completed_at=_T4,
                    )
                ),
            )
            self.assertIs(
                TaskExecutionMutationOutcome.APPLIED,
                execution.update_progress(
                    TaskProgressCommand(
                        authority=authority,
                        progress=0.8,
                        message="生成完成",
                        public_status="processing",
                        updated_at=_T5,
                    )
                ),
            )
            self.assertIs(
                TaskExecutionMutationOutcome.APPLIED,
                execution.finish(
                    TaskTerminalCommand(
                        authority=authority,
                        transition=TaskTransition.BUSINESS_SUCCEEDED,
                        public_status="completed",
                        message="报告已生成",
                        result_ref="report-result:1",
                        completed_at=_T6,
                    )
                ),
            )
            unit_of_work.commit()

        connection = sqlite3.connect(self.database_path)
        try:
            execution_row = connection.execute(
                "SELECT execution_state, public_status FROM llm_task_executions WHERE execution_id = ?",
                (request.task_id.value,),
            ).fetchone()
            latest_row = connection.execute(
                "SELECT execution_id, status FROM llm_tasks WHERE business_type='report' AND business_key=?",
                (request.business_ref.business_key,),
            ).fetchone()
            event_count = connection.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id = ?",
                (request.task_id.value,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(("succeeded", "completed"), execution_row)
        self.assertEqual((request.task_id.value, "completed"), latest_row)
        self.assertGreaterEqual(event_count, 7)


class SQLiteRecoveryAndFencingTests(SQLiteTaskControlStoreTestCase):
    def test_mark_stale_rejects_latest_before_abandoning_source_attempt(self) -> None:
        """latest 保护必须先于 Attempt abandon，拒绝路径不得留下部分写。"""

        request = _request("task-mark-stale-latest", "business-mark-stale")
        self._admit(request)
        authority = self._claim(request)

        with self.factories.recovery() as unit_of_work:
            candidate = unit_of_work.recovery.load_candidate(request.task_id)
            assert candidate is not None
            self.assertTrue(candidate.latest_is_current)
            result = unit_of_work.recovery.classify_candidate_if_current(
                TaskRecoveryClassificationCommand(
                    candidate=candidate,
                    classification=RecoveryClassification.MARK_STALE,
                    policy_version="test-mark-stale-v1",
                    classified_at=_T30,
                )
            )
            self.assertIs(TaskRecoveryMutationOutcome.SOURCE_CHANGED, result.outcome)
            unit_of_work.commit()

        with self.factories.execution() as unit_of_work:
            task = unit_of_work.execution.get_task(request.task_id)
        with self.transaction_manager.begin(read_only=True) as transaction:
            attempt_state = transaction.connection.execute(
                """
                SELECT state FROM task_attempts
                WHERE task_id = ? AND attempt_no = ?
                """,
                (request.task_id.value, authority.attempt_no),
            ).fetchone()[0]
            transaction.commit()
        assert task is not None
        self.assertIs(TaskState.RUNNING, task.state)
        self.assertEqual("leased", str(attempt_state))

    def test_two_independent_connections_allow_only_one_execution_claim(self) -> None:
        request = _request("task-sqlite-claim-cas", "business-claim-cas")
        self._admit(request)
        first_authority = self._claim(request)

        # Factory 每次打开独立连接；第二个 claim 重新读取数据库当前事实，不能复用 accepted 快照。
        with self.factories.execution() as unit_of_work:
            second = unit_of_work.execution.claim(
                TaskClaimRequest(
                    task_id=request.task_id,
                    task_type=request.task_type,
                    owner=_owner("worker-racing"),
                    lease_token="lease-racing",
                    claimed_at=_T2,
                    lease_expires_at=_T40,
                )
            )
            self.assertIs(TaskExecutionMutationOutcome.NOT_RUNNABLE, second.outcome)
            unit_of_work.rollback()
        self.assertEqual(1, first_authority.attempt_no)

    def test_old_recovery_owner_is_fenced_and_new_owner_converges_old_intent(self) -> None:
        request = _request("task-sqlite-recovery", "business-recovery")
        self._admit(request)
        execution_authority = self._claim(request)
        step = _pending_step(request.task_id)
        with self.factories.execution() as unit_of_work:
            execution = unit_of_work.execution
            execution.start(execution_authority, started_at=_T2)
            execution.begin_step(
                TaskStepIntentCommand(
                    authority=execution_authority,
                    step=step,
                    intent_at=_T3,
                )
            )
            outcome = execution.complete_step(
                TaskStepCompletionCommand(
                    authority=execution_authority,
                    step_key=step.step_key,
                    step_attempt_no=1,
                    transition=TaskStepTransition.MARK_OUTCOME_UNKNOWN,
                    checkpoint=None,
                    error_code="provider_outcome_unknown",
                    completed_at=_T4,
                    recovery_isolation=TaskRecoveryIsolation(
                        case_id="case-sqlite-recovery",
                        reason_code="provider_outcome_unknown",
                        policy_version="report-recovery-v1",
                    ),
                )
            )
            self.assertIs(TaskExecutionMutationOutcome.APPLIED, outcome)
            unit_of_work.commit()

        with self.factories.recovery() as unit_of_work:
            claim = unit_of_work.recovery.claim_case(
                TaskRecoveryClaimRequest(
                    case_id="case-sqlite-recovery",
                    generation=1,
                    owner_id="recovery/worker-old",
                    lease_token="recovery-token-old",
                    claimed_at=_T5,
                    lease_expires_at=_T10,
                )
            )
            self.assertIs(TaskRecoveryMutationOutcome.APPLIED, claim.outcome)
            assert claim.authority is not None
            old_authority = claim.authority
            operation = TaskRecoveryOperation(
                operation_id="operation-sqlite-recovery",
                case_id="case-sqlite-recovery",
                generation=1,
                recovery_fencing_token=old_authority.fencing_token,
                kind=RecoveryOperationKind.PROBE,
                step_key=step.step_key,
                idempotency_key="case-sqlite-recovery:rag.generate:probe",
                intent_digest="b" * 64,
                external_ref="provider:request-recovery",
                state=RecoveryOperationState.INTENT_RECORDED,
                intent_at=_T6,
            )
            operation_command = TaskRecoveryOperationIntentCommand(
                authority=old_authority,
                operation=operation,
            )
            self.assertIs(
                TaskRecoveryMutationOutcome.APPLIED,
                unit_of_work.recovery.begin_operation(operation_command),
            )
            self.assertIs(
                TaskRecoveryMutationOutcome.DUPLICATE_OPERATION,
                unit_of_work.recovery.begin_operation(operation_command),
            )
            unit_of_work.commit()

        # 第二个独立连接只能在旧 recovery lease 到期后接管，并递增 recovery fencing。
        with self.factories.recovery() as unit_of_work:
            takeover = unit_of_work.recovery.claim_case(
                TaskRecoveryClaimRequest(
                    case_id="case-sqlite-recovery",
                    generation=1,
                    owner_id="recovery/worker-new",
                    lease_token="recovery-token-new",
                    claimed_at=_T10,
                    lease_expires_at=_T40,
                )
            )
            self.assertIs(TaskRecoveryMutationOutcome.APPLIED, takeover.outcome)
            assert takeover.authority is not None
            new_authority = takeover.authority
            unit_of_work.commit()
        self.assertGreater(new_authority.fencing_token, old_authority.fencing_token)

        old_observation = TaskRecoveryObservation(
            observation_id="observation-sqlite-recovery-old",
            operation_id=operation.operation_id,
            case_id=operation.case_id,
            generation=1,
            recovery_fencing_token=old_authority.fencing_token,
            kind=RecoveryObservationKind.NO_EFFECT_CONFIRMED,
            evidence_digest="c" * 64,
            observed_at=_T11,
            step_key=step.step_key,
            external_ref=operation.external_ref,
        )
        with self.factories.recovery() as unit_of_work:
            self.assertIs(
                TaskRecoveryMutationOutcome.AUTHORITY_LOST,
                unit_of_work.recovery.append_observation(
                    old_authority,
                    old_observation,
                ),
            )
            unit_of_work.rollback()

        observation = replace(
            old_observation,
            observation_id="observation-sqlite-recovery-new",
            recovery_fencing_token=new_authority.fencing_token,
        )
        with self.factories.recovery() as unit_of_work:
            self.assertIs(
                TaskRecoveryMutationOutcome.APPLIED,
                unit_of_work.recovery.append_observation(new_authority, observation),
            )
            self.assertEqual(1, len(unit_of_work.recovery.list_operations(operation.case_id)))
            self.assertEqual(1, len(unit_of_work.recovery.list_observations(operation.case_id)))
            unit_of_work.commit()

        with self.factories.execution() as unit_of_work:
            task = unit_of_work.execution.get_task(request.task_id)
            current_step = unit_of_work.execution.get_step(request.task_id, step.step_key)
            unit_of_work.rollback()
        assert task is not None and current_step is not None
        resolution = TaskRecoveryStepResolution(
            source_step_key=step.step_key,
            source_step_attempt_no=1,
            expected_step_row_version=current_step.row_version,
            operation_id=operation.operation_id,
            observation_id=observation.observation_id,
            evidence_digest=observation.evidence_digest,
            target_transition=TaskStepTransition.RETRY_AUTHORIZED,
        )
        decision = TaskRecoveryDecision(
            decision_id="decision-sqlite-recovery",
            task_id=request.task_id,
            case_id=operation.case_id,
            generation=1,
            recovery_fencing_token=new_authority.fencing_token,
            expected_task_row_version=task.row_version,
            source_attempt_no=execution_authority.attempt_no,
            source_fencing_token=execution_authority.fencing_token,
            kind=RecoveryDecisionKind.RETRY_AUTHORIZED,
            evidence_digest=observation.evidence_digest,
            reason_code="no_effect_confirmed",
            policy_version="report-recovery-v1",
            actor_marker="automatic/recovery-0",
            decided_at=_T12,
            retry_from_step_key=step.step_key,
            step_resolution=resolution,
        )
        with self.factories.recovery() as unit_of_work:
            self.assertIs(
                TaskRecoveryMutationOutcome.APPLIED,
                unit_of_work.recovery.decide_if_current(new_authority, decision),
            )
            unit_of_work.commit()
        with self.factories.recovery() as unit_of_work:
            self.assertIs(
                TaskRecoveryMutationOutcome.DUPLICATE_DECISION,
                unit_of_work.recovery.decide_if_current(new_authority, decision),
            )
            unit_of_work.rollback()

        with self.factories.execution() as unit_of_work:
            reset_step = unit_of_work.execution.get_step(request.task_id, step.step_key)
            old_step_attempt = unit_of_work.execution.get_step_attempt(
                request.task_id,
                step.step_key,
                1,
            )
            reset_task = unit_of_work.execution.get_task(request.task_id)
            unit_of_work.rollback()
        assert reset_step is not None and old_step_attempt is not None and reset_task is not None
        self.assertIs(TaskStepState.PENDING, reset_step.state)
        self.assertIs(TaskStepState.OUTCOME_UNKNOWN, old_step_attempt.state)
        self.assertIs(TaskState.ACCEPTED, reset_task.state)

        # Recovery 本身不创建新 Attempt；重新执行必须回到标准 claim/start/begin_step 路径。
        second_authority = self._claim(request, slot="worker-1")
        with self.factories.execution() as unit_of_work:
            unit_of_work.execution.start(second_authority, started_at=_T13)
            self.assertIs(
                TaskExecutionMutationOutcome.APPLIED,
                unit_of_work.execution.begin_step(
                    TaskStepIntentCommand(
                        authority=second_authority,
                        step=reset_step,
                        intent_at=_T13,
                    )
                ),
            )
            second_step_attempt = unit_of_work.execution.get_step_attempt(
                request.task_id,
                step.step_key,
                2,
            )
            self.assertIsNotNone(second_step_attempt)
            unit_of_work.commit()

    def test_store_write_without_explicit_transaction_fails_closed(self) -> None:
        connection = sqlite3.connect(self.database_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            store = SQLiteTaskControlStore(connection)
            with self.assertRaisesRegex(RuntimeError, "显式 UnitOfWork"):
                store.admit_one(_request("task-no-uow", "business-no-uow"))
        finally:
            connection.close()

    def test_control_store_does_not_become_callback_delivery_writer(self) -> None:
        """2-2 只能读取 Admission Guard；Callback 写入留给后续唯一专用 Store。"""

        source_path = (
            Path(__file__).resolve().parents[1]
            / "app/modules/tasks/adapters/sqlite/control_store.py"
        )
        source = source_path.read_text(encoding="utf-8")
        self.assertNotIn("INSERT INTO callback_delivery_guards", source)
        self.assertNotIn("UPDATE callback_delivery_guards", source)

    def test_expired_checkpoint_can_finalize_without_replaying_external_step(self) -> None:
        """Reaper 先隔离过期 Attempt，再由 checkpoint CAS 原子收敛 Task/latest。"""

        request = _request("task-sqlite-finalize", "business-finalize")
        self._admit(request)
        authority = self._claim(request)
        step = _pending_step(request.task_id)
        checkpoint = TaskStepCheckpoint(
            code="result_committed",
            result_ref="report-result:finalize",
            result_digest="d" * 64,
            external_ref="provider:finalize",
            observation_ref="observation:finalize",
        )
        with self.factories.execution() as unit_of_work:
            execution = unit_of_work.execution
            execution.start(authority, started_at=_T2)
            execution.begin_step(
                TaskStepIntentCommand(authority=authority, step=step, intent_at=_T3)
            )
            self.assertIs(
                TaskExecutionMutationOutcome.APPLIED,
                execution.complete_step(
                    TaskStepCompletionCommand(
                        authority=authority,
                        step_key=step.step_key,
                        step_attempt_no=1,
                        transition=TaskStepTransition.SUCCEED,
                        checkpoint=checkpoint,
                        error_code="",
                        completed_at=_T4,
                    )
                ),
            )
            unit_of_work.commit()

        with self.factories.recovery() as unit_of_work:
            recovery = unit_of_work.recovery
            self.assertEqual(
                (request.task_id,),
                recovery.scan_expired_attempts(expired_before=_T30, limit=10),
            )
            candidate = recovery.load_candidate(request.task_id)
            assert candidate is not None
            classified = recovery.classify_candidate_if_current(
                TaskRecoveryClassificationCommand(
                    candidate=candidate,
                    classification=RecoveryClassification.FINALIZE_FROM_CHECKPOINT,
                    policy_version="report-recovery-v1",
                    classified_at=_T30,
                    case_id="case-sqlite-finalize",
                )
            )
            self.assertIs(TaskRecoveryMutationOutcome.APPLIED, classified.outcome)
            unit_of_work.commit()

        with self.factories.recovery() as unit_of_work:
            claim = unit_of_work.recovery.claim_case(
                TaskRecoveryClaimRequest(
                    case_id="case-sqlite-finalize",
                    generation=1,
                    owner_id="recovery/finalize",
                    lease_token="recovery-token-finalize",
                    claimed_at=_T31,
                    lease_expires_at=_T50,
                )
            )
            self.assertIs(TaskRecoveryMutationOutcome.APPLIED, claim.outcome)
            assert claim.authority is not None
            recovery_authority = claim.authority
            unit_of_work.commit()

        with self.factories.execution() as unit_of_work:
            task = unit_of_work.execution.get_task(request.task_id)
            unit_of_work.rollback()
        assert task is not None
        decision = TaskRecoveryDecision(
            decision_id="decision-sqlite-finalize",
            task_id=request.task_id,
            case_id="case-sqlite-finalize",
            generation=1,
            recovery_fencing_token=recovery_authority.fencing_token,
            expected_task_row_version=task.row_version,
            source_attempt_no=authority.attempt_no,
            source_fencing_token=authority.fencing_token,
            kind=RecoveryDecisionKind.FINALIZE_FROM_CHECKPOINT,
            evidence_digest="e" * 64,
            reason_code="checkpoint_verified",
            policy_version="report-recovery-v1",
            actor_marker="automatic/recovery-finalize",
            decided_at=_T33,
            terminal_state=TaskState.SUCCEEDED,
            terminal_projection=TaskRecoveryTerminalProjection(
                source_step_key=step.step_key,
                source_step_attempt_no=1,
                checkpoint_code=checkpoint.code,
                checkpoint_digest=checkpoint.result_digest,
                public_status="completed",
                message="报告已从检查点确认完成",
                result_ref=checkpoint.result_ref,
            ),
        )
        with self.factories.recovery() as unit_of_work:
            self.assertIs(
                TaskRecoveryMutationOutcome.APPLIED,
                unit_of_work.recovery.decide_if_current(
                    recovery_authority,
                    decision,
                ),
            )
            unit_of_work.commit()

        connection = sqlite3.connect(self.database_path)
        try:
            execution_row = connection.execute(
                "SELECT execution_state, public_status, result_payload FROM llm_task_executions WHERE execution_id=?",
                (request.task_id.value,),
            ).fetchone()
            latest_row = connection.execute(
                "SELECT status, result_payload FROM llm_tasks WHERE business_type='report' AND business_key=?",
                (request.business_ref.business_key,),
            ).fetchone()
            attempt_state = connection.execute(
                "SELECT state FROM task_attempts WHERE task_id=? AND attempt_no=1",
                (request.task_id.value,),
            ).fetchone()[0]
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            connection.close()
        expected_payload = '{"result_ref":"report-result:finalize"}'
        self.assertEqual(("succeeded", "completed", expected_payload), execution_row)
        self.assertEqual(("completed", expected_payload), latest_row)
        self.assertEqual("abandoned", attempt_state)
        self.assertEqual("ok", integrity)
        self.assertEqual([], foreign_keys)


if __name__ == "__main__":
    unittest.main()
