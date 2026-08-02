"""知识谱系来源映射的纯应用层离线门禁。"""

from __future__ import annotations

import unittest

from app.modules.chat.application.source_mapper import (
    ChatSourceDocument,
    ChatSourceMapper,
    ChatSourceMappingError,
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


if __name__ == "__main__":
    unittest.main()
