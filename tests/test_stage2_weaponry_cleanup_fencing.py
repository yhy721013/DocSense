"""阶段 2-5 第 5 步：Weaponry ownership/cleanup fencing 验收。"""

from __future__ import annotations

import ast
from pathlib import Path
import sqlite3
import unittest

from app.modules.tasks.adapters.sqlite import (
    SQLiteConnectionFactory,
    SQLiteTransactionManager,
    build_sqlite_task_control_uow_factories,
)
from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.tasks.ports import TaskAdmissionOutcome, TaskAdmissionRequest
from app.modules.weaponry.adapters.sqlite import (
    SQLiteWeaponryResourceStoreAdapter,
    bootstrap_weaponry_task_control_database,
)
from app.modules.weaponry.ports import (
    AcquireWeaponryCleanupLease,
    CompleteWeaponryResourceCleanup,
    PrepareWeaponryResourceCleanup,
    RegisterWeaponryResource,
    ReleaseWeaponryCleanupLease,
    WeaponryCleanupLeaseAcquireOutcome,
    WeaponryPortStateError,
    WeaponryResourceCleanupOutcome,
    WeaponryResourceKind,
    WeaponryResourceOwnership,
    WeaponryResourceRecord,
    WeaponryTrackedResource,
)
from tests import workspace_tempdir


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_T0 = "2026-08-14T00:00:00.000000Z"


def _request() -> TaskAdmissionRequest[tuple[str, ...]]:
    return TaskAdmissionRequest(
        task_id=TaskId("weaponry-cleanup-fencing-task-1"),
        task_type="weaponry",
        business_ref=TaskBusinessRef("weaponry", "132"),
        input_schema_version=2,
        input_snapshot=("132",),
        input_payload={"schemaVersion": 2, "architectureId": 132},
        public_request_payload={"architectureId": 132},
        initial_public_status="waiting",
        trace_id="trace-weaponry-cleanup-fencing-task-1",
        accepted_at=_T0,
    )


class WeaponryCleanupFencingTests(unittest.TestCase):
    def _build_store(self, root: Path):
        old_path = root / "old.sqlite3"
        database_path = root / "task-control-v2.sqlite3"
        sqlite3.connect(old_path).close()
        bootstrap = bootstrap_weaponry_task_control_database(old_path, database_path)
        manager = SQLiteTransactionManager(
            SQLiteConnectionFactory(bootstrap, busy_timeout_ms=100)
        )
        factories = build_sqlite_task_control_uow_factories(manager)
        request = _request()
        with factories.admission() as unit_of_work:
            result = unit_of_work.admission.admit_one(request)
            self.assertIs(TaskAdmissionOutcome.ACCEPTED, result.outcome)
            unit_of_work.commit()
        return request, SQLiteWeaponryResourceStoreAdapter(
            transaction_manager=manager,
            cleanup_lease_seconds=60.0,
            retry_delay_seconds=1.0,
        )

    def test_old_cleanup_lease_cannot_commit_after_new_fencing_owner(self) -> None:
        with workspace_tempdir() as tmp:
            request, store = self._build_store(Path(tmp))
            record = store.create(
                WeaponryResourceRecord(
                    task_id=request.task_id,
                    business_ref=request.business_ref,
                )
            )
            record = store.register(
                RegisterWeaponryResource(
                    task_id=request.task_id,
                    resource=WeaponryTrackedResource(
                        resource_id="retrieval-scope:scope-132",
                        kind=WeaponryResourceKind.RETRIEVAL_SCOPE,
                        external_ref="scope-132",
                        ownership=WeaponryResourceOwnership.OWNED,
                        idempotency_key="weaponry:cleanup:scope-132",
                    ),
                    expected_version=record.version,
                )
            )
            record = store.prepare_cleanup(
                PrepareWeaponryResourceCleanup(request.task_id, record.version)
            )
            first = store.acquire_cleanup(
                AcquireWeaponryCleanupLease(request.task_id, record.version)
            )
            self.assertIs(WeaponryCleanupLeaseAcquireOutcome.ACQUIRED, first.outcome)
            assert first.lease is not None
            leased = store.get(request.task_id)
            assert leased is not None
            store.release_cleanup(ReleaseWeaponryCleanupLease(first.lease, leased.version))
            released = store.get(request.task_id)
            assert released is not None
            second = store.acquire_cleanup(
                AcquireWeaponryCleanupLease(request.task_id, released.version)
            )
            self.assertIs(WeaponryCleanupLeaseAcquireOutcome.ACQUIRED, second.outcome)
            assert second.lease is not None
            self.assertGreater(
                second.lease.fencing_token,
                first.lease.fencing_token,
            )
            current = store.get(request.task_id)
            assert current is not None

            with self.assertRaises(WeaponryPortStateError) as raised:
                store.complete_cleanup(
                    CompleteWeaponryResourceCleanup(
                        task_id=request.task_id,
                        lease=first.lease,
                        resource_id="retrieval-scope:scope-132",
                        outcome=WeaponryResourceCleanupOutcome.SUCCEEDED,
                        expected_version=current.version,
                    )
                )
            self.assertEqual("resource_cleanup_lease_mismatch", raised.exception.error_code)
            self.assertEqual(current, store.get(request.task_id))

    def test_v2_store_rejects_cross_execution_resource_identity(self) -> None:
        with workspace_tempdir() as tmp:
            request, store = self._build_store(Path(tmp))
            with self.assertRaises(WeaponryPortStateError) as raised:
                store.create(
                    WeaponryResourceRecord(
                        task_id=request.task_id,
                        business_ref=TaskBusinessRef("weaponry", "133"),
                    )
                )
            self.assertEqual(
                "resource_execution_identity_mismatch",
                raised.exception.error_code,
            )

    def test_cleanup_chain_never_deletes_by_expected_name(self) -> None:
        """名称只可用于 Creation Intent 唯一查回，删除必须使用已登记 external_ref。"""

        cleanup_path = (
            PROJECT_ROOT
            / "app/modules/weaponry/adapters/anythingllm_resource_cleanup.py"
        )
        cleanup_tree = ast.parse(cleanup_path.read_text(encoding="utf-8"))
        attributes = {
            node.attr for node in ast.walk(cleanup_tree) if isinstance(node, ast.Attribute)
        }
        self.assertNotIn("expected_name", attributes)

        recovery_path = (
            PROJECT_ROOT
            / "app/modules/weaponry/adapters/creation_intent_recovery.py"
        )
        recovery_tree = ast.parse(recovery_path.read_text(encoding="utf-8"))
        recovery_calls = {
            node.func.attr
            for node in ast.walk(recovery_tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertFalse(
            {"delete_workspace", "delete_thread"} & recovery_calls,
            "Creation Intent 查名恢复只能形成外部引用或隔离，不能顺手删除",
        )


if __name__ == "__main__":
    unittest.main()
