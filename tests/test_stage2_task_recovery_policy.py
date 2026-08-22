"""阶段 2-7 三业务纯策略和有限 Reaper 的定向离线验收。"""

from __future__ import annotations

from dataclasses import replace
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.modules.analysis.application import (
    AnalysisTaskRecoveryPolicy,
    resolve_analysis_step,
)
from app.modules.analysis.adapters.sqlite import (
    SQLiteAnalysisResultSnapshotStore,
    bootstrap_analysis_task_control_database,
)
from app.modules.analysis.adapters.sqlite.recovery_finalization import (
    SQLiteAnalysisRecoveryFinalizationPreflight,
)
from app.modules.report.adapters.sqlite.recovery_finalization import (
    SQLiteReportRecoveryFinalizationPreflight,
)
from app.modules.weaponry.adapters.sqlite.recovery_finalization import (
    SQLiteWeaponryRecoveryFinalizationPreflight,
)
from app.modules.tasks.adapters.recovery_finalization import (
    RoutedTaskRecoveryFinalizationPreflight,
)
from app.modules.tasks.adapters.sqlite import (
    SQLiteConnectionFactory,
    SQLiteTaskControlStore,
    SQLiteTransactionManager,
    build_sqlite_task_control_uow_factories,
)
from app.modules.report.application import ReportTaskRecoveryPolicy, resolve_report_step
from app.modules.tasks.application import RecoverExpiredTaskAttempts
from app.modules.tasks.application import (
    ClaimRecoveryCaseCommand,
    RecoveryCoordinator,
    RecoveryOperationRequest,
    RecoveryOperationResult,
)
from app.modules.tasks.adapters import SecureTaskLeaseTokenFactory
from app.modules.tasks.domain import (
    RecoveryClassification,
    RecoveryDecisionKind,
    RecoveryObservationKind,
    RecoveryOperationKind,
    TaskBusinessRef,
    TaskBatchRef,
    TaskId,
    TaskRecord,
    TaskRecoveryCandidate,
    TaskRecoveryDecision,
    TaskRecoveryStepResolution,
    TaskRecoveryTerminalProjection,
    TaskState,
    TaskStepCheckpoint,
    TaskStepState,
    TaskStepTransition,
)
from app.modules.tasks.ports import (
    TaskClaimRequest,
    TaskExecutionMutationOutcome,
    TaskRecoveryMutationOutcome,
    TaskStepIntentCommand,
    TaskStepCompletionCommand,
)
from app.modules.analysis.domain.task_inputs import FrozenJsonObject
from app.modules.weaponry.application import (
    WeaponryTaskRecoveryPolicy,
    resolve_weaponry_step,
)
from tests.fakes.task_execution import FakeClock
from tests.test_task_control_sqlite_store import (
    SQLiteTaskControlStoreTestCase,
    _T2,
    _T3,
    _request,
)


_DIGEST = "a" * 64


def _candidate(task_type: str, *, latest: bool = True) -> TaskRecoveryCandidate:
    task_id = TaskId(f"policy-{task_type}-1")
    return TaskRecoveryCandidate(
        task=TaskRecord(
            task_id=task_id,
            task_type=task_type,
            business_ref=TaskBusinessRef(task_type, f"key-{task_type}"),
            state=TaskState.RUNNING,
            current_attempt_no=1,
            fencing_token=1,
            row_version=1,
            recovery_generation=0,
        ),
        source_attempt_no=1,
        source_fencing_token=1,
        reason_code="lease_expired",
        latest_is_current=latest,
        evidence_digest=_DIGEST,
    )


class BusinessTaskRecoveryPolicyTests(unittest.TestCase):
    def test_three_business_policies_only_retry_when_no_step_exists(self) -> None:
        policies = (
            ReportTaskRecoveryPolicy(),
            WeaponryTaskRecoveryPolicy(),
            AnalysisTaskRecoveryPolicy(),
        )
        for policy, task_type in zip(policies, ("report", "weaponry", "file")):
            with self.subTest(task_type=task_type):
                self.assertIs(
                    RecoveryClassification.RETRY_SAFE,
                    policy.classify(
                        _candidate(task_type),
                        steps=(),
                        observations=(),
                    ),
                )

    def test_started_or_unknown_step_never_maps_to_retry_safe(self) -> None:
        definitions = (
            (ReportTaskRecoveryPolicy(), "report", resolve_report_step("rag.generate")),
            (
                WeaponryTaskRecoveryPolicy(),
                "weaponry",
                resolve_weaponry_step("rag.workspace.create"),
            ),
            (
                AnalysisTaskRecoveryPolicy(),
                "file",
                resolve_analysis_step("knowledge.workspace.ensure"),
            ),
        )
        for policy, task_type, definition in definitions:
            candidate = _candidate(task_type)
            step = definition.new_step(
                task_id=candidate.task.task_id,
                step_key=definition.key_pattern,
                idempotency_key=f"{task_type}:stable-key",
            )
            for state in (TaskStepState.RUNNING, TaskStepState.OUTCOME_UNKNOWN):
                with self.subTest(task_type=task_type, state=state.value):
                    current = replace(
                        step,
                        state=state,
                        current_step_attempt_no=1,
                        row_version=1,
                    )
                    self.assertIs(
                        RecoveryClassification.RECONCILE_REQUIRED,
                        policy.classify(
                            candidate,
                            steps=(current,),
                            observations=(),
                        ),
                    )

    def test_only_verified_terminal_checkpoint_can_finalize(self) -> None:
        policy = ReportTaskRecoveryPolicy()
        candidate = _candidate("report")
        definition = resolve_report_step("artifact.publish")
        step = replace(
            definition.new_step(
                task_id=candidate.task.task_id,
                step_key="artifact.publish",
                idempotency_key="report:artifact:stable",
            ),
            state=TaskStepState.SUCCEEDED,
            current_step_attempt_no=1,
            checkpoint=TaskStepCheckpoint(
                code="artifact_published_v1",
                result_ref="report-result:1",
                result_digest=_DIGEST,
            ),
            row_version=1,
        )
        self.assertIs(
            RecoveryClassification.FINALIZE_FROM_CHECKPOINT,
            policy.classify(candidate, steps=(step,), observations=()),
        )


class BusinessTaskReaperSQLiteTests(SQLiteTaskControlStoreTestCase):
    def _reaper(self) -> RecoverExpiredTaskAttempts:
        return RecoverExpiredTaskAttempts(
            clock=FakeClock("2026-08-12T00:00:30.000000Z"),
            query_uow_factory=self.factories.queries,
            recovery_uow_factory=self.factories.recovery,
            policies={
                "report": ReportTaskRecoveryPolicy(),
                "weaponry": WeaponryTaskRecoveryPolicy(),
                "file": AnalysisTaskRecoveryPolicy(),
            },
            case_id_factory=lambda task_type: f"{task_type}-case-fixed",
        )

    def test_no_step_expiry_returns_to_accepted_with_new_claim_required(self) -> None:
        request = _request("task-reaper-safe", "business-reaper-safe")
        self._admit(request)
        self._claim(request)

        result = self._reaper().run_once()
        self.assertEqual((1, 1, 0), (result.scanned, result.classified, result.source_changed))
        with self.factories.execution() as unit_of_work:
            task = unit_of_work.execution.get_task(request.task_id)
        assert task is not None
        self.assertIs(TaskState.ACCEPTED, task.state)
        self.assertEqual(1, task.current_attempt_no)

    def test_running_external_step_is_atomically_isolated_as_unknown(self) -> None:
        request = _request("task-reaper-case", "business-reaper-case")
        self._admit(request)
        authority = self._claim(request)
        step = resolve_report_step("rag.generate").new_step(
            task_id=request.task_id,
            step_key="rag.generate",
            idempotency_key=f"{request.task_id}:rag.generate",
        )
        with self.factories.execution() as unit_of_work:
            self.assertIs(
                TaskExecutionMutationOutcome.APPLIED,
                unit_of_work.execution.start(authority, started_at=_T2),
            )
            self.assertIs(
                TaskExecutionMutationOutcome.APPLIED,
                unit_of_work.execution.begin_step(
                    TaskStepIntentCommand(authority=authority, step=step, intent_at=_T3)
                ),
            )
            unit_of_work.commit()

        result = self._reaper().run_once()
        self.assertEqual(1, result.classified)
        with self.factories.execution() as unit_of_work:
            task = unit_of_work.execution.get_task(request.task_id)
            current_step = unit_of_work.execution.get_step(request.task_id, step.step_key)
            old_attempt = unit_of_work.execution.get_step_attempt(
                request.task_id,
                step.step_key,
                1,
            )
        assert task is not None and current_step is not None and old_attempt is not None
        self.assertIs(TaskState.RECOVERY_REQUIRED, task.state)
        self.assertIs(TaskStepState.OUTCOME_UNKNOWN, current_step.state)
        self.assertIs(TaskStepState.OUTCOME_UNKNOWN, old_attempt.state)


class _NoEffectProbe:
    def execute(self, request: RecoveryOperationRequest) -> RecoveryOperationResult:
        return RecoveryOperationResult(
            kind=RecoveryObservationKind.NO_EFFECT_CONFIRMED,
            evidence_digest="b" * 64,
            reason_code="local_probe_no_effect_confirmed",
        )


class RecoveryCoordinatorSQLiteTests(BusinessTaskReaperSQLiteTests):
    def _open_case(self):
        request = _request("task-coordinator", "business-coordinator")
        self._admit(request)
        authority = self._claim(request)
        step = resolve_report_step("rag.generate").new_step(
            task_id=request.task_id,
            step_key="rag.generate",
            idempotency_key=f"{request.task_id}:rag.generate",
        )
        with self.factories.execution() as unit_of_work:
            self.assertIs(
                TaskExecutionMutationOutcome.APPLIED,
                unit_of_work.execution.start(authority, started_at=_T2),
            )
            self.assertIs(
                TaskExecutionMutationOutcome.APPLIED,
                unit_of_work.execution.begin_step(
                    TaskStepIntentCommand(authority=authority, step=step, intent_at=_T3)
                ),
            )
            unit_of_work.commit()
        self.assertEqual(1, self._reaper().run_once().classified)
        with self.factories.recovery() as unit_of_work:
            snapshot = unit_of_work.recovery.load_case_snapshot("report-case-fixed")
        assert snapshot is not None
        return request, step, snapshot

    def _coordinator(self) -> RecoveryCoordinator:
        return RecoveryCoordinator(
            clock=FakeClock("2026-08-12T00:00:31.000000Z"),
            recovery_uow_factory=self.factories.recovery,
            lease_token_factory=SecureTaskLeaseTokenFactory(),
            policies={
                "report": ReportTaskRecoveryPolicy(),
                "weaponry": WeaponryTaskRecoveryPolicy(),
                "file": AnalysisTaskRecoveryPolicy(),
            },
            recovery_lease_seconds=30,
        )

    def test_retry_decision_is_rejected_without_registered_business_resolver(self) -> None:
        request, step, snapshot = self._open_case()
        coordinator = self._coordinator()
        claimed = coordinator.claim(
            ClaimRecoveryCaseCommand(
                task_id=request.task_id,
                case_id=snapshot.case.case_id,
                generation=snapshot.case.generation,
                expected_task_row_version=snapshot.task.row_version,
                source_attempt_no=snapshot.case.source_attempt_no,
                source_fencing_token=snapshot.case.source_fencing_token,
                expected_recovery_fencing_token=0,
                owner_id="stage2-recovery-worker-1",
                operator_marker="operator:test-suite",
                reason_code="verified_local_probe",
            )
        )
        self.assertIs(TaskRecoveryMutationOutcome.APPLIED, claimed.outcome)
        assert claimed.session is not None
        observed = coordinator.execute_operation(
            claimed.session,
            RecoveryOperationRequest(
                operation_id="operation-local-probe-1",
                kind=RecoveryOperationKind.PROBE,
                step_key=step.step_key,
                idempotency_key="case:local-probe:1",
                intent_digest="a" * 64,
            ),
            _NoEffectProbe(),
        )
        self.assertIs(TaskRecoveryMutationOutcome.APPLIED, observed.outcome)
        assert observed.session is not None and observed.observation is not None

        with self.factories.recovery() as unit_of_work:
            current = unit_of_work.recovery.load_case_snapshot(snapshot.case.case_id)
        assert current is not None
        current_step = next(item for item in current.steps if item.step_key == step.step_key)
        decision = TaskRecoveryDecision(
            decision_id="decision-retry-1",
            task_id=request.task_id,
            case_id=current.case.case_id,
            generation=current.case.generation,
            recovery_fencing_token=observed.session.authority.fencing_token,
            expected_task_row_version=current.task.row_version,
            source_attempt_no=current.case.source_attempt_no,
            source_fencing_token=current.case.source_fencing_token,
            kind=RecoveryDecisionKind.RETRY_AUTHORIZED,
            evidence_digest=observed.observation.evidence_digest,
            reason_code="no_effect_confirmed",
            policy_version="report-task-recovery.v1",
            actor_marker="operator:test-suite",
            decided_at="2026-08-12T00:00:32.000000Z",
            retry_from_step_key=step.step_key,
            step_resolution=TaskRecoveryStepResolution(
                source_step_key=step.step_key,
                source_step_attempt_no=current_step.current_step_attempt_no,
                expected_step_row_version=current_step.row_version,
                operation_id="operation-local-probe-1",
                observation_id=observed.observation.observation_id,
                evidence_digest=observed.observation.evidence_digest,
                target_transition=TaskStepTransition.RETRY_AUTHORIZED,
            ),
        )
        self.assertIs(
            TaskRecoveryMutationOutcome.INVALID_STATE,
            coordinator.decide(observed.session, decision),
        )
        with self.factories.execution() as unit_of_work:
            task = unit_of_work.execution.get_task(request.task_id)
            reset = unit_of_work.execution.get_step(request.task_id, step.step_key)
            old_attempt = unit_of_work.execution.get_step_attempt(
                request.task_id, step.step_key, 1
            )
        assert task is not None and reset is not None and old_attempt is not None
        self.assertIs(TaskState.RECOVERY_REQUIRED, task.state)
        self.assertEqual("", task.retry_from_step_key)
        self.assertIs(TaskStepState.OUTCOME_UNKNOWN, reset.state)
        self.assertIs(TaskStepState.OUTCOME_UNKNOWN, old_attempt.state)

    def test_stale_expected_recovery_fencing_cannot_claim(self) -> None:
        request, _step, snapshot = self._open_case()
        outcome = self._coordinator().claim(
            ClaimRecoveryCaseCommand(
                task_id=request.task_id,
                case_id=snapshot.case.case_id,
                generation=snapshot.case.generation,
                expected_task_row_version=snapshot.task.row_version,
                source_attempt_no=snapshot.case.source_attempt_no,
                source_fencing_token=snapshot.case.source_fencing_token,
                expected_recovery_fencing_token=9,
                owner_id="stage2-recovery-worker-stale",
                operator_marker="operator:test-suite",
                reason_code="stale_snapshot",
            )
        )
        self.assertIs(TaskRecoveryMutationOutcome.SOURCE_CHANGED, outcome.outcome)


class RecoveryFinalizationAtomicSQLiteTests(unittest.TestCase):
    """结果快照、Decision、Task/latest 与 Callback eligibility 的原子门禁。"""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        root = Path(self._directory.name)
        old_path = root / "old.sqlite3"
        self.database_path = root / "task-control.sqlite3"
        sqlite3.connect(old_path).close()
        bootstrap = bootstrap_analysis_task_control_database(
            old_path,
            self.database_path,
        )
        self.manager = SQLiteTransactionManager(
            SQLiteConnectionFactory(bootstrap, busy_timeout_ms=100)
        )
        self.factories = build_sqlite_task_control_uow_factories(
            self.manager,
            recovery_finalization_preflight_builder=self._preflight_builder,
        )

    def tearDown(self) -> None:
        self._directory.cleanup()

    @staticmethod
    def _preflight_builder(connection):
        return RoutedTaskRecoveryFinalizationPreflight(
            {
                "report": SQLiteReportRecoveryFinalizationPreflight(connection),
                "weaponry": SQLiteWeaponryRecoveryFinalizationPreflight(connection),
                "file": SQLiteAnalysisRecoveryFinalizationPreflight(connection),
            }
        )

    def test_finalize_preflight_failure_rolls_back_and_success_marks_callback(self) -> None:
        from tests.test_task_control_sqlite_store import _file_request, _owner

        request = replace(
            _file_request("analysis-finalize-recovery", "analysis-finalize-key"),
            batch=TaskBatchRef("a" * 32, 1),
        )
        with self.factories.admission() as unit_of_work:
            self.assertEqual(
                "accepted",
                unit_of_work.admission.admit_one(request).outcome.value,
            )
            unit_of_work.commit()
        with self.factories.execution() as unit_of_work:
            claimed = unit_of_work.execution.claim(
                TaskClaimRequest(
                    task_id=request.task_id,
                    task_type="file",
                    owner=_owner("analysis-finalize-worker"),
                    lease_token="analysis-finalize-lease",
                    claimed_at="2026-08-12T00:00:01.000000Z",
                    lease_expires_at="2026-08-12T00:00:30.000000Z",
                )
            )
            assert claimed.attempt is not None
            authority = claimed.attempt.authority
            self.assertIs(
                TaskExecutionMutationOutcome.APPLIED,
                unit_of_work.execution.start(authority, started_at=_T2),
            )
            unit_of_work.commit()

        payload = FrozenJsonObject.from_mapping(
            {
                "businessType": "file",
                "data": {"fileName": request.business_ref.business_key, "status": "2"},
                "msg": "解析成功",
            },
            name="analysis_recovery_terminal_payload",
        )
        serialized = json.dumps(
            payload.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        definition = resolve_analysis_step("result.snapshot")
        step = definition.new_step(
            task_id=request.task_id,
            step_key="result.snapshot",
            idempotency_key=f"analysis:{request.task_id.value}:result-snapshot:{digest}",
        )
        with self.manager.begin(read_only=False) as transaction:
            control = SQLiteTaskControlStore(transaction.connection)
            self.assertIs(
                TaskExecutionMutationOutcome.APPLIED,
                control.begin_step(
                    TaskStepIntentCommand(authority=authority, step=step, intent_at=_T3)
                ),
            )
            SQLiteAnalysisResultSnapshotStore.from_connection(
                transaction.connection
            ).save(
                task_id=request.task_id,
                business_ref=request.business_ref,
                payload=payload,
                created_at=_T3,
            )
            self.assertIs(
                TaskExecutionMutationOutcome.APPLIED,
                control.complete_step(
                    TaskStepCompletionCommand(
                        authority=authority,
                        step_key=step.step_key,
                        step_attempt_no=1,
                        transition=TaskStepTransition.SUCCEED,
                        checkpoint=TaskStepCheckpoint(
                            code="analysis_result_snapshot_v1",
                            result_ref=f"analysis-result:v1:{digest}",
                            result_digest=digest,
                        ),
                        error_code="",
                        completed_at="2026-08-12T00:00:04.000000Z",
                    )
                ),
            )
            transaction.commit()

        reaper = RecoverExpiredTaskAttempts(
            clock=FakeClock("2026-08-12T00:00:31.000000Z"),
            query_uow_factory=self.factories.queries,
            recovery_uow_factory=self.factories.recovery,
            policies={
                "report": ReportTaskRecoveryPolicy(),
                "weaponry": WeaponryTaskRecoveryPolicy(),
                "file": AnalysisTaskRecoveryPolicy(),
            },
            case_id_factory=lambda _task_type: "analysis-finalize-case",
        )
        self.assertEqual(1, reaper.run_once().classified)
        with self.factories.recovery() as unit_of_work:
            snapshot = unit_of_work.recovery.load_case_snapshot(
                "analysis-finalize-case"
            )
        assert snapshot is not None
        coordinator = RecoveryCoordinator(
            clock=FakeClock("2026-08-12T00:00:32.000000Z"),
            recovery_uow_factory=self.factories.recovery,
            lease_token_factory=SecureTaskLeaseTokenFactory(),
            policies={
                "report": ReportTaskRecoveryPolicy(),
                "weaponry": WeaponryTaskRecoveryPolicy(),
                "file": AnalysisTaskRecoveryPolicy(),
            },
            recovery_lease_seconds=30,
        )
        claimed_case = coordinator.claim(
            ClaimRecoveryCaseCommand(
                task_id=request.task_id,
                case_id=snapshot.case.case_id,
                generation=snapshot.case.generation,
                expected_task_row_version=snapshot.task.row_version,
                source_attempt_no=snapshot.case.source_attempt_no,
                source_fencing_token=snapshot.case.source_fencing_token,
                expected_recovery_fencing_token=0,
                owner_id="analysis-finalize-coordinator",
                operator_marker="operator:finalize-test",
                reason_code="result_snapshot_verified",
            )
        )
        assert claimed_case.session is not None
        with self.factories.recovery() as unit_of_work:
            current = unit_of_work.recovery.load_case_snapshot(snapshot.case.case_id)
        assert current is not None
        source = next(item for item in current.steps if item.step_key == "result.snapshot")

        def decision(decision_id: str, checkpoint_digest: str) -> TaskRecoveryDecision:
            return TaskRecoveryDecision(
                decision_id=decision_id,
                task_id=request.task_id,
                case_id=current.case.case_id,
                generation=current.case.generation,
                recovery_fencing_token=claimed_case.session.authority.fencing_token,
                expected_task_row_version=current.task.row_version,
                source_attempt_no=current.case.source_attempt_no,
                source_fencing_token=current.case.source_fencing_token,
                kind=RecoveryDecisionKind.FINALIZE_FROM_CHECKPOINT,
                evidence_digest=digest,
                reason_code="result_snapshot_verified",
                policy_version="analysis-task-recovery.v1",
                actor_marker="operator:finalize-test",
                decided_at="2026-08-12T00:00:33.000000Z",
                terminal_state=TaskState.SUCCEEDED,
                terminal_projection=TaskRecoveryTerminalProjection(
                    source_step_key="result.snapshot",
                    source_step_attempt_no=source.current_step_attempt_no,
                    checkpoint_code="analysis_result_snapshot_v1",
                    checkpoint_digest=checkpoint_digest,
                    public_status="2",
                    message="解析完成",
                    result_ref=f"analysis-result:v1:{digest}",
                ),
            )

        # 错误摘要甚至不能通过 Policy；事务必须不留下 Decision 或 Task 终态。
        self.assertIs(
            TaskRecoveryMutationOutcome.INVALID_STATE,
            coordinator.decide(claimed_case.session, decision("bad-finalize", "f" * 64)),
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                ("recovery_required", 0),
                connection.execute(
                    """
                    SELECT e.execution_state,
                           (SELECT COUNT(*) FROM task_recovery_decisions
                            WHERE task_id = e.execution_id)
                    FROM llm_task_executions AS e WHERE e.execution_id = ?
                    """,
                    (request.task_id.value,),
                ).fetchone(),
            )

        self.assertIs(
            TaskRecoveryMutationOutcome.APPLIED,
            coordinator.decide(claimed_case.session, decision("good-finalize", digest)),
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            terminal = connection.execute(
                "SELECT execution_state, callback_status FROM llm_task_executions "
                "WHERE execution_id = ?",
                (request.task_id.value,),
            ).fetchone()
            guard = connection.execute(
                "SELECT owner_execution_id, state FROM callback_delivery_guards "
                "WHERE business_type = 'file' AND business_key = ?",
                (request.business_ref.business_key,),
            ).fetchone()
        self.assertEqual(("succeeded", "pending"), terminal)
        self.assertEqual((request.task_id.value, "idle"), guard)


if __name__ == "__main__":
    unittest.main()
