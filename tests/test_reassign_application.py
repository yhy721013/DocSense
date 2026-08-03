"""阶段 1E-4：分类节点变更 Application 正常路径与隔离收口测试。"""

from __future__ import annotations

import gc
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.modules.reassign.application import (
    DocumentReassignmentService,
    ReassignmentExecutionSettings,
)
from app.modules.reassign.adapters import SQLiteReassignmentRepository
from app.modules.reassign.domain import (
    ReassignDocumentCommand,
    ReassignmentDocumentSnapshot,
    ReassignmentMutationOutcome,
    ReassignmentOperationStatus,
    ReassignmentPublicMessage,
    ReassignmentResultCategory,
    ReassignmentStepName,
)
from app.modules.reassign.ports import (
    ReassignmentDocumentMutationResult,
    ReassignmentEventType,
    ReassignmentKnowledgeOutcome,
    ReassignmentWorkspaceOwnership,
    ReassignmentWorkspacePreparationResult,
    ReassignmentWorkspaceProbeResult,
    ReassignmentWorkspaceProbeState,
    ReassignmentWorkspaceReference,
    ReassignmentWorkspaceReferenceProbeRequest,
)
from app.services.core.database import DatabaseService
from tests.fakes.reassign import (
    FakeReassignmentKnowledgePort,
    FakeReassignmentKnowledgePortFactory,
    FakeReassignmentRepository,
    PostCommitFailureReassignmentRepository,
)


class FixedClock:
    """为 Application lease 与 Fake Repository 提供完全相同的稳定 UTC 时钟。"""

    def __call__(self) -> datetime:
        return datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


class _IdentifierSequence:
    """让测试能够稳定读取内部 Operation 状态，而不把 UUID 断言写死在业务代码中。"""

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._index = 0

    def __call__(self) -> str:
        self._index += 1
        return f"{self._prefix}-{self._index}"


class _BrokenKnowledgePortFactory:
    """若 local-only 错误触碰远端 Factory，立即让测试失败。"""

    def __init__(self) -> None:
        self.create_count = 0

    def create(self, *, elapsed_seconds: float = 0.0):
        self.create_count += 1
        raise RuntimeError("local-only 不应创建 Knowledge Port")


class _BrokenReservationRepository:
    """模拟 reserve 前或事务提交时的基础设施异常。"""

    def unit_of_work(self, *, read_only: bool = False):
        raise RuntimeError("repository unavailable")


class _MalformedStepWriteUnitOfWork:
    """仅一次返回非法 Step DTO，其余行为委托给严格 Fake。"""

    def __init__(self, inner, owner: "_MalformedStepWriteRepository") -> None:
        self._inner = inner
        self._owner = owner

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._inner.__exit__(exc_type, exc_value, traceback)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def begin_step_mutation(self, **kwargs):
        if self._owner.consume_fault("begin"):
            return None
        return self._inner.begin_step_mutation(**kwargs)

    def complete_step(self, completion):
        if self._owner.consume_fault("complete"):
            return None
        return self._inner.complete_step(completion)


class _MalformedStepWriteRepository:
    """模拟未来 Repository Adapter 违反 Step 返回类型契约。"""

    def __init__(self, base: FakeReassignmentRepository, fault_method: str) -> None:
        self.base = base
        self.fault_method = fault_method
        self.fault_consumed = False

    def consume_fault(self, method: str) -> bool:
        if not self.fault_consumed and self.fault_method == method:
            self.fault_consumed = True
            return True
        return False

    def unit_of_work(self, *, read_only: bool = False):
        return _MalformedStepWriteUnitOfWork(
            self.base.unit_of_work(read_only=read_only),
            self,
        )


class DocumentReassignmentServiceTests(unittest.TestCase):
    """仅使用严格 Fake；不启动 run.py、不访问网络，也不创建真实 HTTP Client。"""

    def setUp(self) -> None:
        self.clock = FixedClock()

    def test_execution_settings_reject_lease_shorter_than_remote_budget(self) -> None:
        """组合根不能配置一个会在单次远端预算结束前过期的 lease。"""

        with self.assertRaisesRegex(
            ValueError,
            "remote_total_timeout_seconds",
        ):
            ReassignmentExecutionSettings(
                lease_owner="test-instance-a",
                lease_duration_seconds=79,
                remote_total_timeout_seconds=75,
                lease_safety_margin_seconds=5,
                clock=self.clock,
            )

    def _snapshot(
        self,
        *,
        architecture_id: int = 11,
        doc_path: str | None = "/documents/document.pdf",
    ) -> ReassignmentDocumentSnapshot:
        return ReassignmentDocumentSnapshot(
            document_row_id=1 if architecture_id == 11 else architecture_id,
            file_name="document.pdf",
            source_architecture_id=architecture_id,
            anything_doc_id="doc-1",
            doc_path=doc_path,
            original_file_name="原始文件.pdf",
        )

    def _service(
        self,
        repository: FakeReassignmentRepository,
        knowledge: FakeReassignmentKnowledgePort,
    ) -> DocumentReassignmentService:
        factory = FakeReassignmentKnowledgePortFactory(lambda: knowledge)
        return DocumentReassignmentService(
            repository,
            factory,
            ReassignmentExecutionSettings(
                lease_owner="test-instance-a",
                lease_duration_seconds=120,
                remote_total_timeout_seconds=75,
                lease_safety_margin_seconds=5,
                clock=self.clock,
                operation_id_factory=_IdentifierSequence("operation"),
                lease_token_factory=_IdentifierSequence("lease"),
                workspace_claim_token_factory=_IdentifierSequence("workspace-claim"),
            ),
        )

    @staticmethod
    def _command(*, target: object = 12) -> ReassignDocumentCommand:
        return ReassignDocumentCommand(
            file_name="document.pdf",
            old_architecture_id_raw=11,
            new_architecture_id_raw=target,
            old_architecture_id_query_value=11,
        )

    def test_remote_success_persists_all_checkpoints_and_local_cas(self) -> None:
        """来源解绑、目标创建、加入、Pin 与本地成功终态必须按固定顺序完整确认。"""

        snapshot = self._snapshot()
        repository = FakeReassignmentRepository(
            documents=(snapshot,),
            workspace_mappings=((11, "source-workspace"),),
            clock=self.clock,
        )
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )
        applied = ReassignmentDocumentMutationResult(ReassignmentKnowledgeOutcome.APPLIED)
        knowledge.expect_detach_document(applied)
        knowledge.expect_prepare_target_workspace(
            ReassignmentWorkspacePreparationResult(
                ReassignmentKnowledgeOutcome.APPLIED,
                workspace=ReassignmentWorkspaceReference("target-workspace"),
                ownership=ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION,
            )
        )
        knowledge.expect_attach_document(applied)
        knowledge.expect_pin_document_best_effort(applied)

        result = self._service(repository, knowledge).execute(self._command())

        self.assertEqual(ReassignmentResultCategory.SUCCEEDED, result.category)
        self.assertEqual(ReassignmentPublicMessage.SUCCEEDED, result.public_message)
        knowledge.assert_expectations_consumed()
        prepare_request = next(
            request
            for method, request in knowledge.calls
            if method == "prepare_target_workspace"
        )
        self.assertEqual("archId-12", prepare_request.desired_workspace_name)
        with repository.unit_of_work(read_only=True) as unit_of_work:
            moved = unit_of_work.get_document_snapshot(
                file_name="document.pdf",
                source_architecture_id=12,
            )
            operation = unit_of_work.get_operation("operation-1")
            prepare_step = unit_of_work.get_step(
                operation_id="operation-1",
                step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
            )
        self.assertIsNotNone(moved)
        self.assertIsNotNone(operation)
        self.assertEqual(ReassignmentOperationStatus.SUCCEEDED, operation.operation.status)
        self.assertIsNotNone(prepare_step)
        self.assertEqual("succeeded", prepare_step.step.state.value)
        with repository.unit_of_work(read_only=True) as unit_of_work:
            events = unit_of_work.list_events("operation-1")
        event_types = [event.event_type for event in events]
        self.assertIn(ReassignmentEventType.LEASE_RENEWED, event_types)
        self.assertIn(ReassignmentEventType.BEST_EFFORT_PIN_ATTEMPTED, event_types)
        self.assertIn(ReassignmentEventType.BEST_EFFORT_PIN_COMPLETED, event_types)

    def test_compatible_target_representations_share_canonical_workspace_name(self) -> None:
        """公开兼容表示必须先投影为同一数据库整数，再生成永久 Workspace 名称。"""

        cases = (
            (12, "archId-12"),
            ("0012", "archId-12"),
            (" 12 ", "archId-12"),
            (False, "archId-0"),
            (-12, "archId--12"),
        )
        for target, expected_name in cases:
            with self.subTest(target=target):
                repository = FakeReassignmentRepository(
                    documents=(self._snapshot(),),
                    workspace_mappings=((11, "source-workspace"),),
                    clock=self.clock,
                )
                knowledge = FakeReassignmentKnowledgePort(
                    transaction_active=lambda: repository.transaction_active
                )
                applied = ReassignmentDocumentMutationResult(
                    ReassignmentKnowledgeOutcome.APPLIED
                )
                knowledge.expect_detach_document(applied)
                knowledge.expect_prepare_target_workspace(
                    ReassignmentWorkspacePreparationResult(
                        ReassignmentKnowledgeOutcome.APPLIED,
                        workspace=ReassignmentWorkspaceReference("target-workspace"),
                        ownership=(
                            ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION
                        ),
                    )
                )
                knowledge.expect_attach_document(applied)
                knowledge.expect_pin_document_best_effort(applied)

                result = self._service(repository, knowledge).execute(
                    self._command(target=target)
                )

                self.assertEqual(ReassignmentResultCategory.SUCCEEDED, result.category)
                prepare_request = next(
                    request
                    for method, request in knowledge.calls
                    if method == "prepare_target_workspace"
                )
                self.assertEqual(expected_name, prepare_request.desired_workspace_name)
                knowledge.assert_expectations_consumed()

    def test_empty_doc_path_only_updates_local_authority_without_network_calls(self) -> None:
        """空 doc_path 保持既有兼容分支：远端三步均不应发生。"""

        snapshot = self._snapshot(doc_path="")
        repository = FakeReassignmentRepository(documents=(snapshot,), clock=self.clock)
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )

        result = self._service(repository, knowledge).execute(self._command())

        self.assertTrue(result.success)
        self.assertEqual((), knowledge.calls)
        with repository.unit_of_work(read_only=True) as unit_of_work:
            moved = unit_of_work.get_document_snapshot(
                file_name="document.pdf",
                source_architecture_id=12,
            )
        self.assertIsNotNone(moved)

    def test_local_commit_post_commit_exception_reconciles_persisted_success(self) -> None:
        """事务已提交但退出确认异常时，必须按权威终态返回成功而非误报待恢复。"""

        snapshot = self._snapshot(doc_path="")
        base_repository = FakeReassignmentRepository(
            documents=(snapshot,),
            clock=self.clock,
        )
        repository = PostCommitFailureReassignmentRepository(
            base_repository,
            target_method="commit_local_architecture",
        )
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: base_repository.transaction_active
        )

        result = self._service(repository, knowledge).execute(self._command())

        self.assertEqual(ReassignmentResultCategory.SUCCEEDED, result.category)
        self.assertEqual(ReassignmentPublicMessage.SUCCEEDED, result.public_message)
        with base_repository.unit_of_work(read_only=True) as unit_of_work:
            operation = unit_of_work.get_operation("operation-1")
        self.assertIsNotNone(operation)
        self.assertEqual(
            ReassignmentOperationStatus.SUCCEEDED,
            operation.operation.status,
        )

    def test_empty_doc_path_does_not_create_knowledge_port(self) -> None:
        """local-only 不能因远端配置或 Factory 故障而失败。"""

        snapshot = self._snapshot(doc_path="")
        repository = FakeReassignmentRepository(documents=(snapshot,), clock=self.clock)
        factory = _BrokenKnowledgePortFactory()
        service = DocumentReassignmentService(
            repository,
            factory,
            ReassignmentExecutionSettings(
                lease_owner="test-instance-a",
                lease_duration_seconds=120,
                remote_total_timeout_seconds=75,
                lease_safety_margin_seconds=5,
                clock=self.clock,
                operation_id_factory=_IdentifierSequence("operation"),
                lease_token_factory=_IdentifierSequence("lease"),
                workspace_claim_token_factory=_IdentifierSequence("workspace-claim"),
            ),
        )

        result = service.execute(self._command())

        self.assertTrue(result.success)
        self.assertEqual(0, factory.create_count)

    def test_reservation_exception_returns_stable_recovery_result(self) -> None:
        """reserve 结果未知时不向 Web 层泄露 Repository 异常。"""

        knowledge = FakeReassignmentKnowledgePort()
        service = DocumentReassignmentService(
            _BrokenReservationRepository(),
            FakeReassignmentKnowledgePortFactory(lambda: knowledge),
            ReassignmentExecutionSettings(
                lease_owner="test-instance-a",
                lease_duration_seconds=120,
                remote_total_timeout_seconds=75,
                lease_safety_margin_seconds=5,
                clock=self.clock,
            ),
        )

        result = service.execute(self._command())

        self.assertEqual(ReassignmentResultCategory.RECOVERY_REQUIRED, result.category)
        self.assertEqual(ReassignmentPublicMessage.RECOVERY_PENDING, result.public_message)
        self.assertEqual((), knowledge.calls)

    def test_existing_target_mapping_is_probed_and_reused_without_second_create(self) -> None:
        """目标本地映射存在时只能查回并复用，不能再次调用 workspace 创建动作。"""

        snapshot = self._snapshot()
        repository = FakeReassignmentRepository(
            documents=(snapshot,),
            workspace_mappings=(
                (11, "source-workspace"),
                (12, "target-workspace"),
            ),
            clock=self.clock,
        )
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )
        applied = ReassignmentDocumentMutationResult(ReassignmentKnowledgeOutcome.APPLIED)
        knowledge.expect_detach_document(applied)
        knowledge.expect_probe_workspace_reference(
            ReassignmentWorkspaceProbeResult(
                state=ReassignmentWorkspaceProbeState.PRESENT,
                workspace=ReassignmentWorkspaceReference("target-workspace"),
                ownership=ReassignmentWorkspaceOwnership.UNKNOWN,
            )
        )
        knowledge.expect_attach_document(applied)
        knowledge.expect_pin_document_best_effort(applied)

        result = self._service(repository, knowledge).execute(self._command())

        self.assertTrue(result.success)
        self.assertEqual(
            [
                "detach_document",
                "probe_workspace_reference",
                "attach_document",
                "pin_document_best_effort",
            ],
            [name for name, _ in knowledge.calls],
        )
        knowledge.assert_expectations_consumed()

    def test_existing_mapping_uses_persisted_slug_and_casefold_identity(self) -> None:
        """既有 mapping 即使远端改名或 slug 大小写变化也应按持久化引用复用。"""

        snapshot = self._snapshot()
        repository = FakeReassignmentRepository(
            documents=(snapshot,),
            workspace_mappings=(
                (11, "source-workspace"),
                (12, "legacy-target-slug"),
            ),
            clock=self.clock,
        )
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )
        applied = ReassignmentDocumentMutationResult(ReassignmentKnowledgeOutcome.APPLIED)
        knowledge.expect_detach_document(applied)
        knowledge.expect_probe_workspace_reference(
            ReassignmentWorkspaceProbeResult(
                state=ReassignmentWorkspaceProbeState.PRESENT,
                workspace=ReassignmentWorkspaceReference("LEGACY-TARGET-SLUG"),
                ownership=ReassignmentWorkspaceOwnership.UNKNOWN,
            ),
            request=ReassignmentWorkspaceReferenceProbeRequest(
                operation_id="operation-1",
                workspace=ReassignmentWorkspaceReference("legacy-target-slug"),
            ),
        )
        knowledge.expect_attach_document(applied)
        knowledge.expect_pin_document_best_effort(applied)

        result = self._service(repository, knowledge).execute(self._command())

        self.assertTrue(result.success)
        knowledge.assert_expectations_consumed()

    def test_known_source_detach_failure_releases_document_protection(self) -> None:
        """确认未产生解绑副作用时，应记录失败终态而非遗留活动 Operation。"""

        snapshot = self._snapshot()
        repository = FakeReassignmentRepository(
            documents=(snapshot,),
            workspace_mappings=((11, "source-workspace"),),
            clock=self.clock,
        )
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )
        knowledge.expect_detach_document(
            ReassignmentDocumentMutationResult(
                ReassignmentKnowledgeOutcome.KNOWN_FAILURE,
                error_code="remote_rejected",
            )
        )

        result = self._service(repository, knowledge).execute(self._command())

        self.assertEqual(ReassignmentResultCategory.FAILED, result.category)
        self.assertEqual(
            ReassignmentPublicMessage.REMOTE_MIGRATION_FAILED,
            result.public_message,
        )
        knowledge.assert_expectations_consumed()
        with repository.unit_of_work(read_only=True) as unit_of_work:
            operation = unit_of_work.get_operation("operation-1")
        self.assertIsNotNone(operation)
        self.assertEqual(ReassignmentOperationStatus.FAILED, operation.operation.status)

    def test_known_target_attach_failure_restores_source_synchronously(self) -> None:
        """目标明确未加入时应在同一请求恢复来源，并释放活动文档保护。"""

        snapshot = self._snapshot()
        repository = FakeReassignmentRepository(
            documents=(snapshot,),
            workspace_mappings=((11, "source-workspace"),),
            clock=self.clock,
        )
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )
        applied = ReassignmentDocumentMutationResult(ReassignmentKnowledgeOutcome.APPLIED)
        knowledge.expect_detach_document(applied)
        knowledge.expect_prepare_target_workspace(
            ReassignmentWorkspacePreparationResult(
                ReassignmentKnowledgeOutcome.APPLIED,
                workspace=ReassignmentWorkspaceReference("target-workspace"),
                ownership=ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION,
            )
        )
        knowledge.expect_attach_document(
            ReassignmentDocumentMutationResult(
                ReassignmentKnowledgeOutcome.KNOWN_FAILURE,
                error_code="remote_rejected",
            )
        )
        knowledge.expect_attach_document(applied)

        result = self._service(repository, knowledge).execute(self._command())

        self.assertEqual(ReassignmentResultCategory.COMPENSATED, result.category)
        self.assertEqual(
            ReassignmentPublicMessage.REMOTE_MIGRATION_FAILED,
            result.public_message,
        )
        knowledge.assert_expectations_consumed()
        with repository.unit_of_work(read_only=True) as unit_of_work:
            operation = unit_of_work.get_operation("operation-1")
        self.assertIsNotNone(operation)
        self.assertEqual(
            ReassignmentOperationStatus.COMPENSATED,
            operation.operation.status,
        )

    def test_known_workspace_prepare_failure_restores_source_and_releases_claim(self) -> None:
        """来源已解绑后目标准备明确失败，应同步恢复来源并原子释放准备权。"""

        snapshot = self._snapshot()
        repository = FakeReassignmentRepository(
            documents=(snapshot,),
            workspace_mappings=((11, "source-workspace"),),
            clock=self.clock,
        )
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )
        applied = ReassignmentDocumentMutationResult(ReassignmentKnowledgeOutcome.APPLIED)
        knowledge.expect_detach_document(applied)
        knowledge.expect_prepare_target_workspace(
            ReassignmentWorkspacePreparationResult(
                ReassignmentKnowledgeOutcome.KNOWN_FAILURE,
                error_code="workspace_create_rejected",
            )
        )
        knowledge.expect_attach_document(applied)

        result = self._service(repository, knowledge).execute(self._command())

        self.assertEqual(ReassignmentResultCategory.COMPENSATED, result.category)
        self.assertEqual(
            ReassignmentPublicMessage.REMOTE_MIGRATION_FAILED,
            result.public_message,
        )
        with repository.unit_of_work(read_only=True) as unit_of_work:
            operation = unit_of_work.get_operation("operation-1")
        self.assertIsNotNone(operation)
        self.assertEqual(
            ReassignmentOperationStatus.COMPENSATED,
            operation.operation.status,
        )
        self.assertFalse(repository._state.workspace_preparation_claims[12].active)
        knowledge.assert_expectations_consumed()

    def test_explicit_source_restore_failure_uses_compensation_failed_message(self) -> None:
        """补偿明确失败必须与远端前向失败、结果未知使用不同稳定文案。"""

        snapshot = self._snapshot()
        repository = FakeReassignmentRepository(
            documents=(snapshot,),
            workspace_mappings=((11, "source-workspace"),),
            clock=self.clock,
        )
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )
        applied = ReassignmentDocumentMutationResult(ReassignmentKnowledgeOutcome.APPLIED)
        knowledge.expect_detach_document(applied)
        knowledge.expect_prepare_target_workspace(
            ReassignmentWorkspacePreparationResult(
                ReassignmentKnowledgeOutcome.APPLIED,
                workspace=ReassignmentWorkspaceReference("target-workspace"),
                ownership=ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION,
            )
        )
        knowledge.expect_attach_document(
            ReassignmentDocumentMutationResult(
                ReassignmentKnowledgeOutcome.KNOWN_FAILURE,
                error_code="remote_rejected",
            )
        )
        knowledge.expect_attach_document(
            ReassignmentDocumentMutationResult(
                ReassignmentKnowledgeOutcome.KNOWN_FAILURE,
                error_code="restore_rejected",
            )
        )

        result = self._service(repository, knowledge).execute(self._command())

        self.assertEqual(ReassignmentResultCategory.RECOVERY_REQUIRED, result.category)
        self.assertEqual(
            ReassignmentPublicMessage.COMPENSATION_FAILED,
            result.public_message,
        )
        with repository.unit_of_work(read_only=True) as unit_of_work:
            operation = unit_of_work.get_operation("operation-1")
        self.assertIsNotNone(operation)
        self.assertEqual(
            ReassignmentOperationStatus.RECOVERY_REQUIRED,
            operation.operation.status,
        )
        knowledge.assert_expectations_consumed()

    def test_local_cas_conflict_detaches_target_then_restores_source(self) -> None:
        """远端迁移完成但本地唯一冲突时，补偿顺序必须固定且返回本地冲突文案。"""

        source = self._snapshot()
        existing_target = self._snapshot(architecture_id=12, doc_path="/other/document.pdf")
        repository = FakeReassignmentRepository(
            documents=(source, existing_target),
            workspace_mappings=((11, "source-workspace"),),
            clock=self.clock,
        )
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )
        applied = ReassignmentDocumentMutationResult(ReassignmentKnowledgeOutcome.APPLIED)
        knowledge.expect_detach_document(applied)
        knowledge.expect_prepare_target_workspace(
            ReassignmentWorkspacePreparationResult(
                ReassignmentKnowledgeOutcome.APPLIED,
                workspace=ReassignmentWorkspaceReference("target-workspace"),
                ownership=ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION,
            )
        )
        knowledge.expect_attach_document(applied)
        knowledge.expect_pin_document_best_effort(applied)
        knowledge.expect_detach_document(applied)
        knowledge.expect_attach_document(applied)

        result = self._service(repository, knowledge).execute(self._command())

        self.assertEqual(ReassignmentResultCategory.COMPENSATED, result.category)
        self.assertEqual(
            ReassignmentPublicMessage.LOCAL_STATE_CONFLICT,
            result.public_message,
        )
        self.assertEqual(
            [
                "detach_document",
                "prepare_target_workspace",
                "attach_document",
                "pin_document_best_effort",
                "detach_document",
                "attach_document",
            ],
            [name for name, _ in knowledge.calls],
        )
        with repository.unit_of_work(read_only=True) as unit_of_work:
            operation = unit_of_work.get_operation("operation-1")
        self.assertIsNotNone(operation)
        self.assertEqual(
            ReassignmentOperationStatus.COMPENSATED,
            operation.operation.status,
        )
        knowledge.assert_expectations_consumed()

    def test_invalid_begin_step_result_blocks_external_write(self) -> None:
        """写意图返回非法 DTO 时必须 fail closed，不能继续调用 AnythingLLM。"""

        snapshot = self._snapshot()
        base_repository = FakeReassignmentRepository(
            documents=(snapshot,),
            workspace_mappings=((11, "source-workspace"),),
            clock=self.clock,
        )
        repository = _MalformedStepWriteRepository(base_repository, "begin")
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: base_repository.transaction_active
        )

        result = self._service(repository, knowledge).execute(self._command())

        self.assertEqual(ReassignmentResultCategory.FAILED, result.category)
        self.assertEqual(ReassignmentPublicMessage.LOCAL_STATE_CONFLICT, result.public_message)
        self.assertTrue(repository.fault_consumed)
        self.assertEqual((), knowledge.calls)

    def test_invalid_complete_step_result_stops_following_remote_actions(self) -> None:
        """外部写后 checkpoint 返回非法 DTO 时必须隔离，不能继续 prepare。"""

        snapshot = self._snapshot()
        base_repository = FakeReassignmentRepository(
            documents=(snapshot,),
            workspace_mappings=((11, "source-workspace"),),
            clock=self.clock,
        )
        repository = _MalformedStepWriteRepository(base_repository, "complete")
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: base_repository.transaction_active
        )
        knowledge.expect_detach_document(
            ReassignmentDocumentMutationResult(ReassignmentKnowledgeOutcome.APPLIED)
        )

        result = self._service(repository, knowledge).execute(self._command())

        self.assertEqual(ReassignmentResultCategory.RECOVERY_REQUIRED, result.category)
        self.assertTrue(repository.fault_consumed)
        self.assertEqual(
            ["detach_document"],
            [name for name, _ in knowledge.calls],
        )
        knowledge.assert_expectations_consumed()

    def test_unknown_target_attach_is_quarantined_without_local_success(self) -> None:
        """加入结果未知时不得提交本地分类，也不得返回普通远端失败。"""

        snapshot = self._snapshot()
        repository = FakeReassignmentRepository(
            documents=(snapshot,),
            workspace_mappings=((11, "source-workspace"),),
            clock=self.clock,
        )
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )
        applied = ReassignmentDocumentMutationResult(ReassignmentKnowledgeOutcome.APPLIED)
        knowledge.expect_detach_document(applied)
        knowledge.expect_prepare_target_workspace(
            ReassignmentWorkspacePreparationResult(
                ReassignmentKnowledgeOutcome.APPLIED,
                workspace=ReassignmentWorkspaceReference("target-workspace"),
                ownership=ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION,
            )
        )
        knowledge.expect_attach_document(
            ReassignmentDocumentMutationResult(
                ReassignmentKnowledgeOutcome.OUTCOME_UNKNOWN,
                error_code="timeout",
            )
        )

        result = self._service(repository, knowledge).execute(self._command())

        self.assertEqual(ReassignmentResultCategory.RECOVERY_REQUIRED, result.category)
        self.assertEqual(ReassignmentPublicMessage.RECOVERY_PENDING, result.public_message)
        knowledge.assert_expectations_consumed()
        with repository.unit_of_work(read_only=True) as unit_of_work:
            original = unit_of_work.get_document_snapshot(
                file_name="document.pdf",
                source_architecture_id=11,
            )
            operation = unit_of_work.get_operation("operation-1")
        self.assertIsNotNone(original)
        self.assertIsNotNone(operation)
        self.assertEqual(
            ReassignmentOperationStatus.RECOVERY_REQUIRED,
            operation.operation.status,
        )

    def test_unknown_workspace_prepare_retains_target_claim_for_recovery(self) -> None:
        """目标创建结果未知时，其他 Operation 不得立即重新取得创建权。"""

        snapshot = self._snapshot()
        repository = FakeReassignmentRepository(
            documents=(snapshot,),
            workspace_mappings=((11, "source-workspace"),),
            clock=self.clock,
        )
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )
        knowledge.expect_detach_document(
            ReassignmentDocumentMutationResult(ReassignmentKnowledgeOutcome.APPLIED)
        )
        knowledge.expect_prepare_target_workspace(
            ReassignmentWorkspacePreparationResult(
                ReassignmentKnowledgeOutcome.OUTCOME_UNKNOWN,
                error_code="timeout",
            )
        )

        result = self._service(repository, knowledge).execute(self._command())

        self.assertEqual(ReassignmentResultCategory.RECOVERY_REQUIRED, result.category)
        claim_state = repository._state.workspace_preparation_claims[12]
        self.assertTrue(claim_state.active)
        self.assertEqual("operation-1", claim_state.claim.operation_id)
        knowledge.assert_expectations_consumed()

    def test_mapping_conflict_preserves_confirmed_remote_workspace_fact(self) -> None:
        """远端创建成功后 mapping 冲突时，slug、归属和 claim 必须留给 1E-5。"""

        snapshot = self._snapshot()
        repository = FakeReassignmentRepository(
            documents=(snapshot,),
            workspace_mappings=(
                (11, "source-workspace"),
                (99, "CONFLICTING-TARGET-SLUG"),
            ),
            clock=self.clock,
        )
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )
        applied = ReassignmentDocumentMutationResult(ReassignmentKnowledgeOutcome.APPLIED)
        knowledge.expect_detach_document(applied)
        knowledge.expect_prepare_target_workspace(
            ReassignmentWorkspacePreparationResult(
                ReassignmentKnowledgeOutcome.APPLIED,
                workspace=ReassignmentWorkspaceReference(
                    "conflicting-target-slug"
                ),
                ownership=ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION,
            )
        )

        result = self._service(repository, knowledge).execute(self._command())

        self.assertEqual(ReassignmentResultCategory.RECOVERY_REQUIRED, result.category)
        with repository.unit_of_work(read_only=True) as unit_of_work:
            operation = unit_of_work.get_operation("operation-1")
            prepare_step = unit_of_work.get_step(
                operation_id="operation-1",
                step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
            )
            events = unit_of_work.list_events("operation-1")
        self.assertIsNotNone(operation)
        self.assertEqual("conflicting-target-slug", operation.target_workspace_slug)
        self.assertIs(
            ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION,
            operation.target_workspace_ownership,
        )
        self.assertIsNotNone(prepare_step)
        self.assertEqual(
            "conflicting-target-slug",
            prepare_step.step.external_reference,
        )
        self.assertIsNone(prepare_step.probe_outcome)
        self.assertTrue(repository._state.workspace_preparation_claims[12].active)
        fact_events = [
            event
            for event in events
            if event.event_type
            is ReassignmentEventType.WORKSPACE_PREPARATION_FACT_RECORDED
        ]
        self.assertEqual(1, len(fact_events))
        self.assertIs(
            ReassignmentMutationOutcome.CONFIRMED_EFFECT,
            fact_events[0].probe_outcome,
        )
        knowledge.assert_expectations_consumed()

    def test_local_only_target_name_conflict_is_failed_without_recovery_lock(self) -> None:
        """本地-only CAS 唯一冲突没有远端副作用，应安全失败并释放源文档保护。"""

        source = self._snapshot(doc_path="")
        target = ReassignmentDocumentSnapshot(
            document_row_id=2,
            file_name="document.pdf",
            source_architecture_id=12,
            anything_doc_id="doc-2",
            doc_path="",
            original_file_name="目标已存在.pdf",
        )
        repository = FakeReassignmentRepository(
            documents=(source, target),
            clock=self.clock,
        )
        knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: repository.transaction_active
        )

        result = self._service(repository, knowledge).execute(self._command())

        self.assertEqual(ReassignmentResultCategory.FAILED, result.category)
        self.assertEqual(
            ReassignmentPublicMessage.LOCAL_STATE_CONFLICT,
            result.public_message,
        )
        with repository.unit_of_work(read_only=True) as unit_of_work:
            operation = unit_of_work.get_operation("operation-1")
        self.assertIsNotNone(operation)
        self.assertEqual(ReassignmentOperationStatus.FAILED, operation.operation.status)


class DocumentReassignmentSQLiteCompositionTests(unittest.TestCase):
    """使用真实 SQLite Repository 与严格 Knowledge Fake 验证 1E-4 组合边界。"""

    def test_remote_success_commits_mapping_claim_release_and_document_cas(self) -> None:
        """Application 不经 Flask 也能把真实本地事务与外部 Port 调用完整组合。"""

        clock = FixedClock()
        with tempfile.TemporaryDirectory(prefix="docsense-reassign-application-") as directory:
            database_path = Path(directory) / "knowledge.sqlite3"
            database = DatabaseService(str(database_path))
            database.save_document_record(
                "document.pdf",
                11,
                anything_doc_id="doc-1",
                doc_path="/documents/document.pdf",
                original_name="原始文件.pdf",
                ingested_file_name="ingested-document.pdf",
            )
            database.add_workspace(11, "source-workspace")
            repository = SQLiteReassignmentRepository(database_path, clock=clock)
            knowledge = FakeReassignmentKnowledgePort()
            applied = ReassignmentDocumentMutationResult(
                ReassignmentKnowledgeOutcome.APPLIED
            )
            knowledge.expect_detach_document(applied)
            knowledge.expect_prepare_target_workspace(
                ReassignmentWorkspacePreparationResult(
                    ReassignmentKnowledgeOutcome.APPLIED,
                    workspace=ReassignmentWorkspaceReference("target-workspace"),
                    ownership=ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION,
                )
            )
            knowledge.expect_attach_document(applied)
            knowledge.expect_pin_document_best_effort(applied)
            service = DocumentReassignmentService(
                repository,
                FakeReassignmentKnowledgePortFactory(
                    lambda: knowledge
                ),
                ReassignmentExecutionSettings(
                    lease_owner="test-sqlite-instance",
                    lease_duration_seconds=120,
                    remote_total_timeout_seconds=75,
                    lease_safety_margin_seconds=5,
                    clock=clock,
                    operation_id_factory=_IdentifierSequence("sqlite-operation"),
                    lease_token_factory=_IdentifierSequence("sqlite-lease"),
                    workspace_claim_token_factory=_IdentifierSequence("sqlite-claim"),
                ),
            )

            result = service.execute(
                ReassignDocumentCommand(
                    file_name="document.pdf",
                    old_architecture_id_raw=11,
                    new_architecture_id_raw=12,
                    old_architecture_id_query_value=11,
                )
            )

            self.assertTrue(result.success)
            knowledge.assert_expectations_consumed()
            with repository.unit_of_work(read_only=True) as unit_of_work:
                moved = unit_of_work.get_document_snapshot(
                    file_name="document.pdf",
                    source_architecture_id=12,
                )
                operation = unit_of_work.get_operation("sqlite-operation-1")
                events = unit_of_work.list_events("sqlite-operation-1")
                target_slug = (
                    None
                    if operation is None
                    else unit_of_work.get_workspace_slug(
                        operation.operation.target_architecture_raw
                    )
                )
            self.assertIsNotNone(moved)
            self.assertEqual("target-workspace", target_slug)
            self.assertIsNotNone(operation)
            self.assertEqual(ReassignmentOperationStatus.SUCCEEDED, operation.operation.status)
            event_types = [event.event_type for event in events]
            self.assertIn(
                ReassignmentEventType.BEST_EFFORT_PIN_ATTEMPTED,
                event_types,
            )
            self.assertIn(
                ReassignmentEventType.BEST_EFFORT_PIN_COMPLETED,
                event_types,
            )
            # Windows 下 DatabaseService 的既有 sqlite3 上下文可能延迟释放连接引用；
            # 显式回收仅用于临时测试库清理，不影响生产执行语义。
            del service
            del repository
            del database
            gc.collect()


if __name__ == "__main__":
    unittest.main()
