"""阶段 1E-5：恢复事实表、claim 接管与终态原子收口的 SQLite 集成测试。"""

from __future__ import annotations

import gc
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.modules.reassign.adapters import SQLiteReassignmentRepository
from app.modules.reassign.application import (
    RecoverReassignmentCommand,
    RecoverReassignmentOperation,
    ReassignmentExecutionSettings,
    ReassignmentRecoveryResultCategory,
)
from app.modules.reassign.domain import (
    ReassignDocumentCommand,
    ReassignmentBindingState,
    ReassignmentContractError,
    ReassignmentMutationOutcome,
    ReassignmentOperationStatus,
    ReassignmentStepName,
    ReassignmentStepState,
    ReassignmentTerminalEvidence,
    ReassignmentTerminalEvidenceKind,
)
from app.modules.reassign.ports import (
    ReassignmentDocumentMutationResult,
    ReassignmentEventType,
    ReassignmentExpiredLeaseTakeoverRequest,
    ReassignmentKnowledgeOutcome,
    ReassignmentLocalCommitState,
    ReassignmentMembershipProbeResult,
    ReassignmentMembershipState,
    ReassignmentOperationRecord,
    ReassignmentOperationTransition,
    ReassignmentRecoveryFinalizationRequest,
    ReassignmentRecoveryObservation,
    ReassignmentReservationOutcome,
    ReassignmentReservationRequest,
    ReassignmentStepCompletion,
    ReassignmentWorkspacePreparationClaim,
    ReassignmentWorkspacePreparationClaimOutcome,
    ReassignmentWorkspacePreparationClaimRequest,
    ReassignmentWorkspacePreparationFactRequest,
    ReassignmentWorkspaceOwnership,
    ReassignmentWorkspaceProbeResult,
    ReassignmentWorkspaceProbeState,
    ReassignmentWorkspaceReference,
    ReassignmentWriteOutcome,
)
from app.services.core.database import DatabaseService
from tests.fakes.reassign import (
    FakeReassignmentKnowledgePort,
    FakeReassignmentKnowledgePortFactory,
)


class AdjustableClock:
    """使 lease 与 claim 可在不等待真实时间的情况下进入过期窗口。"""

    def __init__(self) -> None:
        self.value = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)

    def expires_after(self, *, seconds: int) -> str:
        return (
            (self.value + timedelta(seconds=seconds))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )


class ReassignmentRecoverySQLiteTests(unittest.TestCase):
    """仅使用临时 SQLite，不启动 run.py、不创建 HTTP Client。"""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="docsense-reassign-recovery-")
        self.db_path = Path(self._temp_dir.name) / "knowledge.sqlite3"
        self.database = DatabaseService(str(self.db_path))
        self.database.save_document_record(
            "document.pdf",
            11,
            anything_doc_id="doc-1",
            doc_path="/documents/document.pdf",
            original_name="原始文件.pdf",
            ingested_file_name="ingested-document.pdf",
        )
        self.clock = AdjustableClock()
        self.repository = SQLiteReassignmentRepository(self.db_path, clock=self.clock)

    def tearDown(self) -> None:
        gc.collect()
        self._temp_dir.cleanup()

    def _reserve_running_with_claim(self) -> tuple[
        ReassignmentOperationRecord,
        ReassignmentWorkspacePreparationClaim,
    ]:
        command = ReassignDocumentCommand(
            file_name="document.pdf",
            old_architecture_id_raw=11,
            old_architecture_id_query_value=11,
            new_architecture_id_raw=12,
        )
        with self.repository.unit_of_work() as unit_of_work:
            reserved = unit_of_work.reserve(
                ReassignmentReservationRequest(
                    command=command,
                    operation_id="operation-recovery-sqlite",
                    lease_owner="forward-owner",
                    lease_token="forward-token",
                    lease_expires_at=self.clock.expires_after(seconds=30),
                )
            )
        self.assertIs(ReassignmentReservationOutcome.ACQUIRED, reserved.outcome)
        assert reserved.record is not None
        with self.repository.unit_of_work() as unit_of_work:
            running = unit_of_work.transition_operation(
                ReassignmentOperationTransition(
                    lease=reserved.record.lease,
                    next_status=ReassignmentOperationStatus.RUNNING,
                    current_step=ReassignmentStepName.RESERVE_DOCUMENT,
                )
            )
        self.assertIsInstance(running, ReassignmentOperationRecord)
        assert isinstance(running, ReassignmentOperationRecord)
        with self.repository.unit_of_work() as unit_of_work:
            claimed = unit_of_work.acquire_workspace_preparation_claim(
                ReassignmentWorkspacePreparationClaimRequest(
                    operation_lease=running.lease,
                    target_architecture_raw=12,
                    claim_token="forward-claim-token",
                    claim_expires_at=running.lease.expires_at,
                )
            )
        self.assertIs(
            ReassignmentWorkspacePreparationClaimOutcome.ACQUIRED,
            claimed.outcome,
        )
        assert claimed.claim is not None
        return running, claimed.claim

    def _take_over(
        self,
        record: ReassignmentOperationRecord,
    ):
        with self.repository.unit_of_work() as unit_of_work:
            return unit_of_work.take_over_expired_lease(
                ReassignmentExpiredLeaseTakeoverRequest(
                    operation_id=record.operation.operation_id,
                    expected_fencing_token=record.lease.fencing_token,
                    lease_owner="recovery-owner",
                    lease_token="recovery-token",
                    lease_expires_at=self.clock.expires_after(seconds=300),
                    actor="oncall@example.test",
                    reason_code="sqlite_recovery_test",
                    workspace_claim_token="recovery-claim-token",
                )
            )

    def test_expired_takeover_transfers_matching_claim_with_new_owner_and_fencing(self) -> None:
        """Operation/claim 在同一短事务接管，旧 owner 不能再释放新的 claim。"""

        record, old_claim = self._reserve_running_with_claim()
        self.clock.advance(seconds=31)

        takeover = self._take_over(record)

        self.assertIs(ReassignmentWriteOutcome.APPLIED, takeover.outcome)
        assert takeover.lease is not None and takeover.workspace_preparation_claim is not None
        new_claim = takeover.workspace_preparation_claim
        self.assertEqual("recovery-owner", new_claim.owner)
        self.assertEqual("recovery-claim-token", new_claim.token)
        self.assertGreater(new_claim.fencing_token, old_claim.fencing_token)
        with self.repository.unit_of_work() as unit_of_work:
            stale_release = unit_of_work.release_workspace_preparation_claim(old_claim)
        self.assertIs(ReassignmentWriteOutcome.STALE_LEASE, stale_release)
        with self.repository.unit_of_work(read_only=True) as unit_of_work:
            events = unit_of_work.list_events(record.operation.operation_id)
        self.assertIn(
            ReassignmentEventType.WORKSPACE_PREPARATION_CLAIM_TAKEN_OVER,
            [event.event_type for event in events],
        )

    def test_latest_observation_gates_terminal_and_releases_recovered_claim_atomically(self) -> None:
        """旧探测不能关闭 Operation；最新探测通过后，终态和 claim 释放共同提交。"""

        record, _ = self._reserve_running_with_claim()
        self.clock.advance(seconds=31)
        takeover = self._take_over(record)
        assert takeover.lease is not None and takeover.workspace_preparation_claim is not None
        lease = takeover.lease
        claim = takeover.workspace_preparation_claim
        # ``running -> compensated`` 不是合法出边。真实恢复器会在第一笔补偿写前留下
        # ``compensating`` 阶段事实；这里显式模拟该已提交检查点，以验证终态原子收口本身。
        with self.repository.unit_of_work() as unit_of_work:
            compensating = unit_of_work.transition_operation(
                ReassignmentOperationTransition(
                    lease=lease,
                    next_status=ReassignmentOperationStatus.COMPENSATING,
                    current_step=ReassignmentStepName.COMPENSATE_TARGET_DOCUMENT,
                    recovery_authorized=True,
                )
            )
        self.assertIsInstance(compensating, ReassignmentOperationRecord)
        observation = ReassignmentRecoveryObservation(
            lease=lease,
            local_commit_state=ReassignmentLocalCommitState.SOURCE_UNCHANGED,
            source_binding_state=ReassignmentBindingState.CONFIRMED_PRESENT,
            target_binding_state=ReassignmentBindingState.CONFIRMED_ABSENT,
            remote_membership_required=True,
            actor="oncall@example.test",
            reason_code="sqlite_recovery_test",
        )
        with self.repository.unit_of_work() as unit_of_work:
            first = unit_of_work.record_recovery_observation(observation)
        with self.repository.unit_of_work() as unit_of_work:
            latest = unit_of_work.record_recovery_observation(observation)
        self.assertNotEqual(first.observation_id, latest.observation_id)
        with self.assertRaises(ReassignmentContractError):
            with self.repository.unit_of_work() as unit_of_work:
                unit_of_work.finalize_recovery_operation(
                    ReassignmentRecoveryFinalizationRequest(
                        lease=lease,
                        observation=first,
                        next_status=ReassignmentOperationStatus.COMPENSATED,
                        current_step=ReassignmentStepName.FINALIZE_OPERATION,
                        terminal_evidence=ReassignmentTerminalEvidence(
                            ReassignmentTerminalEvidenceKind.COMPENSATION_CONFIRMED
                        ),
                        preparation_claim=claim,
                    )
                )
        with self.repository.unit_of_work() as unit_of_work:
            finalized = unit_of_work.finalize_recovery_operation(
                ReassignmentRecoveryFinalizationRequest(
                    lease=lease,
                    observation=latest,
                    next_status=ReassignmentOperationStatus.COMPENSATED,
                    current_step=ReassignmentStepName.FINALIZE_OPERATION,
                    terminal_evidence=ReassignmentTerminalEvidence(
                        ReassignmentTerminalEvidenceKind.COMPENSATION_CONFIRMED
                    ),
                    preparation_claim=claim,
                )
            )
        self.assertIsInstance(finalized, ReassignmentOperationRecord)
        assert isinstance(finalized, ReassignmentOperationRecord)
        self.assertIs(ReassignmentOperationStatus.COMPENSATED, finalized.operation.status)
        with closing(sqlite3.connect(self.db_path)) as connection:
            claim_state = connection.execute(
                """
                SELECT state FROM reassign_workspace_preparation_claims
                WHERE operation_id = ?
                """,
                (record.operation.operation_id,),
            ).fetchone()
        assert claim_state is not None
        self.assertEqual("released", claim_state[0])

    def test_local_probe_distinguishes_source_target_and_conflict(self) -> None:
        """恢复服务可依据冻结身份判断 CAS 未提交、已提交或第三方变更。"""

        record, _ = self._reserve_running_with_claim()
        with self.repository.unit_of_work(read_only=True) as unit_of_work:
            before = unit_of_work.probe_local_commit_state(record.operation.operation_id)
        self.assertIs(ReassignmentLocalCommitState.SOURCE_UNCHANGED, before)
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("UPDATE documents SET architecture_id = ? WHERE id = ?", (12, 1))
            connection.commit()
        with self.repository.unit_of_work(read_only=True) as unit_of_work:
            target = unit_of_work.probe_local_commit_state(record.operation.operation_id)
        self.assertIs(ReassignmentLocalCommitState.TARGET_COMMITTED, target)
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("UPDATE documents SET architecture_id = ? WHERE id = ?", (99, 1))
            connection.commit()
        with self.repository.unit_of_work(read_only=True) as unit_of_work:
            conflict = unit_of_work.probe_local_commit_state(record.operation.operation_id)
        self.assertIs(ReassignmentLocalCommitState.CONFLICT, conflict)

    def test_recovery_success_finalizer_rejects_missing_forward_persistent_facts(self) -> None:
        """远端观测一致不能替代目标 mapping 和前向 Step 的持久检查点。"""

        record, _ = self._reserve_running_with_claim()
        self.clock.advance(seconds=31)
        takeover = self._take_over(record)
        assert takeover.lease is not None
        lease = takeover.lease
        # 模拟本地 CAS 已经生效、远端成员关系也符合成功状态，但 process 在前向事实
        # 持久化前退出。终态入口必须拒绝这类“看起来成功”的不完整现场。
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE documents SET architecture_id = ? WHERE id = ?",
                (12, 1),
            )
            connection.commit()
        with self.repository.unit_of_work() as unit_of_work:
            observation = unit_of_work.record_recovery_observation(
                ReassignmentRecoveryObservation(
                    lease=lease,
                    local_commit_state=ReassignmentLocalCommitState.TARGET_COMMITTED,
                    source_binding_state=ReassignmentBindingState.CONFIRMED_ABSENT,
                    target_binding_state=ReassignmentBindingState.CONFIRMED_PRESENT,
                    remote_membership_required=True,
                    actor="oncall@example.test",
                    reason_code="sqlite_recovery_success_fact_gate",
                )
            )
        with self.assertRaises(ReassignmentContractError):
            with self.repository.unit_of_work() as unit_of_work:
                unit_of_work.finalize_recovery_operation(
                    ReassignmentRecoveryFinalizationRequest(
                        lease=lease,
                        observation=observation,
                        next_status=ReassignmentOperationStatus.SUCCEEDED,
                        current_step=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
                        terminal_evidence=ReassignmentTerminalEvidence(
                            ReassignmentTerminalEvidenceKind.FORWARD_SUCCESS_CONFIRMED
                        ),
                    )
                )
        with self.repository.unit_of_work(read_only=True) as unit_of_work:
            current = unit_of_work.get_operation(record.operation.operation_id)
        assert current is not None
        # 终态写事务整体回滚，不能提前释放活动保护或半提交任何收口状态。
        self.assertIs(ReassignmentOperationStatus.RUNNING, current.operation.status)
        self.assertEqual(lease.fencing_token, current.lease.fencing_token)

    def test_recovery_workspace_fact_requires_new_fencing_after_isolation(self) -> None:
        """旧 owner 不能借 recovery_authorized 覆盖 prepare 现场，接管后的新 fencing 才可记事实。"""

        record, _ = self._reserve_running_with_claim()
        with self.repository.unit_of_work() as unit_of_work:
            started = unit_of_work.begin_step_mutation(
                lease=record.lease,
                step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
            )
        self.assertIs(ReassignmentStepState.MUTATION_STARTED, started.step.state)
        with self.repository.unit_of_work() as unit_of_work:
            isolated = unit_of_work.transition_operation(
                ReassignmentOperationTransition(
                    lease=record.lease,
                    next_status=ReassignmentOperationStatus.RECOVERY_REQUIRED,
                    current_step=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                    error_code="simulated_prepare_checkpoint_loss",
                )
            )
        self.assertIs(ReassignmentOperationStatus.RECOVERY_REQUIRED, isolated.operation.status)
        old_request = ReassignmentWorkspacePreparationFactRequest(
            lease=record.lease,
            workspace_slug="target-workspace",
            ownership=ReassignmentWorkspaceOwnership.UNKNOWN,
            error_code="recovery_workspace_preparation_fact",
            recovery_authorized=True,
        )
        with self.repository.unit_of_work() as unit_of_work:
            old_owner_result = unit_of_work.record_workspace_preparation_fact(
                old_request
            )
        self.assertIs(ReassignmentWriteOutcome.CONFLICT, old_owner_result)

        self.clock.advance(seconds=31)
        takeover = self._take_over(record)
        assert takeover.lease is not None
        with self.repository.unit_of_work() as unit_of_work:
            recovered_fact = unit_of_work.record_workspace_preparation_fact(
                ReassignmentWorkspacePreparationFactRequest(
                    lease=takeover.lease,
                    workspace_slug="target-workspace",
                    ownership=ReassignmentWorkspaceOwnership.UNKNOWN,
                    error_code="recovery_workspace_preparation_fact",
                    recovery_authorized=True,
                )
            )
        self.assertIsInstance(recovered_fact, ReassignmentOperationRecord)
        assert isinstance(recovered_fact, ReassignmentOperationRecord)
        self.assertEqual("target-workspace", recovered_fact.target_workspace_slug)
        self.assertIs(
            ReassignmentWorkspaceOwnership.UNKNOWN,
            recovered_fact.target_workspace_ownership,
        )

    def test_running_operation_enters_compensating_before_sqlite_backed_recovery_writes(self) -> None:
        """真实 SQLite 状态机下，补偿写前必须留存 compensating 检查点。"""

        # 来源 mapping 在 reserve 前存在，确保冻结快照保留可恢复的旧 workspace 身份；目标
        # mapping 故意缺失，让恢复器走确定性名称的只读查回，模拟创建后 mapping 未提交窗口。
        self.database.add_workspace(11, "source-workspace")
        record, _ = self._reserve_running_with_claim()
        for step_name in (
            ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
            ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
        ):
            with self.repository.unit_of_work() as unit_of_work:
                started = unit_of_work.begin_step_mutation(
                    lease=record.lease,
                    step_name=step_name,
                )
            self.assertIs(ReassignmentStepState.MUTATION_STARTED, started.step.state)
            with self.repository.unit_of_work() as unit_of_work:
                completed = unit_of_work.complete_step(
                    ReassignmentStepCompletion(
                        lease=record.lease,
                        step_name=step_name,
                        next_state=ReassignmentStepState.OUTCOME_UNKNOWN,
                        error_code="simulated_checkpoint_loss",
                        probe_outcome=ReassignmentMutationOutcome.OUTCOME_UNKNOWN,
                    )
                )
            self.assertIs(ReassignmentStepState.OUTCOME_UNKNOWN, completed.step.state)

        self.clock.advance(seconds=31)
        knowledge = FakeReassignmentKnowledgePort(transaction_active=lambda: False)
        workspace = ReassignmentWorkspaceReference("target-workspace")

        def expect_remote_state(
            source_state: ReassignmentMembershipState,
            target_state: ReassignmentMembershipState,
        ) -> None:
            """注册一次确定性 workspace 查回和两侧成员探测。"""

            knowledge.expect_probe_target_workspace(
                ReassignmentWorkspaceProbeResult(
                    state=ReassignmentWorkspaceProbeState.PRESENT,
                    workspace=workspace,
                    ownership=ReassignmentWorkspaceOwnership.UNKNOWN,
                )
            )
            knowledge.expect_probe_document_membership(
                ReassignmentMembershipProbeResult(source_state)
            )
            knowledge.expect_probe_document_membership(
                ReassignmentMembershipProbeResult(target_state)
            )

        expect_remote_state(
            ReassignmentMembershipState.ABSENT,
            ReassignmentMembershipState.PRESENT,
        )
        knowledge.expect_detach_document(
            ReassignmentDocumentMutationResult(ReassignmentKnowledgeOutcome.APPLIED)
        )
        expect_remote_state(
            ReassignmentMembershipState.ABSENT,
            ReassignmentMembershipState.ABSENT,
        )
        knowledge.expect_attach_document(
            ReassignmentDocumentMutationResult(ReassignmentKnowledgeOutcome.APPLIED)
        )
        expect_remote_state(
            ReassignmentMembershipState.PRESENT,
            ReassignmentMembershipState.ABSENT,
        )

        service = RecoverReassignmentOperation(
            self.repository,
            FakeReassignmentKnowledgePortFactory(lambda: knowledge),
            ReassignmentExecutionSettings(
                lease_owner="sqlite-recovery-owner",
                lease_duration_seconds=120,
                remote_total_timeout_seconds=75,
                lease_safety_margin_seconds=5,
                clock=self.clock,
                operation_id_factory=lambda: "unused-operation-id",
                lease_token_factory=lambda: "sqlite-recovery-lease",
                workspace_claim_token_factory=lambda: "sqlite-recovery-claim",
            ),
        )

        result = service.recover(
            RecoverReassignmentCommand(
                operation_id=record.operation.operation_id,
                expected_fencing_token=record.lease.fencing_token,
                actor="oncall@example.test",
                reason_code="sqlite_running_recovery_test",
            )
        )

        self.assertIs(ReassignmentRecoveryResultCategory.COMPENSATED, result.category)
        with self.repository.unit_of_work(read_only=True) as unit_of_work:
            recovered = unit_of_work.get_operation(record.operation.operation_id)
            events = unit_of_work.list_events(record.operation.operation_id)
        assert recovered is not None
        self.assertIs(ReassignmentOperationStatus.COMPENSATED, recovered.operation.status)
        compensating_event_index = next(
            index
            for index, event in enumerate(events)
            if event.event_type is ReassignmentEventType.OPERATION_TRANSITIONED
            and event.operation_status is ReassignmentOperationStatus.COMPENSATING
        )
        first_compensation_write_index = next(
            index
            for index, event in enumerate(events)
            if event.event_type is ReassignmentEventType.STEP_MUTATION_STARTED
            and event.step_name is ReassignmentStepName.COMPENSATE_TARGET_DOCUMENT
        )
        self.assertLess(compensating_event_index, first_compensation_write_index)
        knowledge.assert_expectations_consumed()


if __name__ == "__main__":  # pragma: no cover - 允许单文件离线执行。
    unittest.main()
