"""阶段 2-4 Report v2 资源 Store 的临时 SQLite 验收。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import sqlite3
from threading import Barrier
import unittest

from app.modules.report.adapters.resource_store import SQLiteReportResourceStoreAdapter
from app.modules.report.adapters.sqlite import (
    SQLiteReportResourceStore,
    bootstrap_report_task_control_database,
    report_artifact_result_ref,
)
from app.modules.report.ports import (
    ReportArtifactCategory,
    ReportArtifactRef,
    ReportArtifactScope,
    ReportResourceRecord,
)
from app.modules.report.domain import ReportResourceConcurrencyError
from app.modules.tasks.adapters.sqlite import (
    SQLiteConnectionFactory,
    SQLiteTransactionManager,
    build_sqlite_task_control_uow_factories,
)
from app.modules.tasks.domain import TaskBusinessRef, TaskId, TaskOwnerIdentity, TaskTransition
from app.modules.tasks.ports import (
    TaskAdmissionOutcome,
    TaskAdmissionRequest,
    TaskClaimRequest,
    TaskExecutionMutationOutcome,
    TaskTerminalCommand,
)
from tests import workspace_tempdir


_T0 = "2026-08-13T01:00:00.000000Z"
_T1 = "2026-08-13T01:00:01.000000Z"
_T2 = "2026-08-13T01:00:02.000000Z"
_T3 = "2026-08-13T01:00:03.000000Z"
_T30 = "2026-08-13T01:00:30.000000Z"


class ReportResourceStoreV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = workspace_tempdir()
        root = Path(self._directory.__enter__())
        old_path = root / "old.sqlite3"
        sqlite3.connect(old_path).close()
        bootstrap = bootstrap_report_task_control_database(
            old_path,
            root / "task-control-v2.sqlite3",
        )
        self.manager = SQLiteTransactionManager(
            SQLiteConnectionFactory(bootstrap, busy_timeout_ms=100)
        )
        self.factories = build_sqlite_task_control_uow_factories(self.manager)
        self.backend = SQLiteReportResourceStore(self.manager)
        self.store = SQLiteReportResourceStoreAdapter(self.backend)
        self.task_id = TaskId("report-task-001")
        self.business_ref = TaskBusinessRef("report", "report-001")
        request = TaskAdmissionRequest(
            task_id=self.task_id,
            task_type="report",
            business_ref=self.business_ref,
            input_schema_version=2,
            input_snapshot={"report_id": "report-001"},
            input_payload={"report_id": "report-001"},
            public_request_payload={"reportId": "report-001"},
            initial_public_status="0",
            trace_id="trace-report-001",
            accepted_at=_T0,
        )
        with self.factories.admission() as unit_of_work:
            result = unit_of_work.admission.admit_one(request)
            self.assertIs(TaskAdmissionOutcome.ACCEPTED, result.outcome)
            unit_of_work.commit()

    def tearDown(self) -> None:
        self._directory.__exit__(None, None, None)

    def _finish_successfully(self, final_artifact: ReportArtifactRef) -> None:
        owner = TaskOwnerIdentity(
            instance_start_id="12345678-1234-4234-8234-123456789abc",
            process_id=101,
            executor_name="ReportExecutor",
            worker_slot="worker-0",
        )
        with self.factories.execution() as unit_of_work:
            claimed = unit_of_work.execution.claim(
                TaskClaimRequest(
                    task_id=self.task_id,
                    task_type="report",
                    owner=owner,
                    lease_token="report-task-lease",
                    claimed_at=_T1,
                    lease_expires_at=_T30,
                )
            )
            self.assertIs(TaskExecutionMutationOutcome.APPLIED, claimed.outcome)
            assert claimed.attempt is not None
            authority = claimed.attempt.authority
            self.assertIs(
                TaskExecutionMutationOutcome.APPLIED,
                unit_of_work.execution.start(authority, started_at=_T2),
            )
            self.assertIs(
                TaskExecutionMutationOutcome.APPLIED,
                unit_of_work.execution.finish(
                    TaskTerminalCommand(
                        authority=authority,
                        transition=TaskTransition.BUSINESS_SUCCEEDED,
                        public_status="1",
                        message="生成成功",
                        result_ref=report_artifact_result_ref(final_artifact),
                        completed_at=_T3,
                    )
                ),
            )
            unit_of_work.commit()

    def test_create_cas_terminal_ownership_and_recovery_scan(self) -> None:
        scope = ReportArtifactScope(self.task_id, "report-task-001")
        created = self.store.create(
            ReportResourceRecord(
                task_id=self.task_id,
                business_ref=self.business_ref,
                scope=scope,
            )
        )
        self.assertEqual(1, created.version)
        final_artifact = ReportArtifactRef(
            task_id=self.task_id,
            artifact_id="output/report.html",
            category=ReportArtifactCategory.REPORT_HTML,
            size_bytes=18,
            checksum="a" * 64,
        )
        tracked = self.store.save(
            replace(created, final_artifact=final_artifact),
            expected_version=created.version,
        )
        self.assertEqual(2, tracked.version)
        self._finish_successfully(final_artifact)

        prepared = self.store.prepare_cleanup(self.task_id)

        self.assertEqual((final_artifact,), prepared.retained)
        self.assertEqual((self.task_id,), self.store.list_recoverable(limit=10))
        self.assertEqual(prepared, self.store.prepare_cleanup(self.task_id))

    def test_identity_conflict_and_missing_execution_fail_closed(self) -> None:
        scope = ReportArtifactScope(self.task_id, "report-task-001")
        self.store.create(
            ReportResourceRecord(
                task_id=self.task_id,
                business_ref=self.business_ref,
                scope=scope,
            )
        )
        with self.assertRaises(ValueError):
            self.backend.create_report_resource_record(
                execution_id=self.task_id.value,
                business_type="report",
                business_key="different-report",
                artifact_namespace=scope.namespace,
                state="tracking",
                record_payload={},
                created_at=_T0,
            )
        self.assertIsNone(self.store.get(TaskId("missing-report-task")))

    def test_concurrent_same_version_updates_have_one_cas_winner(self) -> None:
        """专用 v2 Store 必须在独立连接并发下保持单一 CAS 胜者。"""

        created = self.store.create(
            ReportResourceRecord(
                task_id=self.task_id,
                business_ref=self.business_ref,
                scope=ReportArtifactScope(self.task_id, "report-task-001"),
            )
        )
        barrier = Barrier(20)

        def update(index: int) -> bool:
            barrier.wait(timeout=10)
            try:
                self.store.save(
                    replace(
                        created,
                        last_error_stage="cas_test",
                        last_error_message=f"writer-{index}",
                    ),
                    expected_version=created.version,
                )
            except ReportResourceConcurrencyError:
                return False
            return True

        with ThreadPoolExecutor(max_workers=20) as executor:
            outcomes = tuple(executor.map(update, range(20)))

        self.assertEqual(1, outcomes.count(True))
        self.assertEqual(19, outcomes.count(False))
        reloaded = self.store.get(self.task_id)
        assert reloaded is not None
        self.assertEqual(2, reloaded.version)

    def test_recovery_deferral_and_operation_attempts_survive_reload(self) -> None:
        """冷却水位和操作次数都来自专用 Store，不能依赖旧 Task Service JSON。"""

        created = self.store.create(
            ReportResourceRecord(
                task_id=self.task_id,
                business_ref=self.business_ref,
                scope=ReportArtifactScope(self.task_id, "report-task-001"),
            )
        )
        persisted = self.store.save(
            replace(
                created,
                operation_attempts=(
                    ("context_delete", 2),
                    ("global_document_delete", 1),
                ),
            ),
            expected_version=created.version,
        )
        reloaded = SQLiteReportResourceStoreAdapter(
            SQLiteReportResourceStore(self.manager)
        ).get(self.task_id)
        assert reloaded is not None
        self.assertEqual(persisted.operation_attempts, reloaded.operation_attempts)

        owner = TaskOwnerIdentity(
            instance_start_id="12345678-1234-4234-8234-123456789abc",
            process_id=102,
            executor_name="ReportExecutor",
            worker_slot="worker-1",
        )
        with self.factories.execution() as unit_of_work:
            claimed = unit_of_work.execution.claim(
                TaskClaimRequest(
                    task_id=self.task_id,
                    task_type="report",
                    owner=owner,
                    lease_token="report-failed-lease",
                    claimed_at=_T1,
                    lease_expires_at=_T30,
                )
            )
            self.assertIs(TaskExecutionMutationOutcome.APPLIED, claimed.outcome)
            assert claimed.attempt is not None
            authority = claimed.attempt.authority
            self.assertIs(
                TaskExecutionMutationOutcome.APPLIED,
                unit_of_work.execution.start(authority, started_at=_T2),
            )
            self.assertIs(
                TaskExecutionMutationOutcome.APPLIED,
                unit_of_work.execution.finish(
                    TaskTerminalCommand(
                        authority=authority,
                        transition=TaskTransition.BUSINESS_FAILED,
                        public_status="2",
                        message="模拟失败",
                        result_ref="",
                        completed_at=_T3,
                    )
                ),
            )
            unit_of_work.commit()

        self.assertEqual((self.task_id,), self.store.list_recoverable(limit=10))
        self.assertTrue(
            self.store.defer_recovery(
                self.task_id,
                retry_at="2999-01-01T00:00:00+00:00",
                reason="exception:CorruptPayload",
            )
        )
        self.assertEqual((), self.store.list_recoverable(limit=10))


if __name__ == "__main__":
    unittest.main()
