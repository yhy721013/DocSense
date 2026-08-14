"""阶段 2-5 第 3 步：Weaponry Control 组件 Schema 与文档快照 Store 验收。"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import unittest

from app.modules.tasks.adapters.sqlite import (
    SQLiteConnectionFactory,
    SQLiteTransactionManager,
    TaskControlBootstrapError,
    bootstrap_task_control_database,
    build_sqlite_task_control_uow_factories,
)
from app.modules.tasks.adapters.sqlite.schema import (
    canonical_manifest_json,
    component_manifest_fingerprint,
    component_schema_ddl,
    validate_task_control_schema,
)
from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.tasks.ports import TaskAdmissionOutcome, TaskAdmissionRequest
from app.modules.weaponry.adapters.sqlite import (
    WEAPONRY_CONTROL_COMPONENT_NAME,
    SQLiteWeaponryTaskDocumentSnapshotStore,
    SQLiteWeaponryResultSnapshotStore,
    bootstrap_weaponry_task_control_database,
    load_weaponry_control_manifest,
)
from app.modules.weaponry.domain import (
    WeaponryAnalyseDataSource,
    WeaponryCallbackPayload,
    WeaponryDocumentSnapshot,
    WeaponryFieldResult,
    WeaponryFieldSpecification,
    WeaponryTableCellResult,
)
from tests import workspace_tempdir


_T0 = "2026-08-14T00:00:00.000000Z"


def _source(value: dict) -> WeaponryAnalyseDataSource:
    """把既有公开黄金样例转换为不可变领域来源。"""

    return WeaponryAnalyseDataSource(
        content=value["content"],
        source=value["source"],
        occurred_at=value["time"],
        file_name=value["fileName"],
        rows=tuple(value["rows"]),
        translation=value["translate"],
    )


def _callback_payload(raw: dict) -> WeaponryCallbackPayload:
    """只供 Store 往返测试使用，覆盖 INPUT、TABLE 与失败三种既有形状。"""

    data = raw["data"]
    fields: list[WeaponryFieldResult] = []
    for raw_field in data.get("weaponryTemplateFieldList", []):
        specification = WeaponryFieldSpecification.from_mapping(raw_field)
        if specification.field_type == "INPUT":
            fields.append(
                WeaponryFieldResult(
                    specification=specification,
                    analyse_data=raw_field["analyseData"],
                    sources=tuple(
                        _source(item) for item in raw_field["analyseDataSource"]
                    ),
                )
            )
            continue
        rows = tuple(
            tuple(
                WeaponryTableCellResult(
                    specification=specification.columns[index],
                    analyse_data=cell["analyseData"],
                    sources=tuple(
                        _source(item) for item in cell["analyseDataSource"]
                    ),
                )
                for index, cell in enumerate(row)
            )
            for row in raw_field["tableFieldList"]
        )
        fields.append(
            WeaponryFieldResult(specification=specification, table_rows=rows)
        )
    return WeaponryCallbackPayload(
        architecture_id=data["architectureId"],
        status=data["status"],
        message=raw["msg"],
        fields=tuple(fields),
    )


def _request(task_id: str, business_key: str) -> TaskAdmissionRequest[tuple[str, ...]]:
    """构造不经过公开 HTTP Adapter 的最小 Weaponry 受理请求。"""

    return TaskAdmissionRequest(
        task_id=TaskId(task_id),
        task_type="weaponry",
        business_ref=TaskBusinessRef("weaponry", business_key),
        input_schema_version=2,
        input_snapshot=(business_key,),
        input_payload={"schemaVersion": 2, "architectureId": int(business_key)},
        public_request_payload={"architectureId": int(business_key)},
        initial_public_status="waiting",
        trace_id=f"trace-{task_id}",
        accepted_at=_T0,
    )


class WeaponryControlComponentSchemaTests(unittest.TestCase):
    _CONTRACT_PATH = (
        Path(__file__).parent / "contracts" / "stage2_weaponry_component_contract.json"
    )

    @staticmethod
    def _empty_database(path: Path) -> None:
        sqlite3.connect(path).close()

    def test_fresh_database_publishes_root_and_weaponry_component_together(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            old_path = root / "old.sqlite3"
            target_path = root / "task-control-v2.sqlite3"
            self._empty_database(old_path)

            result = bootstrap_weaponry_task_control_database(old_path, target_path)

            self.assertTrue(result.created)
            self.assertEqual(
                (WEAPONRY_CONTROL_COMPONENT_NAME,),
                result.identity.registered_components,
            )
            connection = sqlite3.connect(target_path, isolation_level=None)
            try:
                connection.execute("PRAGMA foreign_keys = ON")
                identity = validate_task_control_schema(
                    connection,
                    known_components={
                        WEAPONRY_CONTROL_COMPONENT_NAME: load_weaponry_control_manifest()
                    },
                    required_components={WEAPONRY_CONTROL_COMPONENT_NAME: 1},
                )
                self.assertEqual(result.identity, identity)
            finally:
                connection.close()
            self.assertFalse(any(root.glob("*.bootstrap-*.sqlite3*")))

    def test_existing_root_upgrades_once_but_partial_object_fails_closed(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            old_path = root / "old.sqlite3"
            target_path = root / "task-control-v2.sqlite3"
            self._empty_database(old_path)
            root_only = bootstrap_task_control_database(old_path, target_path)
            upgraded = bootstrap_weaponry_task_control_database(old_path, target_path)
            reopened = bootstrap_weaponry_task_control_database(old_path, target_path)
            self.assertEqual((), root_only.identity.registered_components)
            self.assertFalse(upgraded.created)
            self.assertEqual(upgraded.identity, reopened.identity)

        with workspace_tempdir() as tmp:
            root = Path(tmp)
            old_path = root / "old.sqlite3"
            target_path = root / "task-control-v2.sqlite3"
            self._empty_database(old_path)
            bootstrap_task_control_database(old_path, target_path)
            connection = sqlite3.connect(target_path)
            connection.execute(component_schema_ddl(load_weaponry_control_manifest())[0])
            connection.commit()
            connection.close()

            with self.assertRaises(TaskControlBootstrapError) as raised:
                bootstrap_weaponry_task_control_database(old_path, target_path)
            self.assertEqual("schema_table_list_drift", raised.exception.code)

    def test_document_snapshot_is_task_scoped_and_joins_root_execution(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            old_path = root / "old.sqlite3"
            target_path = root / "task-control-v2.sqlite3"
            self._empty_database(old_path)
            bootstrap = bootstrap_weaponry_task_control_database(old_path, target_path)
            manager = SQLiteTransactionManager(
                SQLiteConnectionFactory(bootstrap, busy_timeout_ms=100)
            )
            factories = build_sqlite_task_control_uow_factories(manager)
            request = _request("weaponry-snapshot-task-1", "132")
            with factories.admission() as unit_of_work:
                result = unit_of_work.admission.admit_one(request)
                self.assertIs(TaskAdmissionOutcome.ACCEPTED, result.outcome)
                unit_of_work.commit()

            snapshots = (
                WeaponryDocumentSnapshot(
                    sequence_no=1,
                    document_key="document-1",
                    file_name="document-1.pdf",
                    original_name="原始文档.pdf",
                    ingested_file_name="document-1-ingested.pdf",
                    source_architecture_id=132,
                    external_document_ref="folder/document-1.pdf",
                    anything_document_id="anything-document-1",
                ),
            )
            store = SQLiteWeaponryTaskDocumentSnapshotStore(manager)
            self.assertEqual(
                snapshots,
                store.replace_for_task(
                    task_id=request.task_id,
                    business_ref=request.business_ref,
                    documents=snapshots,
                ),
            )
            self.assertEqual(snapshots, store.list_for_task(request.task_id))

            with self.assertRaises(ValueError):
                store.replace_for_task(
                    task_id=request.task_id,
                    business_ref=TaskBusinessRef("weaponry", "133"),
                    documents=snapshots,
                )
            self.assertEqual(snapshots, store.list_for_task(request.task_id))

    def test_manifest_identity_matches_frozen_step3_contract(self) -> None:
        """无组件版本升级时，任何 DDL 漂移都必须使验收失败。"""

        contract = json.loads(self._CONTRACT_PATH.read_text(encoding="utf-8"))
        manifest = load_weaponry_control_manifest()
        canonical_bytes = canonical_manifest_json(manifest).encode("utf-8")

        self.assertEqual(
            contract["component"]["canonicalUtf8Bytes"],
            len(canonical_bytes),
        )
        self.assertEqual(
            contract["component"]["fingerprint"],
            component_manifest_fingerprint(manifest),
        )
        self.assertEqual(
            contract["component"]["tables"],
            [table["name"] for table in manifest["tables"]],
        )

    def test_result_snapshot_round_trips_existing_callback_contracts(self) -> None:
        """完整结果快照必须无损恢复公开 Callback，且同 Task 不允许改写。"""

        golden_path = Path(__file__).parent / "contracts" / "stage1d_weaponry_contracts.json"
        golden = json.loads(golden_path.read_text(encoding="utf-8"))["goldenCallbacks"]
        callback_cases = tuple(
            raw for raw in golden.values() if isinstance(raw, dict) and "data" in raw
        )
        for index, raw in enumerate(callback_cases, start=1):
            with workspace_tempdir() as tmp:
                root = Path(tmp)
                old_path = root / "old.sqlite3"
                target_path = root / "task-control-v2.sqlite3"
                self._empty_database(old_path)
                bootstrap = bootstrap_weaponry_task_control_database(
                    old_path,
                    target_path,
                )
                manager = SQLiteTransactionManager(
                    SQLiteConnectionFactory(bootstrap, busy_timeout_ms=100)
                )
                factories = build_sqlite_task_control_uow_factories(manager)
                store = SQLiteWeaponryResultSnapshotStore(manager)
                task_id = TaskId(f"weaponry-result-task-{index}")
                request = _request(task_id.value, "10502")
                with factories.admission() as unit_of_work:
                    admitted = unit_of_work.admission.admit_one(request)
                    self.assertIs(TaskAdmissionOutcome.ACCEPTED, admitted.outcome)
                    unit_of_work.commit()
                payload = _callback_payload(raw)
                saved = store.save(
                    task_id=task_id,
                    business_ref=request.business_ref,
                    payload=payload,
                    created_at=_T0,
                )
                loaded = store.get(task_id)
                self.assertIsNotNone(loaded)
                assert loaded is not None
                self.assertEqual(raw, loaded.payload.to_public_dict())
                self.assertEqual(saved.result_digest, loaded.result_digest)

                # 幂等写使用已持久时间；相同 TaskId 的不同 payload 必须失败关闭。
                repeated = store.save(
                    task_id=task_id,
                    business_ref=request.business_ref,
                    payload=payload,
                    created_at="2026-08-14T00:00:01.000000Z",
                )
                self.assertEqual(_T0, repeated.created_at)
                with self.assertRaises(ValueError):
                    store.save(
                        task_id=task_id,
                        business_ref=request.business_ref,
                        payload=WeaponryCallbackPayload(
                            architecture_id=10502,
                            status="3",
                            message="不同结果",
                        ),
                        created_at=_T0,
                    )


if __name__ == "__main__":
    unittest.main()
