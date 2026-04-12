"""tests/test_weaponry_service.py — weaponry_service 核心映射函数的单元测试"""
import unittest
from unittest.mock import patch
from datetime import datetime

from app.services.llm_service.weaponry_service import (
    _strip_document_metadata,
    _map_source_to_analyse_data_source,
    _build_analyse_data_sources,
)


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


if __name__ == "__main__":
    unittest.main()
