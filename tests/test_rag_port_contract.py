"""阶段 4 供应商无关 Port、DTO 与内存 Fake 的离线契约测试。

测试只验证应用服务层可观察的业务语义，不访问文件系统中的业务文件、不发送网络请求，
也不实例化任何具体集成客户端。这样后续替换适配器实现时，业务层仍可依赖同一组契约。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import cast
import unittest

import app.ports as port_module
from app.ports import (
    CollectionRef,
    DocumentRagFactory,
    DocumentRagPort,
    DocumentRagSession,
    IndexedDocument,
    KnowledgeIndexPort,
    PreparedDocumentRef,
    RagOperationError,
    RagSource,
    validate_rag_query_max_attempts,
)
from tests.fakes import (
    FakeDocumentRagFactory,
    FakeDocumentRagPort,
    FakeKnowledgeIndexPort,
    FakeRagOutcome,
)


class RagDtoContractTests(unittest.TestCase):
    """验证 RAG DTO 的不可变性和最小数据约束。"""

    def test_source_and_trace_snapshots_are_immutable(self) -> None:
        """调用完成后修改 Fake 不得反向改变已返回的来源和轨迹快照。"""
        source = RagSource(document_ref="document:target", text="证据片段")
        port = FakeDocumentRagPort(
            analyse_outcomes=[FakeRagOutcome(text="分析完成", sources=[source])],
            ask_outcomes=[FakeRagOutcome(text="追问完成", sources=[source])],
        )
        session = port.open_isolated_session(
            context_name="file-task-1",
            conversation_name="analysis",
        )

        analysis_result = session.analyse("sample.pdf", "分析文件")
        session.ask("继续检查")

        self.assertEqual(1, len(analysis_result.trace.attempts))
        self.assertEqual(2, len(session.trace.attempts))
        with self.assertRaises(FrozenInstanceError):
            setattr(source, "document_ref", "document:changed")

    def test_empty_success_text_is_rejected_and_recorded(self) -> None:
        """空文本不能伪装为成功结果，失败原因必须进入可审计轨迹。"""
        port = FakeDocumentRagPort(
            analyse_outcomes=[FakeRagOutcome(text="", sources=())],
        )
        session = port.open_isolated_session(
            context_name="file-task-2",
            conversation_name="analysis",
        )

        with self.assertRaises(RagOperationError) as raised:
            session.analyse("sample.pdf", "分析文件", max_attempts=1)

        self.assertEqual("response", raised.exception.trace.failure_stage)
        self.assertEqual("response", raised.exception.trace.attempts[0].failure_stage)

    def test_source_requires_stable_document_reference(self) -> None:
        """来源缺少文档身份时必须立即失败，不能延迟到业务层做猜测匹配。"""
        with self.assertRaises(ValueError):
            RagSource(document_ref="", text="无法归属的证据")

    def test_query_attempt_limit_rejects_boolean_and_float(self) -> None:
        """供应商无关查询策略不得把布尔值或浮点数解释为模型调用次数。"""
        for invalid_value in (True, 1.0):
            with self.subTest(value=invalid_value):
                with self.assertRaises(ValueError):
                    validate_rag_query_max_attempts(  # type: ignore[arg-type]
                        invalid_value
                    )


class DocumentRagPortContractTests(unittest.TestCase):
    """验证隔离 RAG 会话的调用顺序、重试轨迹和清理语义。"""

    def test_fake_implements_runtime_checkable_protocols(self) -> None:
        """Fake 必须能够直接替换业务服务依赖的两个 Protocol。"""
        port = FakeDocumentRagPort()
        session = port.open_isolated_session(
            context_name="file-task-3",
            conversation_name="analysis",
        )

        self.assertIsInstance(port, DocumentRagPort)
        self.assertIsInstance(session, DocumentRagSession)

    def test_factory_creates_independent_task_scopes(self) -> None:
        """每次进入 Factory 租约都必须产生独立 Port，并在退出时归零活动计数。"""
        factory = FakeDocumentRagFactory()

        with factory.create() as first_port:
            self.assertIsInstance(factory, DocumentRagFactory)
            self.assertEqual(1, factory.active_leases)
        with factory.create() as second_port:
            self.assertEqual(1, factory.active_leases)
            self.assertIsNot(first_port, second_port)

        self.assertEqual(0, factory.active_leases)
        self.assertEqual(2, len(factory.ports))

    def test_analyse_retry_and_follow_up_are_recorded_in_order(self) -> None:
        """首次失败、重试成功和后续追问必须按真实发生顺序保留。"""
        source = RagSource(document_ref="document:target", text="目标证据")
        port = FakeDocumentRagPort(
            analyse_outcomes=[
                FakeRagOutcome(text="无来源回答", sources=()),
                FakeRagOutcome(text="有效分析", sources=(source,)),
            ],
            ask_outcomes=[FakeRagOutcome(text="有效追问", sources=(source,))],
        )
        session = port.open_isolated_session(
            context_name="file-task-4",
            conversation_name="analysis",
        )

        analysis_result = session.analyse("sample.pdf", "分析文件", max_attempts=2)
        follow_up_result = session.ask("修复字段")

        self.assertEqual("有效分析", analysis_result.text)
        self.assertEqual("有效追问", follow_up_result.text)
        self.assertEqual(
            ["analyse", "analyse", "ask"],
            [attempt.operation for attempt in session.trace.attempts],
        )
        self.assertEqual(
            [1, 2, 1],
            [attempt.attempt for attempt in session.trace.attempts],
        )
        self.assertEqual("sources", session.trace.attempts[0].failure_stage)
        self.assertIsNone(session.trace.attempts[1].failure_stage)

    def test_analyse_can_only_be_called_once(self) -> None:
        """重复 analyse 会隐式重复文档准备，因此必须在端口边界被拒绝。"""
        session = FakeDocumentRagPort().open_isolated_session(
            context_name="file-task-5",
            conversation_name="analysis",
        )
        session.analyse("sample.pdf", "分析文件")

        with self.assertRaises(RagOperationError) as raised:
            session.analyse("sample.pdf", "再次分析")

        self.assertEqual("analyse_repeated", raised.exception.trace.failure_stage)

    def test_ask_requires_successful_analysis(self) -> None:
        """会话尚未完成文档准备时，不允许执行不带上传动作的后续查询。"""
        session = FakeDocumentRagPort().open_isolated_session(
            context_name="file-task-6",
            conversation_name="analysis",
        )

        with self.assertRaises(RagOperationError) as raised:
            session.ask("过早追问")

        self.assertEqual("session_not_prepared", raised.exception.trace.failure_stage)

    def test_close_is_idempotent_and_blocks_later_calls(self) -> None:
        """重复关闭不得重放清理，关闭后的任何模型调用都必须失败。"""
        session = FakeDocumentRagPort().open_isolated_session(
            context_name="file-task-7",
            conversation_name="analysis",
        )

        first = session.close(retain_document=True)
        second = session.close(retain_document=False)

        self.assertTrue(first.success)
        self.assertFalse(first.already_closed)
        self.assertTrue(second.success)
        self.assertTrue(second.already_closed)
        self.assertTrue(session.retain_document_on_close)
        with self.assertRaises(RagOperationError) as raised:
            session.analyse("sample.pdf", "分析文件")
        self.assertEqual("session_closed", raised.exception.trace.failure_stage)

    def test_cleanup_failure_is_stable_across_repeated_close(self) -> None:
        """首次清理失败后仍不盲目重放删除，并稳定返回原始错误。"""
        session = FakeDocumentRagPort(cleanup_error_message="删除隔离资源失败").open_isolated_session(
            context_name="file-task-8",
            conversation_name="analysis",
        )

        first = session.close(retain_document=False)
        second = session.close(retain_document=True)

        self.assertFalse(first.success)
        self.assertFalse(first.already_closed)
        self.assertFalse(second.success)
        self.assertTrue(second.already_closed)
        self.assertEqual("删除隔离资源失败", second.error_message)
        self.assertFalse(session.retain_document_on_close)

    def test_sources_can_be_optional_for_explicit_non_rag_query(self) -> None:
        """调用方显式关闭来源要求时，无来源但有文本的回答可以成功。"""
        port = FakeDocumentRagPort(
            analyse_outcomes=[FakeRagOutcome(text="无来源但有效", sources=())],
        )
        session = port.open_isolated_session(
            context_name="file-task-9",
            conversation_name="analysis",
        )

        result = session.analyse(
            "sample.pdf",
            "执行非检索检查",
            require_sources=False,
            max_attempts=1,
        )

        self.assertEqual("无来源但有效", result.text)
        self.assertEqual((), result.sources)


class RagOpeningRollbackContractTests(unittest.TestCase):
    """验证 Session 尚未返回时，端口内部承担部分创建回滚责任。"""

    def test_context_creation_failure_exposes_no_external_reference(self) -> None:
        """第一个资源即创建失败时，轨迹不得伪造任何已创建引用。"""
        port = FakeDocumentRagPort(open_failure_stage="context_create")

        with self.assertRaises(RagOperationError) as raised:
            port.open_isolated_session(
                context_name="file-task-10",
                conversation_name="analysis",
            )

        trace = raised.exception.trace
        self.assertIsNone(trace.context_ref)
        self.assertIsNone(trace.conversation_ref)
        self.assertEqual("context_create", trace.failure_stage)
        self.assertEqual(0, len(port.sessions))

    def test_conversation_failure_rolls_back_created_context(self) -> None:
        """第二个资源创建失败时，端口必须自行回滚并记录成功的清理尝试。"""
        port = FakeDocumentRagPort(open_failure_stage="conversation_create")

        with self.assertRaises(RagOperationError) as raised:
            port.open_isolated_session(
                context_name="file-task-11",
                conversation_name="analysis",
            )

        trace = raised.exception.trace
        self.assertIsNotNone(trace.context_ref)
        self.assertIsNone(trace.conversation_ref)
        self.assertEqual(
            ["context_create", "conversation_create", "context_rollback"],
            [event.operation for event in trace.lifecycle_events],
        )
        self.assertEqual((), trace.attempts)
        self.assertIsNone(trace.lifecycle_events[-1].failure_stage)
        self.assertEqual(0, len(port.sessions))

    def test_rollback_failure_preserves_reference_and_cleanup_error(self) -> None:
        """回滚自身失败时，异常必须同时保留残留资源引用和清理错误。"""
        port = FakeDocumentRagPort(
            open_failure_stage="conversation_create",
            rollback_error_message="上下文删除失败",
        )

        with self.assertRaises(RagOperationError) as raised:
            port.open_isolated_session(
                context_name="file-task-12",
                conversation_name="analysis",
            )

        trace = raised.exception.trace
        self.assertIsNotNone(trace.context_ref)
        self.assertEqual("cleanup", trace.lifecycle_events[-1].failure_stage)
        self.assertEqual(
            "上下文删除失败",
            trace.lifecycle_events[-1].error_message,
        )
        self.assertIn("回滚失败", trace.error_message or "")


class KnowledgeIndexPortContractTests(unittest.TestCase):
    """验证长期知识库 Port 的集合、幂等、删除和并发语义。"""

    def test_fake_implements_runtime_checkable_protocol(self) -> None:
        """知识库 Fake 必须可直接注入只依赖 Protocol 的业务服务。"""
        self.assertIsInstance(FakeKnowledgeIndexPort(), KnowledgeIndexPort)

    def test_ensure_collection_is_idempotent(self) -> None:
        """相同集合名称必须返回同一个稳定引用。"""
        port = FakeKnowledgeIndexPort()

        first = port.ensure_collection("architecture-1")
        second = port.ensure_collection("architecture-1")

        self.assertEqual(first, second)

    def test_store_reconcile_and_remove_preserve_idempotency(self) -> None:
        """保存重试复用原文档，删除可重复执行，对账反映当前真实状态。"""
        port = FakeKnowledgeIndexPort()
        collection = port.ensure_collection("architecture-2")

        created = port.store_document(
            collection,
            "sample.pdf",
            {"file_name": "sample.pdf"},
            idempotency_key="sha256:abc",
        )
        reused = port.store_document(
            collection,
            "another-path.pdf",
            {"file_name": "changed.pdf"},
            idempotency_key="sha256:abc",
        )
        reconciled = port.reconcile_document(
            collection,
            idempotency_key="sha256:abc",
        )

        self.assertTrue(created.created)
        self.assertFalse(created.reused)
        self.assertFalse(reused.created)
        self.assertTrue(reused.reused)
        self.assertEqual(created.document_ref, reused.document_ref)
        self.assertEqual(created.external_location, reused.external_location)
        self.assertIsNotNone(reconciled)
        self.assertTrue(cast(IndexedDocument, reconciled).reused)

        first_removal = port.remove_document(collection, created.external_location)
        second_removal = port.remove_document(collection, created.external_location)

        self.assertTrue(first_removal.success)
        self.assertFalse(first_removal.already_applied)
        self.assertTrue(second_removal.success)
        self.assertTrue(second_removal.already_applied)
        self.assertIsNone(
            port.reconcile_document(collection, idempotency_key="sha256:abc")
        )

    def test_store_prepared_document_preserves_rag_document_identity(self) -> None:
        """长期知识库登记必须复用 RAG 已上传文档，不得生成第二个外部位置。"""
        port = FakeKnowledgeIndexPort()
        collection = port.ensure_collection("architecture-prepared")
        prepared = PreparedDocumentRef(
            document_ref="document:prepared",
            external_location="external:prepared",
        )

        stored = port.store_prepared_document(
            collection,
            prepared,
            {"file_name": "sample.pdf"},
            idempotency_key="sha256:prepared",
        )

        self.assertEqual(prepared.document_ref, stored.document_ref)
        self.assertEqual(prepared.external_location, stored.external_location)
        self.assertTrue(stored.created)

    def test_same_idempotency_key_is_scoped_to_collection(self) -> None:
        """不同业务集合可以安全使用相同幂等键而不会错误复用文档。"""
        port = FakeKnowledgeIndexPort()
        first_collection = port.ensure_collection("architecture-3")
        second_collection = port.ensure_collection("architecture-4")

        first = port.store_document(
            first_collection,
            "sample.pdf",
            {},
            idempotency_key="shared-key",
        )
        second = port.store_document(
            second_collection,
            "sample.pdf",
            {},
            idempotency_key="shared-key",
        )

        self.assertNotEqual(first.document_ref, second.document_ref)
        self.assertTrue(first.created)
        self.assertTrue(second.created)

    def test_forged_collection_reference_is_rejected(self) -> None:
        """结构相似但不属于当前端口实例的集合引用不得用于索引操作。"""
        port = FakeKnowledgeIndexPort()
        forged = CollectionRef(ref="collection:999", name="forged")

        with self.assertRaises(ValueError):
            port.store_document(
                forged,
                "sample.pdf",
                {},
                idempotency_key="key",
            )

    def test_concurrent_same_key_creates_only_one_document(self) -> None:
        """并发提交相同幂等键时只能产生一个首次创建结果和一个文档身份。"""
        port = FakeKnowledgeIndexPort()
        collection = port.ensure_collection("architecture-5")

        def store_once(index: int) -> IndexedDocument:
            """从工作线程提交同一逻辑文档，并返回稳定结果用于聚合断言。"""
            return port.store_document(
                collection,
                f"sample-{index}.pdf",
                {"worker": index},
                idempotency_key="concurrent-key",
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(store_once, range(24)))

        self.assertEqual(1, sum(result.created for result in results))
        self.assertEqual(23, sum(result.reused for result in results))
        self.assertEqual(1, len({result.document_ref for result in results}))
        self.assertEqual(1, len({result.external_location for result in results}))


class PortBoundaryTests(unittest.TestCase):
    """防止供应商协议细节重新泄漏到应用服务端口。"""

    def test_production_port_package_does_not_export_test_fakes(self) -> None:
        """生产抽象包不得反向依赖或导出测试目录中的具体替身。"""
        self.assertFalse(hasattr(port_module, "FakeDocumentRagPort"))
        self.assertFalse(hasattr(port_module, "FakeDocumentRagFactory"))
        self.assertFalse(hasattr(port_module, "FakeKnowledgeIndexPort"))

    def test_port_source_does_not_contain_supplier_protocol_terms(self) -> None:
        """端口源码只能表达业务概念，不得出现具体客户端、字段或请求协议词。"""
        project_root = Path(__file__).resolve().parents[1]
        port_directory = project_root / "app" / "ports"
        forbidden_terms = (
            "AnythingLLM",
            "workspace_slug",
            "thread_slug",
            "docpath",
            "custom-documents",
            "requests",
            "httpx",
            "Authorization",
            "api_key",
            "/workspace/",
            "tests.",
        )

        for source_file in port_directory.glob("*.py"):
            source = source_file.read_text(encoding="utf-8")
            for term in forbidden_terms:
                with self.subTest(file=source_file.name, term=term):
                    self.assertNotIn(term, source)


if __name__ == "__main__":
    unittest.main()
