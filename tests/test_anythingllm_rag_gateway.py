"""AnythingLLM 纯方案 B RAG Gateway 的离线状态机测试。

测试使用带接口约束的 Mock 原子 Client 和任务私有临时文件，不创建 HTTP Transport。
重点验证跨 Client 编排、不可变上传副本、稳定失败阶段、来源身份、调用轨迹和资源生命周期。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


_TEST_DIRECTORY = tempfile.TemporaryDirectory(prefix="docsense-rag-tests-")
_SAMPLE_FILE_PATH = Path(_TEST_DIRECTORY.name) / "sample.pdf"
_SAMPLE_FILE_PATH.write_bytes(b"offline rag test document")

from app.integrations.anythingllm.documents import AnythingLLMDocumentClient
from app.integrations.anythingllm.errors import (
    AnythingLLMHTTPError,
    AnythingLLMProtocolError,
)
from app.integrations.anythingllm.models import (
    AnythingLLMAnswer,
    AnythingLLMDocument,
    AnythingLLMSource,
    AnythingLLMThread,
    AnythingLLMWorkspace,
)
from app.integrations.anythingllm.rag_gateway import AnythingLLMRagGateway
from app.integrations.anythingllm.threads import AnythingLLMThreadClient
from app.integrations.anythingllm.workspaces import AnythingLLMWorkspaceClient
from app.ports import (
    DocumentRagPort,
    DocumentRagSession,
    RagOperationError,
    RagPromptKind,
)


class _GatewayHarness:
    """为每个测试创建相互独立的原子 Client Mock 和标准成功响应。"""

    SOURCE_MARKER = "docsense_ref:0123456789abcdef0123456789abcdef"

    def __init__(self, *, embedding_max_attempts: int = 2) -> None:
        """初始化默认可完成整个状态机的 Mock 组合。"""
        self.document_client = Mock(spec=AnythingLLMDocumentClient)
        self.workspace_client = Mock(spec=AnythingLLMWorkspaceClient)
        self.thread_client = Mock(spec=AnythingLLMThreadClient)
        self.workspace = AnythingLLMWorkspace(
            id="workspace-id",
            slug="context-ref",
            name="analysis-context",
        )
        self.thread = AnythingLLMThread(id="thread-id", slug="conversation-ref")
        self.document = AnythingLLMDocument(
            id="document-id",
            location="custom-documents/sample.pdf-document-id.json",
            title="sample.pdf",
            document_ref="document:document-id",
        )
        self.source = AnythingLLMSource(
            document_ref=self.document.document_ref,
            text="目标证据",
            source_marker=self.SOURCE_MARKER,
        )
        self.answer = AnythingLLMAnswer(
            text="分析结果",
            raw_text="原始分析结果",
            sources=(self.source,),
        )
        self.workspace_client.create_workspace.return_value = self.workspace
        self.thread_client.create_thread.return_value = self.thread
        self.document_client.upload_document.return_value = self.document
        self.workspace_client.update_embeddings.return_value = self.workspace
        self.workspace_client.update_pin.return_value = None
        self.thread_client.ask.return_value = self.answer
        self.gateway = AnythingLLMRagGateway(
            self.document_client,
            self.workspace_client,
            self.thread_client,
            user_id=7,
            embedding_max_attempts=embedding_max_attempts,
            source_marker_factory=lambda: self.SOURCE_MARKER,
        )

    def open_session(self) -> DocumentRagSession:
        """使用统一测试名称打开一个隔离会话。"""
        return self.gateway.open_isolated_session(
            context_name="analysis-context",
            conversation_name="analysis-conversation",
        )


def _http_error(status_code: int) -> AnythingLLMHTTPError:
    """构造不包含敏感响应正文的稳定 HTTP 异常。"""
    return AnythingLLMHTTPError(
        f"上游返回 HTTP {status_code}",
        method="POST",
        url="http://127.0.0.1/api",
        status_code=status_code,
        response_summary="",
    )


class AnythingLLMRagGatewaySuccessTests(unittest.TestCase):
    """验证标准成功路径、DTO 转换和后续追问行为。"""

    def test_gateway_and_session_implement_application_protocols(self) -> None:
        """生产 Gateway 必须可以直接注入只依赖应用层 Protocol 的业务服务。"""
        harness = _GatewayHarness()
        session = harness.open_session()

        self.assertIsInstance(harness.gateway, DocumentRagPort)
        self.assertIsInstance(session, DocumentRagSession)

    def test_invalid_source_marker_factory_fails_before_context_creation(self) -> None:
        """测试接缝返回弱标记时不得创建任何外部 Workspace。"""
        harness = _GatewayHarness()
        gateway = AnythingLLMRagGateway(
            harness.document_client,
            harness.workspace_client,
            harness.thread_client,
            source_marker_factory=lambda: "docsense_ref:too-short",
        )

        with self.assertRaises(ValueError):
            gateway.open_isolated_session(
                context_name="analysis-context",
                conversation_name="analysis-conversation",
            )

        harness.workspace_client.create_workspace.assert_not_called()

    def test_analyse_executes_pure_b_sequence_and_maps_sources(self) -> None:
        """成功分析必须按创建、上传、加入、Pin、查询顺序完成并返回业务 DTO。"""
        harness = _GatewayHarness()
        manager = Mock()
        manager.attach_mock(harness.workspace_client.create_workspace, "create_context")
        manager.attach_mock(harness.thread_client.create_thread, "create_conversation")
        manager.attach_mock(harness.document_client.upload_document, "upload")
        manager.attach_mock(harness.workspace_client.update_embeddings, "bind")
        manager.attach_mock(harness.workspace_client.update_pin, "pin")
        manager.attach_mock(harness.thread_client.ask, "query")

        with self.assertLogs(
            "app.integrations.anythingllm.rag_gateway",
            level="INFO",
        ) as captured:
            session = harness.open_session()
            result = session.analyse(str(_SAMPLE_FILE_PATH), "分析文档")

        self.assertEqual("分析结果", result.text)
        self.assertEqual("document:document-id", result.sources[0].document_ref)
        self.assertEqual("目标证据", result.sources[0].text)
        self.assertEqual(
            harness.document.document_ref,
            result.prepared_document.document_ref,
        )
        self.assertEqual(
            harness.document.location,
            result.prepared_document.external_location,
        )
        self.assertEqual(
            hashlib.sha256(_SAMPLE_FILE_PATH.read_bytes()).hexdigest(),
            result.prepared_document.content_sha256,
        )
        self.assertEqual(
            [
                "create_context",
                "create_conversation",
                "upload",
                "bind",
                "pin",
                "query",
            ],
            [call[0] for call in manager.mock_calls],
        )
        query_kwargs = harness.thread_client.ask.call_args.kwargs
        self.assertNotIn("document_ids", query_kwargs)
        self.assertEqual("query", query_kwargs["mode"])
        upload_kwargs = harness.document_client.upload_document.call_args.kwargs
        self.assertEqual(
            {"docSource": harness.SOURCE_MARKER},
            upload_kwargs["metadata"],
        )
        log_text = "\n".join(captured.output)
        self.assertIn("AnythingLLM 文档上传完成", log_text)
        self.assertIn("AnythingLLM 文档已加入隔离工作区", log_text)
        self.assertIn("AnythingLLM 文档固定完成", log_text)
        self.assertIn("AnythingLLM 来源归属校验完成", log_text)
        self.assertIn("AnythingLLM 查询完成", log_text)
        self.assertNotIn(harness.SOURCE_MARKER, log_text)

    def test_source_optional_fields_can_be_absent(self) -> None:
        """来源只有随机标记和文本时仍可转换，不得要求展示型可选字段。"""
        harness = _GatewayHarness()
        harness.thread_client.ask.return_value = AnythingLLMAnswer(
            text="分析结果",
            raw_text="分析结果",
            sources=(
                AnythingLLMSource(
                    document_ref=harness.document.document_ref,
                    text="最小来源",
                    source_marker=harness.SOURCE_MARKER,
                ),
            ),
        )

        result = harness.open_session().analyse(str(_SAMPLE_FILE_PATH), "分析文档")

        source = result.sources[0]
        self.assertIsNone(source.id)
        self.assertIsNone(source.title)
        self.assertIsNone(source.url)
        self.assertIsNone(source.score)

    def test_follow_up_query_reuses_prepared_session(self) -> None:
        """后续 ask 只能追加线程查询，不得重新上传、嵌入或 Pin。"""
        harness = _GatewayHarness()
        session = harness.open_session()
        session.analyse(str(_SAMPLE_FILE_PATH), "分析文档")
        harness.thread_client.ask.return_value = AnythingLLMAnswer(
            text="修复结果",
            raw_text="修复结果",
            sources=(harness.source,),
        )

        result = session.ask(
            "修复结构",
            prompt_kind=RagPromptKind.JSON_REPAIR,
        )

        self.assertEqual("修复结果", result.text)
        self.assertEqual(1, harness.document_client.upload_document.call_count)
        self.assertEqual(1, harness.workspace_client.update_embeddings.call_count)
        self.assertEqual(1, harness.workspace_client.update_pin.call_count)
        self.assertEqual(2, harness.thread_client.ask.call_count)
        self.assertEqual(
            ["analyse", "ask"],
            [attempt.operation for attempt in session.trace.attempts],
        )
        self.assertEqual("json_repair", session.trace.attempts[-1].prompt_kind)
        self.assertEqual("query", session.trace.attempts[-1].query_mode)

    def test_invalid_prompt_kind_is_rejected_before_external_query(self) -> None:
        """未知提示词用途不得先产生一次真实线程问答再在审计构造阶段失败。"""
        harness = _GatewayHarness()
        session = harness.open_session()
        session.analyse(str(_SAMPLE_FILE_PATH), "分析文档")
        query_count_before_invalid_call = harness.thread_client.ask.call_count

        with self.assertRaises(TypeError):
            session.ask(
                "修复结构",
                prompt_kind="json_repair",  # type: ignore[arg-type]
            )

        self.assertEqual(
            query_count_before_invalid_call,
            harness.thread_client.ask.call_count,
        )

    def test_explicit_non_source_query_can_succeed_without_sources(self) -> None:
        """调用方关闭来源要求时，有效文本可以在无来源条件下成功返回。"""
        harness = _GatewayHarness()
        harness.thread_client.ask.return_value = AnythingLLMAnswer(
            text="无来源回答",
            raw_text="无来源回答",
            sources=(),
        )

        result = harness.open_session().analyse(
            str(_SAMPLE_FILE_PATH),
            "执行非检索检查",
            require_sources=False,
            max_attempts=1,
        )

        self.assertEqual("无来源回答", result.text)
        self.assertEqual((), result.sources)


class AnythingLLMRagGatewayPreparationFailureTests(unittest.TestCase):
    """验证上传、嵌入和 Pin 阶段的失败分类及有限恢复。"""

    def test_upload_missing_required_identity_fields_is_protocol_failure(self) -> None:
        """ID、位置或稳定文档身份任一缺失都不得继续进入嵌入阶段。"""
        malformed_documents = (
            SimpleNamespace(id="", location="location", document_ref="name:sample.pdf"),
            SimpleNamespace(id="id", location="", document_ref="name:sample.pdf"),
            SimpleNamespace(id="id", location="location", document_ref=""),
        )
        for document in malformed_documents:
            with self.subTest(document=document):
                harness = _GatewayHarness()
                harness.document_client.upload_document.return_value = document
                session = harness.open_session()

                with self.assertRaises(RagOperationError) as raised:
                    session.analyse(str(_SAMPLE_FILE_PATH), "分析文档")

                self.assertEqual("upload_protocol", raised.exception.trace.failure_stage)
                harness.workspace_client.update_embeddings.assert_not_called()

    def test_upload_protocol_exception_is_mapped_to_upload_protocol(self) -> None:
        """原子 Client 拒绝缺字段响应时，Gateway 必须保留协议失败语义。"""
        harness = _GatewayHarness()
        harness.document_client.upload_document.side_effect = AnythingLLMProtocolError(
            "上传响应缺少 location"
        )

        with self.assertRaises(RagOperationError) as raised:
            harness.open_session().analyse(str(_SAMPLE_FILE_PATH), "分析文档")

        self.assertEqual("upload_protocol", raised.exception.trace.failure_stage)

    def test_embedding_missing_empty_or_conflicting_workspace_is_protocol_failure(self) -> None:
        """嵌入响应必须返回非空且与目标一致的工作区引用。"""
        invalid_results = (
            None,
            AnythingLLMWorkspace(id="", slug="", name=""),
            AnythingLLMWorkspace(id="other", slug="other", name="other"),
        )
        for result in invalid_results:
            with self.subTest(result=result):
                harness = _GatewayHarness()
                harness.workspace_client.update_embeddings.return_value = result

                with self.assertRaises(RagOperationError) as raised:
                    harness.open_session().analyse(str(_SAMPLE_FILE_PATH), "分析文档")

                self.assertEqual(
                    "embedding_protocol",
                    raised.exception.trace.failure_stage,
                )
                self.assertEqual((), raised.exception.trace.attempts)
                self.assertEqual(
                    [
                        "context_create",
                        "conversation_create",
                        "document_upload",
                        "document_bind",
                    ],
                    [
                        event.operation
                        for event in raised.exception.trace.lifecycle_events
                    ],
                )
                harness.workspace_client.update_pin.assert_not_called()

    def test_transient_embedding_gateway_error_is_retried_without_sleep(self) -> None:
        """标准暂态网关错误允许有限重试，成功后继续 Pin。"""
        harness = _GatewayHarness(embedding_max_attempts=2)
        harness.workspace_client.update_embeddings.side_effect = [
            _http_error(503),
            harness.workspace,
        ]

        result = harness.open_session().analyse(str(_SAMPLE_FILE_PATH), "分析文档")

        self.assertEqual("分析结果", result.text)
        self.assertEqual(2, harness.workspace_client.update_embeddings.call_count)
        self.assertEqual(1, harness.workspace_client.update_pin.call_count)

    def test_non_transient_embedding_error_is_not_retried(self) -> None:
        """4xx 等非暂态错误必须立即失败，避免盲目重放外部写操作。"""
        harness = _GatewayHarness(embedding_max_attempts=2)
        harness.workspace_client.update_embeddings.side_effect = _http_error(400)

        with self.assertRaises(RagOperationError) as raised:
            harness.open_session().analyse(str(_SAMPLE_FILE_PATH), "分析文档")

        self.assertEqual("embedding", raised.exception.trace.failure_stage)
        self.assertEqual(1, harness.workspace_client.update_embeddings.call_count)

    def test_pin_protocol_error_is_mapped_without_recovery(self) -> None:
        """Pin 非 JSON 或显式失败属于协议错误，不应执行 404 专用恢复。"""
        harness = _GatewayHarness()
        harness.workspace_client.update_pin.side_effect = AnythingLLMProtocolError(
            "Pin 响应不是合法 JSON"
        )
        session = harness.open_session()

        with self.assertRaises(RagOperationError) as raised:
            session.analyse(str(_SAMPLE_FILE_PATH), "分析文档")

        self.assertEqual("pin_protocol", raised.exception.trace.failure_stage)
        self.assertEqual(1, harness.workspace_client.update_embeddings.call_count)
        self.assertEqual(1, harness.workspace_client.update_pin.call_count)
        harness.document_client.delete_document.assert_not_called()

        session.close(retain_document=False)

        harness.document_client.delete_document.assert_called_once_with(
            harness.document.location,
            user_id=7,
        )

    def test_first_pin_404_rebinds_once_then_succeeds(self) -> None:
        """首次 Pin 404 时重新加入文档一次，随后只重试一次 Pin。"""
        harness = _GatewayHarness()
        harness.workspace_client.update_pin.side_effect = [_http_error(404), None]

        result = harness.open_session().analyse(str(_SAMPLE_FILE_PATH), "分析文档")

        self.assertEqual("分析结果", result.text)
        self.assertEqual(2, harness.workspace_client.update_embeddings.call_count)
        self.assertEqual(2, harness.workspace_client.update_pin.call_count)
        harness.workspace_client.list_documents.assert_not_called()
        harness.workspace_client.find_document.assert_not_called()

    def test_second_pin_404_fails_as_not_found(self) -> None:
        """恢复后的第二次 404 必须终止，不能继续循环或回退查询路径。"""
        harness = _GatewayHarness()
        harness.workspace_client.update_pin.side_effect = [
            _http_error(404),
            _http_error(404),
        ]

        with self.assertRaises(RagOperationError) as raised:
            harness.open_session().analyse(str(_SAMPLE_FILE_PATH), "分析文档")

        self.assertEqual("pin_not_found", raised.exception.trace.failure_stage)
        self.assertEqual(2, harness.workspace_client.update_embeddings.call_count)
        self.assertEqual(2, harness.workspace_client.update_pin.call_count)


class AnythingLLMRagGatewayQueryContractTests(unittest.TestCase):
    """验证来源精确匹配、查询重试和逐次调用轨迹。"""

    def test_empty_sources_first_then_success_retries_only_query(self) -> None:
        """来源暂时缺失时只重发问答，不重复任何文档准备步骤。"""
        harness = _GatewayHarness()
        harness.thread_client.ask.side_effect = [
            AnythingLLMAnswer(text="第一次回答", raw_text="第一次回答", sources=()),
            harness.answer,
        ]

        result = harness.open_session().analyse(
            str(_SAMPLE_FILE_PATH),
            "分析文档",
            max_attempts=2,
        )

        self.assertEqual("分析结果", result.text)
        self.assertEqual(2, harness.thread_client.ask.call_count)
        self.assertEqual(1, harness.document_client.upload_document.call_count)
        self.assertEqual(1, harness.workspace_client.update_embeddings.call_count)
        self.assertEqual(1, harness.workspace_client.update_pin.call_count)
        self.assertEqual("sources", result.trace.attempts[0].failure_stage)
        self.assertIsNone(result.trace.attempts[1].failure_stage)

    def test_query_protocol_failure_first_then_success_is_retried(self) -> None:
        """模型无最终事件等查询失败可在同一线程内有限重试。"""
        harness = _GatewayHarness()
        harness.thread_client.ask.side_effect = [
            AnythingLLMProtocolError("线程问答未返回最终事件"),
            harness.answer,
        ]

        result = harness.open_session().analyse(
            str(_SAMPLE_FILE_PATH),
            "分析文档",
            max_attempts=2,
        )

        self.assertEqual("分析结果", result.text)
        self.assertEqual("query", result.trace.attempts[0].failure_stage)
        self.assertIsNone(result.trace.attempts[1].failure_stage)

    def test_unknown_query_exception_is_audited_without_retry(self) -> None:
        """未知编程异常必须形成一次失败 attempt，但不得作为暂态故障自动重放。"""
        harness = _GatewayHarness()
        harness.thread_client.ask.side_effect = RuntimeError("内部实现异常")
        session = harness.open_session()

        with self.assertRaises(RagOperationError) as raised:
            session.analyse(str(_SAMPLE_FILE_PATH), "分析文档", max_attempts=3)

        self.assertEqual(1, harness.thread_client.ask.call_count)
        self.assertEqual(1, len(raised.exception.trace.attempts))
        self.assertEqual("query", raised.exception.trace.failure_stage)
        self.assertNotIn(
            "内部实现异常",
            raised.exception.trace.error_message or "",
        )

    def test_retry_exhaustion_preserves_all_attempts(self) -> None:
        """重试耗尽后异常轨迹必须保存每次原始回答和失败阶段。"""
        harness = _GatewayHarness()
        harness.thread_client.ask.side_effect = [
            AnythingLLMAnswer(text="回答一", raw_text="原始一", sources=()),
            AnythingLLMAnswer(text="回答二", raw_text="原始二", sources=()),
        ]

        with self.assertRaises(RagOperationError) as raised:
            harness.open_session().analyse(
                str(_SAMPLE_FILE_PATH),
                "分析文档",
                max_attempts=2,
            )

        trace = raised.exception.trace
        self.assertEqual("sources", trace.failure_stage)
        self.assertEqual(2, len(trace.attempts))
        self.assertEqual(["原始一", "原始二"], [item.raw_response for item in trace.attempts])

    def test_legacy_document_reference_does_not_participate_in_trusted_match(self) -> None:
        """展示型引用即使错误，只要结构化随机标记正确也不影响可信归属。"""
        harness = _GatewayHarness()
        source_with_untrusted_legacy_ref = AnythingLLMSource(
            document_ref="name:sample.pdf.backup",
            text="目标文档证据",
            source_marker=harness.SOURCE_MARKER,
        )
        harness.thread_client.ask.return_value = AnythingLLMAnswer(
            text="可信来源回答",
            raw_text="可信来源回答",
            sources=(source_with_untrusted_legacy_ref,),
        )

        result = harness.open_session().analyse(
            str(_SAMPLE_FILE_PATH),
            "分析文档",
            max_attempts=1,
        )

        self.assertEqual(harness.document.document_ref, result.sources[0].document_ref)

    def test_mismatched_source_marker_is_rejected(self) -> None:
        """其他 Session 的合法格式标记不得被归属于当前上传文档。"""
        harness = _GatewayHarness()
        wrong_source = AnythingLLMSource(
            document_ref=harness.document.document_ref,
            text="其他文档证据",
            source_marker="docsense_ref:ffffffffffffffffffffffffffffffff",
        )
        harness.thread_client.ask.return_value = AnythingLLMAnswer(
            text="错误来源回答",
            raw_text="错误来源回答",
            sources=(wrong_source,),
        )

        with self.assertRaises(RagOperationError) as raised:
            harness.open_session().analyse(str(_SAMPLE_FILE_PATH), "分析文档", max_attempts=1)

        self.assertEqual("sources", raised.exception.trace.failure_stage)
        self.assertEqual((), raised.exception.trace.attempts[0].sources)

    def test_mixed_verified_and_unmarked_sources_are_rejected(self) -> None:
        """存在一个可信来源也不能掩盖同一回答中的无标记来源。"""
        harness = _GatewayHarness()
        unmarked_source = AnythingLLMSource(
            document_ref="name:untrusted.pdf",
            text="身份未知的附加证据",
        )
        harness.thread_client.ask.return_value = AnythingLLMAnswer(
            text="混合来源回答",
            raw_text="混合来源回答",
            sources=(harness.source, unmarked_source),
        )

        with self.assertRaises(RagOperationError) as raised:
            harness.open_session().analyse(str(_SAMPLE_FILE_PATH), "分析文档", max_attempts=1)

        attempt = raised.exception.trace.attempts[0]
        self.assertEqual("sources", attempt.failure_stage)
        self.assertEqual(1, len(attempt.sources))
        self.assertEqual(harness.document.document_ref, attempt.sources[0].document_ref)

    def test_unresolved_source_is_not_given_a_guessed_reference(self) -> None:
        """缺少随机标记的来源必须被排除，不能回退到展示型引用或路径。"""
        harness = _GatewayHarness()
        harness.thread_client.ask.return_value = AnythingLLMAnswer(
            text="身份不明回答",
            raw_text="身份不明回答",
            sources=(
                AnythingLLMSource(
                    document_ref=harness.document.document_ref,
                    text="未知证据",
                ),
            ),
        )

        with self.assertRaises(RagOperationError) as raised:
            harness.open_session().analyse(
                str(_SAMPLE_FILE_PATH),
                "分析文档",
                max_attempts=1,
            )

        self.assertEqual("sources", raised.exception.trace.failure_stage)
        self.assertEqual((), raised.exception.trace.attempts[0].sources)

    def test_logs_do_not_include_full_prompt(self) -> None:
        """失败与重试日志只能记录结构化维度，不得输出完整 Prompt。"""
        harness = _GatewayHarness()
        harness.thread_client.ask.return_value = AnythingLLMAnswer(
            text="无来源回答",
            raw_text="无来源回答",
            sources=(),
        )
        secret_prompt = "包含业务敏感内容的完整提示词"

        with self.assertLogs(
            "app.integrations.anythingllm.rag_gateway",
            level="WARNING",
        ) as captured:
            with self.assertRaises(RagOperationError):
                harness.open_session().analyse(
                    str(_SAMPLE_FILE_PATH),
                    secret_prompt,
                    max_attempts=1,
                )

        self.assertNotIn(secret_prompt, "\n".join(captured.output))


class AnythingLLMRagGatewayLifecycleTests(unittest.TestCase):
    """验证部分创建回滚和幂等清理。"""

    def test_context_creation_failure_exposes_no_reference(self) -> None:
        """工作区创建失败时不得继续创建线程或伪造资源引用。"""
        harness = _GatewayHarness()
        harness.workspace_client.create_workspace.side_effect = _http_error(500)

        with self.assertRaises(RagOperationError) as raised:
            harness.open_session()

        self.assertEqual("context_create", raised.exception.trace.failure_stage)
        self.assertIsNone(raised.exception.trace.context_ref)
        harness.thread_client.create_thread.assert_not_called()
        harness.workspace_client.delete_workspace.assert_not_called()

    def test_conversation_failure_rolls_back_context_inside_gateway(self) -> None:
        """线程创建失败时 Gateway 必须自行删除工作区并记录回滚成功。"""
        harness = _GatewayHarness()
        harness.thread_client.create_thread.side_effect = _http_error(500)

        with self.assertRaises(RagOperationError) as raised:
            harness.open_session()

        trace = raised.exception.trace
        self.assertEqual("conversation_create", trace.failure_stage)
        self.assertEqual("context-ref", trace.context_ref)
        self.assertIsNone(trace.conversation_ref)
        self.assertEqual(
            ["context_create", "conversation_create", "context_rollback"],
            [event.operation for event in trace.lifecycle_events],
        )
        self.assertEqual((), trace.attempts)
        self.assertIsNone(trace.lifecycle_events[-1].failure_stage)
        harness.workspace_client.delete_workspace.assert_called_once_with(
            "context-ref",
            user_id=7,
        )

    def test_rollback_failure_preserves_context_and_cleanup_error(self) -> None:
        """内部回滚失败时，轨迹必须保留残留引用和 cleanup 错误。"""
        harness = _GatewayHarness()
        harness.thread_client.create_thread.side_effect = _http_error(500)
        harness.workspace_client.delete_workspace.side_effect = _http_error(503)

        with self.assertRaises(RagOperationError) as raised:
            harness.open_session()

        trace = raised.exception.trace
        self.assertEqual("context-ref", trace.context_ref)
        self.assertEqual("cleanup", trace.lifecycle_events[-1].failure_stage)
        self.assertIn("回滚失败", trace.error_message or "")

    def test_close_is_idempotent_and_deletes_only_context(self) -> None:
        """成功清理只删除一次工作区，不单独重放线程删除。"""
        harness = _GatewayHarness()
        session = harness.open_session()

        first = session.close(retain_document=True)
        second = session.close(retain_document=False)

        self.assertTrue(first.success)
        self.assertFalse(first.already_closed)
        self.assertTrue(second.success)
        self.assertTrue(second.already_closed)
        harness.workspace_client.delete_workspace.assert_called_once_with(
            "context-ref",
            user_id=7,
        )
        harness.thread_client.delete_thread.assert_not_called()
        harness.document_client.delete_document.assert_not_called()

    def test_cleanup_failure_is_not_replayed(self) -> None:
        """首次删除失败后重复 close 返回原错误，但不得再次发送删除请求。"""
        harness = _GatewayHarness()
        harness.workspace_client.delete_workspace.side_effect = _http_error(503)
        session = harness.open_session()

        first = session.close(retain_document=True)
        second = session.close(retain_document=False)

        self.assertFalse(first.success)
        self.assertFalse(first.already_closed)
        self.assertFalse(second.success)
        self.assertTrue(second.already_closed)
        self.assertEqual(first.error_message, second.error_message)
        self.assertEqual(1, harness.workspace_client.delete_workspace.call_count)

    def test_failed_preparation_deletes_global_document_only_when_closed(self) -> None:
        """嵌入失败后先保留审计现场，审计成功触发 close 时再永久删除全局文档。"""
        harness = _GatewayHarness()
        harness.workspace_client.update_embeddings.side_effect = _http_error(400)
        session = harness.open_session()

        with self.assertRaises(RagOperationError):
            session.analyse(str(_SAMPLE_FILE_PATH), "分析文档")

        harness.document_client.delete_document.assert_not_called()
        cleanup = session.close(retain_document=True)

        self.assertTrue(cleanup.success)
        harness.document_client.delete_document.assert_called_once_with(
            harness.document.location,
            user_id=7,
        )
        delete_events = [
            event
            for event in session.trace.lifecycle_events
            if event.operation == "global_document_delete"
        ]
        self.assertEqual(1, len(delete_events))
        self.assertTrue(delete_events[0].success)

    def test_business_failure_can_explicitly_delete_successful_analysis_document(self) -> None:
        """RAG 成功但后续业务契约失败时，关闭策略必须允许删除已上传全局文档。"""
        harness = _GatewayHarness()
        session = harness.open_session()
        session.analyse(str(_SAMPLE_FILE_PATH), "分析文档")

        cleanup = session.close(retain_document=False)

        self.assertTrue(cleanup.success)
        harness.document_client.delete_document.assert_called_once_with(
            harness.document.location,
            user_id=7,
        )

    def test_successful_analysis_retains_global_document(self) -> None:
        """全部业务成功时显式保留文档，关闭只删除临时工作区。"""
        harness = _GatewayHarness()
        session = harness.open_session()
        session.analyse(str(_SAMPLE_FILE_PATH), "分析文档")

        cleanup = session.close(retain_document=True)

        self.assertTrue(cleanup.success)
        harness.document_client.delete_document.assert_not_called()
        harness.workspace_client.delete_workspace.assert_called_once_with(
            "context-ref",
            user_id=7,
        )

    def test_global_document_delete_failure_does_not_skip_workspace_cleanup(self) -> None:
        """补偿删除失败必须被汇总，但仍应尽力删除临时工作区并暴露失败结果。"""
        harness = _GatewayHarness()
        harness.document_client.delete_document.side_effect = _http_error(503)
        session = harness.open_session()
        session.analyse(str(_SAMPLE_FILE_PATH), "分析文档")

        cleanup = session.close(retain_document=False)
        repeated_cleanup = session.close(retain_document=False)

        self.assertFalse(cleanup.success)
        self.assertTrue(cleanup.error_message)
        self.assertTrue(repeated_cleanup.already_closed)
        self.assertEqual(1, harness.document_client.delete_document.call_count)
        harness.workspace_client.delete_workspace.assert_called_once_with(
            "context-ref",
            user_id=7,
        )

    def test_invalid_attempt_limit_fails_before_upload(self) -> None:
        """无效重试参数必须在产生上传副作用前被拒绝。"""
        harness = _GatewayHarness()
        session = harness.open_session()

        with self.assertRaises(ValueError):
            session.analyse(str(_SAMPLE_FILE_PATH), "分析文档", max_attempts=0)

        harness.document_client.upload_document.assert_not_called()

    def test_attempt_limit_above_hard_cap_fails_before_upload(self) -> None:
        """超过端口硬上限的查询次数必须在上传前失败，防止错误配置制造无界调用。"""
        harness = _GatewayHarness()
        session = harness.open_session()

        with self.assertRaises(ValueError):
            session.analyse(str(_SAMPLE_FILE_PATH), "分析文档", max_attempts=4)

        harness.document_client.upload_document.assert_not_called()

    def test_embedding_attempt_limit_above_hard_cap_is_rejected(self) -> None:
        """嵌入总调用次数超过三次时必须在 Gateway 构造阶段失败。"""
        harness = _GatewayHarness()

        with self.assertRaises(ValueError):
            AnythingLLMRagGateway(
                harness.document_client,
                harness.workspace_client,
                harness.thread_client,
                embedding_max_attempts=4,
            )


class AnythingLLMRagGatewayBoundaryTests(unittest.TestCase):
    """静态阻止恢复路径、文件附件和固定休眠重新进入 Gateway。"""

    def test_gateway_source_contains_no_get_files_or_sleep_escape_hatch(self) -> None:
        """纯方案 B Gateway 不得反查工作区文档、发送 files 或执行固定等待。"""
        source_path = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "integrations"
            / "anythingllm"
            / "rag_gateway.py"
        )
        source = source_path.read_text(encoding="utf-8")
        forbidden_terms = (
            ".list_documents(",
            ".find_document(",
            ".get_workspace(",
            "document_ids=",
            "time.sleep",
            "sleep(",
        )

        for term in forbidden_terms:
            with self.subTest(term=term):
                self.assertNotIn(term, source)


if __name__ == "__main__":
    unittest.main()
