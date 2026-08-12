"""阶段 2-2 第 1 步：Task Control Schema 与 Bootstrap 验收。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from app.modules.tasks.adapters.process_guard import FileProcessSingletonGuard
from app.modules.tasks.adapters.sqlite.bootstrap import (
    TaskControlBootstrapError,
    bootstrap_task_control_database,
)
from app.modules.tasks.adapters.sqlite.schema import (
    APPLICATION_ID,
    ROOT_MANIFEST_FINGERPRINT,
    TaskControlSchemaError,
    USER_VERSION,
    create_root_schema,
    root_schema_ddl,
    validate_task_control_schema,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATABASE_CONTRACT_PATH = (
    PROJECT_ROOT
    / "app"
    / "modules"
    / "tasks"
    / "adapters"
    / "sqlite"
    / "database_contract.json"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _create_valid_database(path: Path) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        create_root_schema(
            connection,
            db_instance_uuid=str(uuid4()),
            created_at=_utc_now(),
        )
    finally:
        connection.close()


def _open_for_validation(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA query_only = ON")
    return connection


class TaskControlRootSchemaTests(unittest.TestCase):
    """验证 Manifest 生成、身份核验与实际结构防漂移门禁。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._template_directory = tempfile.TemporaryDirectory()
        cls.template_path = Path(cls._template_directory.name) / "valid.sqlite3"
        _create_valid_database(cls.template_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._template_directory.cleanup()

    def _copy_valid_database(self, root: Path) -> Path:
        path = root / "task-control.sqlite3"
        shutil.copyfile(self.template_path, path)
        return path

    def test_manifest_fingerprint_and_ddl_are_frozen(self) -> None:
        contract = json.loads(DATABASE_CONTRACT_PATH.read_text(encoding="utf-8"))
        root_contract = contract["schemaComposition"]["rootManifest"]
        self.assertEqual(root_contract["expectedFingerprint"], ROOT_MANIFEST_FINGERPRINT)
        ddl = "\n".join(root_schema_ddl()).upper()
        self.assertNotIn("IF NOT EXISTS", ddl)
        self.assertNotIn("AUTOINCREMENT", ddl)
        self.assertEqual(15, ddl.count("CREATE TABLE"))
        self.assertEqual(24, ddl.count("CREATE UNIQUE INDEX") + ddl.count("CREATE INDEX"))

    def test_fresh_root_schema_has_strict_identity_and_no_component(self) -> None:
        connection = _open_for_validation(self.template_path)
        try:
            identity = validate_task_control_schema(connection)
            self.assertEqual(ROOT_MANIFEST_FINGERPRINT, identity.root_fingerprint)
            self.assertEqual((), identity.registered_components)
            self.assertEqual(
                APPLICATION_ID,
                connection.execute("PRAGMA application_id").fetchone()[0],
            )
            self.assertEqual(
                USER_VERSION,
                connection.execute("PRAGMA user_version").fetchone()[0],
            )
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM task_control_schema_metadata"
                ).fetchone()[0],
            )
        finally:
            connection.close()

    def test_metadata_fingerprint_drift_is_rejected_without_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._copy_valid_database(Path(directory))
            connection = sqlite3.connect(path)
            connection.execute(
                "UPDATE task_control_schema_metadata SET schema_fingerprint = ?",
                ("A" * 64,),
            )
            connection.commit()
            connection.close()

            validation = _open_for_validation(path)
            try:
                with self.assertRaisesRegex(TaskControlSchemaError, "fingerprint") as raised:
                    validate_task_control_schema(validation)
                self.assertEqual("database_root_identity_mismatch", raised.exception.code)
            finally:
                validation.close()
            verification = sqlite3.connect(path)
            try:
                self.assertEqual(
                    "A" * 64,
                    verification.execute(
                        "SELECT schema_fingerprint FROM task_control_schema_metadata"
                    ).fetchone()[0],
                )
            finally:
                verification.close()

    def test_unknown_component_and_unregistered_object_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._copy_valid_database(root)
            connection = sqlite3.connect(path)
            connection.execute(
                """
                INSERT INTO task_control_schema_components (
                    component_name, component_version, root_schema_generation,
                    schema_fingerprint, manifest_profile, installed_at
                ) VALUES ('future_control', 1, 2, ?, 'canonical_json_v1', ?)
                """,
                ("B" * 64, _utc_now()),
            )
            connection.commit()
            connection.close()
            validation = _open_for_validation(path)
            try:
                with self.assertRaises(TaskControlSchemaError) as raised:
                    validate_task_control_schema(validation)
                self.assertEqual("database_unknown_component", raised.exception.code)
            finally:
                validation.close()

            path = root / "object-drift.sqlite3"
            shutil.copyfile(self.template_path, path)
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE unregistered_control_fact (id INTEGER)")
            connection.commit()
            connection.close()
            validation = _open_for_validation(path)
            try:
                with self.assertRaises(TaskControlSchemaError) as raised:
                    validate_task_control_schema(validation)
                self.assertEqual("schema_table_list_drift", raised.exception.code)
            finally:
                validation.close()

    def test_check_partial_predicate_and_fk_deferrability_are_verified(self) -> None:
        mutations = (
            (
                "check.sqlite3",
                "llm_task_executions",
                "progress >= 0 AND progress <= 1",
                "progress >= 0 AND progress <= 2",
                "schema_check_drift",
            ),
            (
                "partial.sqlite3",
                "idx_task_recovery_decisions_single_close",
                "closes_case = 1",
                "closes_case = 0",
                "schema_index_predicate_drift",
            ),
            (
                "deferrable.sqlite3",
                "llm_tasks",
                "NOT DEFERRABLE",
                "DEFERRABLE INITIALLY DEFERRED",
                "schema_foreign_key_deferrability_drift",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for filename, object_name, old, new, expected_code in mutations:
                with self.subTest(object_name=object_name):
                    path = root / filename
                    shutil.copyfile(self.template_path, path)
                    connection = sqlite3.connect(path, isolation_level=None)
                    connection.execute("PRAGMA writable_schema = ON")
                    connection.execute(
                        "UPDATE sqlite_schema SET sql = replace(sql, ?, ?) WHERE name = ?",
                        (old, new, object_name),
                    )
                    connection.execute("PRAGMA schema_version = 99")
                    connection.execute("PRAGMA writable_schema = OFF")
                    connection.close()
                    validation = _open_for_validation(path)
                    try:
                        with self.assertRaises(TaskControlSchemaError) as raised:
                            validate_task_control_schema(validation)
                        self.assertEqual(expected_code, raised.exception.code)
                    finally:
                        validation.close()

    def test_missing_index_and_forbidden_trigger_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._copy_valid_database(root)
            connection = sqlite3.connect(path)
            connection.execute("DROP INDEX idx_task_events_type_created")
            connection.commit()
            connection.close()
            validation = _open_for_validation(path)
            try:
                with self.assertRaises(TaskControlSchemaError) as raised:
                    validate_task_control_schema(validation)
                self.assertEqual("schema_object_union_drift", raised.exception.code)
            finally:
                validation.close()

            path = root / "sqlite-sequence.sqlite3"
            shutil.copyfile(self.template_path, path)
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE temporary_autoincrement (id INTEGER PRIMARY KEY AUTOINCREMENT)"
            )
            connection.execute("DROP TABLE temporary_autoincrement")
            connection.commit()
            connection.close()
            validation = _open_for_validation(path)
            try:
                with self.assertRaises(TaskControlSchemaError) as raised:
                    validate_task_control_schema(validation)
                self.assertEqual(
                    "schema_forbidden_internal_object",
                    raised.exception.code,
                )
            finally:
                validation.close()

            path = root / "trigger.sqlite3"
            shutil.copyfile(self.template_path, path)
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TRIGGER forbidden_trigger AFTER UPDATE ON llm_tasks BEGIN SELECT 1; END"
            )
            connection.commit()
            connection.close()
            validation = _open_for_validation(path)
            try:
                with self.assertRaises(TaskControlSchemaError) as raised:
                    validate_task_control_schema(validation)
                self.assertEqual("schema_object_union_drift", raised.exception.code)
            finally:
                validation.close()


class TaskControlBootstrapTests(unittest.TestCase):
    """验证旧库门禁、文件集保护、不覆盖发布和重复严格打开。"""

    @staticmethod
    def _create_empty_old_database(path: Path) -> None:
        sqlite3.connect(path).close()

    def test_absent_database_is_published_once_and_reopen_is_read_only_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_path = root / "old.sqlite3"
            new_path = root / "new.sqlite3"
            self._create_empty_old_database(old_path)

            first = bootstrap_task_control_database(old_path, new_path)
            second = bootstrap_task_control_database(old_path, new_path)

            self.assertTrue(first.created)
            self.assertFalse(second.created)
            self.assertEqual(first.identity, second.identity)
            self.assertEqual(new_path.resolve(), first.database_path)
            self.assertFalse(Path(f"{new_path}-wal").exists())
            self.assertFalse(Path(f"{new_path}-shm").exists())
            self.assertFalse(Path(f"{new_path}-journal").exists())
            self.assertEqual([], list(root.glob("*.bootstrap-*.sqlite3*")))

    def test_same_path_and_residual_sidecar_are_rejected_without_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_path = root / "old.sqlite3"
            self._create_empty_old_database(old_path)
            with self.assertRaises(TaskControlBootstrapError) as raised:
                bootstrap_task_control_database(old_path, old_path)
            self.assertEqual("database_path_conflict", raised.exception.code)

            new_path = root / "new.sqlite3"
            residual = Path(f"{new_path}-wal")
            residual.write_bytes(b"owned-by-unknown-process")
            with self.assertRaises(TaskControlBootstrapError) as raised:
                bootstrap_task_control_database(old_path, new_path)
            self.assertEqual("target_file_set_conflict", raised.exception.code)
            self.assertEqual(b"owned-by-unknown-process", residual.read_bytes())
            self.assertFalse(new_path.exists())

    def test_old_active_fact_blocks_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_path = root / "old.sqlite3"
            connection = sqlite3.connect(old_path)
            connection.execute(
                """
                CREATE TABLE llm_tasks (
                    business_type TEXT,
                    status TEXT,
                    callback_status TEXT
                )
                """
            )
            connection.execute(
                "INSERT INTO llm_tasks VALUES ('file', '0', 'pending')"
            )
            connection.commit()
            connection.close()
            new_path = root / "new.sqlite3"

            with self.assertRaises(TaskControlBootstrapError) as raised:
                bootstrap_task_control_database(old_path, new_path)
            self.assertEqual("legacy_preflight_blocked", raised.exception.code)
            self.assertFalse(new_path.exists())

    def test_existing_identity_drift_is_rejected_and_not_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_path = root / "old.sqlite3"
            new_path = root / "new.sqlite3"
            self._create_empty_old_database(old_path)
            _create_valid_database(new_path)
            connection = sqlite3.connect(new_path)
            connection.execute("PRAGMA application_id = 123")
            connection.close()

            with self.assertRaises(TaskControlBootstrapError) as raised:
                bootstrap_task_control_database(old_path, new_path)
            self.assertEqual("database_application_id_mismatch", raised.exception.code)
            connection = sqlite3.connect(new_path)
            try:
                self.assertEqual(123, connection.execute("PRAGMA application_id").fetchone()[0])
            finally:
                connection.close()

    def test_schema_lock_contention_fails_before_target_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_path = root / "old.sqlite3"
            new_path = root / "new.sqlite3"
            self._create_empty_old_database(old_path)
            guard = FileProcessSingletonGuard(
                new_path.with_name(f"{new_path.name}.schema.lock"),
                component_name="测试占用者",
            )
            self.assertTrue(guard.acquire())
            try:
                with self.assertLogs(
                    "app.modules.tasks.adapters.sqlite.bootstrap",
                    level="ERROR",
                ) as captured:
                    with self.assertRaises(TaskControlBootstrapError) as raised:
                        bootstrap_task_control_database(old_path, new_path)
                self.assertEqual("bootstrap_schema_lock_busy", raised.exception.code)
                self.assertFalse(new_path.exists())
                messages = "\n".join(captured.output)
                self.assertIn("sha256:", messages)
                self.assertNotIn(str(root), messages)
            finally:
                guard.release()


if __name__ == "__main__":
    unittest.main()
