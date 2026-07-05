"""本地知识库映射数据库的离线一致性测试。"""

from __future__ import annotations

import json
import sqlite3
import unittest

from app.services.core.database import DatabaseService
from tests import workspace_tempdir


class DatabaseServiceTests(unittest.TestCase):
    """验证向前兼容迁移、显式 UPSERT 和冲突检测。"""

    def test_document_upsert_preserves_row_and_replaces_metadata_atomically(self):
        """同名文档更新应修改原行，而不是使用 REPLACE 删除后重建。"""
        with workspace_tempdir() as tmp:
            service = DatabaseService(db_path=f"{tmp}/knowledge.sqlite3")
            service.save_document_record(
                "demo.pdf",
                100,
                "document-1",
                doc_path="external-1",
                metadata={"country": "美国"},
            )
            service.save_document_record(
                "demo.pdf",
                101,
                "document-2",
                doc_path="external-2",
                metadata={"country": "中国"},
            )

            record = service.get_document_record("demo.pdf")
            self.assertEqual(record["architecture_id"], 101)
            self.assertEqual(record["anything_doc_id"], "document-2")
            self.assertEqual(record["metadata"], {"country": "中国"})

    def test_invalid_historical_metadata_json_is_not_silently_hidden(self):
        """损坏的历史元数据必须可观察地失败，不能回退成空对象。"""
        with workspace_tempdir() as tmp:
            db_path = f"{tmp}/knowledge.sqlite3"
            service = DatabaseService(db_path=db_path)
            service.save_document_record("demo.pdf", 100, "document-1")
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE documents SET metadata_json = '[]' WHERE file_name = ?",
                    ("demo.pdf",),
                )

            with self.assertRaisesRegex(ValueError, "必须是 JSON 对象"):
                service.get_document_record("demo.pdf")

    def test_metadata_input_must_be_mapping_and_strict_json(self):
        """非映射值和 NaN 不得被静默转换成无法跨实现复现的元数据。"""
        with workspace_tempdir() as tmp:
            service = DatabaseService(db_path=f"{tmp}/knowledge.sqlite3")
            with self.assertRaises(TypeError):
                service.save_document_record(
                    "list.pdf",
                    100,
                    "document-list",
                    metadata=[],  # type: ignore[arg-type]
                )
            with self.assertRaises(ValueError):
                service.save_document_record(
                    "nan.pdf",
                    100,
                    "document-nan",
                    metadata={"score": float("nan")},
                )

    def test_workspace_mapping_is_idempotent_but_rejects_conflict(self):
        """相同映射可重复提交，任一侧映射到其他值时必须明确失败。"""
        with workspace_tempdir() as tmp:
            service = DatabaseService(db_path=f"{tmp}/knowledge.sqlite3")
            service.add_workspace(100, "architecture-100")
            service.add_workspace(100, "architecture-100")

            with self.assertRaisesRegex(ValueError, "工作区映射冲突"):
                service.add_workspace(100, "architecture-other")
            with self.assertRaisesRegex(ValueError, "工作区映射冲突"):
                service.add_workspace(101, "architecture-100")

    def test_existing_documents_table_is_migrated_with_empty_metadata_object(self):
        """旧数据库初始化后应新增 metadata_json，且历史行读取为空对象。"""
        with workspace_tempdir() as tmp:
            db_path = f"{tmp}/knowledge.sqlite3"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE documents (
                        file_name TEXT PRIMARY KEY,
                        original_name TEXT NOT NULL DEFAULT '',
                        architecture_id INTEGER NOT NULL,
                        anything_doc_id TEXT NOT NULL,
                        doc_path TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO documents (
                        file_name, original_name, architecture_id,
                        anything_doc_id, doc_path
                    ) VALUES ('legacy.pdf', 'legacy.pdf', 1, 'doc-1', 'external-1')
                    """
                )

            service = DatabaseService(db_path=db_path)
            record = service.get_document_record("legacy.pdf")
            self.assertEqual(record["metadata"], {})
            with sqlite3.connect(db_path) as conn:
                raw_metadata = conn.execute(
                    "SELECT metadata_json FROM documents WHERE file_name = 'legacy.pdf'"
                ).fetchone()[0]
            self.assertEqual(json.loads(raw_metadata), {})


if __name__ == "__main__":
    unittest.main()
