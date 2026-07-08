"""tests/test_weaponry_service.py — weaponry_service 核心映射函数的单元测试"""
import os
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.services.llm_service import weaponry_service as weaponry_service_module
from app.services.llm_service.weaponry_service import (
    WeaponryRetrievalContext,
    _count_query_fields,
    _strip_document_metadata,
    _map_source_to_analyse_data_source,
    _build_analyse_data_sources,
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


class TestMapSourceToAnalyseDataSource(unittest.TestCase):
    """测试 _map_source_to_analyse_data_source 字段映射。"""

    @patch("app.services.llm_service.weaponry_service._translate_if_needed", return_value="translated")
    def test_basic_mapping(self, mock_translate):
        source = {
            "text": (
                "<document_metadata>\n"
                "sourceDocument: test.pdf\n"
                "</document_metadata>\n\n"
                "实际的 chunk 正文"
            ),
            "score": 0.85,
            "metadata": {"title": "test.pdf"},
        }
        result = _map_source_to_analyse_data_source(source, text_response="LLM的回答")

        self.assertEqual(result["content"], "LLM的回答")
        self.assertEqual(result["source"], "实际的 chunk 正文")
        self.assertEqual(result["translate"], "translated")
        # time 应该是日期时间格式
        self.assertRegex(result["time"], r"\d{4}-\d{2}-\d{2}")

        # 翻译应基于清理后的 chunk text
        mock_translate.assert_called_once_with("实际的 chunk 正文")

    @patch("app.services.llm_service.weaponry_service._translate_if_needed", return_value="")
    def test_empty_source(self, mock_translate):
        result = _map_source_to_analyse_data_source({}, text_response="回答")
        self.assertEqual(result["content"], "回答")
        self.assertEqual(result["source"], "")


class TestBuildAnalyseDataSources(unittest.TestCase):
    """测试 _build_analyse_data_sources 排序和空值处理。"""

    @patch("app.services.llm_service.weaponry_service._translate_if_needed", return_value="")
    def test_sorted_by_score_descending(self, mock_translate):
        sources = [
            {"text": "chunk-low", "score": 0.3},
            {"text": "chunk-high", "score": 0.9},
            {"text": "chunk-mid", "score": 0.5},
        ]
        result = _build_analyse_data_sources(sources, text_response="回答")
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["source"], "chunk-high")
        self.assertEqual(result[1]["source"], "chunk-mid")
        self.assertEqual(result[2]["source"], "chunk-low")

    @patch("app.services.llm_service.weaponry_service._translate_if_needed", return_value="")
    def test_empty_sources_returns_empty_object(self, mock_translate):
        result = _build_analyse_data_sources([], text_response="回答")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["source"], "")
        self.assertEqual(result[0]["content"], "回答")

    @patch("app.services.llm_service.weaponry_service._translate_if_needed", return_value="")
    def test_strips_metadata_from_chunks(self, mock_translate):
        sources = [
            {
                "text": "<document_metadata>\nfoo\n</document_metadata>\n\n实际内容",
                "score": 0.8,
            },
        ]
        result = _build_analyse_data_sources(sources, text_response="回答")
        self.assertEqual(result[0]["source"], "实际内容")

    @patch("app.services.llm_service.weaponry_service._translate_if_needed", return_value="")
    def test_non_dict_items_skipped(self, mock_translate):
        sources = [{"text": "valid", "score": 0.5}, "not-a-dict", None]
        result = _build_analyse_data_sources(sources, text_response="回答")
        self.assertEqual(len(result), 1)


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

    def test_target_document_records_preserve_selected_file_order(self):
        class FakeKB:
            def list_document_records(self):
                return [
                    {"file_name": "a.pdf", "architecture_id": 123},
                    {"file_name": "b.pdf", "architecture_id": 123},
                    {"file_name": "other.pdf", "architecture_id": 456},
                ]

        records = _target_document_records(FakeKB(), 123, ["b.pdf", "a.pdf"])

        self.assertEqual([record["file_name"] for record in records], ["b.pdf", "a.pdf"])
        with self.assertRaisesRegex(ValueError, "不存在或不属于当前类别"):
            _target_document_records(FakeKB(), 123, ["other.pdf"])

    def test_target_document_records_reject_invalid_database_contract(self):
        """数据库实现若错误返回 None，应产生可诊断的契约异常而非迭代器异常。"""
        class InvalidKB:
            def list_document_records(self):
                return None

        with self.assertRaisesRegex(TypeError, "文档记录查询返回契约错误"):
            _target_document_records(InvalidKB(), 123, ["a.pdf"])

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
        self.assertIn("术语规则参考开始", client.prompts[0])
        self.assertIn("中文型号规则", client.prompts[0])
        self.assertEqual(calls[0][1], 8)
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
        self.assertNotIn("术语规则参考开始", client.prompts[0])
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
        kb_service.get_workspace_slug.return_value = "architecture-ws"
        kb_service.list_document_records.return_value = [
            {
                "file_name": "selected.pdf",
                "original_name": "选中文件.pdf",
                "architecture_id": 10502,
                "anything_doc_id": "selected-doc-id",
                "doc_path": "custom-documents/selected.json",
            },
            {
                "file_name": "unselected.pdf",
                "original_name": "未选文件.pdf",
                "architecture_id": 10502,
                "anything_doc_id": "unselected-doc-id",
                "doc_path": "custom-documents/unselected.json",
            },
        ]

        run_weaponry_task(
            task_service=MagicMock(),
            kb_service=kb_service,
            progress_hub=MagicMock(),
            request_payload={
                "businessType": "weaponry",
                "params": {
                    "architectureId": 10502,
                    "weaponryTemplateFieldList": [
                        {"fieldName": "舰级名称", "fieldType": "INPUT"}
                    ],
                },
            },
            callback_url="",
            callback_timeout=5.0,
            selected_file_names=["selected.pdf"],
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
            selected_file_names=["selected.pdf"],
        )
        self.assertEqual(mock_query_input.call_args.args[1], "selected-ws")
        client.delete_workspace.assert_called_once_with("selected-ws", user_id=1)

    @patch("app.services.llm_service.weaponry_service.AnythingLLMClient")
    def test_selected_workspace_is_deleted_when_document_binding_fails(self, MockClient):
        client = MockClient.return_value
        client.create_rag_workspace.return_value = {"slug": "selected-ws"}
        client.update_embeddings_batch.return_value = False
        client.delete_workspace.return_value = True

        kb_service = MagicMock()
        kb_service.get_workspace_slug.return_value = "architecture-ws"
        kb_service.list_document_records.return_value = [
            {
                "file_name": "selected.pdf",
                "original_name": "选中文件.pdf",
                "architecture_id": 10502,
                "anything_doc_id": "selected-doc-id",
                "doc_path": "custom-documents/selected.json",
            }
        ]
        task_service = MagicMock()

        run_weaponry_task(
            task_service=task_service,
            kb_service=kb_service,
            progress_hub=MagicMock(),
            request_payload={
                "businessType": "weaponry",
                "params": {
                    "architectureId": 10502,
                    "weaponryTemplateFieldList": [
                        {"fieldName": "舰级名称", "fieldType": "INPUT"}
                    ],
                },
            },
            callback_url="",
            callback_timeout=5.0,
            selected_file_names=["selected.pdf"],
        )

        task_service.mark_business_result.assert_called_once()
        self.assertEqual(task_service.mark_business_result.call_args.kwargs["status"], "3")
        client.create_thread.assert_not_called()
        client.delete_workspace.assert_called_once_with("selected-ws", user_id=1)


if __name__ == "__main__":
    unittest.main()
