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
    CollectionSpec,
    DocumentRagFactory,
    DocumentRagPort,
    DocumentRagSession,
    IndexedDocument,
    KnowledgeDocumentMetadata,
    KnowledgeIndexPort,
    KnowledgeOperationContext,
    OperationResult,
    PreparedDocumentRef,
    RagAttempt,
    RagOperationError,
    RagPromptKind,
    RagSource,
    build_document_idempotency_key,
    normalize_rag_prompt,
    validate_rag_query_max_attempts,
)
from tests.fakes import (
    FakeDocumentRagFactory,
    FakeDocumentRagPort,
    FakeKnowledgeIndexFactory,
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

    def test_prompt_normalization_unifies_line_endings_and_boundary_whitespace(self) -> None:
        """Prompt 只统一跨平台表示，正文内部的换行和缩进必须保持不变。"""
        raw_prompt = "\r\n  第一行\r\n    第二行  \r第三行\t\r\n"

        normalized = normalize_rag_prompt(raw_prompt)

        self.assertEqual("第一行\n    第二行  \n第三行", normalized)
        with self.assertRaises(ValueError):
            normalize_rag_prompt(" \r\n\t ")
        with self.assertRaises(TypeError):
            normalize_rag_prompt(123)  # type: ignore[arg-type]


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
        follow_up_result = session.ask(
            "修复字段",
            prompt_kind=RagPromptKind.JSON_REPAIR,
        )

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
        self.assertEqual(
            ["analysis", "analysis", "json_repair"],
            [attempt.prompt_kind for attempt in session.trace.attempts],
        )
        self.assertTrue(all(attempt.query_mode == "query" for attempt in session.trace.attempts))
        self.assertTrue(all(len(attempt.prompt_digest) == 64 for attempt in session.trace.attempts))

    def test_invalid_prompt_kind_is_rejected_before_fake_call_is_consumed(self) -> None:
        """非法用途分类不得消费一次预设模型结果，防止生产实现产生先调用后失败。"""
        source = RagSource(document_ref="document:target", text="目标证据")
        port = FakeDocumentRagPort(
            ask_outcomes=[FakeRagOutcome(text="修复完成", sources=(source,))],
        )
        session = port.open_isolated_session(
            context_name="file-task-prompt-kind",
            conversation_name="analysis",
        )
        session.analyse("sample.pdf", "分析文件")

        with self.assertRaises(TypeError):
            session.ask("修复字段", prompt_kind="json_repair")  # type: ignore[arg-type]

        result = session.ask(
            "修复字段",
            prompt_kind=RagPromptKind.JSON_REPAIR,
        )
        self.assertEqual("修复完成", result.text)

    def test_analyse_records_explicit_two_stage_prompt_kinds(self) -> None:
        """首次查询必须能区分领域分类和字段抽取，并把用途写入轨迹。"""
        for prompt_kind in (
            RagPromptKind.ARCHITECTURE_CLASSIFICATION,
            RagPromptKind.ANALYSIS_EXTRACTION,
        ):
            with self.subTest(prompt_kind=prompt_kind):
                session = FakeDocumentRagPort().open_isolated_session(
                    context_name=f"file-task-{prompt_kind.value}",
                    conversation_name="analysis",
                )

                session.analyse(
                    "sample.pdf",
                    "执行阶段查询",
                    prompt_kind=prompt_kind,
                )

                self.assertEqual(
                    prompt_kind.value,
                    session.trace.attempts[0].prompt_kind,
                )

    def test_fake_records_architecture_reselect_as_dedicated_prompt_kind(self) -> None:
        """分支冲突重选必须与契约修复分开审计，不能复用通用 follow_up。"""
        session = FakeDocumentRagPort().open_isolated_session(
            context_name="file-task-architecture-reselect",
            conversation_name="analysis",
        )
        session.analyse(
            "sample.pdf",
            "领域分类",
            prompt_kind=RagPromptKind.ARCHITECTURE_CLASSIFICATION,
        )

        session.ask(
            "在已确认装备分支内受限重选",
            prompt_kind=RagPromptKind.ARCHITECTURE_RESELECT,
        )

        self.assertEqual(
            ["architecture_classification", "architecture_reselect"],
            [attempt.prompt_kind for attempt in session.trace.attempts],
        )

    def test_invalid_analyse_prompt_kind_does_not_start_fake_session(self) -> None:
        """非法首次用途不得消费结果或把 Session 错误标记为已经开始。"""
        session = FakeDocumentRagPort().open_isolated_session(
            context_name="file-task-invalid-analyse-kind",
            conversation_name="analysis",
        )

        with self.assertRaises(TypeError):
            session.analyse(
                "sample.pdf",
                "执行分类",
                prompt_kind="architecture_classification",  # type: ignore[arg-type]
            )

        result = session.analyse(
            "sample.pdf",
            "执行分类",
            prompt_kind=RagPromptKind.ARCHITECTURE_CLASSIFICATION,
        )
        self.assertEqual("模拟结果", result.text)
        self.assertEqual(1, len(session.trace.attempts))

    def test_fake_records_classification_then_extraction_sequence(self) -> None:
        """两阶段流程复用文档，但必须在不同对话中形成稳定轨迹。"""
        session = FakeDocumentRagPort().open_isolated_session(
            context_name="file-task-two-stage",
            conversation_name="analysis",
        )

        session.analyse(
            "sample.pdf",
            "领域分类",
            prompt_kind=RagPromptKind.ARCHITECTURE_CLASSIFICATION,
        )
        session.start_fresh_conversation(
            conversation_name="analysis-extraction",
        )
        session.ask(
            "字段抽取",
            prompt_kind=RagPromptKind.ANALYSIS_EXTRACTION,
        )

        self.assertEqual(
            ["architecture_classification", "analysis_extraction"],
            [attempt.prompt_kind for attempt in session.trace.attempts],
        )
        self.assertEqual(
            ["conversation:1", "conversation:1:fresh"],
            list(session.attempt_conversation_refs),
        )
        conversation_events = [
            event
            for event in session.trace.lifecycle_events
            if event.operation == "conversation_create" and event.success
        ]
        self.assertEqual([1, 2], [event.attempt for event in conversation_events])
        self.assertEqual(
            2,
            len({event.external_ref for event in conversation_events}),
        )

    def test_fresh_conversation_requires_prepared_session_and_allows_two_switches(self) -> None:
        """切换门禁应允许两个隔离阶段，并在第三次调用前拒绝外部副作用。"""
        session = FakeDocumentRagPort().open_isolated_session(
            context_name="file-task-fresh-gates",
            conversation_name="analysis",
        )

        with self.assertRaises(RagOperationError) as before_analyse:
            session.start_fresh_conversation(conversation_name="extraction")
        self.assertEqual(
            "session_not_prepared",
            before_analyse.exception.trace.failure_stage,
        )
        self.assertEqual(
            1,
            len(
                [
                    event
                    for event in session.trace.lifecycle_events
                    if event.operation == "conversation_create"
                ]
            ),
        )

        session.analyse("sample.pdf", "领域分类")
        self.assertTrue(
            session.start_fresh_conversation(conversation_name="reselect")
        )
        self.assertTrue(
            session.start_fresh_conversation(conversation_name="extraction")
        )
        with self.assertRaises(RagOperationError) as repeated:
            session.start_fresh_conversation(conversation_name="third")
        self.assertEqual(
            "conversation_switch_repeated",
            repeated.exception.trace.failure_stage,
        )
        self.assertEqual(
            3,
            len(
                [
                    event
                    for event in session.trace.lifecycle_events
                    if event.operation == "conversation_create"
                ]
            ),
        )

        session.close(retain_document=False)
        with self.assertRaises(RagOperationError) as after_close:
            session.start_fresh_conversation(conversation_name="closed")
        self.assertEqual("session_closed", after_close.exception.trace.failure_stage)

    def test_second_fresh_conversation_failure_is_fatal_without_fallback(self) -> None:
        """第二次线程创建失败后必须阻断 ask，不能退回第一次切换后的线程。"""
        session = FakeDocumentRagPort(
            fresh_conversation_error_messages=(
                "",
                "创建字段抽取对话失败",
            ),
        ).open_isolated_session(
            context_name="file-task-second-fresh-failure",
            conversation_name="analysis",
        )
        session.analyse("sample.pdf", "领域分类")
        session.start_fresh_conversation(conversation_name="reselect")

        with self.assertRaises(RagOperationError) as raised:
            session.start_fresh_conversation(conversation_name="extraction")

        self.assertEqual("conversation_create", raised.exception.trace.failure_stage)
        with self.assertRaises(RagOperationError) as ask_raised:
            session.ask("不得退回重选线程抽取")
        self.assertEqual(
            "session_not_prepared",
            ask_raised.exception.trace.failure_stage,
        )
        self.assertEqual(1, len(session.attempt_conversation_refs))

    def test_optional_fresh_failure_consumes_attempt_but_allows_extraction_switch(
        self,
    ) -> None:
        """可选重选线程失败应 fail-open，且剩余一次切换仍可用于字段抽取。"""
        session = FakeDocumentRagPort(
            fresh_conversation_error_messages=(
                "创建可选重选对话失败",
                "",
            ),
        ).open_isolated_session(
            context_name="file-task-optional-fresh-failure",
            conversation_name="analysis",
        )
        session.analyse("sample.pdf", "领域分类")

        created = session.start_fresh_conversation(
            conversation_name="reselect",
            failure_is_fatal=False,
        )
        self.assertFalse(created)
        self.assertIsNone(session.trace.failure_stage)
        with self.assertRaises(RagOperationError) as repeated:
            session.start_fresh_conversation(
                conversation_name="reselect",
                failure_is_fatal=False,
            )
        self.assertEqual(
            "conversation_switch_repeated",
            repeated.exception.trace.failure_stage,
        )
        self.assertTrue(
            session.start_fresh_conversation(conversation_name="extraction")
        )
        result = session.ask("字段抽取")

        self.assertEqual("模拟结果", result.text)
        self.assertEqual(
            ["conversation:1", "conversation:1:fresh:2"],
            list(session.attempt_conversation_refs),
        )
        conversation_events = [
            event
            for event in session.trace.lifecycle_events
            if event.operation == "conversation_create"
        ]
        self.assertEqual(
            [True, False, True],
            [event.success for event in conversation_events],
        )

    def test_optional_ask_failure_preserves_attempt_and_allows_extraction(self) -> None:
        """可选重选查询失败应保留失败 attempt，但不得污染后续字段抽取。"""
        source = RagSource(document_ref="document:target", text="目标证据")
        session = FakeDocumentRagPort(
            ask_outcomes=(
                FakeRagOutcome(
                    text=None,
                    failure_stage="query",
                    error_message="可选重选查询失败",
                ),
                FakeRagOutcome(text="字段结果", sources=(source,)),
            ),
        ).open_isolated_session(
            context_name="file-task-optional-query-failure",
            conversation_name="analysis",
        )
        session.analyse("sample.pdf", "领域分类")
        session.start_fresh_conversation(conversation_name="reselect")

        optional_result = session.ask_optional(
            "受限重选",
            prompt_kind=RagPromptKind.ARCHITECTURE_RESELECT,
        )

        self.assertIsNone(optional_result)
        self.assertIsNone(session.trace.failure_stage)
        self.assertEqual("query", session.trace.attempts[-1].failure_stage)
        session.start_fresh_conversation(conversation_name="extraction")
        extraction_result = session.ask(
            "字段抽取",
            prompt_kind=RagPromptKind.ANALYSIS_EXTRACTION,
        )
        self.assertEqual("字段结果", extraction_result.text)
        self.assertEqual(
            [
                "conversation:1",
                "conversation:1:fresh",
                "conversation:1:fresh:2",
            ],
            list(session.attempt_conversation_refs),
        )

    def test_fresh_conversation_failure_does_not_fall_back_to_primary(self) -> None:
        """第二对话创建失败后不得允许 ask 在原对话继续抽取。"""
        session = FakeDocumentRagPort(
            fresh_conversation_error_message="创建阶段隔离对话失败",
        ).open_isolated_session(
            context_name="file-task-fresh-failure",
            conversation_name="analysis",
        )
        session.analyse("sample.pdf", "领域分类")

        with self.assertRaises(RagOperationError) as raised:
            session.start_fresh_conversation(conversation_name="extraction")
        self.assertEqual("conversation_create", raised.exception.trace.failure_stage)
        with self.assertRaises(RagOperationError) as ask_raised:
            session.ask("不得回退抽取")
        self.assertEqual(
            "session_not_prepared",
            ask_raised.exception.trace.failure_stage,
        )
        self.assertEqual(1, len(session.attempt_conversation_refs))
        cleanup = session.close(retain_document=True)
        self.assertTrue(cleanup.success)
        self.assertTrue(
            any(
                event.operation == "global_document_delete"
                for event in session.trace.lifecycle_events
            )
        )

    def test_attempt_rejects_source_status_that_conflicts_with_counts(self) -> None:
        """来源总数为零时不能伪造 matched 状态绕过审计判定。"""
        with self.assertRaisesRegex(ValueError, "来源验证统计不一致"):
            RagAttempt(
                operation="analyse",
                attempt=1,
                prompt_kind=RagPromptKind.ANALYSIS,
                raw_response="无来源回答",
                sources=(),
                failure_stage="sources",
                error_message="模型回答缺少来源",
                source_count=0,
                verified_source_count=0,
                source_marker_status="matched",
            )

    def test_success_attempt_can_explicitly_audit_empty_response(self) -> None:
        """空字符串是已返回的成功结果，不能与尚未产生响应的 None 混淆。"""

        attempt = RagAttempt(
            operation="analyse",
            attempt=1,
            prompt_kind=RagPromptKind.ANALYSIS,
            raw_response="",
            sources=(),
            failure_stage=None,
            error_message=None,
        )

        self.assertEqual("", attempt.raw_response)
        with self.assertRaisesRegex(ValueError, "明确包含 raw_response"):
            RagAttempt(
                operation="analyse",
                attempt=1,
                prompt_kind=RagPromptKind.ANALYSIS,
                raw_response=None,
                sources=(),
                failure_stage=None,
                error_message=None,
            )

    def test_report_generation_is_a_first_class_prompt_kind(self) -> None:
        self.assertEqual(
            "report_generation",
            RagPromptKind.REPORT_GENERATION.value,
        )

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

    @staticmethod
    def _operation_context(execution_id: str = "execution-001") -> KnowledgeOperationContext:
        """构造显式业务操作上下文，避免测试通过 metadata 偷渡身份。"""
        return KnowledgeOperationContext(
            execution_id=execution_id,
            business_type="file",
            business_key="sample.pdf",
        )

    @staticmethod
    def _metadata(**attributes: object) -> KnowledgeDocumentMetadata:
        """构造类型化永久文档元数据，避免测试依赖适配器私有字段。"""
        return KnowledgeDocumentMetadata(
            file_name="sample.pdf",
            original_name="sample.pdf",
            ingested_file_name="sample.pdf",
            attributes=attributes,
        )

    def test_fake_implements_runtime_checkable_protocol(self) -> None:
        """知识库 Fake 必须可直接注入只依赖 Protocol 的业务服务。"""
        self.assertIsInstance(FakeKnowledgeIndexPort(), KnowledgeIndexPort)

    def test_document_metadata_deeply_freezes_nested_json(self) -> None:
        """调用方不得通过嵌套字典或列表修改已建立的幂等快照。"""
        source = {"nested": {"values": [1, 2]}}
        metadata = KnowledgeDocumentMetadata(
            file_name="sample.pdf",
            original_name="sample.pdf",
            ingested_file_name="sample.pdf",
            attributes=source,
        )
        source["nested"]["values"].append(3)

        snapshot = metadata.attributes_dict()
        self.assertEqual({"nested": {"values": [1, 2]}}, snapshot)
        with self.assertRaises(TypeError):
            metadata.attributes["nested"]["new"] = True

    def test_document_metadata_preserves_business_original_name_raw_value(self) -> None:
        """业务原始名只能用于空值判断，不能被内部规范化逻辑改写。"""
        original_file_name = "  甲方原始资料.mhtml  "

        metadata = KnowledgeDocumentMetadata(
            file_name="e9a7f5.mhtml",
            original_name=original_file_name,
            ingested_file_name="e9a7f5.mhtml.normalized.pdf",
            attributes={},
        )

        self.assertEqual(metadata.original_name, original_file_name)

    def test_default_idempotency_key_changes_with_collection_or_content(self) -> None:
        """同名文件的新内容或不同存储 architecture 必须得到不同默认键。"""
        first = build_document_idempotency_key(
            file_name="hash.pdf",
            architecture_id=100,
            content_sha256="a" * 64,
        )
        changed_content = build_document_idempotency_key(
            file_name="hash.pdf",
            architecture_id=100,
            content_sha256="b" * 64,
        )
        changed_collection = build_document_idempotency_key(
            file_name="hash.pdf",
            architecture_id=101,
            content_sha256="a" * 64,
        )

        self.assertTrue(first.startswith("document:v1:"))
        self.assertNotEqual(first, changed_content)
        self.assertNotEqual(first, changed_collection)

    def test_ensure_collection_is_idempotent(self) -> None:
        """相同集合名称必须返回同一个稳定引用。"""
        port = FakeKnowledgeIndexPort()

        spec = CollectionSpec(architecture_id=1, name="architecture-1")
        first = port.ensure_collection(spec)
        second = port.ensure_collection(spec)

        self.assertEqual(first, second)

    def test_store_reconcile_and_remove_preserve_idempotency(self) -> None:
        """保存重试复用原文档，删除可重复执行，对账反映当前真实状态。"""
        port = FakeKnowledgeIndexPort()
        collection = port.ensure_collection(CollectionSpec(2, "architecture-2"))
        operation_context = self._operation_context()

        created = port.store_document(
            collection,
            "sample.pdf",
            self._metadata(),
            operation_context=operation_context,
            idempotency_key="sha256:abc",
        )
        reused = port.store_document(
            collection,
            "another-path.pdf",
            self._metadata(),
            operation_context=operation_context,
            idempotency_key="sha256:abc",
        )
        reconciled = port.reconcile_document(
            collection,
            operation_context=operation_context,
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

        first_removal = port.detach_document(
            collection,
            created.external_location,
            operation_context=operation_context,
        )
        second_removal = port.detach_document(
            collection,
            created.external_location,
            operation_context=operation_context,
        )

        self.assertTrue(first_removal.success)
        self.assertFalse(first_removal.already_applied)
        self.assertTrue(second_removal.success)
        self.assertTrue(second_removal.already_applied)
        self.assertIsNone(
            port.reconcile_document(
                collection,
                operation_context=operation_context,
                idempotency_key="sha256:abc",
            )
        )

    def test_store_prepared_document_preserves_rag_document_identity(self) -> None:
        """长期知识库登记必须复用 RAG 已上传文档，不得生成第二个外部位置。"""
        port = FakeKnowledgeIndexPort()
        collection = port.ensure_collection(CollectionSpec(20, "architecture-prepared"))
        prepared = PreparedDocumentRef(
            document_ref="document:prepared",
            external_location="external:prepared",
            content_sha256="a" * 64,
            ingested_file_name="sample.pdf",
            structured_source_key="docsense_ref:" + "a" * 32,
        )

        stored = port.store_prepared_document(
            collection,
            prepared,
            self._metadata(),
            operation_context=self._operation_context(),
            idempotency_key="sha256:prepared",
        )

        self.assertEqual(prepared.document_ref, stored.document_ref)
        self.assertEqual(prepared.external_location, stored.external_location)
        self.assertTrue(stored.created)

    def test_same_idempotency_key_is_scoped_to_collection(self) -> None:
        """不同业务集合可以安全使用相同幂等键而不会错误复用文档。"""
        port = FakeKnowledgeIndexPort()
        first_collection = port.ensure_collection(CollectionSpec(3, "architecture-3"))
        second_collection = port.ensure_collection(CollectionSpec(4, "architecture-4"))
        operation_context = self._operation_context()

        first = port.store_document(
            first_collection,
            "sample.pdf",
            self._metadata(),
            operation_context=operation_context,
            idempotency_key="shared-key",
        )
        second = port.store_document(
            second_collection,
            "sample.pdf",
            self._metadata(),
            operation_context=operation_context,
            idempotency_key="shared-key",
        )

        self.assertNotEqual(first.document_ref, second.document_ref)
        self.assertTrue(first.created)
        self.assertTrue(second.created)

    def test_forged_collection_reference_is_rejected(self) -> None:
        """结构相似但不属于当前端口实例的集合引用不得用于索引操作。"""
        port = FakeKnowledgeIndexPort()
        forged = CollectionRef(ref="collection:999", name="forged", architecture_id=999)

        with self.assertRaises(ValueError):
            port.store_document(
                forged,
                "sample.pdf",
                self._metadata(),
                operation_context=self._operation_context(),
                idempotency_key="key",
            )

    def test_concurrent_same_key_creates_only_one_document(self) -> None:
        """并发提交相同幂等键时只能产生一个首次创建结果和一个文档身份。"""
        port = FakeKnowledgeIndexPort()
        collection = port.ensure_collection(CollectionSpec(5, "architecture-5"))
        operation_context = self._operation_context()

        def store_once(index: int) -> IndexedDocument:
            """从工作线程提交同一逻辑文档，并返回稳定结果用于聚合断言。"""
            return port.store_document(
                collection,
                f"sample-{index}.pdf",
                self._metadata(),
                operation_context=operation_context,
                idempotency_key="concurrent-key",
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(store_once, range(24)))

        self.assertEqual(1, sum(result.created for result in results))
        self.assertEqual(23, sum(result.reused for result in results))
        self.assertEqual(1, len({result.document_ref for result in results}))
        self.assertEqual(1, len({result.external_location for result in results}))

    def test_same_idempotency_key_with_different_metadata_is_rejected(self) -> None:
        """幂等重试不得用新 metadata 静默覆盖首次操作快照。"""
        port = FakeKnowledgeIndexPort()
        collection = port.ensure_collection(CollectionSpec(6, "architecture-metadata"))
        operation_context = self._operation_context()
        port.store_document(
            collection,
            "sample.pdf",
            self._metadata(country="美国"),
            operation_context=operation_context,
            idempotency_key="metadata-key",
        )

        with self.assertRaisesRegex(ValueError, "不同 metadata"):
            port.store_document(
                collection,
                "sample.pdf",
                self._metadata(country="中国"),
                operation_context=operation_context,
                idempotency_key="metadata-key",
            )

    def test_prepared_document_identity_conflict_is_rejected(self) -> None:
        """相同幂等键不得在重试时切换到另一份已上传文档。"""
        port = FakeKnowledgeIndexPort()
        collection = port.ensure_collection(
            CollectionSpec(7, "architecture-prepared-conflict")
        )
        operation_context = self._operation_context()
        port.store_prepared_document(
            collection,
            PreparedDocumentRef(
                "document:first",
                "external:first",
                "a" * 64,
                "first-upload.pdf",
                "docsense_ref:" + "a" * 32,
            ),
            self._metadata(),
            operation_context=operation_context,
            idempotency_key="prepared-key",
        )

        with self.assertRaisesRegex(ValueError, "不同的预备文档"):
            port.store_prepared_document(
                collection,
                PreparedDocumentRef(
                    "document:second",
                    "external:second",
                    "b" * 64,
                    "second-upload.pdf",
                    "docsense_ref:" + "b" * 32,
                ),
                self._metadata(),
                operation_context=operation_context,
                idempotency_key="prepared-key",
            )

    def test_operation_result_rejects_ambiguous_failure_state(self) -> None:
        """失败结果必须带错误，且不能同时声明操作早已成功应用。"""
        with self.assertRaises(ValueError):
            OperationResult(success=False, already_applied=False)
        with self.assertRaises(ValueError):
            OperationResult(
                success=False,
                already_applied=True,
                error_message="解除绑定失败",
            )

    def test_fake_factory_preserves_permanent_state_across_task_leases(self) -> None:
        """任务级 Port 可以更换，但永久知识库状态必须跨租约共享。"""
        factory = FakeKnowledgeIndexFactory()
        spec = CollectionSpec(8, "architecture-shared")
        context = self._operation_context()
        with factory.create() as first_port:
            collection = first_port.ensure_collection(spec)
            created = first_port.store_document(
                collection,
                "sample.pdf",
                self._metadata(),
                operation_context=context,
                idempotency_key="shared-across-leases",
            )
        with factory.create() as second_port:
            same_collection = second_port.ensure_collection(spec)
            reconciled = second_port.reconcile_document(
                same_collection,
                operation_context=self._operation_context("execution-002"),
                idempotency_key="shared-across-leases",
            )

        self.assertIsNotNone(reconciled)
        recovered = cast(IndexedDocument, reconciled)
        self.assertEqual(created.document_ref, recovered.document_ref)
        self.assertTrue(recovered.reused)


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
