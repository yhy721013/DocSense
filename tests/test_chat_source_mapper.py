"""知识谱系来源映射的纯应用层离线门禁。"""

from __future__ import annotations

import unittest

from app.modules.chat.application.source_mapper import (
    ChatSourceDocument,
    ChatSourceMapper,
    ChatSourceMappingError,
    sanitize_weaponry_source_content,
)
from app.modules.chat.ports.conversations import ChatSourceEvidence


class ChatSourceMapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.documents = (
            ChatSourceDocument("a.pdf", "原始 A.pdf", "docsense_ref:" + "a" * 32),
            ChatSourceDocument("b.pdf", "原始 B.pdf", "docsense_ref:" + "b" * 32),
        )

    def test_empty_sources_are_a_legal_success(self) -> None:
        self.assertEqual((), ChatSourceMapper.map_sources((), self.documents))

    def test_order_duplicates_and_lossless_content_are_preserved(self) -> None:
        content = "  首行\r\n第二行 Ω  "
        sources = (
            ChatSourceEvidence(content, self.documents[1].structured_source_key),
            ChatSourceEvidence("重复", self.documents[0].structured_source_key),
            ChatSourceEvidence("重复", self.documents[0].structured_source_key),
        )

        mapped = ChatSourceMapper.map_sources(sources, self.documents)

        self.assertEqual(("b.pdf", "a.pdf", "a.pdf"), tuple(x.file_name for x in mapped))
        self.assertEqual(content, mapped[0].content)
        self.assertEqual("原始 B.pdf", mapped[0].original_file_name)
        self.assertEqual(("重复", "重复"), tuple(x.content for x in mapped[1:]))

    def test_missing_out_of_scope_and_ambiguous_keys_fail_closed(self) -> None:
        invalid_sources = (
            ChatSourceEvidence("正文", ""),
            ChatSourceEvidence("正文", "docsense_ref:" + "c" * 32),
        )
        for source in invalid_sources:
            with self.subTest(source_key=source.structured_source_key):
                with self.assertRaises(ChatSourceMappingError):
                    ChatSourceMapper.map_sources((source,), self.documents)

        duplicate_documents = self.documents + (
            ChatSourceDocument("c.pdf", "原始 C.pdf", self.documents[0].structured_source_key),
        )
        with self.assertRaisesRegex(ChatSourceMappingError, "重复"):
            ChatSourceMapper.map_sources(
                (ChatSourceEvidence("正文", self.documents[0].structured_source_key),),
                duplicate_documents,
            )

    def test_empty_or_whitespace_only_content_fails_without_rewriting(self) -> None:
        for content in ("", " \r\n\t"):
            with self.subTest(content=repr(content)):
                with self.assertRaisesRegex(ChatSourceMappingError, "正文为空"):
                    ChatSourceMapper.map_sources(
                        (ChatSourceEvidence(content, self.documents[0].structured_source_key),),
                        self.documents,
                    )

    def test_mapper_removes_prefix_and_preserves_clean_body_exactly(self) -> None:
        """Mapper 输出必须可直接供 SSE 与 History 共用，不能再携带供应商包装。"""

        body = "  正文首行\r\n正文尾行 e\u0301  "
        raw = (
            "<document_metadata>\nsourceDocument: internal.pdf\n"
            "</document_metadata>\r\n\r\n"
            + body
        )

        mapped = ChatSourceMapper.map_sources(
            (ChatSourceEvidence(raw, self.documents[0].structured_source_key),),
            self.documents,
        )

        self.assertEqual(body, mapped[0].content)
        self.assertNotIn("<document_metadata>", mapped[0].content)

    def test_mapper_rejects_wrapper_only_and_malformed_prefix(self) -> None:
        """清洗后无业务正文或供应商包装不完整时，整组来源失败关闭。"""

        invalid_contents = (
            "<document_metadata>source: a</document_metadata>\n\n",
            "<document_metadata>source: a",
        )
        for content in invalid_contents:
            with self.subTest(content=content):
                with self.assertRaises(ChatSourceMappingError):
                    ChatSourceMapper.map_sources(
                        (
                            ChatSourceEvidence(
                                content,
                                self.documents[0].structured_source_key,
                            ),
                        ),
                        self.documents,
                    )


class WeaponrySourceContentSanitizerTests(unittest.TestCase):
    """冻结前置供应商 Metadata 的窄清洗规则。"""

    def test_complete_prefix_is_removed_without_rewriting_body(self) -> None:
        """删除包装和空白分隔行，但保留正文缩进、换行与 Unicode 码点。"""

        body = "  第一行\r\n第二行 e\u0301 与 é  "
        raw = (
            "\ufeff \r\n<DOCUMENT_METADATA>\r\n"
            "sourceDocument: 内部文件.pdf\r\n"
            "</DOCUMENT_METADATA>\r\n\t\r\n"
            + body
        )

        self.assertEqual(body, sanitize_weaponry_source_content(raw))

    def test_content_without_prefix_and_mid_body_tag_are_unchanged(self) -> None:
        """没有前置包装时绝不执行通用 strip 或正文内标签替换。"""

        samples = (
            "  普通正文\r\n尾部  ",
            "正文中的 <document_metadata>业务标签</document_metadata> 保持不变",
            "",
            " \r\n\t",
        )
        for sample in samples:
            with self.subTest(sample=repr(sample)):
                self.assertEqual(sample, sanitize_weaponry_source_content(sample))

    def test_malformed_or_repeated_prefix_fails_closed(self) -> None:
        """可能含供应商元数据但无法窄解析时，不允许原样公开。"""

        invalid_samples = (
            "<document_metadata>未闭合",
            "<document_metadata source=\"x\">值</document_metadata>正文",
            (
                "<document_metadata>第一层</document_metadata>\n"
                "<document_metadata>第二层</document_metadata>正文"
            ),
        )
        for sample in invalid_samples:
            with self.subTest(sample=sample):
                with self.assertRaises(ChatSourceMappingError):
                    sanitize_weaponry_source_content(sample)

    def test_wrapper_only_returns_empty_for_mapper_level_validation(self) -> None:
        """纯函数只负责清洗；清洗后空正文由既有 Mapper 非空门禁统一拒绝。"""

        self.assertEqual(
            "",
            sanitize_weaponry_source_content(
                "<document_metadata>sourceDocument: a.pdf</document_metadata>\r\n"
            ),
        )

    def test_non_string_content_is_rejected(self) -> None:
        """纯规则不使用通用字符串化掩盖供应商协议错误。"""

        with self.assertRaises(TypeError):
            sanitize_weaponry_source_content(123)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
