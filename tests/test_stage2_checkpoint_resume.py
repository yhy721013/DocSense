"""阶段 2-7 业务续跑快照、预检和新 Attempt 链路的离线验收。"""

from __future__ import annotations

from pathlib import Path
import sqlite3
import unittest

from app.modules.analysis.adapters import AnalysisV5TaskCommandCodec
from app.modules.analysis.adapters.sqlite import (
    SQLiteAnalysisExecutionUnitOfWorkFactory,
    SQLiteAnalysisResultSnapshotStore,
    SQLiteAnalysisV2ResourceStoreAdapter,
    bootstrap_analysis_task_control_database,
)
from app.modules.analysis.adapters.sqlite.recovery_resume import (
    SQLiteAnalysisRecoveryResumePreflight,
)
from app.modules.analysis.application import AnalysisStepRuntime, AnalysisTaskRecoveryPolicy
from app.modules.analysis.adapters import SQLiteAnalysisV2BatchAdmissionAdapter
from app.modules.report.application import ReportTaskRecoveryPolicy
from app.modules.tasks.adapters import CodecTaskExecutionSnapshotLoader, SecureTaskLeaseTokenFactory
from app.modules.tasks.adapters.sqlite import (
    SQLiteCallbackControlStore,
    SQLiteConnectionFactory,
    SQLiteTaskControlStore,
    SQLiteTransactionManager,
    build_sqlite_task_control_uow_factories,
)
from app.modules.tasks.application import (
    ClaimRecoveryCaseCommand,
    RecoveryCoordinator,
    RecoveryOperationRequest,
    RecoveryOperationResult,
    TaskExecutionRuntime,
)
from app.modules.tasks.domain import (
    RecoveryDecisionKind,
    RecoveryObservationKind,
    RecoveryOperationKind,
    TaskLeaseRuntimeSettings,
    TaskOwnerIdentity,
    TaskRecoveryDecision,
    TaskRecoveryStepResolution,
    TaskStepCheckpoint,
    TaskStepState,
    TaskStepTransition,
)
from app.modules.tasks.ports import (
    TaskExecutionRuntimeOutcome,
    TaskRecoveryMutationOutcome,
    TaskStepContinuationDraft,
    TaskWorkflowContextPort,
)
from app.modules.weaponry.application import WeaponryTaskRecoveryPolicy
from tests import workspace_tempdir
from tests.fakes import FakeClock, FakeLeaseHeartbeatSupervisor, FixedTaskLeaseTokenFactory
from tests.test_stage2_analysis_v2_admission import (
    _IdentityFactory,
    _command,
    _execution_profile,
    _translation_profile,
)


_T0 = "2026-08-21T00:00:00.000000Z"


class _NoEffectProbe:
    def execute(self, request: RecoveryOperationRequest) -> RecoveryOperationResult:
        return RecoveryOperationResult(
            kind=RecoveryObservationKind.NO_EFFECT_CONFIRMED,
            evidence_digest="b" * 64,
            reason_code="analysis_source_not_sent",
        )


class _SourceResumeWorkflow:
    """第一次形成 unknown；第二次必须从同一快照创建 Step Attempt 2。"""

    def __init__(self, steps: AnalysisStepRuntime, *, fail_unknown: bool) -> None:
        self._steps = steps
        self._fail_unknown = fail_unknown

    def run(self, context: TaskWorkflowContextPort) -> None:
        execution = context.loaded_input.snapshot
        profile_fingerprint = _execution_profile().fingerprint
        draft = TaskStepContinuationDraft(
            schema_version=1,
            input_payload_fingerprint=context.loaded_input.input_payload_fingerprint,
            execution_profile_fingerprint=profile_fingerprint,
            payload={
                "business_key": execution.business_ref.business_key,
                "resolver": "analysis.source_download.v1",
                "step_key": "source.download",
                "task_id": execution.task_id.value,
            },
        )
        restored = self._steps.load_resume_continuation(
            context,
            execution_profile_fingerprint=profile_fingerprint,
        )
        if restored is not None:
            if restored.step_key != "source.download" or restored.draft != draft:
                raise AssertionError("恢复快照未解析为同一 Source intent")
            draft = restored.draft
        active = self._steps.begin(
            context,
            step_key="source.download",
            idempotency_key=f"analysis:{execution.task_id.value}:source:stable",
            continuation=draft,
        )
        if self._fail_unknown:
            self._steps.fail(
                context,
                active,
                error_code="analysis_source_download_outcome_unknown",
                outcome_unknown=True,
            )
            return
        self._steps.succeed(
            context,
            active,
            TaskStepCheckpoint(
                code="source_downloaded_v1",
                result_ref="analysis-source:v1:" + "c" * 64,
                result_digest="c" * 64,
            ),
        )


class Stage2CheckpointResumeTests(unittest.TestCase):
    def test_authorized_resume_creates_new_task_and_step_attempt_without_overwrite(self) -> None:
        with workspace_tempdir() as temporary_root:
            root = Path(temporary_root)
            old_path = root / "old.sqlite3"
            database_path = root / "task-control.sqlite3"
            sqlite3.connect(old_path).close()
            bootstrap = bootstrap_analysis_task_control_database(old_path, database_path)
            manager = SQLiteTransactionManager(
                SQLiteConnectionFactory(bootstrap, busy_timeout_ms=100)
            )
            root_uows = build_sqlite_task_control_uow_factories(
                manager,
                recovery_resume_preflight_builder=(
                    SQLiteAnalysisRecoveryResumePreflight
                ),
            )
            clock = FakeClock(_T0)
            codec = AnalysisV5TaskCommandCodec(
                execution_profile=_execution_profile(),
                translation_profile=_translation_profile(),
            )
            admitted = SQLiteAnalysisV2BatchAdmissionAdapter(
                admission_uow_factory=root_uows.admission,
                codec=codec,
                clock=clock,
                task_id_factory=_IdentityFactory().task_id,
                batch_id_factory=_IdentityFactory().batch_id,
            ).create_batch_if_allowed(_command(1, prefix="checkpoint-resume"))
            task_id = admitted.executions[0].task_id
            business_uows = SQLiteAnalysisExecutionUnitOfWorkFactory(
                manager,
                execution_builder=SQLiteTaskControlStore,
                callback_delivery_builder=SQLiteCallbackControlStore,
                resource_builder=SQLiteAnalysisV2ResourceStoreAdapter.from_connection,
                result_snapshot_builder=SQLiteAnalysisResultSnapshotStore.from_connection,
            )

            first = self._runtime(
                task_id=task_id,
                root_uows=root_uows,
                business_uows=business_uows,
                codec=codec,
                clock=clock,
                lease_token="analysis-resume-lease-1",
                fail_unknown=True,
            ).run(task_id)
            self.assertIs(TaskExecutionRuntimeOutcome.WORKFLOW_RETURNED, first.outcome)

            with sqlite3.connect(database_path) as connection:
                case_id = connection.execute(
                    "SELECT case_id FROM task_recovery_cases WHERE task_id = ?",
                    (task_id.value,),
                ).fetchone()[0]
            with root_uows.recovery() as unit_of_work:
                snapshot = unit_of_work.recovery.load_case_snapshot(case_id)
            assert snapshot is not None
            coordinator = RecoveryCoordinator(
                clock=clock,
                recovery_uow_factory=root_uows.recovery,
                lease_token_factory=SecureTaskLeaseTokenFactory(),
                policies={
                    "report": ReportTaskRecoveryPolicy(),
                    "weaponry": WeaponryTaskRecoveryPolicy(),
                    "file": AnalysisTaskRecoveryPolicy(),
                },
                recovery_lease_seconds=30,
            )
            claimed = coordinator.claim(
                ClaimRecoveryCaseCommand(
                    task_id=task_id,
                    case_id=case_id,
                    generation=snapshot.case.generation,
                    expected_task_row_version=snapshot.task.row_version,
                    source_attempt_no=snapshot.case.source_attempt_no,
                    source_fencing_token=snapshot.case.source_fencing_token,
                    expected_recovery_fencing_token=0,
                    owner_id="analysis-recovery-worker",
                    operator_marker="operator:test-suite",
                    reason_code="source_no_effect_confirmed",
                )
            )
            self.assertIs(TaskRecoveryMutationOutcome.APPLIED, claimed.outcome)
            assert claimed.session is not None
            observed = coordinator.execute_operation(
                claimed.session,
                RecoveryOperationRequest(
                    operation_id="analysis-source-probe-1",
                    kind=RecoveryOperationKind.PROBE,
                    step_key="source.download",
                    idempotency_key="analysis-source-probe:stable",
                    intent_digest="a" * 64,
                ),
                _NoEffectProbe(),
            )
            self.assertIs(TaskRecoveryMutationOutcome.APPLIED, observed.outcome)
            assert observed.session is not None and observed.observation is not None
            with root_uows.recovery() as unit_of_work:
                current = unit_of_work.recovery.load_case_snapshot(case_id)
            assert current is not None
            step = next(item for item in current.steps if item.step_key == "source.download")
            decision = TaskRecoveryDecision(
                decision_id="analysis-source-retry-decision-1",
                task_id=task_id,
                case_id=case_id,
                generation=current.case.generation,
                recovery_fencing_token=observed.session.authority.fencing_token,
                expected_task_row_version=current.task.row_version,
                source_attempt_no=current.case.source_attempt_no,
                source_fencing_token=current.case.source_fencing_token,
                kind=RecoveryDecisionKind.RETRY_AUTHORIZED,
                evidence_digest=observed.observation.evidence_digest,
                reason_code="source_no_effect_confirmed",
                policy_version="analysis-task-recovery.v1",
                actor_marker="operator:test-suite",
                decided_at=_T0,
                retry_from_step_key="source.download",
                step_resolution=TaskRecoveryStepResolution(
                    source_step_key="source.download",
                    source_step_attempt_no=step.current_step_attempt_no,
                    expected_step_row_version=step.row_version,
                    operation_id="analysis-source-probe-1",
                    observation_id=observed.observation.observation_id,
                    evidence_digest=observed.observation.evidence_digest,
                    target_transition=TaskStepTransition.RETRY_AUTHORIZED,
                ),
            )
            self.assertIs(
                TaskRecoveryMutationOutcome.APPLIED,
                coordinator.decide(observed.session, decision),
            )

            second = self._runtime(
                task_id=task_id,
                root_uows=root_uows,
                business_uows=business_uows,
                codec=codec,
                clock=clock,
                lease_token="analysis-resume-lease-2",
                fail_unknown=False,
            ).run(task_id)
            self.assertIs(TaskExecutionRuntimeOutcome.WORKFLOW_RETURNED, second.outcome)
            with sqlite3.connect(database_path) as connection:
                connection.row_factory = sqlite3.Row
                attempts = connection.execute(
                    "SELECT step_attempt_no, state FROM task_step_attempts "
                    "WHERE task_id = ? AND step_key = 'source.download' "
                    "ORDER BY step_attempt_no",
                    (task_id.value,),
                ).fetchall()
                continuations = connection.execute(
                    "SELECT step_attempt_no, source_step_attempt_no, payload_digest "
                    "FROM analysis_step_continuation_snapshots WHERE task_id = ? "
                    "ORDER BY step_attempt_no",
                    (task_id.value,),
                ).fetchall()
                task_attempt_no = connection.execute(
                    "SELECT current_attempt_no FROM llm_task_executions WHERE execution_id = ?",
                    (task_id.value,),
                ).fetchone()[0]
            self.assertEqual([(1, "outcome_unknown"), (2, "succeeded")], [tuple(row) for row in attempts])
            self.assertEqual(2, task_attempt_no)
            self.assertEqual([(1, 0), (2, 1)], [(row[0], row[1]) for row in continuations])
            self.assertEqual(continuations[0][2], continuations[1][2])

    @staticmethod
    def _runtime(
        *,
        task_id,
        root_uows,
        business_uows,
        codec,
        clock,
        lease_token: str,
        fail_unknown: bool,
    ) -> TaskExecutionRuntime:
        workflow = _SourceResumeWorkflow(
            AnalysisStepRuntime(uow_factory=business_uows, clock=clock),
            fail_unknown=fail_unknown,
        )
        return TaskExecutionRuntime(
            task_type="file",
            owner=TaskOwnerIdentity(
                instance_start_id="12345678-1234-4234-8234-123456789abc",
                process_id=100,
                executor_name="file",
                worker_slot="checkpoint-resume-test",
            ),
            clock=clock,
            execution_uow_factory=root_uows.execution,
            lease_token_factory=FixedTaskLeaseTokenFactory((lease_token,)),
            heartbeat_supervisor_factory=lambda: FakeLeaseHeartbeatSupervisor(),
            workflow_runner=workflow,
            snapshot_loader=CodecTaskExecutionSnapshotLoader(
                query_uow_factory=root_uows.queries,
                codec=codec,
            ),
            lease_settings=TaskLeaseRuntimeSettings(
                lease_duration_seconds=60.0,
                heartbeat_interval_seconds=10.0,
                stop_grace_seconds=15.0,
            ),
        )


if __name__ == "__main__":
    unittest.main()
