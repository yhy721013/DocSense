"""阶段 1E-2：分类节点变更端口 DTO、Protocol 与组合边界。"""

from __future__ import annotations

import gc
import tempfile
import unittest
from pathlib import Path

from app.modules.reassign.adapters import SQLiteReassignmentRepository
from app.modules.reassign.composition import ReassignmentPortBundle
from app.modules.reassign.domain import (
    ReassignmentDocumentSnapshot,
    ReassignmentMutationOutcome,
    ReassignmentStepName,
    ReassignmentStepState,
)
from app.modules.reassign.ports import (
    ReassignmentDocumentMutationRequest,
    ReassignmentDocumentReference,
    ReassignmentDocumentMutationResult,
    ReassignmentKnowledgeOutcome,
    ReassignmentKnowledgePort,
    ReassignmentKnowledgePortFactory,
    ReassignmentMembershipProbeResult,
    ReassignmentMembershipState,
    ReassignmentRepositoryPort,
    ReassignmentStepCompletion,
    ReassignmentWorkspacePreparationResult,
    ReassignmentWorkspaceOwnership,
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
)


class ReassignmentPortDtoTests(unittest.TestCase):
    """端口 DTO 必须在进入 Adapter 前拒绝模糊成功、空引用和错误步骤。"""

    def setUp(self) -> None:
        self.snapshot = ReassignmentDocumentSnapshot(
            document_row_id=1,
            file_name="document.pdf",
            source_architecture_id=11,
            anything_doc_id="doc-1",
            doc_path="/documents/document.pdf",
            original_file_name="原始文件.pdf",
        )

    def test_document_reference_requires_exact_nonempty_doc_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "doc_path"):
            ReassignmentDocumentReference(
                document_row_id=1,
                file_name="document.pdf",
                doc_path="",
            )
        self.assertEqual(
            "/documents/document.pdf",
            ReassignmentDocumentReference.from_snapshot(self.snapshot).doc_path,
        )

    def test_workspace_preparation_cannot_report_success_without_slug_or_creation_fact(self) -> None:
        with self.assertRaisesRegex(ValueError, "workspace"):
            ReassignmentWorkspacePreparationResult(
                ReassignmentKnowledgeOutcome.APPLIED
            )
        with self.assertRaisesRegex(ValueError, "失败或未知"):
            ReassignmentWorkspacePreparationResult(
                ReassignmentKnowledgeOutcome.OUTCOME_UNKNOWN,
                workspace=ReassignmentWorkspaceReference("should-not-leak"),
                ownership=ReassignmentWorkspaceOwnership.PREEXISTING,
            )
        result = ReassignmentWorkspacePreparationResult(
            ReassignmentKnowledgeOutcome.ALREADY_IN_DESIRED_STATE,
            workspace=ReassignmentWorkspaceReference("existing-workspace"),
            ownership=ReassignmentWorkspaceOwnership.PREEXISTING,
        )
        self.assertIs(
            ReassignmentWorkspaceOwnership.PREEXISTING,
            result.ownership,
        )

    def test_workspace_reference_probe_request_requires_typed_slug_reference(
        self,
    ) -> None:
        request = ReassignmentWorkspaceReferenceProbeRequest(
            operation_id="operation-1",
            workspace=ReassignmentWorkspaceReference("legacy-target-slug"),
        )
        self.assertEqual("legacy-target-slug", request.workspace.slug)
        with self.assertRaisesRegex(TypeError, "workspace"):
            ReassignmentWorkspaceReferenceProbeRequest(
                operation_id="operation-1",
                workspace="legacy-target-slug",
            )

    def test_mutation_request_keeps_raw_architecture_and_fixed_step(self) -> None:
        request = ReassignmentDocumentMutationRequest(
            operation_id="operation-1",
            step_name=ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
            workspace=ReassignmentWorkspaceReference("target-workspace"),
            document=ReassignmentDocumentReference.from_snapshot(self.snapshot),
            architecture_raw="12",
            idempotency_key="attach-key",
        )
        self.assertEqual('"12"', request.architecture_raw.canonical_json())
        self.assertEqual(
            ReassignmentKnowledgeOutcome.KNOWN_FAILURE,
            ReassignmentDocumentMutationResult(
                ReassignmentKnowledgeOutcome.KNOWN_FAILURE,
                error_code="remote_false",
            ).outcome,
        )

    def test_step_completion_rejects_nonterminal_state(self) -> None:
        fake_repository = FakeReassignmentRepository(documents=(self.snapshot,))
        # 构造无效状态时不依赖 lease 的真实存在；DTO 本身就应阻止调用方表达错误顺序。
        from app.modules.reassign.ports import ReassignmentLease

        lease = ReassignmentLease(
            operation_id="operation-1",
            owner="owner",
            token="token",
            fencing_token=1,
            expires_at="2026-07-24T13:00:00.000000Z",
        )
        self.assertIsInstance(fake_repository, ReassignmentRepositoryPort)
        with self.assertRaisesRegex(ValueError, "已确认终态"):
            ReassignmentStepCompletion(
                lease=lease,
                step_name=ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
                next_state=ReassignmentStepState.MUTATION_STARTED,
            )
        with self.assertRaisesRegex(TypeError, "ReassignmentStepState"):
            ReassignmentStepCompletion(
                lease=lease,
                step_name=ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
                next_state="succeeded",  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "互相矛盾"):
            ReassignmentStepCompletion(
                lease=lease,
                step_name=ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
                next_state=ReassignmentStepState.SUCCEEDED,
                probe_outcome=ReassignmentMutationOutcome.OUTCOME_UNKNOWN,
            )

    def test_membership_probe_requires_explicit_enum(self) -> None:
        self.assertEqual(
            ReassignmentMembershipState.OUTCOME_UNKNOWN,
            ReassignmentMembershipProbeResult(
                ReassignmentMembershipState.OUTCOME_UNKNOWN,
                error_code="probe_timeout",
            ).state,
        )
        with self.assertRaisesRegex(TypeError, "state"):
            ReassignmentMembershipProbeResult("present")  # type: ignore[arg-type]

    def test_workspace_probe_can_express_unknown_creation_ownership(self) -> None:
        """创建超时后的纯查回应保留“存在但归属未知”，禁止误删共享 workspace。"""

        result = ReassignmentWorkspaceProbeResult(
            state=ReassignmentWorkspaceProbeState.PRESENT,
            workspace=ReassignmentWorkspaceReference("target-workspace"),
            ownership=ReassignmentWorkspaceOwnership.UNKNOWN,
        )
        self.assertIs(ReassignmentWorkspaceOwnership.UNKNOWN, result.ownership)
        with self.assertRaisesRegex(ValueError, "不能携带"):
            ReassignmentWorkspaceProbeResult(
                state=ReassignmentWorkspaceProbeState.ABSENT,
                workspace=ReassignmentWorkspaceReference("unexpected"),
            )


class ReassignmentPortProtocolTests(unittest.TestCase):
    """真实 SQLite Adapter 与严格 Fake 都必须符合相同的可运行时检查端口。"""

    def test_sqlite_adapter_and_strict_fakes_implement_ports_and_bundle_keeps_identity(self) -> None:
        snapshot = ReassignmentDocumentSnapshot(
            document_row_id=1,
            file_name="document.pdf",
            source_architecture_id=11,
            doc_path="/documents/document.pdf",
        )
        fake_repository = FakeReassignmentRepository(documents=(snapshot,))
        fake_knowledge = FakeReassignmentKnowledgePort()
        fake_knowledge_factory = FakeReassignmentKnowledgePortFactory(
            builder=lambda: fake_knowledge
        )
        self.assertIsInstance(fake_repository, ReassignmentRepositoryPort)
        self.assertIsInstance(fake_knowledge, ReassignmentKnowledgePort)
        self.assertIsInstance(
            fake_knowledge_factory,
            ReassignmentKnowledgePortFactory,
        )
        bundle = ReassignmentPortBundle(
            repository=fake_repository,
            knowledge_factory=fake_knowledge_factory,
        )
        self.assertIs(bundle.repository, fake_repository)
        self.assertIs(bundle.knowledge_factory, fake_knowledge_factory)
        self.assertIs(bundle.knowledge_factory.create(), fake_knowledge)

        with tempfile.TemporaryDirectory(prefix="docsense-reassign-port-") as temp_dir:
            db_path = Path(temp_dir) / "knowledge.sqlite3"
            DatabaseService(str(db_path))
            sqlite_repository = SQLiteReassignmentRepository(db_path)
            # 既有 DatabaseService 的 sqlite3 Cursor/Connection 引用环在 Windows
            # 上可能延迟到 GC 才释放；适配器自身不持有连接，这里确保临时库可删除。
            gc.collect()
        self.assertIsInstance(sqlite_repository, ReassignmentRepositoryPort)


if __name__ == "__main__":
    unittest.main()
