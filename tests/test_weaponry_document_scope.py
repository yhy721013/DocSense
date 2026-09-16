"""阶段 1D-2 武器谱 Document Scope Port、SQLite Adapter 与严格 Fake 测试。"""

from __future__ import annotations

from pathlib import Path
import sqlite3
import unittest

from app.modules.weaponry.adapters import (
    DatabaseServiceWeaponryDocumentScopeAdapter,
)
from app.modules.weaponry.domain import (
    DOCUMENT_SCOPE_CATEGORY,
    DOCUMENT_SCOPE_EXPLICIT,
)
from app.modules.weaponry.ports import (
    WeaponryDocumentScopeAmbiguityError,
    WeaponryDocumentScopeError,
    WeaponryDocumentScopeIntegrityError,
    WeaponryDocumentScopeNotFoundError,
    WeaponryDocumentScopePort,
)
from app.services.core.database import DatabaseService
from tests import workspace_tempdir
from tests.fakes import FakeWeaponryDocumentScopePort


def _save_document(
    database: DatabaseService,
    *,
    file_name: str,
    architecture_id: int,
    doc_path: str,
    original_name: str | None = None,
    ingested_file_name: str | None = None,
    anything_doc_id: str | None = None,
) -> None:
    database.save_document_record(
        file_name=file_name,
        architecture_id=architecture_id,
        anything_doc_id=anything_doc_id or f"id-{file_name}",
        doc_path=doc_path,
        original_name=original_name or f"原始-{file_name}",
        ingested_file_name=ingested_file_name or f"parsed-{file_name}",
    )


class WeaponryDocumentScopeSQLiteTests(unittest.TestCase):
    def test_explicit_cross_category_scope_preserves_request_order_and_identity(self) -> None:
        with workspace_tempdir() as runtime_directory:
            database = DatabaseService(str(Path(runtime_directory) / "kb.sqlite3"))
            _save_document(
                database,
                file_name="a.pdf",
                architecture_id=10,
                doc_path="custom-documents/a.json",
            )
            _save_document(
                database,
                file_name="b.pdf",
                architecture_id=20,
                doc_path="custom-documents/b.json",
                original_name=" 甲方原始值-b.pdf ",
            )
            adapter = DatabaseServiceWeaponryDocumentScopeAdapter(database)

            scope = adapter.resolve(
                architecture_id=999,
                requested_file_names=("b.pdf", "a.pdf"),
            )
            repeated = adapter.resolve(
                architecture_id=999,
                requested_file_names=("b.pdf", "a.pdf"),
            )

        self.assertIsInstance(adapter, WeaponryDocumentScopePort)
        self.assertEqual(DOCUMENT_SCOPE_EXPLICIT, scope.mode)
        self.assertEqual(("b.pdf", "a.pdf"), scope.requested_file_names)
        self.assertEqual((1, 2), tuple(item.sequence_no for item in scope.documents))
        self.assertEqual(
            ("b.pdf", "a.pdf"),
            tuple(item.file_name for item in scope.documents),
        )
        self.assertEqual(
            (20, 10),
            tuple(item.source_architecture_id for item in scope.documents),
        )
        self.assertEqual(" 甲方原始值-b.pdf ", scope.documents[0].original_name)
        self.assertEqual(
            tuple(item.document_key for item in scope.documents),
            tuple(item.document_key for item in repeated.documents),
        )
        self.assertEqual(2, len({item.document_key for item in scope.documents}))

    def test_category_scope_has_repository_independent_stable_order_and_may_be_empty(self) -> None:
        with workspace_tempdir() as runtime_directory:
            database = DatabaseService(str(Path(runtime_directory) / "kb.sqlite3"))
            _save_document(
                database,
                file_name="z.pdf",
                architecture_id=42,
                doc_path="custom-documents/z.json",
            )
            _save_document(
                database,
                file_name="A.pdf",
                architecture_id=42,
                doc_path="custom-documents/a.json",
            )
            _save_document(
                database,
                file_name="other.pdf",
                architecture_id=43,
                doc_path="custom-documents/other.json",
            )
            adapter = DatabaseServiceWeaponryDocumentScopeAdapter(database)

            scope = adapter.resolve(
                architecture_id=42,
                requested_file_names=(),
            )
            empty = adapter.resolve(
                architecture_id=404,
                requested_file_names=(),
            )

        self.assertEqual(DOCUMENT_SCOPE_CATEGORY, scope.mode)
        self.assertEqual((), scope.requested_file_names)
        self.assertEqual(
            ("A.pdf", "z.pdf"),
            tuple(item.file_name for item in scope.documents),
        )
        self.assertEqual(DOCUMENT_SCOPE_CATEGORY, empty.mode)
        self.assertEqual((), empty.documents)

    def test_explicit_not_found_same_name_and_shared_external_ref_errors_are_exact(self) -> None:
        with workspace_tempdir() as runtime_directory:
            database = DatabaseService(str(Path(runtime_directory) / "kb.sqlite3"))
            _save_document(
                database,
                file_name="same.pdf",
                architecture_id=1,
                doc_path="custom-documents/same-1.json",
            )
            _save_document(
                database,
                file_name="same.pdf",
                architecture_id=2,
                doc_path="custom-documents/same-2.json",
            )
            _save_document(
                database,
                file_name="first.pdf",
                architecture_id=1,
                doc_path="custom-documents/shared.json",
            )
            _save_document(
                database,
                file_name="second.pdf",
                architecture_id=2,
                # 外观不同但规范化后是同一完整位置，受理阶段必须直接识别，不能等到
                # Worker 绑定 AnythingLLM 文档时才异步失败。
                doc_path="custom-documents//shared.json",
            )
            adapter = DatabaseServiceWeaponryDocumentScopeAdapter(database)

            with self.assertRaisesRegex(
                WeaponryDocumentScopeNotFoundError,
                "^文件 missing.pdf 尚未解析，无法用于知识谱系解析$",
            ):
                adapter.resolve(
                    architecture_id=1,
                    requested_file_names=("missing.pdf",),
                )
            with self.assertRaisesRegex(
                WeaponryDocumentScopeAmbiguityError,
                "^文件 same.pdf 在多个知识库分类中存在记录，无法唯一确定引用版本$",
            ):
                adapter.resolve(
                    architecture_id=1,
                    requested_file_names=("same.pdf",),
                )
            with self.assertRaisesRegex(
                WeaponryDocumentScopeAmbiguityError,
                "^选中文件指向同一知识库文档位置，无法唯一溯源$",
            ):
                adapter.resolve(
                    architecture_id=1,
                    requested_file_names=("first.pdf", "second.pdf"),
                )

    def test_invalid_classification_and_missing_external_location_use_approved_errors(self) -> None:
        with workspace_tempdir() as runtime_directory:
            path = str(Path(runtime_directory) / "kb.sqlite3")
            database = DatabaseService(path)
            with sqlite3.connect(path) as connection:
                connection.executemany(
                    """
                    INSERT INTO documents (
                        file_name, original_name, ingested_file_name,
                        architecture_id, anything_doc_id, doc_path, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, '{}')
                    """,
                    (
                        ("bad-category.pdf", "bad", "bad.pdf", 0, "bad", "ref",),
                        ("float-category.pdf", "float", "float.pdf", 1.5, "float", "float-ref",),
                        ("missing-ref.pdf", "missing", "missing.pdf", 1, "", None,),
                    ),
                )
            adapter = DatabaseServiceWeaponryDocumentScopeAdapter(database)

            with self.assertRaisesRegex(
                WeaponryDocumentScopeError,
                "^文件 bad-category.pdf 的知识库分类记录无效$",
            ):
                adapter.resolve(
                    architecture_id=1,
                    requested_file_names=("bad-category.pdf",),
                )
            with self.assertRaisesRegex(
                WeaponryDocumentScopeError,
                "^文件 missing-ref.pdf 缺少知识库文档位置$",
            ):
                adapter.resolve(
                    architecture_id=1,
                    requested_file_names=("missing-ref.pdf",),
                )
            with self.assertRaisesRegex(
                WeaponryDocumentScopeError,
                "^文件 float-category.pdf 的知识库分类记录无效$",
            ):
                adapter.resolve(
                    architecture_id=1,
                    requested_file_names=("float-category.pdf",),
                )

    def test_missing_ingested_name_is_infrastructure_integrity_failure_not_guessed(self) -> None:
        with workspace_tempdir() as runtime_directory:
            path = str(Path(runtime_directory) / "kb.sqlite3")
            database = DatabaseService(path)
            with sqlite3.connect(path) as connection:
                connection.execute(
                    """
                    INSERT INTO documents (
                        file_name, original_name, ingested_file_name,
                        architecture_id, anything_doc_id, doc_path, metadata_json
                    ) VALUES ('legacy.mhtml', '原始.mhtml', '', 1,
                              'legacy-id', 'custom-documents/legacy.json', '{}')
                    """
                )
            adapter = DatabaseServiceWeaponryDocumentScopeAdapter(database)

            with self.assertRaises(WeaponryDocumentScopeIntegrityError):
                adapter.resolve(
                    architecture_id=1,
                    requested_file_names=("legacy.mhtml",),
                )

    def test_resolution_is_read_only_and_never_writes_legacy_snapshot_table(self) -> None:
        with workspace_tempdir() as runtime_directory:
            path = str(Path(runtime_directory) / "kb.sqlite3")
            database = DatabaseService(path)
            _save_document(
                database,
                file_name="a.pdf",
                architecture_id=1,
                doc_path="custom-documents/a.json",
            )
            before = database.list_document_records()

            DatabaseServiceWeaponryDocumentScopeAdapter(database).resolve(
                architecture_id=1,
                requested_file_names=(),
            )

            after = database.list_document_records()
            with sqlite3.connect(path) as connection:
                legacy_table = connection.execute(
                    """
                    SELECT COUNT(*) FROM sqlite_master
                    WHERE type = 'table' AND name = 'weaponry_task_document_snapshots'
                    """
                ).fetchone()[0]

        self.assertEqual(before, after)
        self.assertEqual(0, legacy_table)


class WeaponryDocumentScopeFakeTests(unittest.TestCase):
    def test_fake_requires_explicit_configuration_and_records_exact_call(self) -> None:
        with workspace_tempdir() as runtime_directory:
            database = DatabaseService(str(Path(runtime_directory) / "kb.sqlite3"))
            _save_document(
                database,
                file_name="a.pdf",
                architecture_id=1,
                doc_path="custom-documents/a.json",
            )
            scope = DatabaseServiceWeaponryDocumentScopeAdapter(database).resolve(
                architecture_id=1,
                requested_file_names=("a.pdf",),
            )

        fake = FakeWeaponryDocumentScopePort()
        fake.scopes[(1, ("a.pdf",))] = scope

        self.assertIs(scope, fake.resolve(architecture_id=1, requested_file_names=("a.pdf",)))
        self.assertEqual([(1, ("a.pdf",))], fake.calls)
        with self.assertRaises(AssertionError):
            fake.resolve(architecture_id=2, requested_file_names=())

    def test_fake_propagates_configured_failure_without_fallback(self) -> None:
        fake = FakeWeaponryDocumentScopePort()
        error = WeaponryDocumentScopeNotFoundError("configured")
        fake.errors[(1, ("missing.pdf",))] = error

        with self.assertRaises(WeaponryDocumentScopeNotFoundError) as context:
            fake.resolve(
                architecture_id=1,
                requested_file_names=("missing.pdf",),
            )

        self.assertIs(error, context.exception)


if __name__ == "__main__":
    unittest.main()
