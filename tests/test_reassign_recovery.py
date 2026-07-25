"""阶段 1E-5：分类节点变更补偿、恢复、fencing 与崩溃窗口离线验收。"""

from __future__ import annotations

import hashlib
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.modules.reassign.application import (
    RecoverReassignmentCommand,
    RecoverReassignmentOperation,
    ReassignmentExecutionSettings,
    ReassignmentRecoveryResultCategory,
)
from app.modules.reassign.domain import (
    ReassignDocumentCommand,
    ReassignmentBindingState,
    ReassignmentDocumentSnapshot,
    ReassignmentMutationOutcome,
    ReassignmentOperationStatus,
    ReassignmentStepName,
    ReassignmentStepState,
)
from app.modules.reassign.ports import (
    ReassignmentDocumentMutationResult,
    ReassignmentEventType,
    ReassignmentKnowledgeOutcome,
    ReassignmentLease,
    ReassignmentLeaseUpdateResult,
    ReassignmentMembershipProbeResult,
    ReassignmentMembershipState,
    ReassignmentOperationTransition,
    ReassignmentReservationOutcome,
    ReassignmentReservationRequest,
    ReassignmentStepCompletion,
    ReassignmentWorkspaceMappingRequest,
    ReassignmentWorkspaceOwnership,
    ReassignmentWorkspaceProbeResult,
    ReassignmentWorkspaceProbeState,
    ReassignmentWorkspaceReference,
    ReassignmentWriteOutcome,
)
from tests.fakes.reassign import (
    FakeReassignmentKnowledgePort,
    FakeReassignmentKnowledgePortFactory,
    FakeReassignmentRepository,
    FakeReassignmentUnitOfWork,
)


class MutableClock:
    """让 Operation 先过期、接管后再保持有效的可控 UTC 时钟。"""

    def __init__(self) -> None:
        self.current = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, *, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)

    def expires_after(self, *, seconds: float) -> str:
        return (
            (self.current + timedelta(seconds=seconds))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )


class _IdentifierSequence:
    """为测试生成稳定的不透明 lease/claim 标识。"""

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._index = 0

    def __call__(self) -> str:
        self._index += 1
        return f"{self._prefix}-{self._index}"


class ReassignmentRecoveryTests(unittest.TestCase):
    """严格 Fake 验证恢复器不会在事务内 I/O 或对未知结果盲重放。"""

    def setUp(self) -> None:
        self.clock = MutableClock()

    @staticmethod
    def _snapshot() -> ReassignmentDocumentSnapshot:
        return ReassignmentDocumentSnapshot(
            document_row_id=1,
            file_name="document.pdf",
            source_architecture_id=11,
            anything_doc_id="doc-1",
            doc_path="/documents/document.pdf",
            original_file_name="原始文件.pdf",
        )

    def _repository(self) -> FakeReassignmentRepository:
        return FakeReassignmentRepository(
            documents=(self._snapshot(),),
            workspace_mappings=((11, "source-workspace"), (12, "target-workspace")),
            clock=self.clock,
        )

    def _service(
        self,
        repository: FakeReassignmentRepository,
        knowledge: FakeReassignmentKnowledgePort,
    ) -> RecoverReassignmentOperation:
        return RecoverReassignmentOperation(
            repository,
            FakeReassignmentKnowledgePortFactory(lambda: knowledge),
            ReassignmentExecutionSettings(
                lease_owner="recovery-instance-a",
                lease_duration_seconds=120,
                remote_total_timeout_seconds=75,
                lease_safety_margin_seconds=5,
                clock=self.clock,
                operation_id_factory=_IdentifierSequence("unused-operation"),
                lease_token_factory=_IdentifierSequence("recovery-lease"),
                workspace_claim_token_factory=_IdentifierSequence("recovery-claim"),
            ),
        )

    def _reserve_running_operation(
        self,
        repository: FakeReassignmentRepository,
    ):
        command = ReassignDocumentCommand(
            file_name="document.pdf",
            old_architecture_id_raw=11,
            old_architecture_id_query_value=11,
            new_architecture_id_raw=12,
        )
        with repository.unit_of_work() as unit_of_work:
            reservation = unit_of_work.reserve(
                ReassignmentReservationRequest(
                    command=command,
                    operation_id="operation-recovery-1",
                    lease_owner="forward-instance-a",
                    lease_token="forward-lease-1",
                    lease_expires_at=self.clock.expires_after(seconds=30),
                )
            )
        self.assertIs(ReassignmentReservationOutcome.ACQUIRED, reservation.outcome)
        self.assertIsNotNone(reservation.record)
        record = reservation.record
        assert record is not None
        with repository.unit_of_work() as unit_of_work:
            promoted = unit_of_work.transition_operation(
                ReassignmentOperationTransition(
                    lease=record.lease,
                    next_status=ReassignmentOperationStatus.RUNNING,
                    current_step=ReassignmentStepName.RESERVE_DOCUMENT,
                )
            )
        self.assertEqual(ReassignmentOperationStatus.RUNNING, promoted.operation.status)
        return record

    @staticmethod
    def _record_unknown_step(
        repository: FakeReassignmentRepository,
        record,
        step_name: ReassignmentStepName,
    ) -> None:
        with repository.unit_of_work() as unit_of_work:
            started = unit_of_work.begin_step_mutation(
                lease=record.lease,
                step_name=step_name,
            )
        assert started.step.state is ReassignmentStepState.MUTATION_STARTED
        with repository.unit_of_work() as unit_of_work:
            completed = unit_of_work.complete_step(
                ReassignmentStepCompletion(
                    lease=record.lease,
                    step_name=step_name,
                    next_state=ReassignmentStepState.OUTCOME_UNKNOWN,
                    error_code="simulated_checkpoint_loss",
                    probe_outcome=ReassignmentMutationOutcome.OUTCOME_UNKNOWN,
                )
        )
        assert completed.step.state is ReassignmentStepState.OUTCOME_UNKNOWN

    @staticmethod
    def _record_confirmed_effect_step(
        repository: FakeReassignmentRepository,
        record,
        step_name: ReassignmentStepName,
    ) -> None:
        """补齐已经提交检查点的前向成功事实，供成功恢复场景复用。"""

        with repository.unit_of_work() as unit_of_work:
            started = unit_of_work.begin_step_mutation(
                lease=record.lease,
                step_name=step_name,
            )
        assert started.step.state is ReassignmentStepState.MUTATION_STARTED
        with repository.unit_of_work() as unit_of_work:
            completed = unit_of_work.complete_step(
                ReassignmentStepCompletion(
                    lease=record.lease,
                    step_name=step_name,
                    next_state=ReassignmentStepState.SUCCEEDED,
                    probe_outcome=ReassignmentMutationOutcome.CONFIRMED_EFFECT,
                )
            )
        assert completed.step.state is ReassignmentStepState.SUCCEEDED

    @staticmethod
    def _mark_recovery_required(
        repository: FakeReassignmentRepository,
        record,
        *,
        current_step: ReassignmentStepName,
    ) -> None:
        with repository.unit_of_work() as unit_of_work:
            transitioned = unit_of_work.transition_operation(
                ReassignmentOperationTransition(
                    lease=record.lease,
                    next_status=ReassignmentOperationStatus.RECOVERY_REQUIRED,
                    current_step=current_step,
                    error_code="simulated_crash_window",
                )
            )
        assert transitioned.operation.status is ReassignmentOperationStatus.RECOVERY_REQUIRED

    def _expired_recovery_command(self) -> RecoverReassignmentCommand:
        return RecoverReassignmentCommand(
            operation_id="operation-recovery-1",
            expected_fencing_token=1,
            actor="oncall@example.test",
            reason_code="crash_window_reconciliation",
        )

    @staticmethod
    def _workspace_present(slug: str) -> ReassignmentWorkspaceProbeResult:
        return ReassignmentWorkspaceProbeResult(
            state=ReassignmentWorkspaceProbeState.PRESENT,
            workspace=ReassignmentWorkspaceReference(slug),
            ownership=ReassignmentWorkspaceOwnership.PREEXISTING,
        )

    @staticmethod
    def _membership(state: ReassignmentMembershipState) -> ReassignmentMembershipProbeResult:
        return ReassignmentMembershipProbeResult(state=state)

    @staticmethod
    def _mutation(outcome: ReassignmentKnowledgeOutcome) -> ReassignmentDocumentMutationResult:
        return ReassignmentDocumentMutationResult(outcome=outcome)

    def _prepare_unknown_remote_operation(self, repository: FakeReassignmentRepository):
        """模拟来源解绑/目标挂载 HTTP 已完成但两个本地检查点均未提交的窗口。"""

        record = self._reserve_running_operation(repository)
        self._record_unknown_step(
            repository,
            record,
            ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
        )
        self._record_unknown_step(
            repository,
            record,
            ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
        )
        self._mark_recovery_required(
            repository,
            record,
            current_step=ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
        )
        self.clock.advance(seconds=31)
        return record

    def _prepare_compensating_operation(
        self,
        repository: FakeReassignmentRepository,
    ):
        """模拟前向写未知后已进入补偿阶段、但尚未完成最终检查点的现场。"""

        record = self._reserve_running_operation(repository)
        self._record_unknown_step(
            repository,
            record,
            ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
        )
        self._record_unknown_step(
            repository,
            record,
            ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
        )
        with repository.unit_of_work() as unit_of_work:
            compensating = unit_of_work.transition_operation(
                ReassignmentOperationTransition(
                    lease=record.lease,
                    next_status=ReassignmentOperationStatus.COMPENSATING,
                    current_step=ReassignmentStepName.COMPENSATE_TARGET_DOCUMENT,
                )
            )
        self.assertIs(ReassignmentOperationStatus.COMPENSATING, compensating.operation.status)
        return record

    def _expect_initial_remote_probe(
        self,
        knowledge: FakeReassignmentKnowledgePort,
        *,
        source: ReassignmentMembershipState,
        target: ReassignmentMembershipState,
    ) -> None:
        knowledge.expect_probe_workspace_reference(
            self._workspace_present("target-workspace")
        )
        knowledge.expect_probe_document_membership(self._membership(source))
        knowledge.expect_probe_document_membership(self._membership(target))

    def test_unknown_forward_writes_are_probed_then_compensated_in_fixed_order(self) -> None:
        """目标先解绑、来源后恢复；每个写后窗口都以新探测和检查点收敛。"""

        repository = self._repository()
        self._prepare_unknown_remote_operation(repository)
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )
        self._expect_initial_remote_probe(
            knowledge,
            source=ReassignmentMembershipState.ABSENT,
            target=ReassignmentMembershipState.PRESENT,
        )
        knowledge.expect_detach_document(self._mutation(ReassignmentKnowledgeOutcome.APPLIED))
        self._expect_initial_remote_probe(
            knowledge,
            source=ReassignmentMembershipState.ABSENT,
            target=ReassignmentMembershipState.ABSENT,
        )
        knowledge.expect_attach_document(self._mutation(ReassignmentKnowledgeOutcome.APPLIED))
        self._expect_initial_remote_probe(
            knowledge,
            source=ReassignmentMembershipState.PRESENT,
            target=ReassignmentMembershipState.ABSENT,
        )

        result = self._service(repository, knowledge).recover(
            self._expired_recovery_command()
        )

        self.assertIs(ReassignmentRecoveryResultCategory.COMPENSATED, result.category)
        with repository.unit_of_work(read_only=True) as unit_of_work:
            current = unit_of_work.get_operation("operation-recovery-1")
            target_compensation = unit_of_work.get_step(
                operation_id="operation-recovery-1",
                step_name=ReassignmentStepName.COMPENSATE_TARGET_DOCUMENT,
            )
            source_compensation = unit_of_work.get_step(
                operation_id="operation-recovery-1",
                step_name=ReassignmentStepName.COMPENSATE_SOURCE_DOCUMENT,
            )
            events = unit_of_work.list_events("operation-recovery-1")
        assert current is not None and target_compensation is not None and source_compensation is not None
        self.assertIs(ReassignmentOperationStatus.COMPENSATED, current.operation.status)
        self.assertIs(ReassignmentStepState.SUCCEEDED, target_compensation.step.state)
        self.assertIs(ReassignmentStepState.SUCCEEDED, source_compensation.step.state)
        takeover = next(
            event
            for event in events
            if event.event_type is ReassignmentEventType.LEASE_TAKEN_OVER
        )
        self.assertEqual(
            hashlib.sha256(b"oncall@example.test").hexdigest()[:16],
            takeover.actor_digest,
        )
        self.assertEqual("crash_window_reconciliation", takeover.reason_code)
        self.assertEqual(
            [
                "probe_workspace_reference",
                "probe_document_membership",
                "probe_document_membership",
                "detach_document",
                "probe_workspace_reference",
                "probe_document_membership",
                "probe_document_membership",
                "attach_document",
                "probe_workspace_reference",
                "probe_document_membership",
                "probe_document_membership",
            ],
            [method for method, _ in knowledge.calls],
        )
        knowledge.assert_expectations_consumed()

    def test_local_target_without_forward_checkpoints_is_isolated(self) -> None:
        """远端一致但前向事实不完整时，恢复器不能伪造成功终态。"""

        repository = self._repository()
        record = self._reserve_running_operation(repository)
        self._record_unknown_step(
            repository,
            record,
            ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
        )
        self._mark_recovery_required(
            repository,
            record,
            current_step=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
        )
        source = repository._state.documents.pop(("document.pdf", 11))
        target = replace(source, source_architecture_id=12)
        repository._state.documents[(target.file_name, target.source_architecture_id)] = target
        self.clock.advance(seconds=31)
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )
        self._expect_initial_remote_probe(
            knowledge,
            source=ReassignmentMembershipState.ABSENT,
            target=ReassignmentMembershipState.PRESENT,
        )

        result = self._service(repository, knowledge).recover(
            self._expired_recovery_command()
        )

        self.assertIs(ReassignmentRecoveryResultCategory.RECOVERY_REQUIRED, result.category)
        with repository.unit_of_work(read_only=True) as unit_of_work:
            current = unit_of_work.get_operation("operation-recovery-1")
        assert current is not None
        self.assertIs(
            ReassignmentOperationStatus.RECOVERY_REQUIRED,
            current.operation.status,
        )
        self.assertFalse(
            any(method in {"detach_document", "attach_document"} for method, _ in knowledge.calls)
        )
        knowledge.assert_expectations_consumed()

    def test_local_target_with_complete_forward_facts_is_recovered_as_success(self) -> None:
        """本地和远端均成功且持久前向事实完整时，才允许恢复成功。"""

        repository = self._repository()
        record = self._reserve_running_operation(repository)
        self._record_confirmed_effect_step(
            repository,
            record,
            ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
        )
        # 目标分类已有 mapping 时，正常前向路径会在同一持久写中登记 mapping 并完成
        # prepare Step；不能只手工改 Step，否则成功恢复仍缺少目标 workspace 身份事实。
        with repository.unit_of_work() as unit_of_work:
            prepare_started = unit_of_work.begin_step_mutation(
                lease=record.lease,
                step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
            )
        self.assertIs(
            ReassignmentStepState.MUTATION_STARTED,
            prepare_started.step.state,
        )
        with repository.unit_of_work() as unit_of_work:
            prepared = unit_of_work.record_workspace_mapping(
                ReassignmentWorkspaceMappingRequest(
                    lease=record.lease,
                    target_architecture_raw=12,
                    workspace_slug="target-workspace",
                    ownership=ReassignmentWorkspaceOwnership.PREEXISTING,
                )
            )
        self.assertEqual("target-workspace", prepared.target_workspace_slug)
        self._record_confirmed_effect_step(
            repository,
            record,
            ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
        )
        self._record_unknown_step(
            repository,
            record,
            ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
        )
        self._mark_recovery_required(
            repository,
            record,
            current_step=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
        )
        source = repository._state.documents.pop(("document.pdf", 11))
        target = replace(source, source_architecture_id=12)
        repository._state.documents[(target.file_name, target.source_architecture_id)] = target
        self.clock.advance(seconds=31)
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )
        self._expect_initial_remote_probe(
            knowledge,
            source=ReassignmentMembershipState.ABSENT,
            target=ReassignmentMembershipState.PRESENT,
        )

        result = self._service(repository, knowledge).recover(
            self._expired_recovery_command()
        )

        self.assertIs(
            ReassignmentRecoveryResultCategory.RECOVERED_SUCCEEDED,
            result.category,
        )
        with repository.unit_of_work(read_only=True) as unit_of_work:
            current = unit_of_work.get_operation("operation-recovery-1")
        assert current is not None
        self.assertIs(ReassignmentOperationStatus.SUCCEEDED, current.operation.status)
        self.assertFalse(
            any(method in {"detach_document", "attach_document"} for method, _ in knowledge.calls)
        )
        knowledge.assert_expectations_consumed()

    def test_compensation_known_failure_isolated_without_restoring_source(self) -> None:
        """目标解绑失败时绝不伪造补偿成功，也不能继续恢复来源造成双绑定。"""

        repository = self._repository()
        self._prepare_unknown_remote_operation(repository)
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )
        self._expect_initial_remote_probe(
            knowledge,
            source=ReassignmentMembershipState.ABSENT,
            target=ReassignmentMembershipState.PRESENT,
        )
        knowledge.expect_detach_document(
            self._mutation(ReassignmentKnowledgeOutcome.KNOWN_FAILURE)
        )

        result = self._service(repository, knowledge).recover(
            self._expired_recovery_command()
        )

        self.assertIs(ReassignmentRecoveryResultCategory.RECOVERY_REQUIRED, result.category)
        with repository.unit_of_work(read_only=True) as unit_of_work:
            current = unit_of_work.get_operation("operation-recovery-1")
            source_compensation = unit_of_work.get_step(
                operation_id="operation-recovery-1",
                step_name=ReassignmentStepName.COMPENSATE_SOURCE_DOCUMENT,
            )
        assert current is not None and source_compensation is not None
        self.assertIs(ReassignmentOperationStatus.RECOVERY_REQUIRED, current.operation.status)
        self.assertIs(ReassignmentStepState.PENDING, source_compensation.step.state)
        self.assertEqual(
            [
                "probe_workspace_reference",
                "probe_document_membership",
                "probe_document_membership",
                "detach_document",
            ],
            [method for method, _ in knowledge.calls],
        )
        knowledge.assert_expectations_consumed()

    def test_write_intent_before_http_is_confirmed_as_no_side_effect_failure(self) -> None:
        """旧解绑只提交意图即崩溃时，探测来源仍在后不得错误进入补偿。"""

        repository = self._repository()
        record = self._reserve_running_operation(repository)
        with repository.unit_of_work() as unit_of_work:
            started = unit_of_work.begin_step_mutation(
                lease=record.lease,
                step_name=ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
            )
        self.assertIs(ReassignmentStepState.MUTATION_STARTED, started.step.state)
        self._mark_recovery_required(
            repository,
            record,
            current_step=ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
        )
        self.clock.advance(seconds=31)
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )
        self._expect_initial_remote_probe(
            knowledge,
            source=ReassignmentMembershipState.PRESENT,
            target=ReassignmentMembershipState.ABSENT,
        )

        result = self._service(repository, knowledge).recover(
            self._expired_recovery_command()
        )

        self.assertIs(
            ReassignmentRecoveryResultCategory.RECOVERED_FAILED_NO_SIDE_EFFECT,
            result.category,
        )
        with repository.unit_of_work(read_only=True) as unit_of_work:
            current = unit_of_work.get_operation(record.operation.operation_id)
            detach_step = unit_of_work.get_step(
                operation_id=record.operation.operation_id,
                step_name=ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
            )
        assert current is not None and detach_step is not None
        self.assertIs(ReassignmentOperationStatus.FAILED, current.operation.status)
        self.assertIs(ReassignmentStepState.KNOWN_FAILED, detach_step.step.state)
        self.assertEqual(
            [
                "probe_workspace_reference",
                "probe_document_membership",
                "probe_document_membership",
            ],
            [method for method, _ in knowledge.calls],
        )
        knowledge.assert_expectations_consumed()

    def test_workspace_prepare_checkpoint_loss_records_fact_then_restores_source(self) -> None:
        """目标 workspace 已创建但 mapping 未提交时，恢复器只查回、留事实并恢复旧绑定。"""

        repository = FakeReassignmentRepository(
            documents=(self._snapshot(),),
            workspace_mappings=((11, "source-workspace"),),
            clock=self.clock,
        )
        record = self._reserve_running_operation(repository)
        self._record_unknown_step(
            repository,
            record,
            ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
        )
        with repository.unit_of_work() as unit_of_work:
            prepare_started = unit_of_work.begin_step_mutation(
                lease=record.lease,
                step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
            )
        self.assertIs(ReassignmentStepState.MUTATION_STARTED, prepare_started.step.state)
        self._mark_recovery_required(
            repository,
            record,
            current_step=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
        )
        self.clock.advance(seconds=31)
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )
        workspace = ReassignmentWorkspaceReference("target-workspace")

        def expect_target_probe(source: ReassignmentMembershipState) -> None:
            """注册一次无写入的目标 workspace 查回与双侧成员探测。"""

            knowledge.expect_probe_target_workspace(
                ReassignmentWorkspaceProbeResult(
                    state=ReassignmentWorkspaceProbeState.PRESENT,
                    workspace=workspace,
                    ownership=ReassignmentWorkspaceOwnership.UNKNOWN,
                )
            )
            knowledge.expect_probe_document_membership(self._membership(source))
            knowledge.expect_probe_document_membership(
                self._membership(ReassignmentMembershipState.ABSENT)
            )

        expect_target_probe(ReassignmentMembershipState.ABSENT)
        knowledge.expect_attach_document(self._mutation(ReassignmentKnowledgeOutcome.APPLIED))
        expect_target_probe(ReassignmentMembershipState.PRESENT)

        result = self._service(repository, knowledge).recover(
            self._expired_recovery_command()
        )

        self.assertIs(ReassignmentRecoveryResultCategory.COMPENSATED, result.category)
        with repository.unit_of_work(read_only=True) as unit_of_work:
            current = unit_of_work.get_operation(record.operation.operation_id)
            prepare_step = unit_of_work.get_step(
                operation_id=record.operation.operation_id,
                step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
            )
            events = unit_of_work.list_events(record.operation.operation_id)
        assert current is not None and prepare_step is not None
        self.assertEqual("target-workspace", current.target_workspace_slug)
        self.assertIs(
            ReassignmentWorkspaceOwnership.UNKNOWN,
            current.target_workspace_ownership,
        )
        self.assertIs(ReassignmentStepState.MUTATION_STARTED, prepare_step.step.state)
        self.assertEqual("target-workspace", prepare_step.step.external_reference)
        self.assertIn(
            ReassignmentEventType.WORKSPACE_PREPARATION_FACT_RECORDED,
            [event.event_type for event in events],
        )
        self.assertEqual(
            [
                "probe_target_workspace",
                "probe_document_membership",
                "probe_document_membership",
                "attach_document",
                "probe_target_workspace",
                "probe_document_membership",
                "probe_document_membership",
            ],
            [method for method, _ in knowledge.calls],
        )
        knowledge.assert_expectations_consumed()

    def test_workspace_prepare_intent_without_created_workspace_is_no_side_effect_failure(self) -> None:
        """创建意图后尚未发 HTTP 时，查回不存在即可收敛，不创建第二个 workspace。"""

        repository = FakeReassignmentRepository(
            documents=(self._snapshot(),),
            workspace_mappings=((11, "source-workspace"),),
            clock=self.clock,
        )
        record = self._reserve_running_operation(repository)
        with repository.unit_of_work() as unit_of_work:
            prepare_started = unit_of_work.begin_step_mutation(
                lease=record.lease,
                step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
            )
        self.assertIs(ReassignmentStepState.MUTATION_STARTED, prepare_started.step.state)
        self._mark_recovery_required(
            repository,
            record,
            current_step=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
        )
        self.clock.advance(seconds=31)
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )
        knowledge.expect_probe_target_workspace(
            ReassignmentWorkspaceProbeResult(ReassignmentWorkspaceProbeState.ABSENT)
        )
        knowledge.expect_probe_document_membership(
            self._membership(ReassignmentMembershipState.PRESENT)
        )

        result = self._service(repository, knowledge).recover(
            self._expired_recovery_command()
        )

        self.assertIs(
            ReassignmentRecoveryResultCategory.RECOVERED_FAILED_NO_SIDE_EFFECT,
            result.category,
        )
        with repository.unit_of_work(read_only=True) as unit_of_work:
            current = unit_of_work.get_operation(record.operation.operation_id)
            prepare_step = unit_of_work.get_step(
                operation_id=record.operation.operation_id,
                step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
            )
        assert current is not None and prepare_step is not None
        self.assertIs(ReassignmentOperationStatus.FAILED, current.operation.status)
        self.assertIs(ReassignmentStepState.KNOWN_FAILED, prepare_step.step.state)
        self.assertEqual(
            ["probe_target_workspace", "probe_document_membership"],
            [method for method, _ in knowledge.calls],
        )
        knowledge.assert_expectations_consumed()

    def test_target_compensation_checkpoint_loss_is_probed_without_replaying_detach(self) -> None:
        """目标解绑已发生但检查点丢失时，探测收敛后只继续尚未完成的来源恢复。"""

        repository = self._repository()
        record = self._prepare_compensating_operation(repository)
        # 上一进程已经提交“将要解绑目标”的意图，随后 HTTP 完成但尚未来得及写 Step 结果。
        with repository.unit_of_work() as unit_of_work:
            started = unit_of_work.begin_step_mutation(
                lease=record.lease,
                step_name=ReassignmentStepName.COMPENSATE_TARGET_DOCUMENT,
            )
        self.assertIs(ReassignmentStepState.MUTATION_STARTED, started.step.state)
        self.clock.advance(seconds=31)
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )
        self._expect_initial_remote_probe(
            knowledge,
            source=ReassignmentMembershipState.ABSENT,
            target=ReassignmentMembershipState.ABSENT,
        )
        knowledge.expect_attach_document(self._mutation(ReassignmentKnowledgeOutcome.APPLIED))
        self._expect_initial_remote_probe(
            knowledge,
            source=ReassignmentMembershipState.PRESENT,
            target=ReassignmentMembershipState.ABSENT,
        )

        result = self._service(repository, knowledge).recover(
            self._expired_recovery_command()
        )

        self.assertIs(ReassignmentRecoveryResultCategory.COMPENSATED, result.category)
        with repository.unit_of_work(read_only=True) as unit_of_work:
            target_step = unit_of_work.get_step(
                operation_id=record.operation.operation_id,
                step_name=ReassignmentStepName.COMPENSATE_TARGET_DOCUMENT,
            )
            source_step = unit_of_work.get_step(
                operation_id=record.operation.operation_id,
                step_name=ReassignmentStepName.COMPENSATE_SOURCE_DOCUMENT,
            )
        assert target_step is not None and source_step is not None
        self.assertIs(ReassignmentStepState.SUCCEEDED, target_step.step.state)
        self.assertIs(ReassignmentStepState.SUCCEEDED, source_step.step.state)
        self.assertEqual(
            [
                "probe_workspace_reference",
                "probe_document_membership",
                "probe_document_membership",
                "attach_document",
                "probe_workspace_reference",
                "probe_document_membership",
                "probe_document_membership",
            ],
            [method for method, _ in knowledge.calls],
        )
        knowledge.assert_expectations_consumed()

    def test_source_restore_checkpoint_loss_is_probed_and_finalized_without_external_replay(self) -> None:
        """旧绑定恢复已发生但检查点丢失时，双侧探测足以安全终结补偿。"""

        repository = self._repository()
        record = self._prepare_compensating_operation(repository)
        with repository.unit_of_work() as unit_of_work:
            target_started = unit_of_work.begin_step_mutation(
                lease=record.lease,
                step_name=ReassignmentStepName.COMPENSATE_TARGET_DOCUMENT,
            )
        self.assertIs(ReassignmentStepState.MUTATION_STARTED, target_started.step.state)
        with repository.unit_of_work() as unit_of_work:
            target_completed = unit_of_work.complete_step(
                ReassignmentStepCompletion(
                    lease=record.lease,
                    step_name=ReassignmentStepName.COMPENSATE_TARGET_DOCUMENT,
                    next_state=ReassignmentStepState.SUCCEEDED,
                    probe_outcome=ReassignmentMutationOutcome.CONFIRMED_EFFECT,
                )
            )
        self.assertIs(ReassignmentStepState.SUCCEEDED, target_completed.step.state)
        with repository.unit_of_work() as unit_of_work:
            source_started = unit_of_work.begin_step_mutation(
                lease=record.lease,
                step_name=ReassignmentStepName.COMPENSATE_SOURCE_DOCUMENT,
            )
        self.assertIs(ReassignmentStepState.MUTATION_STARTED, source_started.step.state)
        self.clock.advance(seconds=31)
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )
        self._expect_initial_remote_probe(
            knowledge,
            source=ReassignmentMembershipState.PRESENT,
            target=ReassignmentMembershipState.ABSENT,
        )

        result = self._service(repository, knowledge).recover(
            self._expired_recovery_command()
        )

        self.assertIs(ReassignmentRecoveryResultCategory.COMPENSATED, result.category)
        with repository.unit_of_work(read_only=True) as unit_of_work:
            current = unit_of_work.get_operation(record.operation.operation_id)
            source_step = unit_of_work.get_step(
                operation_id=record.operation.operation_id,
                step_name=ReassignmentStepName.COMPENSATE_SOURCE_DOCUMENT,
            )
        assert current is not None and source_step is not None
        self.assertIs(ReassignmentOperationStatus.COMPENSATED, current.operation.status)
        self.assertIs(ReassignmentStepState.SUCCEEDED, source_step.step.state)
        self.assertEqual(
            [
                "probe_workspace_reference",
                "probe_document_membership",
                "probe_document_membership",
            ],
            [method for method, _ in knowledge.calls],
        )
        knowledge.assert_expectations_consumed()

    def test_unknown_probe_never_replays_external_write(self) -> None:
        """只要任一精确成员探测未知，就仅保留可审计现场。"""

        repository = self._repository()
        self._prepare_unknown_remote_operation(repository)
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )
        self._expect_initial_remote_probe(
            knowledge,
            source=ReassignmentMembershipState.OUTCOME_UNKNOWN,
            target=ReassignmentMembershipState.PRESENT,
        )

        result = self._service(repository, knowledge).recover(
            self._expired_recovery_command()
        )

        self.assertIs(ReassignmentRecoveryResultCategory.RECOVERY_REQUIRED, result.category)
        self.assertEqual(
            [
                "probe_workspace_reference",
                "probe_document_membership",
                "probe_document_membership",
            ],
            [method for method, _ in knowledge.calls],
        )
        knowledge.assert_expectations_consumed()

    def test_reference_probe_returning_another_slug_is_isolated_before_mutation(self) -> None:
        """Adapter 返回错误 workspace 引用时不得探测成员，更不能补偿无关永久资源。"""

        repository = self._repository()
        self._prepare_unknown_remote_operation(repository)
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )
        knowledge.expect_probe_workspace_reference(
            self._workspace_present("wrong-target-workspace")
        )

        result = self._service(repository, knowledge).recover(
            self._expired_recovery_command()
        )

        self.assertIs(
            ReassignmentRecoveryResultCategory.RECOVERY_REQUIRED,
            result.category,
        )
        self.assertEqual(
            ["probe_workspace_reference"],
            [method for method, _ in knowledge.calls],
        )
        knowledge.assert_expectations_consumed()

    def test_malformed_takeover_result_is_recovery_pending_not_uncaught_exception(self) -> None:
        """Repository 违反接管返回契约时必须 fail closed，不能抛出属性访问异常。"""

        repository = self._repository()
        self._prepare_unknown_remote_operation(repository)
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )

        with patch.object(
            FakeReassignmentUnitOfWork,
            "take_over_expired_lease",
            return_value=None,
        ):
            result = self._service(repository, knowledge).recover(
                self._expired_recovery_command()
            )

        self.assertIs(
            ReassignmentRecoveryResultCategory.RECOVERY_REQUIRED,
            result.category,
        )
        self.assertEqual((), knowledge.calls)

    def test_takeover_result_with_wrong_lease_identity_is_rejected(self) -> None:
        """即使 DTO 类型正确，也不能接受属于其他 Operation 的 lease。"""

        repository = self._repository()
        self._prepare_unknown_remote_operation(repository)
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )
        wrong_result = ReassignmentLeaseUpdateResult(
            outcome=ReassignmentWriteOutcome.APPLIED,
            lease=ReassignmentLease(
                operation_id="another-operation",
                owner="recovery-instance-a",
                token="recovery-lease-1",
                fencing_token=2,
                expires_at=self.clock.expires_after(seconds=120),
            ),
        )

        with patch.object(
            FakeReassignmentUnitOfWork,
            "take_over_expired_lease",
            return_value=wrong_result,
        ):
            result = self._service(repository, knowledge).recover(
                self._expired_recovery_command()
            )

        self.assertIs(
            ReassignmentRecoveryResultCategory.RECOVERY_REQUIRED,
            result.category,
        )
        self.assertEqual((), knowledge.calls)

    def test_malformed_renewal_result_is_isolated_before_remote_call(self) -> None:
        """续租返回非法 DTO 时不允许执行任何远端探测或补偿写。"""

        repository = self._repository()
        self._prepare_unknown_remote_operation(repository)
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )

        with patch.object(
            FakeReassignmentUnitOfWork,
            "renew_lease",
            return_value=None,
        ):
            result = self._service(repository, knowledge).recover(
                self._expired_recovery_command()
            )

        self.assertIs(
            ReassignmentRecoveryResultCategory.RECOVERY_REQUIRED,
            result.category,
        )
        self.assertEqual((), knowledge.calls)

    def test_initial_repository_read_failure_is_not_reported_as_operation_missing(self) -> None:
        """数据库读取异常必须保持可重试，不能被压缩成 operation_not_found。"""

        repository = self._repository()
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )

        with patch.object(
            FakeReassignmentUnitOfWork,
            "get_operation",
            side_effect=RuntimeError("simulated repository read failure"),
        ):
            result = self._service(repository, knowledge).recover(
                self._expired_recovery_command()
            )

        self.assertIs(
            ReassignmentRecoveryResultCategory.RECOVERY_REQUIRED,
            result.category,
        )
        self.assertEqual((), knowledge.calls)

    def test_nonexpired_or_stale_takeover_never_creates_knowledge_port(self) -> None:
        """预期 token 不匹配或 lease 未过期时，恢复服务在任何远端调用前退出。"""

        repository = self._repository()
        self._reserve_running_operation(repository)
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )
        service = self._service(repository, knowledge)

        nonexpired = service.recover(self._expired_recovery_command())
        stale = service.recover(
            RecoverReassignmentCommand(
                operation_id="operation-recovery-1",
                expected_fencing_token=99,
                actor="oncall@example.test",
                reason_code="stale_token_check",
            )
        )

        self.assertIs(ReassignmentRecoveryResultCategory.TAKEOVER_REJECTED, nonexpired.category)
        self.assertIs(ReassignmentRecoveryResultCategory.TAKEOVER_REJECTED, stale.category)
        self.assertEqual((), knowledge.calls)
        knowledge.assert_expectations_consumed()

    def test_same_operation_concurrent_recovery_has_exactly_one_fencing_owner(self) -> None:
        """两个恢复者携带同一旧 token 时，只有一个可接管并安全关闭 local-only Operation。"""

        local_snapshot = replace(self._snapshot(), doc_path="")
        repository = FakeReassignmentRepository(
            documents=(local_snapshot,),
            clock=self.clock,
        )
        record = self._reserve_running_operation(repository)
        self.clock.advance(seconds=31)
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )
        service = self._service(repository, knowledge)

        def recover_once() -> ReassignmentRecoveryResultCategory:
            return service.recover(self._expired_recovery_command()).category

        with ThreadPoolExecutor(max_workers=2) as executor:
            categories = list(executor.map(lambda _: recover_once(), range(2)))

        self.assertEqual(
            1,
            categories.count(
                ReassignmentRecoveryResultCategory.RECOVERED_FAILED_NO_SIDE_EFFECT
            ),
        )
        self.assertEqual(
            1,
            categories.count(ReassignmentRecoveryResultCategory.TAKEOVER_REJECTED),
        )
        with repository.unit_of_work(read_only=True) as unit_of_work:
            current = unit_of_work.get_operation(record.operation.operation_id)
            events = unit_of_work.list_events(record.operation.operation_id)
        assert current is not None
        self.assertIs(ReassignmentOperationStatus.FAILED, current.operation.status)
        self.assertEqual(
            1,
            sum(
                event.event_type is ReassignmentEventType.LEASE_TAKEN_OVER
                for event in events
            ),
        )
        # local-only 路径不应创建 Knowledge Port；若未来实现意外回归到远端分支，严格 Fake
        # 会在没有预期调用时立即失败。
        self.assertEqual((), knowledge.calls)
        knowledge.assert_expectations_consumed()


if __name__ == "__main__":  # pragma: no cover - 允许单文件离线执行。
    unittest.main()
