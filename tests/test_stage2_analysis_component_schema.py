"""阶段 2-6 步骤 2：Analysis Control 组件 Schema 与结果快照 Store。"""

from __future__ import annotations

from pathlib import Path
import sqlite3
import unittest

from app.modules.analysis.adapters.sqlite import (
    ANALYSIS_CONTROL_COMPONENT_NAME,
    ANALYSIS_CONTROL_COMPONENT_VERSION,
    SQLiteAnalysisResultSnapshotStore,
    SQLiteAnalysisV2ResourceStoreAdapter,
    bootstrap_analysis_task_control_database,
    load_analysis_control_manifest,
)
from app.modules.analysis.ports import (
    AnalysisExecutionRef,
    AnalysisResourceCommand,
    AnalysisResourceState,
)
from app.modules.analysis.domain.task_inputs import FrozenJsonObject
from app.modules.tasks.adapters.sqlite import (
    SQLiteConnectionFactory,
    SQLiteTransactionManager,
    build_sqlite_task_control_uow_factories,
)
from app.modules.tasks.adapters.sqlite.schema import validate_task_control_schema
from app.modules.tasks.adapters.sqlite.schema import (
    canonical_manifest_json,
    component_manifest_fingerprint,
)
import json
from app.modules.tasks.domain import TaskBatchRef, TaskBusinessRef, TaskId
from app.modules.tasks.ports import TaskAdmissionOutcome, TaskAdmissionRequest
from tests import workspace_tempdir


_T0 = "2026-08-15T00:00:00.000000Z"


def _request() -> TaskAdmissionRequest[tuple[str, ...]]:
    task_id = TaskId("analysis-component-task-1")
    return TaskAdmissionRequest(
        task_id=task_id,
        task_type="file",
        business_ref=TaskBusinessRef("file", "analysis-key.txt"),
        input_schema_version=5,
        input_snapshot=("analysis-key.txt",),
        input_payload={"schema_version": 5, "task_id": task_id.value},
        public_request_payload={"businessType": "file", "params": []},
        initial_public_status="0",
        trace_id="trace-analysis-component",
        accepted_at=_T0,
        batch=TaskBatchRef("a" * 32, 1),
    )


class AnalysisControlComponentSchemaTests(unittest.TestCase):
    _CONTRACT_PATH = Path(__file__).parent / "contracts" / "stage2_analysis_component_contract.json"
    @staticmethod
    def _empty_database(path: Path) -> None:
        sqlite3.connect(path).close()

    def test_fresh_database_installs_and_strictly_reopens_component(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            old_path = root / "old.sqlite3"
            target_path = root / "task-control-v2.sqlite3"
            self._empty_database(old_path)

            created = bootstrap_analysis_task_control_database(old_path, target_path)
            reopened = bootstrap_analysis_task_control_database(old_path, target_path)

            self.assertTrue(created.created)
            self.assertFalse(reopened.created)
            self.assertEqual(created.identity, reopened.identity)
            self.assertEqual(
                (ANALYSIS_CONTROL_COMPONENT_NAME,),
                created.identity.registered_components,
            )
            connection = sqlite3.connect(target_path, isolation_level=None)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA foreign_keys = ON")
                identity = validate_task_control_schema(
                    connection,
                    known_components={
                        ANALYSIS_CONTROL_COMPONENT_NAME: load_analysis_control_manifest()
                    },
                    required_components={
                        ANALYSIS_CONTROL_COMPONENT_NAME: ANALYSIS_CONTROL_COMPONENT_VERSION
                    },
                )
            finally:
                connection.close()
            self.assertEqual(created.identity, identity)

    def test_manifest_identity_matches_frozen_component_contract(self) -> None:
        contract = json.loads(self._CONTRACT_PATH.read_text(encoding="utf-8"))
        manifest = load_analysis_control_manifest()
        canonical_bytes = canonical_manifest_json(manifest).encode("utf-8")
        self.assertEqual(contract["component"]["canonicalUtf8Bytes"], len(canonical_bytes))
        self.assertEqual(
            contract["component"]["fingerprint"],
            component_manifest_fingerprint(manifest),
        )
        self.assertEqual(
            contract["component"]["tables"],
            [table["name"] for table in manifest["tables"]],
        )

    def test_result_snapshot_is_task_scoped_idempotent_and_immutable(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            old_path = root / "old.sqlite3"
            target_path = root / "task-control-v2.sqlite3"
            self._empty_database(old_path)
            bootstrap = bootstrap_analysis_task_control_database(old_path, target_path)
            manager = SQLiteTransactionManager(
                SQLiteConnectionFactory(bootstrap, busy_timeout_ms=100)
            )
            factories = build_sqlite_task_control_uow_factories(manager)
            request = _request()
            with factories.admission() as unit_of_work:
                admitted = unit_of_work.admission.admit_one(request)
                self.assertIs(TaskAdmissionOutcome.ACCEPTED, admitted.outcome)
                unit_of_work.commit()

            payload = FrozenJsonObject.from_mapping(
                {
                    "businessType": "file",
                    "data": {"fileName": "analysis-key.txt", "status": "2"},
                    "msg": "分析完成",
                },
                name="callback",
            )
            store = SQLiteAnalysisResultSnapshotStore(manager)
            first = store.save(
                task_id=request.task_id,
                business_ref=request.business_ref,
                payload=payload,
                created_at=_T0,
            )
            repeated = store.save(
                task_id=request.task_id,
                business_ref=request.business_ref,
                payload=payload,
                created_at="2026-08-15T00:00:01.000000Z",
            )
            loaded = store.get(request.task_id)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(payload, loaded.payload)
            self.assertEqual(first.result_digest, loaded.result_digest)
            self.assertEqual(_T0, repeated.created_at)

            different = FrozenJsonObject.from_mapping(
                {"businessType": "file", "data": {"status": "3"}, "msg": "失败"},
                name="different_callback",
            )
            with self.assertRaises(ValueError):
                store.save(
                    task_id=request.task_id,
                    business_ref=request.business_ref,
                    payload=different,
                    created_at=_T0,
                )

    def test_resource_store_uses_state_version_cas_and_borrowable_schema(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            old_path = root / "old.sqlite3"
            target_path = root / "task-control-v2.sqlite3"
            self._empty_database(old_path)
            bootstrap = bootstrap_analysis_task_control_database(old_path, target_path)
            manager = SQLiteTransactionManager(
                SQLiteConnectionFactory(bootstrap, busy_timeout_ms=100)
            )
            factories = build_sqlite_task_control_uow_factories(manager)
            request = _request()
            with factories.admission() as unit_of_work:
                admitted = unit_of_work.admission.admit_one(request)
                self.assertIs(TaskAdmissionOutcome.ACCEPTED, admitted.outcome)
                unit_of_work.commit()

            execution = AnalysisExecutionRef(
                request.task_id,
                request.business_ref.business_key,
                "a" * 32,
                1,
            )
            clock_values = iter(
                (
                    _T0,
                    "2026-08-15T00:00:01.000000Z",
                    "2026-08-15T00:00:02.000000Z",
                )
            )
            store = SQLiteAnalysisV2ResourceStoreAdapter(
                manager,
                clock=lambda: next(clock_values),
            )
            created = store.create(
                AnalysisResourceCommand(
                    execution,
                    None,
                    None,
                    AnalysisResourceState.TRACKING,
                    FrozenJsonObject.from_mapping(
                        {"workspace_ref": "opaque-ref"},
                        name="resource",
                    ),
                )
            )
            self.assertEqual(1, created.version)
            advanced = store.advance(
                AnalysisResourceCommand(
                    execution,
                    AnalysisResourceState.TRACKING,
                    created.version,
                    AnalysisResourceState.CLEANUP_PENDING,
                    created.record_payload,
                )
            )
            self.assertEqual(2, advanced.version)
            deferred = store.defer_recovery(
                execution,
                expected_version=advanced.version,
                retry_at="2026-08-15T00:10:00.000000Z",
                reason="provider_busy",
            )
            self.assertEqual(3, deferred.version)
            self.assertEqual(1, deferred.recovery_deferral_count)


if __name__ == "__main__":
    unittest.main()
