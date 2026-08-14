"""武器谱内部运维命令验收。"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
import tempfile
import unittest

from app.modules.tasks.adapters.sqlite import (
    SQLiteConnectionFactory,
    SQLiteTaskControlStore,
    SQLiteTransactionManager,
    build_sqlite_task_control_uow_factories,
)
from app.modules.tasks.domain import (
    TaskBusinessRef,
    TaskId,
    TaskOwnerIdentity,
    TaskTransition,
)
from app.modules.tasks.ports import (
    TaskAdmissionOutcome,
    TaskAdmissionRequest,
    TaskClaimRequest,
    TaskExecutionMutationOutcome,
    TaskTerminalCommand,
)
from app.modules.weaponry.adapters import SQLiteWeaponryResourceStoreAdapter
from app.modules.weaponry.adapters.sqlite import (
    bootstrap_weaponry_task_control_database,
)
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
            root = Path(directory)
            old_path = root / "old.sqlite3"
            sqlite3.connect(old_path).close()
            bootstrap = bootstrap_weaponry_task_control_database(
                old_path,
                root / "task-control.sqlite3",
            )
            db_path = str(bootstrap.database_path)
            manager = SQLiteTransactionManager(SQLiteConnectionFactory(bootstrap))
            task_id = TaskId("operations-resource")
            with build_sqlite_task_control_uow_factories(manager).admission() as uow:
                admitted = uow.admission.admit_one(
                    TaskAdmissionRequest(
                        task_id=task_id,
                        task_type="weaponry",
                        business_ref=TaskBusinessRef("weaponry", "7"),
                        input_schema_version=2,
                        input_snapshot={"architecture_id": 7},
                        input_payload={"architecture_id": 7},
                        public_request_payload={"businessType": "weaponry"},
                        initial_public_status="1",
                        trace_id="trace-operations-resource",
                        accepted_at="2026-08-14T00:00:00.000000Z",
                    )
                )
                self.assertIs(TaskAdmissionOutcome.ACCEPTED, admitted.outcome)
                uow.commit()
            with build_sqlite_task_control_uow_factories(manager).execution() as uow:
                claimed = uow.execution.claim(
                    TaskClaimRequest(
                        task_id=task_id,
                        task_type="weaponry",
                        owner=TaskOwnerIdentity(
                            instance_start_id=(
                                "12345678-1234-4234-8234-123456789abc"
                            ),
                            process_id=1,
                            executor_name="WeaponryExecutor",
                            worker_slot="operations-test",
                        ),
                        lease_token="operations-task-lease",
                        claimed_at="2026-08-14T00:00:01.000000Z",
                        lease_expires_at="2026-08-14T00:00:31.000000Z",
                    )
                )
                self.assertIs(TaskExecutionMutationOutcome.APPLIED, claimed.outcome)
                assert claimed.attempt is not None
                authority = claimed.attempt.authority
                self.assertIs(
                    TaskExecutionMutationOutcome.APPLIED,
                    uow.execution.start(
                        authority,
                        started_at="2026-08-14T00:00:01.000000Z",
                    ),
                )
                self.assertIs(
                    TaskExecutionMutationOutcome.APPLIED,
                    uow.execution.finish(
                        TaskTerminalCommand(
                            authority=authority,
                            transition=TaskTransition.BUSINESS_FAILED,
                            public_status="3",
                            message="测试终态",
                            result_ref="weaponry-result:v1:operations-test",
                            completed_at="2026-08-14T00:00:02.000000Z",
                        )
                    ),
                )
                uow.commit()
            store = SQLiteWeaponryResourceStoreAdapter(
                transaction_manager=manager
            )
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
