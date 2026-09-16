"""tests/test_weaponry_service.py — weaponry_service 核心映射函数的单元测试"""
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.modules.weaponry.domain import DeprecatedWeaponryModeError
from app.services.llm_service import weaponry_service as weaponry_service_module
from app.services.llm_service.weaponry_service import (
    WeaponrySelectedDocument,
    WeaponrySelectedDocumentAmbiguityError,
    WeaponrySelectedDocumentNotFoundError,
    WeaponryRetrievalContext,
    _count_query_fields,
    _strip_document_metadata,
    _extract_chunk_source_name,
    _ensure_terms_workspace,
    _format_terms_rule_context,
    _is_target_source,
    _is_terms_source_name,
    _list_workspace_documents,
    _parse_table_json_rows,
    _prepare_retrieval_context,
    _query_input_field,
    _query_table_field,
    _restore_target_workspace_terms,
    _resolve_hashed_source_name,
    _resolve_original_source_name,
    _target_document_records,
    _vector_search_with_top_n,
    resolve_weaponry_selected_documents,
    run_weaponry_task,
)


class TestAnythingLLMHTTPBoundary(unittest.TestCase):
    """验证 weaponry 只依赖 AnythingLLMClient 公开能力，不再拼装 HTTP。"""

    def test_vector_search_delegates_to_public_client_method(self) -> None:
        """带 top_n 的检索应完整转发参数并返回 Client 结果。"""
        client = MagicMock()
        expected = [{"text": "chunk", "score": 0.8}]
        client.vector_search.return_value = expected

        result = _vector_search_with_top_n(
            client,
            "workspace-1",
            "检索问题",
            top_n=12,
            user_id=7,
        )

        self.assertIs(result, expected)
        client.vector_search.assert_called_once_with(
            "workspace-1",
            "检索问题",
            user_id=7,
            top_n=12,
        )

    def test_workspace_document_list_delegates_to_public_client_method(self) -> None:
        """文档列表查询应只调用公开 Facade 方法。"""
        client = MagicMock()
        expected = [{"id": "doc-1", "docpath": "custom-documents/a.json"}]
        client.list_workspace_documents.return_value = expected

        result = _list_workspace_documents(client, "workspace-1", user_id=8)

        self.assertIs(result, expected)
        client.list_workspace_documents.assert_called_once_with(
            "workspace-1",
            user_id=8,
        )

    def test_weaponry_source_contains_no_anythingllm_http_escape_hatch(self) -> None:
        """静态阻止 Session、私有 Header、base_url 和供应商 URL 再次进入业务层。"""
        source = Path(weaponry_service_module.__file__).read_text(encoding="utf-8")
        forbidden_fragments = (
            "client.session",
            "client._json_headers",
            "config.base_url",
            "/workspace/",
        )

        for fragment in forbidden_fragments:
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, source)


class TestWeaponryExtractionStrategy(unittest.TestCase):
    """模式 1 必须在任何检索或模型副作用之前被明确拒绝。"""

    @patch.dict(os.environ, {"WEAPONRY_ANALYSE_MODE": "1"})
    def test_explicit_mode_one_is_rejected_before_vector_search(self) -> None:
        client = MagicMock()

        with self.assertRaises(DeprecatedWeaponryModeError):
            _query_input_field(
                client,
                "workspace",
                "thread",
                {
                    "fieldName": "舰级名称",
                    "fieldType": "INPUT",
                    "fieldDescription": "提取正式舰级名称",
                },
            )

        client.vector_search.assert_not_called()
        client.send_prompt_to_thread.assert_not_called()


class TestStripDocumentMetadata(unittest.TestCase):
    """测试 _strip_document_metadata 去除 <document_metadata> 前缀。"""

    def test_strips_metadata_prefix(self):
        text = (
            "<document_metadata>\n"
            "sourceDocument: sample.txt\n"
            "published: 2026/4/12\n"
            "</document_metadata>\n\n"
            "这里是 chunk 的正文内容"
        )
        result = _strip_document_metadata(text)
        self.assertEqual(result, "这里是 chunk 的正文内容")

    def test_no_metadata_returns_stripped(self):
        text = "  没有 metadata 前缀的纯文本  "
        result = _strip_document_metadata(text)
        self.assertEqual(result, "没有 metadata 前缀的纯文本")

    def test_empty_string(self):
        self.assertEqual(_strip_document_metadata(""), "")

    def test_none_like_empty(self):
        self.assertEqual(_strip_document_metadata(""), "")

    def test_metadata_only(self):
        text = "<document_metadata>\nfoo\n</document_metadata>"
        result = _strip_document_metadata(text)
        self.assertEqual(result, "")


class TestWeaponryRetrievalSplitting(unittest.TestCase):
    """测试 weaponry 目标证据与术语规则分池逻辑。"""

    def test_extract_chunk_source_name_from_metadata_or_raw_text(self):
        self.assertEqual(
            _extract_chunk_source_name({"metadata": {"title": "target.pdf"}, "text": ""}),
            "target.pdf",
        )
        self.assertEqual(
            _extract_chunk_source_name(
                {
                    "metadata": {},
                    "text": "<document_metadata>\nsourceDocument: term_rule_0001_国别.md\n</document_metadata>\n正文",
                }
            ),
            "term_rule_0001_国别.md",
        )

    def test_terms_and_target_source_detection(self):
        context = WeaponryRetrievalContext(
            target_file_names={"JFS_3526-JFS_-16-Aug-2023.pdf"},
            target_doc_paths=set(),
        )
        self.assertTrue(_is_terms_source_name("term_rule_0005_中文型号.md"))
        self.assertFalse(_is_target_source("term_rule_0005_中文型号.md", context))
        self.assertTrue(_is_target_source("JFS_3526-JFS_-16-Aug-2023.pdf", context))
        self.assertTrue(_is_target_source("", context))

    def test_target_document_records_returns_all_documents_in_current_category(self):
        class FakeKB:
            def list_document_records(self):
                return [
                    {"file_name": "a.pdf", "architecture_id": 123},
                    {"file_name": "b.pdf", "architecture_id": 123},
                    {"file_name": "other.pdf", "architecture_id": 456},
                ]

        records = _target_document_records(FakeKB(), 123)

        self.assertEqual([record["file_name"] for record in records], ["a.pdf", "b.pdf"])

    def test_resolve_selected_documents_accepts_cross_category_and_preserves_order(self):
        class FakeKB:
            def list_document_records(self):
                return [
                    {
                        "file_name": "current.pdf",
                        "original_name": "当前类别资料.pdf",
                        "ingested_file_name": "current.pdf",
                        "architecture_id": 123,
                        "doc_path": "custom-documents/current.json",
                    },
                    {
                        "file_name": "other.pdf",
                        "original_name": "其他类别资料.pdf",
                        "ingested_file_name": "other.pdf",
                        "architecture_id": 456,
                        "doc_path": "custom-documents/other.json",
                    },
                ]

        documents = resolve_weaponry_selected_documents(
            FakeKB(),
            ["other.pdf", "current.pdf"],
        )

        self.assertEqual(
            [document.file_name for document in documents],
            ["other.pdf", "current.pdf"],
        )
        self.assertEqual(
            [document.source_architecture_id for document in documents],
            [456, 123],
        )

    def test_resolve_selected_documents_rejects_ambiguous_same_file_name(self):
        class FakeKB:
            def list_document_records(self):
                return [
                    {
                        "file_name": "same.pdf",
                        "ingested_file_name": "same-123.pdf",
                        "architecture_id": 123,
                        "doc_path": "custom-documents/same-123.json",
                    },
                    {
                        "file_name": "same.pdf",
                        "ingested_file_name": "same-456.pdf",
                        "architecture_id": 456,
                        "doc_path": "custom-documents/same-456.json",
                    },
                ]

        with self.assertRaisesRegex(WeaponrySelectedDocumentAmbiguityError, "无法唯一"):
            resolve_weaponry_selected_documents(FakeKB(), ["same.pdf"])

    def test_resolve_selected_documents_rejects_same_external_document_path(self):
        class FakeKB:
            def list_document_records(self):
                return [
                    {
                        "file_name": "first.pdf",
                        "ingested_file_name": "first.pdf",
                        "architecture_id": 123,
                        "doc_path": "custom-documents/shared.json",
                    },
                    {
                        "file_name": "second.pdf",
                        "ingested_file_name": "second.pdf",
                        "architecture_id": 456,
                        "doc_path": "custom-documents/shared.json",
                    },
                ]

        with self.assertRaisesRegex(WeaponrySelectedDocumentAmbiguityError, "同一知识库文档位置"):
            resolve_weaponry_selected_documents(FakeKB(), ["first.pdf", "second.pdf"])

    def test_resolve_selected_documents_rejects_unknown_file(self):
        class FakeKB:
            def list_document_records(self):
                return []

        with self.assertRaisesRegex(WeaponrySelectedDocumentNotFoundError, "尚未解析"):
            resolve_weaponry_selected_documents(FakeKB(), ["missing.pdf"])

    def test_target_document_records_reject_invalid_database_contract(self):
        """数据库实现若错误返回 None，应产生可诊断的契约异常而非迭代器异常。"""
        class InvalidKB:
            def list_document_records(self):
                return None

        with self.assertRaisesRegex(TypeError, "文档记录查询返回契约错误"):
            _target_document_records(InvalidKB(), 123)

    def test_resolve_original_source_name_for_mode2_callback(self):
        context = WeaponryRetrievalContext(
            target_file_names={"hash-name.pdf", "尼米兹级资料.pdf"},
            target_doc_paths={"custom-documents/hash-name-doc.json"},
            source_original_names={
                "hash-name.pdf": "尼米兹级资料.pdf",
                "custom-documents/hash-name-doc.json": "尼米兹级资料.pdf",
                "hash-name-doc.json": "尼米兹级资料.pdf",
            },
            source_file_names={
                "hash-name.pdf": "hash-name.pdf",
                "尼米兹级资料.pdf": "hash-name.pdf",
                "custom-documents/hash-name-doc.json": "hash-name.pdf",
                "hash-name-doc.json": "hash-name.pdf",
            },
            single_target_original_name="尼米兹级资料.pdf",
            single_target_file_name="hash-name.pdf",
        )

        self.assertEqual(
            _resolve_original_source_name("hash-name.pdf", context),
            "尼米兹级资料.pdf",
        )
        self.assertEqual(
            _resolve_original_source_name("custom-documents/hash-name-doc.json", context),
            "尼米兹级资料.pdf",
        )
        self.assertEqual(
            _resolve_original_source_name("", context),
            "尼米兹级资料.pdf",
        )
        self.assertEqual(
            _resolve_original_source_name("term_rule_0005_中文型号.md", context),
            "term_rule_0005_中文型号.md",
        )
        self.assertEqual(
            _resolve_hashed_source_name("尼米兹级资料.pdf", context),
            "hash-name.pdf",
        )
        self.assertEqual(
            _resolve_hashed_source_name("custom-documents/hash-name-doc.json", context),
            "hash-name.pdf",
        )
        self.assertEqual(_resolve_hashed_source_name("", context), "hash-name.pdf")

    @patch(
        "app.services.llm_service.weaponry_service._list_workspace_documents",
        return_value=[],
    )
    @patch(
        "app.services.llm_service.weaponry_service._upload_local_terms_if_needed",
        return_value=[],
    )
    @patch(
        "app.services.llm_service.weaponry_service._ensure_terms_workspace",
        return_value="terms-ws",
    )
    def test_prepare_context_uses_ingested_mhtml_pdf_name_for_source_mapping(
        self,
        _mock_terms_workspace,
        _mock_upload_terms,
        _mock_list_documents,
    ):
        """MHTML 转换后的 PDF 名必须映射回请求业务原始名，而非中间文件名。"""

        class FakeKB:
            def list_document_records(self):
                return [
                    {
                        "file_name": "e9a7f5.mhtml",
                        "original_name": "尼米兹级航母资料.mhtml",
                        "ingested_file_name": "e9a7f5.mhtml.normalized.pdf",
                        "architecture_id": 123,
                        "doc_path": "custom-documents/e9a7f5.json",
                        "anything_doc_id": "document-e9a7f5",
                    }
                ]

        context = _prepare_retrieval_context(
            object(),
            FakeKB(),
            123,
            "target-ws",
        )

        self.assertIn("e9a7f5.mhtml.normalized.pdf", context.target_file_names)
        self.assertTrue(
            _is_target_source("e9a7f5.mhtml.normalized.pdf", context),
        )
        self.assertEqual(
            _resolve_original_source_name(
                "e9a7f5.mhtml.normalized.pdf",
                context,
            ),
            "尼米兹级航母资料.mhtml",
        )
        self.assertEqual(
            _resolve_hashed_source_name(
                "e9a7f5.mhtml.normalized.pdf",
                context,
            ),
            "e9a7f5.mhtml",
        )

    def test_prepare_context_rejects_document_without_ingested_file_name(self):
        """开发期旧数据不得由 doc_path 或标题反推转换后的上传文件名。"""

        class FakeKB:
            def list_document_records(self):
                return [
                    {
                        "file_name": "e9a7f5.mhtml",
                        "original_name": "尼米兹级航母资料.mhtml",
                        "architecture_id": 123,
                        "doc_path": "custom-documents/e9a7f5.json",
                    }
                ]

        with self.assertRaisesRegex(ValueError, "实际上传文件名"):
            _prepare_retrieval_context(object(), FakeKB(), 123, "target-ws")

    @patch(
        "app.services.llm_service.weaponry_service._translate_if_needed",
        return_value="",
    )
    @patch.dict(
        os.environ,
        {
            "WEAPONRY_ANALYSE_MODE": "2",
            "WEAPONRY_TERMS_RULE_CONTEXT_ENABLED": "false",
        },
    )
    def test_query_input_field_returns_requested_original_name_for_mhtml_pdf_source(
        self,
        _mock_translate,
    ):
        """回调 source 必须严格使用 originalFileName，而不能泄露 MHTML 转换中间名。"""

        class FakeClient:
            def send_prompt_to_thread(
                self,
                _workspace_slug,
                _thread_slug,
                _prompt,
                user_id=1,
                mode="chat",
            ):
                return {"textResponse": "尼米兹级航空母舰"}

        context = WeaponryRetrievalContext(
            target_file_names={"e9a7f5.mhtml.normalized.pdf"},
            target_doc_paths=set(),
            source_original_names={
                "e9a7f5.mhtml.normalized.pdf": "尼米兹级航母资料.mhtml",
            },
            source_file_names={
                "e9a7f5.mhtml.normalized.pdf": "e9a7f5.mhtml",
            },
            single_target_original_name="尼米兹级航母资料.mhtml",
            single_target_file_name="e9a7f5.mhtml",
        )

        with patch(
            "app.services.llm_service.weaponry_service._vector_search_with_top_n",
            return_value=[
                {
                    "metadata": {"title": "e9a7f5.mhtml.normalized.pdf"},
                    "text": (
                        "<document_metadata>\n"
                        "sourceDocument: e9a7f5.mhtml.normalized.pdf\n"
                        "</document_metadata>\n"
                        "尼米兹级航空母舰"
                    ),
                    "score": 0.95,
                }
            ],
        ):
            result = _query_input_field(
                FakeClient(),
                "target-ws",
                "thread",
                {
                    "fieldName": "舰级名称",
                    "fieldType": "INPUT",
                    "fieldDescription": "提取舰级名称。",
                },
                retrieval_context=context,
            )

        data_source = result["analyseDataSource"][0]
        self.assertEqual(data_source["source"], "尼米兹级航母资料.mhtml")
        self.assertEqual(data_source["fileName"], "e9a7f5.mhtml")
        self.assertNotIn("normalized.pdf", data_source["source"])

    def test_format_terms_rule_context_uses_only_term_sources(self):
        chunks = [
            {
                "metadata": {"title": "target.pdf"},
                "text": "目标 PDF 正文",
                "score": 0.99,
            },
            {
                "metadata": {"title": "term_rule_0005_中文型号.md"},
                "text": "<document_metadata>\nsourceDocument: term_rule_0005_中文型号.md\n</document_metadata>\n中文型号规则",
                "score": 0.8,
            },
        ]
        context = _format_terms_rule_context(chunks)
        self.assertIn("term_rule_0005_中文型号.md", context)
        self.assertIn("中文型号规则", context)
        self.assertNotIn("目标 PDF 正文", context)

    @patch("app.services.llm_service.weaponry_service._translate_if_needed", return_value="")
    @patch.dict(os.environ, {"WEAPONRY_ANALYSE_MODE": "2", "WEAPONRY_TERMS_RULE_CONTEXT_ENABLED": "true"})
    def test_query_input_field_filters_terms_from_target_evidence(self, _mock_translate):
        calls = []

        class FakeClient:
            def __init__(self):
                self.prompts = []

            def send_prompt_to_thread(self, workspace_slug, thread_slug, prompt, user_id=1, mode="chat"):
                self.prompts.append(prompt)
                return {"textResponse": "Nimitz (CVN 68) class"}

        def fake_vector_search(_client, workspace_slug, query, *, top_n, user_id=1):
            calls.append((workspace_slug, top_n, query))
            if workspace_slug == "target-ws":
                return [
                    {
                        "metadata": {"title": "term_rule_0005_中文型号.md"},
                        "text": "<document_metadata>\nsourceDocument: term_rule_0005_中文型号.md\n</document_metadata>\n中文型号规则",
                        "score": 0.99,
                    },
                    {
                        "metadata": {"title": "JFS_3526-JFS_-16-Aug-2023.pdf"},
                        "text": "<document_metadata>\nsourceDocument: JFS_3526-JFS_-16-Aug-2023.pdf\n</document_metadata>\nNimitz (CVN 68) class",
                        "score": 0.1,
                    },
                    {
                        "metadata": {"title": "JFS_3526-JFS_-16-Aug-2023.pdf"},
                        "text": "<document_metadata>\nsourceDocument: JFS_3526-JFS_-16-Aug-2023.pdf\n</document_metadata>\nThe class serves in the United States Navy.",
                        "score": 0.08,
                    },
                ]
            return [
                {
                    "metadata": {"title": "term_rule_0005_中文型号.md"},
                    "text": "<document_metadata>\nsourceDocument: term_rule_0005_中文型号.md\n</document_metadata>\n中文型号规则",
                    "score": 0.9,
                }
            ]

        client = FakeClient()
        context = WeaponryRetrievalContext(
            target_file_names={"JFS_3526-JFS_-16-Aug-2023.pdf"},
            target_doc_paths=set(),
            source_original_names={
                "jfs_3526-jfs_-16-aug-2023.pdf": "尼米兹级资料.pdf",
            },
            source_file_names={
                "jfs_3526-jfs_-16-aug-2023.pdf": "3199b401658d49e781469534e8613913.pdf",
            },
            single_target_original_name="尼米兹级资料.pdf",
            single_target_file_name="3199b401658d49e781469534e8613913.pdf",
            terms_workspace_slug="terms-ws",
        )

        with patch("app.services.llm_service.weaponry_service._vector_search_with_top_n", side_effect=fake_vector_search):
            result = _query_input_field(
                client,
                "target-ws",
                "thread",
                {
                    "fieldName": "舰级名称",
                    "fieldType": "INPUT",
                    "fieldDescription": "提取舰级名称。",
                },
                retrieval_context=context,
            )

        self.assertEqual(result["analyseData"], "Nimitz (CVN 68) class")
        self.assertEqual(result["analyseDataSource"][0]["source"], "尼米兹级资料.pdf")
        self.assertEqual(
            result["analyseDataSource"][0]["fileName"],
            "3199b401658d49e781469534e8613913.pdf",
        )
        self.assertEqual(
            result["analyseDataSource"][0]["rows"],
            ["Nimitz (CVN 68) class", "The class serves in the United States Navy."],
        )
        self.assertNotIn("term_rule_0005_中文型号.md", result["analyseDataSource"][0]["source"])
        self.assertIn("辅助语境开始", client.prompts[0])
        self.assertIn("中文型号规则", client.prompts[0])
        self.assertEqual(calls[0][1], 8)
        self.assertIn("字段：舰级名称", calls[0][2])
        self.assertIn("语义说明：提取舰级名称。", calls[0][2])
        self.assertNotIn("未找到", calls[0][2])
        self.assertEqual(calls[1][1], 3)

    @patch("app.services.llm_service.weaponry_service._translate_if_needed", return_value="")
    @patch.dict(os.environ, {"WEAPONRY_ANALYSE_MODE": "2", "WEAPONRY_TERMS_RULE_CONTEXT_ENABLED": "false"})
    def test_query_input_field_skips_terms_rule_context_when_disabled(self, _mock_translate):
        calls = []

        class FakeClient:
            def __init__(self):
                self.prompts = []

            def send_prompt_to_thread(self, workspace_slug, thread_slug, prompt, user_id=1, mode="chat"):
                self.prompts.append(prompt)
                return {"textResponse": "Nimitz (CVN 68) class"}

        def fake_vector_search(_client, workspace_slug, query, *, top_n, user_id=1):
            calls.append((workspace_slug, top_n, query))
            if workspace_slug == "target-ws":
                return [
                    {
                        "metadata": {"title": "term_rule_0005_中文型号.md"},
                        "text": "<document_metadata>\nsourceDocument: term_rule_0005_中文型号.md\n</document_metadata>\n中文型号规则",
                        "score": 0.99,
                    },
                    {
                        "metadata": {"title": "JFS_3526-JFS_-16-Aug-2023.pdf"},
                        "text": "<document_metadata>\nsourceDocument: JFS_3526-JFS_-16-Aug-2023.pdf\n</document_metadata>\nNimitz (CVN 68) class",
                        "score": 0.1,
                    },
                ]
            self.fail(f"关闭术语规则辅助时不应检索术语 workspace: {workspace_slug}")

        client = FakeClient()
        context = WeaponryRetrievalContext(
            target_file_names={"JFS_3526-JFS_-16-Aug-2023.pdf"},
            target_doc_paths=set(),
            source_original_names={
                "jfs_3526-jfs_-16-aug-2023.pdf": "尼米兹级资料.pdf",
            },
            source_file_names={
                "jfs_3526-jfs_-16-aug-2023.pdf": "3199b401658d49e781469534e8613913.pdf",
            },
            single_target_original_name="尼米兹级资料.pdf",
            single_target_file_name="3199b401658d49e781469534e8613913.pdf",
            terms_workspace_slug="terms-ws",
        )

        with patch("app.services.llm_service.weaponry_service._vector_search_with_top_n", side_effect=fake_vector_search):
            result = _query_input_field(
                client,
                "target-ws",
                "thread",
                {
                    "fieldName": "舰级名称",
                    "fieldType": "INPUT",
                    "fieldDescription": "提取舰级名称。",
                },
                retrieval_context=context,
            )

        self.assertEqual(result["analyseData"], "Nimitz (CVN 68) class")
        self.assertEqual(result["analyseDataSource"][0]["source"], "尼米兹级资料.pdf")
        self.assertEqual(
            result["analyseDataSource"][0]["fileName"],
            "3199b401658d49e781469534e8613913.pdf",
        )
        self.assertEqual(result["analyseDataSource"][0]["rows"], ["Nimitz (CVN 68) class"])
        self.assertNotIn("term_rule_0005_中文型号.md", result["analyseDataSource"][0]["source"])
        self.assertNotIn("辅助语境开始", client.prompts[0])
        self.assertNotIn("中文型号规则", client.prompts[0])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "target-ws")
        self.assertEqual(calls[0][1], 8)

    @patch("app.services.llm_service.weaponry_service._list_workspace_documents")
    def test_prepare_context_moves_terms_out_of_target_workspace_and_restore(self, mock_list_docs):
        class FakeClient:
            def __init__(self):
                self.embedding_calls = []

            def ensure_workspace(self, name, user_id=1):
                return {"slug": "terms-ws", "name": name}

            def update_embeddings_batch(self, workspace_slug, adds=None, deletes=None, user_id=1):
                self.embedding_calls.append(
                    {
                        "workspace_slug": workspace_slug,
                        "adds": adds or [],
                        "deletes": deletes or [],
                    }
                )
                return True

        class FakeKB:
            def list_document_records(self):
                return [
                    {
                        "file_name": "JFS_3526-JFS_-16-Aug-2023.pdf",
                        "original_name": "JFS_3526-JFS_-16-Aug-2023.pdf",
                        "ingested_file_name": "JFS_3526-JFS_-16-Aug-2023.pdf",
                        "architecture_id": 123,
                        "doc_path": "custom-documents/JFS_3526.pdf.json",
                    }
                ]

        mock_list_docs.return_value = [
            {
                "title": "JFS_3526-JFS_-16-Aug-2023.pdf",
                "docpath": "custom-documents/JFS_3526.pdf.json",
            },
            {
                "title": "term_rule_0005_中文型号.md",
                "docpath": "custom-documents/term_rule_0005_中文型号.md.json",
            },
        ]

        client = FakeClient()
        context = _prepare_retrieval_context(client, FakeKB(), 123, "target-ws")

        self.assertEqual(context.terms_workspace_slug, "terms-ws")
        self.assertEqual(context.target_file_names, {"JFS_3526-JFS_-16-Aug-2023.pdf"})
        self.assertEqual(
            context.source_file_names["jfs_3526-jfs_-16-aug-2023.pdf"],
            "JFS_3526-JFS_-16-Aug-2023.pdf",
        )
        self.assertEqual(
            context.target_workspace_term_doc_paths,
            ["custom-documents/term_rule_0005_中文型号.md.json"],
        )
        self.assertEqual(client.embedding_calls[0]["workspace_slug"], "target-ws")
        self.assertEqual(client.embedding_calls[0]["deletes"], ["custom-documents/term_rule_0005_中文型号.md.json"])

        _restore_target_workspace_terms(client, "target-ws", context)
        self.assertEqual(client.embedding_calls[1]["workspace_slug"], "target-ws")
        self.assertEqual(client.embedding_calls[1]["adds"], ["custom-documents/term_rule_0005_中文型号.md.json"])

    @patch("app.services.llm_service.weaponry_service._list_workspace_documents")
    def test_ensure_terms_workspace_reuses_existing_and_adds_only_missing(self, mock_list_docs):
        class FakeClient:
            def __init__(self):
                self.embedding_calls = []

            def ensure_workspace(self, name, user_id=1):
                return {"slug": "terms-ws", "name": name}

            def update_embeddings_batch(self, workspace_slug, adds=None, deletes=None, user_id=1):
                self.embedding_calls.append(
                    {
                        "workspace_slug": workspace_slug,
                        "adds": adds or [],
                        "deletes": deletes or [],
                    }
                )
                return True

        mock_list_docs.return_value = [
            {
                "title": "term_rule_0001_国别.md",
                "docpath": "custom-documents/term_rule_0001_国别.md-old.json",
            }
        ]

        client = FakeClient()
        slug = _ensure_terms_workspace(
            client,
            [
                "custom-documents/term_rule_0001_国别.md-new.json",
                "custom-documents/term_rule_0002_军种.md-new.json",
            ],
        )

        self.assertEqual(slug, "terms-ws")
        self.assertEqual(len(client.embedding_calls), 1)
        self.assertEqual(client.embedding_calls[0]["workspace_slug"], "terms-ws")
        self.assertEqual(
            client.embedding_calls[0]["adds"],
            ["custom-documents/term_rule_0002_军种.md-new.json"],
        )


class TestWeaponryTableFieldExtraction(unittest.TestCase):
    """测试 TABLE 字段按整表抽取并组装多行结果。"""

    def test_count_query_fields_counts_table_as_single_task(self):
        fields = [
            {"fieldName": "舰名", "fieldType": "INPUT"},
            {
                "fieldName": "雷达配置",
                "fieldType": "TABLE",
                "tableFieldList": [
                    [
                        {"fieldName": "雷达名称", "fieldType": "INPUT"},
                        {"fieldName": "频段", "fieldType": "INPUT"},
                        {"fieldName": "探测距离", "fieldType": "INPUT"},
                    ]
                ],
            },
        ]

        self.assertEqual(_count_query_fields(fields), 2)

    def test_parse_table_json_rows_accepts_code_fenced_array(self):
        rows = _parse_table_json_rows(
            """```json
[
  {"__rowKey":"AN/SPY-1D","雷达名称":"AN/SPY-1D","频段":"S波段"},
  {"__rowKey":"AN/SPS-49","雷达名称":"AN/SPS-49","频段":"L波段"}
]
```""",
            [
                {"fieldName": "雷达名称"},
                {"fieldName": "频段"},
            ],
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["雷达名称"], "AN/SPY-1D")
        self.assertEqual(rows[0]["频段"], "S波段")
        self.assertEqual(rows[1]["雷达名称"], "AN/SPS-49")

    @patch("app.services.llm_service.weaponry_service._translate_if_needed", return_value="")
    @patch.dict(os.environ, {"WEAPONRY_TERMS_RULE_CONTEXT_ENABLED": "false"})
    def test_query_table_field_extracts_rows_in_one_table_prompt(self, _mock_translate):
        vector_calls = []
        progress_calls = []

        class FakeClient:
            def __init__(self):
                self.prompts = []
                self.thread_count = 0

            def create_thread(self, workspace_slug, thread_name, user_id=1):
                self.thread_count += 1
                return {"slug": f"table-thread-{self.thread_count}"}

            def delete_thread(self, workspace_slug, thread_slug, user_id=1):
                return True

            def send_prompt_to_thread(self, workspace_slug, thread_slug, prompt, user_id=1, mode="chat"):
                self.prompts.append(prompt)
                return {
                    "textResponse": (
                        "["
                        "{\"__rowKey\":\"AN/SPY-1D\",\"雷达名称\":\"AN/SPY-1D\",\"频段\":\"S波段\",\"探测距离\":\"约320公里\"},"
                        "{\"__rowKey\":\"AN/SPS-49\",\"雷达名称\":\"AN/SPS-49\",\"频段\":\"L波段\",\"探测距离\":\"约460公里\"}"
                        "]"
                    )
                }

        def fake_vector_search(_client, workspace_slug, query, *, top_n, user_id=1):
            vector_calls.append((workspace_slug, top_n, query))
            return [
                {
                    "metadata": {"title": "carrier-radars.pdf"},
                    "text": (
                        "<document_metadata>\n"
                        "sourceDocument: carrier-radars.pdf\n"
                        "</document_metadata>\n"
                        "The carrier carries AN/SPY-1D S-band radar and AN/SPS-49 L-band radar."
                    ),
                    "score": 0.95,
                }
            ]

        field = {
            "fieldName": "雷达配置",
            "fieldType": "TABLE",
            "fieldDescription": "提取航母装载的各型雷达及指标，每种雷达一行。",
            "tableFieldList": [
                [
                    {"fieldName": "雷达名称", "fieldType": "INPUT"},
                    {"fieldName": "频段", "fieldType": "INPUT"},
                    {"fieldName": "探测距离", "fieldType": "INPUT"},
                ]
            ],
        }
        context = WeaponryRetrievalContext(
            target_file_names={"carrier-radars.pdf"},
            target_doc_paths=set(),
            source_original_names={"carrier-radars.pdf": "航母雷达资料.pdf"},
            source_file_names={"carrier-radars.pdf": "3199b401658d49e781469534e8613913.pdf"},
            single_target_original_name="航母雷达资料.pdf",
            single_target_file_name="3199b401658d49e781469534e8613913.pdf",
        )

        with patch("app.services.llm_service.weaponry_service._vector_search_with_top_n", side_effect=fake_vector_search):
            result = _query_table_field(
                FakeClient(),
                "target-ws",
                "parent-thread",
                field,
                retrieval_context=context,
                on_cell_done=lambda: progress_calls.append(1),
            )

        self.assertEqual(len(vector_calls), 1)
        self.assertEqual(vector_calls[0][1], 16)
        self.assertEqual(len(progress_calls), 1)
        self.assertEqual(len(result["tableFieldList"]), 2)
        first_row = result["tableFieldList"][0]
        second_row = result["tableFieldList"][1]
        self.assertEqual(first_row[0]["analyseData"], "AN/SPY-1D")
        self.assertEqual(first_row[1]["analyseData"], "S波段")
        self.assertEqual(first_row[2]["analyseData"], "约320公里")
        self.assertEqual(first_row[0]["analyseDataSource"][0]["source"], "航母雷达资料.pdf")
        self.assertEqual(
            first_row[0]["analyseDataSource"][0]["fileName"],
            "3199b401658d49e781469534e8613913.pdf",
        )
        self.assertEqual(
            first_row[0]["analyseDataSource"][0]["rows"],
            ["The carrier carries AN/SPY-1D S-band radar and AN/SPS-49 L-band radar."],
        )
        self.assertEqual(second_row[0]["analyseData"], "AN/SPS-49")

    @patch("app.services.llm_service.weaponry_service._vector_search_with_top_n", return_value=[])
    def test_query_table_field_preserves_original_template_when_no_rows(self, _mock_vector_search):
        progress_calls = []
        original_table = [
            [
                {
                    "fieldName": "雷达名称",
                    "fieldType": "INPUT",
                    "analyseData": "",
                    "analyseDataSource": [],
                },
                {
                    "fieldName": "频段",
                    "fieldType": "INPUT",
                    "analyseData": "",
                    "analyseDataSource": [],
                },
            ]
        ]
        field = {
            "fieldName": "雷达配置",
            "fieldType": "TABLE",
            "fieldDescription": "提取雷达配置。",
            "tableFieldList": original_table,
        }

        result = _query_table_field(
            object(),
            "target-ws",
            "parent-thread",
            field,
            on_cell_done=lambda: progress_calls.append(1),
        )

        self.assertEqual(progress_calls, [1])
        self.assertEqual(result["tableFieldList"], original_table)

    @patch(
        "app.services.llm_service.weaponry_service._vector_search_with_top_n",
        return_value=[
            {
                "metadata": {"title": "carrier-radars.pdf"},
                "text": "The document does not contain a structured radar table.",
                "score": 0.75,
            }
        ],
    )
    def test_query_table_field_preserves_original_template_when_model_returns_empty_rows(
        self,
        _mock_vector_search,
    ):
        class FakeClient:
            def send_prompt_to_thread(
                self,
                workspace_slug,
                thread_slug,
                prompt,
                user_id=1,
                mode="chat",
            ):
                return {"textResponse": "[]"}

        original_table = [
            [
                {"fieldName": "雷达名称", "fieldType": "INPUT"},
                {"fieldName": "频段", "fieldType": "INPUT"},
            ]
        ]
        field = {
            "fieldName": "雷达配置",
            "fieldType": "TABLE",
            "fieldDescription": "提取雷达配置。",
            "tableFieldList": original_table,
        }

        result = _query_table_field(
            FakeClient(),
            "target-ws",
            "parent-thread",
            field,
        )

        self.assertEqual(result["tableFieldList"], original_table)


class TestWeaponrySelectedFilesTask(unittest.TestCase):
    @patch("app.services.llm_service.weaponry_service._query_input_field")
    @patch("app.services.llm_service.weaponry_service._prepare_retrieval_context")
    @patch("app.services.llm_service.weaponry_service.AnythingLLMClient")
    def test_selected_files_use_and_cleanup_temporary_workspace(
        self,
        MockClient,
        mock_prepare_context,
        mock_query_input,
    ):
        client = MockClient.return_value
        client.create_rag_workspace.return_value = {"slug": "selected-ws"}
        client.update_embeddings_batch.return_value = True
        client.create_thread.return_value = {"slug": "selected-thread"}
        client.extract_thread_slug.return_value = "selected-thread"
        client.delete_thread.return_value = True
        client.delete_workspace.return_value = True

        context = WeaponryRetrievalContext(
            target_file_names={"selected.pdf"},
            target_doc_paths={"custom-documents/selected.json"},
        )
        mock_prepare_context.return_value = context
        mock_query_input.return_value = {
            "fieldName": "舰级名称",
            "fieldType": "INPUT",
            "analyseData": "尼米兹级",
            "analyseDataSource": [],
        }

        kb_service = MagicMock()
        task_service = MagicMock()
        selected_document = WeaponrySelectedDocument(
            file_name="selected.pdf",
            original_name="跨分类选中文件.pdf",
            source_architecture_id=99999,
            doc_path="custom-documents/selected.json",
            anything_doc_id="selected-doc-id",
            ingested_file_name="selected.mhtml.normalized.pdf",
        )

        progress_hub = MagicMock()
        run_weaponry_task(
            task_service=task_service,
            kb_service=kb_service,
            progress_hub=progress_hub,
            request_payload={
                "businessType": "weaponry",
                "params": {
                    "architectureId": "00010502",
                    "filePathList": ["selected.pdf"],
                    "weaponryTemplateFieldList": [
                        {"fieldName": "舰级名称", "fieldType": "INPUT"}
                    ],
                },
            },
            callback_url="",
            callback_timeout=5.0,
            selected_documents=(selected_document,),
            execution_id="execution-selected",
        )

        client.create_rag_workspace.assert_called_once()
        client.update_embeddings_batch.assert_called_once_with(
            "selected-ws",
            adds=["custom-documents/selected.json"],
            user_id=1,
        )
        mock_prepare_context.assert_called_once_with(
            client,
            kb_service,
            10502,
            "selected-ws",
            user_id=1,
            selected_documents=(selected_document,),
        )
        # 遗留快照即使保存带前导零的兼容字符串，Worker 也必须访问规范业务键并
        # 生成数值型回调身份，不能产生与主接口/check-task 不同的第二份任务。
        for call in task_service.update_task_progress.call_args_list:
            self.assertEqual(("weaponry", "10502"), call.args[:2])
        callback_payload = task_service.mark_business_result.call_args.args[2]
        self.assertEqual(10502, callback_payload["data"]["architectureId"])
        for call in progress_hub.publish.call_args_list:
            self.assertEqual(("weaponry", "10502"), call.args[:2])
            self.assertEqual(10502, call.args[2]["data"]["architectureId"])
        kb_service.get_workspace_slug.assert_not_called()
        kb_service.list_document_records.assert_not_called()
        self.assertEqual(mock_query_input.call_args.args[1], "selected-ws")
        client.delete_workspace.assert_called_once_with("selected-ws", user_id=1)

    @patch("app.services.llm_service.weaponry_service.AnythingLLMClient")
    def test_selected_workspace_is_deleted_when_document_binding_fails(self, MockClient):
        """队列恢复指定范围任务时，必须按 execution_id 读取受理时的快照。"""
        client = MockClient.return_value
        client.create_rag_workspace.return_value = {"slug": "selected-ws"}
        client.update_embeddings_batch.return_value = False
        client.delete_workspace.return_value = True

        kb_service = MagicMock()
        task_service = MagicMock()
        task_service.get_weaponry_task_document_snapshots.return_value = [
            {
                "file_name": "selected.pdf",
                "original_name": "跨分类选中文件.pdf",
                "ingested_file_name": "selected.mhtml.normalized.pdf",
                "source_architecture_id": 99999,
                "doc_path": "custom-documents/selected.json",
                "anything_doc_id": "selected-doc-id",
            }
        ]

        run_weaponry_task(
            task_service=task_service,
            kb_service=kb_service,
            progress_hub=MagicMock(),
            request_payload={
                "businessType": "weaponry",
                "params": {
                    "architectureId": 10502,
                    "filePathList": ["selected.pdf"],
                    "weaponryTemplateFieldList": [
                        {"fieldName": "舰级名称", "fieldType": "INPUT"}
                    ],
                },
            },
            callback_url="",
            callback_timeout=5.0,
            execution_id="execution-selected",
        )

        task_service.get_weaponry_task_document_snapshots.assert_called_once_with(
            architecture_id=10502,
            execution_id="execution-selected",
        )
        task_service.get_task.assert_not_called()
        task_service.mark_business_result.assert_called_once()
        self.assertEqual(task_service.mark_business_result.call_args.kwargs["status"], "3")
        client.create_thread.assert_not_called()
        client.delete_workspace.assert_called_once_with("selected-ws", user_id=1)


if __name__ == "__main__":
    unittest.main()
