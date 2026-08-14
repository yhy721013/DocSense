"""阶段 2-5 第 4 步：Weaponry Step/Recovery Registry 与组合 UoW 验收。"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import unittest

from app.modules.tasks.adapters.sqlite import (
    SQLiteCallbackControlStore,
    SQLiteConnectionFactory,
    SQLiteTaskControlStore,
    SQLiteTransactionManager,
    build_sqlite_task_control_uow_factories,
)
from app.modules.tasks.domain import RecoveryClassification, TaskBusinessRef, TaskId
from app.modules.tasks.ports import TaskAdmissionOutcome, TaskAdmissionRequest
from app.modules.weaponry.adapters.sqlite import (
    SQLiteWeaponryCreationIntentStoreAdapter,
    SQLiteWeaponryExecutionUnitOfWorkFactory,
    SQLiteWeaponryInteractionAuditAdapter,
    SQLiteWeaponryResourceStoreAdapter,
    SQLiteWeaponryResultSnapshotStore,
    SQLiteWeaponryTaskDocumentSnapshotStore,
    bootstrap_weaponry_task_control_database,
)
from app.modules.weaponry.application import (
    WEAPONRY_RECOVERY_MATRICES,
    WEAPONRY_STEP_REGISTRY,
    resolve_weaponry_step,
)
from app.modules.weaponry.domain import (
    WeaponryDocumentSnapshot,
    WeaponryDomainValidationError,
)
from app.modules.weaponry.ports import WeaponryResourceRecord
from tests import workspace_tempdir


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_T0 = "2026-08-14T00:00:00.000000Z"


def _request() -> TaskAdmissionRequest[tuple[str, ...]]:
    return TaskAdmissionRequest(
        task_id=TaskId("weaponry-step-uow-task-1"),
        task_type="weaponry",
        business_ref=TaskBusinessRef("weaponry", "132"),
        input_schema_version=2,
        input_snapshot=("132",),
        input_payload={"schemaVersion": 2, "architectureId": 132},
        public_request_payload={"architectureId": 132},
        initial_public_status="waiting",
        trace_id="trace-weaponry-step-uow-task-1",
        accepted_at=_T0,
    )


def _snapshot() -> WeaponryDocumentSnapshot:
    return WeaponryDocumentSnapshot(
        sequence_no=1,
        document_key="document-1",
        file_name="document-1.pdf",
        original_name="原始文档.pdf",
        ingested_file_name="document-1-ingested.pdf",
        source_architecture_id=132,
        external_document_ref="folder/document-1.pdf",
        anything_document_id="anything-document-1",
    )


class WeaponryStepRegistryTests(unittest.TestCase):
    def test_registry_matches_frozen_stage20_asset(self) -> None:
        asset = json.loads(
            (PROJECT_ROOT / "tests/contracts/stage2_business_step_registry.json")
            .read_text(encoding="utf-8")
        )["businesses"]["weaponry"]["steps"]
        expected = {
            (
                item["stepKey"],
                item["definitionVersion"],
                item["effectKind"],
                item["replayPolicy"],
                item["schemaRef"],
                item["recoveryMatrixRef"],
                item["successResultRef"],
            )
            for item in asset
        }
        actual = {
            (
                item.key_pattern,
                item.definition_version,
                item.effect_kind.value,
                item.replay_policy.value,
                item.schema_ref,
                item.recovery_matrix_ref,
                item.success_result_ref,
            )
            for item in WEAPONRY_STEP_REGISTRY
        }
        self.assertEqual(expected, actual)

    def test_dynamic_keys_are_strict_and_unknown_keys_fail_closed(self) -> None:
        valid = (
            "rag.document.bind:1",
            "field_model.execute:1:2:3",
            "translation.execute:12:3:4",
            "interaction_audit.commit:weaponry:task-1:retrieval:1",
        )
        for step_key in valid:
            with self.subTest(step_key=step_key):
                self.assertTrue(resolve_weaponry_step(step_key).matches(step_key))

        invalid = (
            "rag.document.bind:0",
            "rag.document.bind:01",
            "field_model.execute:1:2",
            "translation.execute:1:-2:3",
            "interaction_audit.commit:含空格 call",
            "unknown.step",
        )
        for step_key in invalid:
            with self.subTest(step_key=step_key):
                with self.assertRaises(WeaponryDomainValidationError):
                    resolve_weaponry_step(step_key)

    def test_every_matrix_covers_five_classifications(self) -> None:
        referenced = {item.recovery_matrix_ref for item in WEAPONRY_STEP_REGISTRY}
        self.assertEqual(referenced, set(WEAPONRY_RECOVERY_MATRICES))
        for matrix in WEAPONRY_RECOVERY_MATRICES.values():
            self.assertEqual(set(RecoveryClassification), set(matrix.rules))


class WeaponryExecutionUnitOfWorkTests(unittest.TestCase):
    def test_component_and_root_facts_commit_or_rollback_together(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            old_path = root / "old.sqlite3"
            database_path = root / "task-control-v2.sqlite3"
            sqlite3.connect(old_path).close()
            bootstrap = bootstrap_weaponry_task_control_database(
                old_path,
                database_path,
            )
            manager = SQLiteTransactionManager(
                SQLiteConnectionFactory(bootstrap, busy_timeout_ms=100)
            )
            root_factories = build_sqlite_task_control_uow_factories(manager)
            request = _request()
            with root_factories.admission() as unit_of_work:
                result = unit_of_work.admission.admit_one(request)
                self.assertIs(TaskAdmissionOutcome.ACCEPTED, result.outcome)
                unit_of_work.commit()

            factory = SQLiteWeaponryExecutionUnitOfWorkFactory(
                manager,
                execution_builder=SQLiteTaskControlStore,
                callback_delivery_builder=SQLiteCallbackControlStore,
                document_snapshot_builder=(
                    SQLiteWeaponryTaskDocumentSnapshotStore.from_connection
                ),
                creation_intent_builder=(
                    SQLiteWeaponryCreationIntentStoreAdapter.from_connection
                ),
                interaction_audit_builder=(
                    SQLiteWeaponryInteractionAuditAdapter.from_connection
                ),
                resource_builder=SQLiteWeaponryResourceStoreAdapter.from_connection,
                result_snapshot_builder=(
                    SQLiteWeaponryResultSnapshotStore.from_connection
                ),
            )
            snapshots = (_snapshot(),)
            record = WeaponryResourceRecord(
                task_id=request.task_id,
                business_ref=request.business_ref,
            )

            # 不提交时，两个组件 Store 必须由最外层统一回滚，不能有内部提前 commit。
            with factory() as unit_of_work:
                unit_of_work.document_snapshots.replace_for_task(
                    task_id=request.task_id,
                    business_ref=request.business_ref,
                    documents=snapshots,
                )
                unit_of_work.resources.create(record)

            with factory() as unit_of_work:
                self.assertEqual((), unit_of_work.document_snapshots.list_for_task(request.task_id))
                self.assertIsNone(unit_of_work.resources.get(request.task_id))
                unit_of_work.rollback()

            with factory() as unit_of_work:
                unit_of_work.document_snapshots.replace_for_task(
                    task_id=request.task_id,
                    business_ref=request.business_ref,
                    documents=snapshots,
                )
                unit_of_work.resources.create(record)
                unit_of_work.commit()

            with factory() as unit_of_work:
                self.assertEqual(
                    snapshots,
                    unit_of_work.document_snapshots.list_for_task(request.task_id),
                )
                self.assertEqual(record, unit_of_work.resources.get(request.task_id))
                unit_of_work.rollback()


if __name__ == "__main__":
    unittest.main()
