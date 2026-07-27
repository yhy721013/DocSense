"""文件对话文档目录解析器的离线契约测试。"""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import Mock

from app.services.chat import (
    ChatDocumentCatalogConflictError,
    DatabaseChatDocumentResolver,
)
from app.services.core.database import DatabaseService


class DatabaseChatDocumentResolverTests(unittest.TestCase):
    """验证显式选择与全量目录选择共享、确定且严格的资格规则。"""

    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tempdir.__enter__()
        self.knowledge_base = DatabaseService(
            db_path=f"{self.tmp}/knowledge.sqlite3"
        )
        self.resolver = DatabaseChatDocumentResolver(self.knowledge_base)

    def tearDown(self) -> None:
        self._tempdir.__exit__(None, None, None)

    def _save_document(
        self,
        file_name: str,
        *,
        architecture_id: int,
        document_id: str,
        doc_path: str | None = None,
        original_name: str | None = None,
    ) -> None:
        self.knowledge_base.save_document_record(
            file_name=file_name,
            architecture_id=architecture_id,
            anything_doc_id=document_id,
            doc_path=(
                doc_path
                if doc_path is not None
                else f"custom-documents/{document_id}.json"
            ),
            original_name=original_name or f"{file_name}.original",
            ingested_file_name=f"{architecture_id}-{file_name}",
        )

    def test_empty_catalog_returns_empty_tuple(self) -> None:
        self.assertEqual((), self.resolver.resolve_all_available())

    def test_all_available_uses_database_stable_order(self) -> None:
        # 故意逆序写入，证明结果顺序来自目录查询契约，而不是偶然的插入顺序。
        self._save_document(
            "beta.pdf",
            architecture_id=2,
            document_id="doc-beta",
        )
        self._save_document(
            "alpha.pdf",
            architecture_id=3,
            document_id="doc-alpha",
            original_name="Alpha 原名.pdf",
        )

        resolved = self.resolver.resolve_all_available()

        self.assertEqual(
            ("alpha.pdf", "beta.pdf"),
            tuple(item.file_name for item in resolved),
        )
        self.assertEqual("Alpha 原名.pdf", resolved[0].original_name)
        self.assertEqual(
            "document:doc-alpha",
            resolved[0].document.document_ref,
        )
        self.assertEqual(
            "custom-documents/doc-alpha.json",
            resolved[0].document.external_location,
        )

    def test_explicit_and_all_available_share_record_conversion(self) -> None:
        self._save_document(
            "alpha.pdf",
            architecture_id=1,
            document_id="doc-alpha",
            doc_path="custom-documents/custom-alpha.json",
            original_name="Alpha 原名.pdf",
        )

        self.assertEqual(
            self.resolver.resolve_many(("alpha.pdf",)),
            self.resolver.resolve_all_available(),
        )

    def test_all_available_reads_catalog_once_without_per_file_queries(self) -> None:
        knowledge_base = Mock(spec=DatabaseService)
        knowledge_base.list_document_records.return_value = [
            {
                "file_name": "alpha.pdf",
                "original_name": "Alpha 原名.pdf",
                "anything_doc_id": "doc-alpha",
                "doc_path": "custom-documents/doc-alpha.json",
            },
            {
                "file_name": "beta.pdf",
                "original_name": "Beta 原名.pdf",
                "anything_doc_id": "doc-beta",
                "doc_path": "custom-documents/doc-beta.json",
            },
        ]
        resolver = DatabaseChatDocumentResolver(knowledge_base)

        resolved = resolver.resolve_all_available()

        self.assertEqual(2, len(resolved))
        knowledge_base.list_document_records.assert_called_once_with()
        knowledge_base.get_document_record.assert_not_called()

    def test_duplicate_business_file_name_rejects_whole_catalog(self) -> None:
        self._save_document(
            "same.pdf",
            architecture_id=1,
            document_id="doc-one",
        )
        self._save_document(
            "same.pdf",
            architecture_id=2,
            document_id="doc-two",
        )

        with self.assertRaisesRegex(
            ChatDocumentCatalogConflictError,
            "^全量文件范围存在重复fileName，无法用于对话$",
        ):
            self.resolver.resolve_all_available()

    def test_duplicate_document_ref_rejects_whole_catalog(self) -> None:
        self._save_document(
            "alpha.pdf",
            architecture_id=1,
            document_id="same-document",
            doc_path="custom-documents/alpha.json",
        )
        self._save_document(
            "beta.pdf",
            architecture_id=2,
            document_id="same-document",
            doc_path="custom-documents/beta.json",
        )

        with self.assertRaisesRegex(
            ChatDocumentCatalogConflictError,
            "^全量文件范围存在重复文档引用，无法用于对话$",
        ):
            self.resolver.resolve_all_available()

    def test_normalized_duplicate_location_rejects_whole_catalog(self) -> None:
        self._save_document(
            "alpha.pdf",
            architecture_id=1,
            document_id="doc-alpha",
            doc_path=r"custom-documents\same.json",
        )
        self._save_document(
            "beta.pdf",
            architecture_id=2,
            document_id="doc-beta",
            doc_path="custom-documents/same.json",
        )

        with self.assertRaisesRegex(
            ChatDocumentCatalogConflictError,
            "^全量文件范围存在重复文档引用，无法用于对话$",
        ):
            self.resolver.resolve_all_available()

    def test_malformed_record_is_not_silently_skipped(self) -> None:
        knowledge_base = Mock(spec=DatabaseService)
        knowledge_base.list_document_records.return_value = [
            {
                "file_name": "broken.pdf",
                "original_name": "损坏记录.pdf",
                "anything_doc_id": "",
                "doc_path": "",
            }
        ]
        resolver = DatabaseChatDocumentResolver(knowledge_base)

        with self.assertRaisesRegex(
            ValueError,
            "^文件 broken.pdf 缺少可用于对话的文档引用$",
        ):
            resolver.resolve_all_available()


if __name__ == "__main__":
    unittest.main()
