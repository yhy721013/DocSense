"""文件对话文档目录解析器的离线契约测试。"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from app.modules.chat import (
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
        source_key: str | None = None,
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
            metadata={
                "docSource": source_key
                or f"docsense_ref:{hashlib.sha256(document_id.encode()).hexdigest()[:32]}"
            },
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

    def test_architecture_resolver_returns_only_exact_direct_files(self) -> None:
        self._save_document(
            "beta.pdf",
            architecture_id=7,
            document_id="doc-beta",
        )
        self._save_document(
            "alpha.pdf",
            architecture_id=7,
            document_id="doc-alpha",
        )
        # 8 可代表树上的子类别或任意相邻类别；精确 SQL 不得把它展开进 7。
        self._save_document(
            "child.pdf",
            architecture_id=8,
            document_id="doc-child",
        )

        candidates = self.resolver.resolve_by_architecture_id(7)

        self.assertEqual("resolved", candidates.resolution_outcome)
        self.assertEqual(
            ("alpha.pdf", "beta.pdf"),
            tuple(document.file_name for document in candidates.documents),
        )
        self.assertFalse(
            any(
                document.file_name == "child.pdf"
                for document in candidates.documents
            )
        )

    def test_architecture_resolver_empty_catalog_is_bounded_not_found(self) -> None:
        candidates = self.resolver.resolve_by_architecture_id(7)

        self.assertEqual("not_found", candidates.resolution_outcome)
        self.assertEqual(
            "architecture_catalog_not_found",
            candidates.error_code,
        )
        self.assertEqual((), candidates.documents)

    def test_architecture_resolver_rejects_partial_or_duplicate_identity(self) -> None:
        cases = (
            [
                {
                    "file_name": "empty-original.pdf",
                    "original_name": "",
                    "anything_doc_id": "doc-empty-original",
                    "doc_path": "custom-documents/empty-original.json",
                    "metadata": {"docSource": "docsense_ref:00000000000000000000000000000001"},
                }
            ],
            [
                {
                    "file_name": "good.pdf",
                    "original_name": "good.pdf",
                    "anything_doc_id": "doc-good",
                    "doc_path": "custom-documents/good.json",
                    "metadata": {"docSource": "docsense_ref:00000000000000000000000000000002"},
                },
                {
                    "file_name": "broken.pdf",
                    "original_name": "broken.pdf",
                    "anything_doc_id": "",
                    "doc_path": "",
                    "metadata": {"docSource": "docsense_ref:00000000000000000000000000000003"},
                },
            ],
            [
                {
                    "file_name": "a.pdf",
                    "original_name": "a.pdf",
                    "anything_doc_id": "same",
                    "doc_path": "custom-documents/a.json",
                    "metadata": {"docSource": "docsense_ref:00000000000000000000000000000004"},
                },
                {
                    "file_name": "b.pdf",
                    "original_name": "b.pdf",
                    "anything_doc_id": "same",
                    "doc_path": "custom-documents/b.json",
                    "metadata": {"docSource": "docsense_ref:00000000000000000000000000000005"},
                },
            ],
            [
                {
                    "file_name": "a.pdf",
                    "original_name": "a.pdf",
                    "anything_doc_id": "doc-a",
                    "doc_path": "custom-documents/same.json",
                    "metadata": {"docSource": "docsense_ref:00000000000000000000000000000006"},
                },
                {
                    "file_name": "b.pdf",
                    "original_name": "b.pdf",
                    "anything_doc_id": "doc-b",
                    "doc_path": r"custom-documents\same.json",
                    "metadata": {"docSource": "docsense_ref:00000000000000000000000000000007"},
                },
            ],
        )
        for records in cases:
            with self.subTest(record_count=len(records)):
                knowledge_base = Mock(spec=DatabaseService)
                knowledge_base.list_document_records_by_architecture_id.return_value = (
                    records
                )
                resolver = DatabaseChatDocumentResolver(knowledge_base)

                candidates = resolver.resolve_by_architecture_id(7)

                self.assertEqual("invalid", candidates.resolution_outcome)
                self.assertEqual(
                    "architecture_catalog_invalid",
                    candidates.error_code,
                )
                self.assertEqual((), candidates.documents)

    def test_architecture_resolver_reads_catalog_once_without_full_scan(self) -> None:
        knowledge_base = Mock(spec=DatabaseService)
        knowledge_base.list_document_records_by_architecture_id.return_value = [
            {
                "file_name": "alpha.pdf",
                "original_name": "Alpha.pdf",
                "anything_doc_id": "doc-alpha",
                "doc_path": "custom-documents/alpha.json",
                "metadata": {"docSource": "docsense_ref:11111111111111111111111111111111"},
            }
        ]
        resolver = DatabaseChatDocumentResolver(knowledge_base)

        resolver.resolve_by_architecture_id(7)

        knowledge_base.list_document_records_by_architecture_id.assert_called_once_with(
            7,
            limit=21,
        )
        knowledge_base.list_document_records.assert_not_called()
        knowledge_base.get_document_record.assert_not_called()

    def test_architecture_resolver_strictly_rejects_original_name_and_source_key(self) -> None:
        """类别快照禁止原名回退，也禁止缺失、坏格式或重复的结构化来源键。"""
        base = {
            "file_name": "alpha.pdf",
            "original_name": "Alpha.pdf",
            "anything_doc_id": "doc-alpha",
            "doc_path": "custom-documents/alpha.json",
            "metadata": {"docSource": "docsense_ref:" + "a" * 32},
        }
        cases = []
        for original_name in (None, 7, "", "   "):
            record = dict(base)
            record["original_name"] = original_name
            cases.append([record])
        for metadata in (None, {}, {"docSource": 7}, {"docSource": "bad"}):
            record = dict(base)
            record["metadata"] = metadata
            cases.append([record])
        duplicate = dict(base)
        duplicate["file_name"] = "beta.pdf"
        duplicate["anything_doc_id"] = "doc-beta"
        duplicate["doc_path"] = "custom-documents/beta.json"
        cases.append([base, duplicate])

        for records in cases:
            with self.subTest(records=records):
                knowledge_base = Mock(spec=DatabaseService)
                knowledge_base.list_document_records_by_architecture_id.return_value = records
                candidates = DatabaseChatDocumentResolver(
                    knowledge_base
                ).resolve_by_architecture_id(7)
                self.assertEqual("invalid", candidates.resolution_outcome)
                self.assertEqual("architecture_catalog_invalid", candidates.error_code)

    def test_architecture_resolver_reads_only_limit_plus_one_candidates(
        self,
    ) -> None:
        """Resolver 只需多读一条，即可让受理层判断类别是否超过文件上限。"""
        for index, file_name in enumerate(
            ("delta.pdf", "alpha.pdf", "charlie.pdf", "beta.pdf"),
        ):
            self._save_document(
                file_name,
                architecture_id=7,
                document_id=f"doc-{index}",
            )
        resolver = DatabaseChatDocumentResolver(
            self.knowledge_base,
            architecture_candidate_limit=2,
        )

        candidates = resolver.resolve_by_architecture_id(7)

        self.assertEqual("resolved", candidates.resolution_outcome)
        self.assertEqual(
            ("alpha.pdf", "beta.pdf", "charlie.pdf"),
            tuple(item.file_name for item in candidates.documents),
        )

    def test_architecture_resolver_does_not_hide_database_execution_failure(self) -> None:
        knowledge_base = Mock(spec=DatabaseService)
        knowledge_base.list_document_records_by_architecture_id.side_effect = (
            sqlite3.OperationalError("forced read failure")
        )
        resolver = DatabaseChatDocumentResolver(knowledge_base)

        with self.assertRaises(sqlite3.OperationalError):
            resolver.resolve_by_architecture_id(7)


if __name__ == "__main__":
    unittest.main()
