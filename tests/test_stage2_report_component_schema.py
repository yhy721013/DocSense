"""阶段 2-4 Report Control 组件 Manifest 与原子安装验收。"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import unittest

from app.modules.report.adapters.sqlite import (
    REPORT_CONTROL_COMPONENT_NAME,
    bootstrap_report_task_control_database,
    load_report_control_manifest,
)
from app.modules.tasks.adapters.sqlite.schema import (
    canonical_manifest_json,
    component_manifest_fingerprint,
    component_schema_ddl,
    validate_task_control_schema,
)
from app.modules.tasks.adapters.sqlite import TaskControlBootstrapError
from tests import workspace_tempdir


class ReportControlComponentSchemaTests(unittest.TestCase):
    _CONTRACT_PATH = (
        Path(__file__).parent / "contracts" / "stage2_report_component_contract.json"
    )

    @staticmethod
    def _empty_database(path: Path) -> None:
        sqlite3.connect(path).close()

    def test_fresh_database_publishes_root_and_report_component_together(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            old_path = root / "old.sqlite3"
            target_path = root / "task-control-v2.sqlite3"
            self._empty_database(old_path)

            result = bootstrap_report_task_control_database(old_path, target_path)

            self.assertTrue(result.created)
            self.assertEqual(
                (REPORT_CONTROL_COMPONENT_NAME,),
                result.identity.registered_components,
            )
            connection = sqlite3.connect(target_path, isolation_level=None)
            try:
                connection.execute("PRAGMA foreign_keys = ON")
                identity = validate_task_control_schema(
                    connection,
                    known_components={
                        REPORT_CONTROL_COMPONENT_NAME: load_report_control_manifest()
                    },
                    required_components={REPORT_CONTROL_COMPONENT_NAME: 1},
                )
                self.assertEqual(result.identity, identity)
            finally:
                connection.close()
            self.assertFalse(any(root.glob("*.bootstrap-*.sqlite3*")))

    def test_existing_empty_root_is_upgraded_once_and_reopen_is_idempotent(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            old_path = root / "old.sqlite3"
            target_path = root / "task-control-v2.sqlite3"
            self._empty_database(old_path)
            from app.modules.tasks.adapters.sqlite import bootstrap_task_control_database

            root_only = bootstrap_task_control_database(old_path, target_path)
            upgraded = bootstrap_report_task_control_database(old_path, target_path)
            reopened = bootstrap_report_task_control_database(old_path, target_path)

            self.assertEqual((), root_only.identity.registered_components)
            self.assertFalse(upgraded.created)
            self.assertEqual(upgraded.identity, reopened.identity)
            self.assertEqual(
                (REPORT_CONTROL_COMPONENT_NAME,),
                upgraded.identity.registered_components,
            )

    def test_unregistered_partial_object_fails_without_repair(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            old_path = root / "old.sqlite3"
            target_path = root / "task-control-v2.sqlite3"
            self._empty_database(old_path)
            from app.modules.tasks.adapters.sqlite import bootstrap_task_control_database

            bootstrap_task_control_database(old_path, target_path)
            connection = sqlite3.connect(target_path)
            connection.execute(component_schema_ddl(load_report_control_manifest())[0])
            connection.commit()
            connection.close()

            with self.assertRaises(TaskControlBootstrapError) as raised:
                bootstrap_report_task_control_database(old_path, target_path)
            self.assertEqual("schema_table_list_drift", raised.exception.code)
            verification = sqlite3.connect(target_path)
            try:
                self.assertEqual(
                    0,
                    verification.execute(
                        "SELECT COUNT(*) FROM task_control_schema_components"
                    ).fetchone()[0],
                )
                self.assertIsNotNone(
                    verification.execute(
                        "SELECT 1 FROM sqlite_schema WHERE name = 'report_resource_records'"
                    ).fetchone()
                )
            finally:
                verification.close()

    def test_manifest_identity_matches_frozen_step3_contract(self) -> None:
        """防止无版本升级地修改组件 DDL，导致多实例看到不同数据库身份。"""

        contract = json.loads(self._CONTRACT_PATH.read_text(encoding="utf-8"))
        manifest = load_report_control_manifest()
        canonical_bytes = canonical_manifest_json(manifest).encode("utf-8")

        self.assertEqual(
            contract["component"]["canonicalUtf8Bytes"],
            len(canonical_bytes),
        )
        self.assertEqual(
            contract["component"]["fingerprint"],
            component_manifest_fingerprint(manifest),
        )


if __name__ == "__main__":
    unittest.main()
