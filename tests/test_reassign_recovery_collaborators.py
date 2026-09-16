"""阶段 1E-7：恢复协作器直接单元测试。

这些用例不经 ``RecoverReassignmentOperation.recover`` 触发协作器，专门防止四个文件退化为
Facade 绑定方法的 callback wrapper。所有依赖均为严格内存 Fake，不启动 ``run.py``、线程、
SQLite 文件或 AnythingLLM 网络请求。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.modules.reassign.application import (
    RecoverReassignmentCommand,
    ReassignmentExecutionSettings,
    ReassignmentRecoveryResultCategory,
)
from app.modules.reassign.application.recovery_checkpoints import (
    ReassignmentRecoveryCheckpointReconciler,
)
from app.modules.reassign.application.recovery_compensator import (
    ReassignmentRecoveryCompensator,
)
from app.modules.reassign.application.recovery_finalizer import (
    ReassignmentRecoveryFinalizer,
)
from app.modules.reassign.application.recovery_observer import (
    ReassignmentRecoveryObserver,
)
from app.modules.reassign.application.recovery_types import RecoveryLeaseContext
from app.modules.reassign.domain import (
    ReassignDocumentCommand,
    ReassignmentBindingState,
    ReassignmentDocumentSnapshot,
    ReassignmentOperationStatus,
    ReassignmentStepName,
    ReassignmentStepState,
    ReassignmentTerminalEvidenceKind,
)
from app.modules.reassign.ports import (
    ReassignmentExpiredLeaseTakeoverRequest,
    ReassignmentLocalCommitState,
    ReassignmentMembershipProbeResult,
    ReassignmentMembershipState,
    ReassignmentOperationTransition,
    ReassignmentReservationOutcome,
    ReassignmentReservationRequest,
    ReassignmentWorkspaceOwnership,
    ReassignmentWorkspaceProbeResult,
    ReassignmentWorkspaceProbeState,
    ReassignmentWorkspaceReference,
)
from tests.fakes.reassign import (
    FakeReassignmentKnowledgePort,
    FakeReassignmentRepository,
    PostCommitFailureReassignmentRepository,
)


class _MutableClock:
    """让测试显式控制初始 lease 过期和接管后的有效期。"""

    def __init__(self) -> None:
        self.current = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def expires_after(self, seconds: float) -> str:
        return (
            (self.current + timedelta(seconds=seconds))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class _IdentifierSequence:
    """为 lease 和 claim 生成稳定且不泄漏业务含义的测试标识。"""

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._index = 0

    def __call__(self) -> str:
        self._index += 1
        return f"{self._prefix}-{self._index}"


class ReassignmentRecoveryCollaboratorTests(unittest.TestCase):
    """验证每个协作器均直接执行自己的 Port 算法。"""

    def setUp(self) -> None:
        self._clock = _MutableClock()

    def _settings(self) -> ReassignmentExecutionSettings:
        return ReassignmentExecutionSettings(
            lease_owner="recovery-collaborator-test",
            lease_duration_seconds=120,
            remote_total_timeout_seconds=75,
            lease_safety_margin_seconds=5,
            clock=self._clock,
            operation_id_factory=_IdentifierSequence("unused-operation"),
            lease_token_factory=_IdentifierSequence("recovery-lease"),
            workspace_claim_token_factory=_IdentifierSequence("recovery-claim"),
        )

    @staticmethod
    def _snapshot() -> ReassignmentDocumentSnapshot:
        return ReassignmentDocumentSnapshot(
            document_row_id=1,
            file_name="collaborator.pdf",
            source_architecture_id=11,
            anything_doc_id="doc-collaborator",
            doc_path="/documents/collaborator.pdf",
            original_file_name="恢复协作器测试.pdf",
        )

    def _take_over_expired_operation(self):
        """构造一个已接管的运行中 Operation，供协作器直接调用。"""

        repository = FakeReassignmentRepository(
            documents=(self._snapshot(),),
            workspace_mappings=((11, "source-workspace"), (12, "target-workspace")),
            clock=self._clock,
        )
        settings = self._settings()
        command = ReassignDocumentCommand(
            file_name="collaborator.pdf",
            old_architecture_id_raw=11,
            old_architecture_id_query_value=11,
            new_architecture_id_raw=12,
        )
        with repository.unit_of_work() as unit_of_work:
            reservation = unit_of_work.reserve(
                ReassignmentReservationRequest(
                    command=command,
                    operation_id="operation-recovery-collaborator",
                    lease_owner="forward-owner",
                    lease_token="forward-token",
                    lease_expires_at=self._clock.expires_after(30),
                )
            )
        self.assertIs(ReassignmentReservationOutcome.ACQUIRED, reservation.outcome)
        record = reservation.record
        assert record is not None
        with repository.unit_of_work() as unit_of_work:
            running = unit_of_work.transition_operation(
                ReassignmentOperationTransition(
                    lease=record.lease,
                    next_status=ReassignmentOperationStatus.RUNNING,
                    current_step=ReassignmentStepName.RESERVE_DOCUMENT,
                )
            )
        self.assertEqual(ReassignmentOperationStatus.RUNNING, running.operation.status)

        self._clock.advance(31)
        lease_expires_at = settings.lease_expires_at()
        with repository.unit_of_work() as unit_of_work:
            takeover = unit_of_work.take_over_expired_lease(
                ReassignmentExpiredLeaseTakeoverRequest(
                    operation_id=record.operation.operation_id,
                    expected_fencing_token=record.lease.fencing_token,
                    lease_owner=settings.lease_owner,
                    lease_token=settings.lease_token_factory(),
                    lease_expires_at=lease_expires_at,
                    reason_code="collaborator_unit_test",
                    actor="collaborator@test.local",
                    workspace_claim_token=settings.workspace_claim_token_factory(),
                )
            )
            current = unit_of_work.get_operation(record.operation.operation_id)
        assert takeover.lease is not None
        assert current is not None
        return (
            repository,
            settings,
            current,
            RecoveryLeaseContext(
                lease=takeover.lease,
                preparation_claim=takeover.workspace_preparation_claim,
            ),
        )

    @staticmethod
    def _recovery_command() -> RecoverReassignmentCommand:
        return RecoverReassignmentCommand(
            operation_id="operation-recovery-collaborator",
            expected_fencing_token=1,
            actor="collaborator@test.local",
            reason_code="collaborator_unit_test",
        )

    def test_observer_directly_probes_remote_state_and_records_observation(self) -> None:
        """Observer 自己续租、探测三个远端事实，并在短事务内追加观察记录。"""

        repository, settings, record, context = self._take_over_expired_operation()
        observer = ReassignmentRecoveryObserver(repository, settings)
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )
        knowledge.expect_probe_workspace_reference(
            ReassignmentWorkspaceProbeResult(
                state=ReassignmentWorkspaceProbeState.PRESENT,
                workspace=ReassignmentWorkspaceReference("target-workspace"),
                ownership=ReassignmentWorkspaceOwnership.PREEXISTING,
            )
        )
        knowledge.expect_probe_document_membership(
            ReassignmentMembershipProbeResult(ReassignmentMembershipState.PRESENT)
        )
        knowledge.expect_probe_document_membership(
            ReassignmentMembershipProbeResult(ReassignmentMembershipState.ABSENT)
        )

        observed = observer.observe_remote(context, record, knowledge)
        self.assertIsNotNone(observed)
        assert observed is not None
        renewed_context, remote = observed
        self.assertIs(
            ReassignmentBindingState.CONFIRMED_PRESENT,
            remote.source_binding_state,
        )
        self.assertIs(
            ReassignmentBindingState.CONFIRMED_ABSENT,
            remote.target_binding_state,
        )
        observation = observer.record_observation(
            renewed_context,
            local_state=ReassignmentLocalCommitState.SOURCE_UNCHANGED,
            source_binding_state=remote.source_binding_state,
            target_binding_state=remote.target_binding_state,
            remote_membership_required=True,
            command=self._recovery_command(),
        )
        self.assertIsNotNone(observation)
        knowledge.assert_expectations_consumed()

    def test_checkpoint_reconciler_directly_persists_probe_resolution(self) -> None:
        """Checkpoint Reconciler 独立把已开始的 local-CAS Step 收敛为可审计结果。"""

        repository, _, record, context = self._take_over_expired_operation()
        with repository.unit_of_work() as unit_of_work:
            started = unit_of_work.begin_step_mutation(
                lease=context.lease,
                step_name=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
                recovery_authorized=True,
            )
        self.assertIs(ReassignmentStepState.MUTATION_STARTED, started.step.state)

        reconciler = ReassignmentRecoveryCheckpointReconciler(repository)
        self.assertTrue(
            reconciler.resolve_local_commit_step(
                context,
                record,
                ReassignmentLocalCommitState.SOURCE_UNCHANGED,
            )
        )
        with repository.unit_of_work(read_only=True) as unit_of_work:
            committed = unit_of_work.get_step(
                operation_id=record.operation.operation_id,
                step_name=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
            )
        assert committed is not None
        self.assertIs(ReassignmentStepState.KNOWN_FAILED, committed.step.state)

    def test_compensator_directly_enters_compensating_before_external_write(self) -> None:
        """Compensator 自己写入 compensating 状态，不能由 Facade 绕过该状态机步骤。"""

        repository, settings, record, context = self._take_over_expired_operation()
        observer = ReassignmentRecoveryObserver(repository, settings)
        compensator = ReassignmentRecoveryCompensator(repository, observer)

        self.assertTrue(compensator.enter(context, record))
        with repository.unit_of_work(read_only=True) as unit_of_work:
            transitioned = unit_of_work.get_operation(record.operation.operation_id)
        assert transitioned is not None
        self.assertIs(
            ReassignmentOperationStatus.COMPENSATING,
            transitioned.operation.status,
        )

    def test_finalizer_directly_isolates_unprovable_operation(self) -> None:
        """Finalizer 独立把无法证明一致的 Operation 保留为 recovery_required。"""

        repository, _, record, context = self._take_over_expired_operation()
        finalizer = ReassignmentRecoveryFinalizer(repository)

        result = finalizer.isolate(
            context,
            record,
            self._recovery_command(),
            current_step=ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
            error_code="collaborator_direct_isolation",
        )
        self.assertIs(ReassignmentRecoveryResultCategory.RECOVERY_REQUIRED, result.category)
        with repository.unit_of_work(read_only=True) as unit_of_work:
            isolated = unit_of_work.get_operation(record.operation.operation_id)
        assert isolated is not None
        self.assertIs(
            ReassignmentOperationStatus.RECOVERY_REQUIRED,
            isolated.operation.status,
        )

    def test_finalizer_reconciles_terminal_committed_before_exit_exception(self) -> None:
        """恢复终态已提交但退出异常时，应按持久事实返回真实收口分类。"""

        base_repository, settings, record, context = self._take_over_expired_operation()
        observer = ReassignmentRecoveryObserver(base_repository, settings)
        observation = observer.record_observation(
            context,
            local_state=ReassignmentLocalCommitState.SOURCE_UNCHANGED,
            source_binding_state=ReassignmentBindingState.CONFIRMED_PRESENT,
            target_binding_state=ReassignmentBindingState.CONFIRMED_ABSENT,
            remote_membership_required=True,
            command=self._recovery_command(),
        )
        self.assertIsNotNone(observation)
        assert observation is not None
        repository = PostCommitFailureReassignmentRepository(
            base_repository,
            target_method="finalize_recovery_operation",
        )
        finalizer = ReassignmentRecoveryFinalizer(repository)

        result = finalizer.finalize(
            context,
            record,
            self._recovery_command(),
            observation,
            next_status=ReassignmentOperationStatus.FAILED,
            current_step=ReassignmentStepName.FINALIZE_OPERATION,
            evidence_kind=(
                ReassignmentTerminalEvidenceKind.NO_SIDE_EFFECT_FAILURE_CONFIRMED
            ),
            category=(
                ReassignmentRecoveryResultCategory.RECOVERED_FAILED_NO_SIDE_EFFECT
            ),
            error_code="recovery_no_side_effect_confirmed",
        )

        self.assertIs(
            ReassignmentRecoveryResultCategory.RECOVERED_FAILED_NO_SIDE_EFFECT,
            result.category,
        )
        with base_repository.unit_of_work(read_only=True) as unit_of_work:
            finalized = unit_of_work.get_operation(record.operation.operation_id)
        assert finalized is not None
        self.assertIs(ReassignmentOperationStatus.FAILED, finalized.operation.status)


if __name__ == "__main__":  # pragma: no cover - 仅支持本地 unittest 入口。
    unittest.main()
