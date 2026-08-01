import hashlib
import io
import json
import os
import sqlite3
import stat
import unittest
from pathlib import Path
from unittest.mock import patch

from app.ports import KnowledgeOperationContext
from app.services.llm_service.knowledge_index_operation_service import (
    KnowledgeIndexOperationService,
)
from app.services.llm_service.task_service import LLMTaskService
from scripts import migrate_analysis_security as migration
from tests import workspace_tempdir
from tests.task_service_fixtures import seed_legacy_file_task


def _json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class AnalysisSecurityMigrationTests(unittest.TestCase):
    def _create_runtime_fixture(self, root: str):
        runtime_dir = Path(root).resolve()
        task_db = runtime_dir / "llm_tasks.sqlite3"
        knowledge_db = runtime_dir / "knowledge_base.sqlite3"

        request_payload = {
            # 历史行内嵌 businessType 可能缺失，DB 列已限定 file 业务。
            "params": [{"fileName": "demo.pdf", "secrets": ["公开"]}],
        }
        result_payload = {
            "businessType": "file",
            "data": {"fileName": "demo.pdf", "secrets": "公开"},
        }
        operation_metadata = {
            "attributes": {"channel": "公开渠道", "secrets": "公开"},
            "file_name": "demo.pdf",
        }
        audit_values = (
            '审计 Prompt 保留 "secrets"',
            '{"secrets":"模型原始响应"}',
            "trace-secrets-digest",
            '{"secrets":"attempt raw response"}',
        )
        with sqlite3.connect(task_db) as connection:
            connection.executescript(
                """
                CREATE TABLE llm_tasks (
                    business_type TEXT NOT NULL,
                    request_payload TEXT NOT NULL,
                    result_payload TEXT
                );
                CREATE TABLE knowledge_index_operations (
                    business_type TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );
                CREATE TABLE llm_interactions (
                    prompt TEXT,
                    response TEXT,
                    trace_digest TEXT
                );
                CREATE TABLE llm_interaction_attempts (
                    raw_response TEXT
                );
                """
            )
            connection.execute(
                "INSERT INTO llm_tasks VALUES ('file', ?, ?)",
                (_json_text(request_payload), _json_text(result_payload)),
            )
            connection.execute(
                "INSERT INTO knowledge_index_operations VALUES ('file', ?)",
                (_json_text(operation_metadata),),
            )
            connection.execute(
                "INSERT INTO llm_interactions VALUES (?, ?, ?)",
                audit_values[:3],
            )
            connection.execute(
                "INSERT INTO llm_interaction_attempts VALUES (?)",
                (audit_values[3],),
            )

        with sqlite3.connect(knowledge_db) as connection:
            connection.execute(
                "CREATE TABLE documents (metadata_json TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO documents VALUES (?)",
                (_json_text({"channel": "公开渠道", "secrets": "公开"}),),
            )

        callback_dir = runtime_dir / "callback"
        callback_dir.mkdir()
        history_callback = callback_dir / "file-demo.json"
        legacy_callback = runtime_dir / "call_back.json"
        callback_payload = {
            "businessType": "file",
            "data": {"fileName": "demo.pdf", "secrets": "公开"},
        }
        history_callback.write_text(
            json.dumps(callback_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        legacy_callback.write_text(
            json.dumps(callback_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        fixed_time_ns = 1_700_000_000_123_456_789
        os.chmod(history_callback, 0o640)
        os.utime(history_callback, ns=(fixed_time_ns, fixed_time_ns))
        # Windows/部分文件系统只保留 100ns 或更粗时间精度；迁移应保留文件系统
        # 实际接受的时间戳，而不是测试请求但底层无法表达的纳秒尾数。
        persisted_mtime_ns = history_callback.stat().st_mtime_ns

        return {
            "runtime": runtime_dir,
            "task_db": task_db,
            "knowledge_db": knowledge_db,
            "history_callback": history_callback,
            "legacy_callback": legacy_callback,
            "history_mtime_ns": persisted_mtime_ns,
            "audit_values": audit_values,
        }

    @staticmethod
    def _plan(paths):
        return migration.build_migration_plan(
            task_db_path=paths["task_db"],
            knowledge_db_path=paths["knowledge_db"],
            runtime_dir=paths["runtime"],
        )

    @staticmethod
    def _read_json_cell(database: Path, table: str, column: str):
        with sqlite3.connect(database) as connection:
            raw_value = connection.execute(
                f'SELECT "{column}" FROM "{table}" ORDER BY rowid LIMIT 1'
            ).fetchone()[0]
        return json.loads(raw_value)

    @staticmethod
    def _audit_snapshot(task_db: Path):
        with sqlite3.connect(task_db) as connection:
            interaction = connection.execute(
                "SELECT prompt, response, trace_digest FROM llm_interactions"
            ).fetchone()
            attempt = connection.execute(
                "SELECT raw_response FROM llm_interaction_attempts"
            ).fetchone()
        return (*interaction, attempt[0])

    def test_default_cli_dry_run_reports_all_targets_without_writing(self):
        with workspace_tempdir() as tmp:
            paths = self._create_runtime_fixture(tmp)
            stdout = io.StringIO()
            environment_before = dict(os.environ)

            with patch("sys.stdout", stdout):
                exit_code = migration.main(
                    ["--runtime-dir", str(paths["runtime"])]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(dict(os.environ), environment_before)
            summary = json.loads(stdout.getvalue())
            self.assertEqual(summary["mode"], "dry-run")
            self.assertEqual(summary["changedTargets"], 6)
            self.assertEqual(summary["renamedKeys"], 6)
            self.assertEqual(
                summary["targets"]["llm_tasks.request_payload"]["changedTargets"],
                1,
            )
            request = self._read_json_cell(
                paths["task_db"], "llm_tasks", "request_payload"
            )
            self.assertIn("secrets", request["params"][0])
            self.assertFalse((paths["runtime"] / "migration_backups").exists())
            self.assertEqual(
                self._audit_snapshot(paths["task_db"]),
                paths["audit_values"],
            )

    def test_apply_is_idempotent_and_preserves_callback_metadata_and_audit(self):
        with workspace_tempdir() as tmp:
            paths = self._create_runtime_fixture(tmp)
            plan = self._plan(paths)

            backup_dir = migration.apply_migration(plan, timestamp="20260710-apply")

            self.assertIsNotNone(backup_dir)
            request = self._read_json_cell(
                paths["task_db"], "llm_tasks", "request_payload"
            )
            result = self._read_json_cell(
                paths["task_db"], "llm_tasks", "result_payload"
            )
            operation = self._read_json_cell(
                paths["task_db"], "knowledge_index_operations", "metadata_json"
            )
            document = self._read_json_cell(
                paths["knowledge_db"], "documents", "metadata_json"
            )
            history = json.loads(paths["history_callback"].read_text(encoding="utf-8"))
            legacy = json.loads(paths["legacy_callback"].read_text(encoding="utf-8"))
            migrated_mappings = (
                (request["params"][0], ["公开"]),
                (result["data"], "公开"),
                (operation["attributes"], "公开"),
                (document, "公开"),
                (history["data"], "公开"),
                (legacy["data"], "公开"),
            )
            for mapping, expected_security in migrated_mappings:
                self.assertNotIn("secrets", mapping)
                self.assertEqual(mapping["security"], expected_security)

            callback_stat = paths["history_callback"].stat()
            # Windows 的 chmod 只映射只读位，无法表达 POSIX 0640；权限位精确保留
            # 只在 POSIX 平台断言，Windows 继续验证内容与纳秒级 mtime 不变。
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(callback_stat.st_mode), 0o640)
            self.assertEqual(callback_stat.st_mtime_ns, paths["history_mtime_ns"])
            self.assertEqual(
                self._audit_snapshot(paths["task_db"]),
                paths["audit_values"],
            )

            manifest = json.loads(
                (backup_dir / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "applied")
            self.assertEqual(len(manifest["databases"]), 2)
            self.assertEqual(len(manifest["callbackFiles"]), 2)
            for item in (*manifest["databases"], *manifest["callbackFiles"]):
                backup = Path(item["backup"])
                self.assertTrue(backup.is_file())
                self.assertEqual(item["backupSha256"], _sha256(backup))

            second_plan = self._plan(paths)
            self.assertEqual(second_plan.changed_targets, 0)
            self.assertEqual(second_plan.renamed_keys, 0)
            self.assertIsNone(
                migration.apply_migration(second_plan, timestamp="20260710-noop")
            )
            backups = list(
                (paths["runtime"] / "migration_backups").glob(
                    "analysis-security-*"
                )
            )
            self.assertEqual(len(backups), 1)

    def test_equal_double_keys_collapse_but_conflicting_values_abort(self):
        with workspace_tempdir() as tmp:
            paths = self._create_runtime_fixture(tmp)
            request = self._read_json_cell(
                paths["task_db"], "llm_tasks", "request_payload"
            )
            request["params"][0]["security"] = ["公开"]
            with sqlite3.connect(paths["task_db"]) as connection:
                connection.execute(
                    "UPDATE llm_tasks SET request_payload = ?",
                    (_json_text(request),),
                )

            migration.apply_migration(self._plan(paths), timestamp="equal")
            migrated = self._read_json_cell(
                paths["task_db"], "llm_tasks", "request_payload"
            )["params"][0]
            self.assertEqual(migrated["security"], ["公开"])
            self.assertNotIn("secrets", migrated)

        with workspace_tempdir() as tmp:
            paths = self._create_runtime_fixture(tmp)
            request = self._read_json_cell(
                paths["task_db"], "llm_tasks", "request_payload"
            )
            request["params"][0]["security"] = ["秘密"]
            with sqlite3.connect(paths["task_db"]) as connection:
                connection.execute(
                    "UPDATE llm_tasks SET request_payload = ?",
                    (_json_text(request),),
                )

            with self.assertRaises(migration.MigrationConflictError):
                self._plan(paths)
            self.assertFalse((paths["runtime"] / "migration_backups").exists())

    def test_non_standard_or_invalid_json_aborts_preflight(self):
        for invalid_json in (
            '{"businessType":"file","data":{"secrets":NaN}}',
            "{",
        ):
            with self.subTest(invalid_json=invalid_json):
                with workspace_tempdir() as tmp:
                    paths = self._create_runtime_fixture(tmp)
                    paths["history_callback"].write_text(
                        invalid_json,
                        encoding="utf-8",
                    )
                    with self.assertRaises(migration.MigrationError):
                        self._plan(paths)
                    self.assertFalse(
                        (paths["runtime"] / "migration_backups").exists()
                    )

    def test_apply_failure_restores_modified_databases_and_callback(self):
        with workspace_tempdir() as tmp:
            paths = self._create_runtime_fixture(tmp)
            plan = self._plan(paths)
            original_apply_callbacks = migration._apply_callback_changes

            def fail_after_first_callback(changes, *, applied_paths=None):
                first = list(changes)[:1]
                original_apply_callbacks(first, applied_paths=applied_paths)
                raise RuntimeError("forced callback failure")

            with patch.object(
                migration,
                "_apply_callback_changes",
                side_effect=fail_after_first_callback,
            ):
                with self.assertRaises(migration.MigrationError):
                    migration.apply_migration(plan, timestamp="rollback")

            request = self._read_json_cell(
                paths["task_db"], "llm_tasks", "request_payload"
            )
            result = self._read_json_cell(
                paths["task_db"], "llm_tasks", "result_payload"
            )
            history = json.loads(
                paths["history_callback"].read_text(encoding="utf-8")
            )
            self.assertIn("secrets", request["params"][0])
            self.assertIn("secrets", result["data"])
            self.assertIn("secrets", history["data"])
            manifest = json.loads(
                (
                    paths["runtime"]
                    / "migration_backups"
                    / "analysis-security-rollback"
                    / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "rolled_back")
            self.assertEqual(manifest["restoreErrors"], [])
            self.assertEqual(len(manifest["databases"]), 2)
            self.assertEqual(len(manifest["callbackFiles"]), 2)

    def test_post_commit_failure_restores_database_backups(self):
        with workspace_tempdir() as tmp:
            paths = self._create_runtime_fixture(tmp)
            plan = self._plan(paths)
            original_integrity_check = migration._check_database_integrity
            integrity_check_count = 0

            def fail_first_post_commit_check(path):
                nonlocal integrity_check_count
                integrity_check_count += 1
                # 前两次用于验证两份 DB 备份，第三次已在 commit 之后。
                if integrity_check_count == 3:
                    raise migration.MigrationError("forced post-commit failure")
                return original_integrity_check(path)

            with patch.object(
                migration,
                "_check_database_integrity",
                side_effect=fail_first_post_commit_check,
            ):
                with self.assertRaises(migration.MigrationError):
                    migration.apply_migration(plan, timestamp="backup-restore")

            request = self._read_json_cell(
                paths["task_db"], "llm_tasks", "request_payload"
            )
            operation = self._read_json_cell(
                paths["task_db"], "knowledge_index_operations", "metadata_json"
            )
            document = self._read_json_cell(
                paths["knowledge_db"], "documents", "metadata_json"
            )
            history = json.loads(
                paths["history_callback"].read_text(encoding="utf-8")
            )
            self.assertIn("secrets", request["params"][0])
            self.assertIn("secrets", operation["attributes"])
            self.assertIn("secrets", document)
            self.assertIn("secrets", history["data"])
            manifest = json.loads(
                (
                    paths["runtime"]
                    / "migration_backups"
                    / "analysis-security-backup-restore"
                    / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "rolled_back")
            self.assertEqual(manifest["restoreErrors"], [])

    def test_migrated_result_payload_is_persisted_for_current_recovery_readers(self):
        with workspace_tempdir() as tmp:
            runtime = Path(tmp).resolve()
            task_db = runtime / "llm_tasks.sqlite3"
            knowledge_db = runtime / "knowledge_base.sqlite3"
            task_service = LLMTaskService(str(task_db))
            seed_legacy_file_task(task_service,
                "replay.pdf",
                {
                    "businessType": "file",
                    "params": [
                        {"fileName": "replay.pdf", "secrets": ["公开"]}
                    ],
                },
            )
            task_service.mark_business_result(
                "file",
                "replay.pdf",
                {
                    "businessType": "file",
                    "data": {"fileName": "replay.pdf", "secrets": "公开"},
                },
                status="2",
            )
            with sqlite3.connect(knowledge_db) as connection:
                connection.execute(
                    "CREATE TABLE documents (metadata_json TEXT NOT NULL)"
                )

            plan = migration.build_migration_plan(
                task_db_path=task_db,
                knowledge_db_path=knowledge_db,
                runtime_dir=runtime,
            )
            migration.apply_migration(plan, timestamp="callback-replay")

            # 1G-5 已删除仅处理旧投影的直发恢复入口。安全迁移的责任是原子改写
            # 权威任务结果；当前 Analysis Recovery Source 会在具备 execution 的
            # 新任务上读取同一字段，二者的行为分别由各自专项测试覆盖。
            callback_payload = task_service.get_task(
                "file",
                "replay.pdf",
            )["result_payload"]
            self.assertEqual(callback_payload["data"]["security"], "公开")
            self.assertNotIn("secrets", callback_payload["data"])

    def test_migrated_operation_metadata_supports_idempotent_replay(self):
        with workspace_tempdir() as tmp:
            runtime = Path(tmp).resolve()
            task_db = runtime / "llm_tasks.sqlite3"
            knowledge_db = runtime / "knowledge_base.sqlite3"
            LLMTaskService(str(task_db))
            operation_service = KnowledgeIndexOperationService(str(task_db))
            first = operation_service.begin(
                collection_ref="collection:1",
                idempotency_key="file:demo.pdf",
                operation_context=KnowledgeOperationContext(
                    execution_id="execution-1",
                    business_type="file",
                    business_key="demo.pdf",
                ),
                source_kind="upload",
                source_digest="sha256:demo",
                metadata={
                    "attributes": {"channel": "公开渠道", "secrets": "公开"}
                },
            )
            with sqlite3.connect(knowledge_db) as connection:
                connection.execute(
                    "CREATE TABLE documents (metadata_json TEXT NOT NULL)"
                )

            plan = migration.build_migration_plan(
                task_db_path=task_db,
                knowledge_db_path=knowledge_db,
                runtime_dir=runtime,
            )
            migration.apply_migration(plan, timestamp="operation-replay")

            replayed = operation_service.begin(
                collection_ref="collection:1",
                idempotency_key="file:demo.pdf",
                operation_context=KnowledgeOperationContext(
                    execution_id="execution-2",
                    business_type="file",
                    business_key="demo.pdf",
                ),
                source_kind="upload",
                source_digest="sha256:demo",
                metadata={
                    "attributes": {"channel": "公开渠道", "security": "公开"}
                },
            )

            self.assertEqual(replayed.execution_id, first.execution_id)
            self.assertEqual(replayed.last_execution_id, "execution-2")
            self.assertEqual(replayed.metadata["attributes"]["security"], "公开")
            self.assertNotIn("secrets", replayed.metadata["attributes"])

    def test_same_database_path_is_rejected(self):
        with workspace_tempdir() as tmp:
            paths = self._create_runtime_fixture(tmp)
            with self.assertRaises(migration.MigrationError):
                migration.build_migration_plan(
                    task_db_path=paths["task_db"],
                    knowledge_db_path=paths["task_db"],
                    runtime_dir=paths["runtime"],
                )


if __name__ == "__main__":
    unittest.main()
