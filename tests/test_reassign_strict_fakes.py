"""阶段 1E-2：分类节点变更严格 Fake 的调用、事务和副作用门禁。"""

from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor

from app.modules.reassign.domain import (
    ReassignmentContractError,
    ReassignmentDocumentSnapshot,
    ReassignmentStepName,
)
from app.modules.reassign.ports import (
    ReassignmentDocumentMutationRequest,
    ReassignmentDocumentMutationResult,
    ReassignmentDocumentReference,
    ReassignmentKnowledgeOutcome,
    ReassignmentWorkspacePreparationRequest,
    ReassignmentWorkspacePreparationResult,
    ReassignmentWorkspaceOwnership,
    ReassignmentWorkspaceProbeResult,
    ReassignmentWorkspaceProbeState,
    ReassignmentWorkspaceReference,
    ReassignmentWorkspaceReferenceProbeRequest,
)
from tests.fakes.reassign import (
    FakeReassignmentKnowledgePort,
    FakeReassignmentRepository,
)


class ReassignmentStrictFakeTests(unittest.TestCase):
    """严格 Fake 不能把未配置网络动作或错误 Saga 顺序伪装成成功。"""

    def setUp(self) -> None:
        self.snapshot = ReassignmentDocumentSnapshot(
            document_row_id=1,
            file_name="document.pdf",
            source_architecture_id=11,
            anything_doc_id="doc-1",
            doc_path="/documents/document.pdf",
            original_file_name="原始文件.pdf",
        )
        self.repository = FakeReassignmentRepository(documents=(self.snapshot,))
        self.knowledge = FakeReassignmentKnowledgePort(
            transaction_active=lambda: self.repository.transaction_active
        )
        self.request = ReassignmentDocumentMutationRequest(
            operation_id="operation-1",
            step_name=ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
            workspace=ReassignmentWorkspaceReference("target-workspace"),
            document=ReassignmentDocumentReference.from_snapshot(self.snapshot),
            architecture_raw=12,
            idempotency_key="attach-key",
        )
        self.applied = ReassignmentDocumentMutationResult(
            ReassignmentKnowledgeOutcome.APPLIED
        )

    def test_unconfigured_external_call_fails_fast(self) -> None:
        with self.assertRaisesRegex(AssertionError, "未声明调用"):
            self.knowledge.attach_document(self.request)

    def test_external_call_inside_active_unit_of_work_fails_fast(self) -> None:
        self.knowledge.expect_attach_document(self.applied, request=self.request)

        with self.assertRaisesRegex(AssertionError, "UnitOfWork 活动"):
            with self.repository.unit_of_work():
                self.knowledge.attach_document(self.request)

    def test_other_thread_transaction_does_not_block_external_call_context(self) -> None:
        """事务门禁必须按调用上下文隔离，不能把其他文档线程误判为事务内网络。"""

        self.knowledge.expect_attach_document(self.applied, request=self.request)
        with self.repository.unit_of_work():
            with ThreadPoolExecutor(max_workers=1) as executor:
                result = executor.submit(
                    self.knowledge.attach_document,
                    self.request,
                ).result(timeout=5)
        self.assertEqual(self.applied, result)

    def test_nested_fake_unit_of_work_is_rejected(self) -> None:
        """Fake 必须像真实独立 SQLite 事务一样拒绝同上下文嵌套写事务。"""

        with self.repository.unit_of_work():
            with self.assertRaisesRegex(ReassignmentContractError, "嵌套"):
                with self.repository.unit_of_work():
                    pass

    def test_expected_call_order_is_strict(self) -> None:
        detach_request = ReassignmentDocumentMutationRequest(
            operation_id="operation-1",
            step_name=ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
            workspace=ReassignmentWorkspaceReference("source-workspace"),
            document=ReassignmentDocumentReference.from_snapshot(self.snapshot),
            architecture_raw=11,
            idempotency_key="detach-key",
        )
        self.knowledge.expect_detach_document(self.applied, request=detach_request)
        self.knowledge.expect_attach_document(self.applied, request=self.request)

        with self.assertRaisesRegex(AssertionError, "调用顺序错误"):
            self.knowledge.attach_document(self.request)

    def test_duplicate_confirmed_side_effect_requires_explicit_permission(self) -> None:
        self.knowledge.expect_attach_document(self.applied, request=self.request)
        self.knowledge.expect_attach_document(self.applied, request=self.request)

        self.assertEqual(self.applied, self.knowledge.attach_document(self.request))
        with self.assertRaisesRegex(AssertionError, "重复外部副作用"):
            self.knowledge.attach_document(self.request)

    def test_explicit_duplicate_permission_is_visible_in_test_setup(self) -> None:
        self.knowledge.expect_attach_document(self.applied, request=self.request)
        self.knowledge.expect_attach_document(
            self.applied,
            request=self.request,
            allow_duplicate=True,
        )

        self.knowledge.attach_document(self.request)
        self.knowledge.attach_document(self.request)
        self.knowledge.assert_expectations_consumed()

    def test_wrong_step_for_external_method_fails_before_consuming_side_effect(self) -> None:
        wrong_request = ReassignmentDocumentMutationRequest(
            operation_id="operation-1",
            step_name=ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
            workspace=ReassignmentWorkspaceReference("source-workspace"),
            document=ReassignmentDocumentReference.from_snapshot(self.snapshot),
            architecture_raw=11,
            idempotency_key="wrong-key",
        )
        self.knowledge.expect_attach_document(self.applied, request=wrong_request)

        with self.assertRaisesRegex(AssertionError, "错误的 Saga Step"):
            self.knowledge.attach_document(wrong_request)
        with self.assertRaisesRegex(AssertionError, "未消费"):
            self.knowledge.assert_expectations_consumed()

    def test_unknown_workspace_preparation_cannot_be_replayed_without_explicit_permission(self) -> None:
        """workspace 创建结果未知时可能已经写入，严格 Fake 必须阻止第二次盲目创建。"""

        request = ReassignmentWorkspacePreparationRequest(
            operation_id="operation-1",
            target_architecture_raw=12,
            desired_workspace_name="architectureId-12",
            idempotency_key="prepare-key",
        )
        unknown = ReassignmentWorkspacePreparationResult(
            ReassignmentKnowledgeOutcome.OUTCOME_UNKNOWN,
            error_code="timeout",
        )
        self.knowledge.expect_prepare_target_workspace(unknown, request=request)
        self.knowledge.expect_prepare_target_workspace(unknown, request=request)

        self.assertEqual(unknown, self.knowledge.prepare_target_workspace(request))
        with self.assertRaisesRegex(AssertionError, "重复外部副作用"):
            self.knowledge.prepare_target_workspace(request)

    def test_workspace_probe_is_repeatable_read_not_external_mutation(self) -> None:
        """结果未知后允许重复纯查询，但仍禁止重复执行可能创建 workspace 的 prepare。"""

        request = ReassignmentWorkspacePreparationRequest(
            operation_id="operation-1",
            target_architecture_raw=12,
            desired_workspace_name="architectureId-12",
            idempotency_key="prepare-key",
        )
        result = ReassignmentWorkspaceProbeResult(
            state=ReassignmentWorkspaceProbeState.PRESENT,
            workspace=ReassignmentWorkspaceReference("target-workspace"),
            ownership=ReassignmentWorkspaceOwnership.UNKNOWN,
        )
        self.knowledge.expect_probe_target_workspace(result, request=request)
        self.knowledge.expect_probe_target_workspace(result, request=request)

        self.assertEqual(result, self.knowledge.probe_target_workspace(request))
        self.assertEqual(result, self.knowledge.probe_target_workspace(request))
        self.knowledge.assert_expectations_consumed()

    def test_workspace_reference_probe_is_repeatable_and_slug_scoped(self) -> None:
        """既有 mapping 查回是纯读，并且严格校验不透明 slug 请求。"""

        request = ReassignmentWorkspaceReferenceProbeRequest(
            operation_id="operation-1",
            workspace=ReassignmentWorkspaceReference("legacy-target-slug"),
        )
        result = ReassignmentWorkspaceProbeResult(
            state=ReassignmentWorkspaceProbeState.PRESENT,
            workspace=ReassignmentWorkspaceReference("LEGACY-TARGET-SLUG"),
            ownership=ReassignmentWorkspaceOwnership.UNKNOWN,
        )
        self.knowledge.expect_probe_workspace_reference(result, request=request)
        self.knowledge.expect_probe_workspace_reference(result, request=request)

        self.assertEqual(result, self.knowledge.probe_workspace_reference(request))
        self.assertEqual(result, self.knowledge.probe_workspace_reference(request))
        self.knowledge.assert_expectations_consumed()


if __name__ == "__main__":
    unittest.main()
