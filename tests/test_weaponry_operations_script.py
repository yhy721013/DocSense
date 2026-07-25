"""武器谱内部运维命令验收。"""

from __future__ import annotations

import argparse
from contextlib import closing
import sqlite3
from pathlib import Path
import tempfile
import unittest

from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.weaponry.adapters import SQLiteWeaponryResourceStoreAdapter
from app.modules.weaponry.ports import (
    QuarantineWeaponryResources,
    RegisterWeaponryResource,
    WeaponryResourceKind,
    WeaponryResourceOwnership,
    WeaponryResourceRecord,
    WeaponryTrackedResource,
)
from scripts.manage_weaponry_operations import (
    build_parser,
    inspect_resources,
    resolve_resources,
)


class WeaponryOperationsScriptTests(unittest.TestCase):
    def test_resolve_and_inspect_resources_use_repository_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "tasks.sqlite3")
            store = SQLiteWeaponryResourceStoreAdapter(db_path)
            task_id = TaskId("operations-resource")
            record = store.create(
                WeaponryResourceRecord(
                    task_id=task_id,
                    business_ref=TaskBusinessRef("weaponry", "7"),
                )
            )
            record = store.register(
                RegisterWeaponryResource(
                    task_id,
                    WeaponryTrackedResource(
                        resource_id="workspace",
                        kind=WeaponryResourceKind.RETRIEVAL_SCOPE,
                        external_ref="private-external-ref",
                        ownership=WeaponryResourceOwnership.OWNED,
                        idempotency_key="private-key",
                    ),
                    record.version,
                )
            )
            store.quarantine(
                QuarantineWeaponryResources(
                    task_id=task_id,
                    expected_version=record.version,
                    error_code="cleanup_outcome_unknown",
                    reason="删除结果未知",
                )
            )
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute(
                    """
                    CREATE TABLE llm_task_executions (
                        execution_id TEXT PRIMARY KEY,
                        business_type TEXT NOT NULL,
                        execution_state TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO llm_task_executions VALUES (?, 'weaponry', 'failed')",
                    (task_id.value,),
                )
                connection.commit()

            resolved = resolve_resources(
                argparse.Namespace(
                    db_path=db_path,
                    task_id=task_id.value,
                    resolution="retry_cleanup",
                    operator="operator-001",
                    reason="已确认远端工作区仍存在",
                    external_state_confirmed=True,
                )
            )
            inspected = inspect_resources(
                argparse.Namespace(db_path=db_path, task_id=task_id.value)
            )

        self.assertEqual("cleanup_pending", resolved["state"])
        self.assertEqual(1, inspected["ownedResourceCount"])
        self.assertEqual(
            {"cleanup_pending": 1},
            inspected["ownedResourceStates"],
        )
        self.assertEqual("retry_cleanup", inspected["operatorAudits"][0]["action"])
        serialized = repr(inspected)
        self.assertNotIn("private-external-ref", serialized)
        self.assertNotIn("private-key", serialized)

    def test_mutating_commands_require_explicit_confirmation_flags(self) -> None:
        parser = build_parser()
        callback = parser.parse_args(
            [
                "release-callback",
                "--architecture-id",
                "0007",
                "--operator",
                "operator",
                "--reason",
                "manual-review",
            ]
        )
        resources = parser.parse_args(
            [
                "resolve-resources",
                "--task-id",
                "task-1",
                "--resolution",
                "confirmed_absent",
                "--operator",
                "operator",
                "--reason",
                "manual-review",
            ]
        )

        self.assertFalse(callback.worker_stopped_confirmed)
        self.assertFalse(resources.external_state_confirmed)


if __name__ == "__main__":
    unittest.main()
