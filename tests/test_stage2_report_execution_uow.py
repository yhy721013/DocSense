"""阶段 2-4 Report 组合 UoW、Step 与终态原子性验收。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import unittest

from app.modules.report.adapters.resource_store import SQLiteReportResourceStoreAdapter
from app.modules.report.adapters.sqlite import (
    SQLiteReportExecutionUnitOfWorkFactory,
    SQLiteReportResourceStore,
    bootstrap_report_task_control_database,
)
from app.modules.report.application import ReportResourceFactService, ReportStepRuntime
from app.modules.report.domain import ReportId, ReportInputSnapshot, ReportSubmission
from app.modules.report.ports import (
    ReportArtifactCategory,
    ReportArtifactRef,
    ReportArtifactScope,
)
from app.modules.tasks.adapters.sqlite import (
    SQLiteCallbackControlStore,
    SQLiteConnectionFactory,
    SQLiteTaskControlStore,
    SQLiteTransactionManager,
    build_sqlite_task_control_uow_factories,
)
from app.modules.tasks.application import TaskExecutionAuthoritySession, TaskWorkflowContext
from app.modules.tasks.domain import (
    TaskBusinessRef,
    TaskExecutionSnapshot,
    TaskId,
    TaskOwnerIdentity,
    TaskStepCheckpoint,
)
from app.modules.tasks.ports import (
    CallbackControlMutationOutcome,
    LoadedTaskExecutionInput,
    TaskAdmissionOutcome,
    TaskAdmissionRequest,
    TaskClaimRequest,
    TaskExecutionMutationOutcome,
)
from tests import workspace_tempdir
from tests.fakes import FakeClock


_T0 = "2026-08-13T02:00:00.000000Z"
_T1 = "2026-08-13T02:00:01.000000Z"
_T30 = "2026-08-13T02:00:30.000000Z"


class ReportExecutionUnitOfWorkTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = workspace_tempdir()
        root = Path(self._directory.__enter__())
        old_path = root / "old.sqlite3"
        sqlite3.connect(old_path).close()
        bootstrap = bootstrap_report_task_control_database(
            old_path,
            root / "task-control.sqlite3",
        )
        self.manager = SQLiteTransactionManager(
            SQLiteConnectionFactory(bootstrap, busy_timeout_ms=100)
        )
        self.task_factories = build_sqlite_task_control_uow_factories(self.manager)
        self.clock = FakeClock(_T1)
        fixed_datetime = datetime(2026, 8, 13, 2, 0, 1, tzinfo=timezone.utc)

        def resource_builder(connection):
            backend = SQLiteReportResourceStore.from_connection(connection)
            return SQLiteReportResourceStoreAdapter(
                backend,
                clock=lambda: fixed_datetime,
            )

        self.report_uow = SQLiteReportExecutionUnitOfWorkFactory(
            self.manager,
            execution_builder=SQLiteTaskControlStore,
            callback_delivery_builder=SQLiteCallbackControlStore,
            resource_builder=resource_builder,
        )
        self.task_id = TaskId("report-uow-task")
        self.business_ref = TaskBusinessRef("report", "132")
        self._admit_and_start()

    def tearDown(self) -> None:
        self._directory.__exit__(None, None, None)

    def _admit_and_start(self) -> None:
        request = TaskAdmissionRequest(
            task_id=self.task_id,
            task_type="report",
            business_ref=self.business_ref,
            input_schema_version=2,
            input_snapshot={"report_id": 132},
            input_payload={"report_id": 132},
            public_request_payload={"reportId": 132},
            initial_public_status="0",
            trace_id="trace-report-uow",
            accepted_at=_T0,
        )
        with self.task_factories.admission() as unit_of_work:
            admitted = unit_of_work.admission.admit_one(request)
            self.assertIs(TaskAdmissionOutcome.ACCEPTED, admitted.outcome)
            unit_of_work.commit()
        owner = TaskOwnerIdentity(
            instance_start_id="12345678-1234-4234-8234-123456789abc",
            process_id=301,
            executor_name="ReportExecutor",
            worker_slot="worker-0",
        )
        with self.task_factories.execution() as unit_of_work:
            claimed = unit_of_work.execution.claim(
                TaskClaimRequest(
                    task_id=self.task_id,
                    task_type="report",
                    owner=owner,
                    lease_token="report-uow-lease",
                    claimed_at=_T1,
                    lease_expires_at=_T30,
                )
            )
            self.assertIs(TaskExecutionMutationOutcome.APPLIED, claimed.outcome)
            assert claimed.attempt is not None
            self.authority = claimed.attempt.authority
            self.assertIs(
                TaskExecutionMutationOutcome.APPLIED,
                unit_of_work.execution.start(self.authority, started_at=_T1),
            )
            unit_of_work.commit()
        snapshot = ReportInputSnapshot.from_submission(
            ReportSubmission(
                report_id=ReportId.from_public_value(132),
                source_urls=("https://example.invalid/a.pdf",),
                template_outline_url="https://example.invalid/t.docx",
                template_desc="模板",
                requirement="要求",
                trace_id="trace-report-uow",
            ),
            task_id=self.task_id.value,
            accepted_at=_T0,
            schema_version=1,
        )
        loaded = LoadedTaskExecutionInput(
            snapshot=TaskExecutionSnapshot(
                task_id=self.task_id,
                task_type="report",
                business_ref=self.business_ref,
                execution_state="running",
                public_status="0",
                progress=0.0,
                message="",
                input_snapshot=snapshot,
                accepted_at=_T0,
                trace_id="trace-report-uow",
            ),
            input_schema_version=1,
            input_payload_fingerprint="a" * 64,
        )
        self.context = TaskWorkflowContext(
            session=TaskExecutionAuthoritySession(self.authority),
            loaded_input=loaded,
        )

    def test_uncommitted_step_and_resource_fact_roll_back_together(self) -> None:
        runtime = ReportStepRuntime(uow_factory=self.report_uow, clock=self.clock)
        active = runtime.begin(
            self.context,
            step_key="artifact.scope.begin",
            idempotency_key=f"report:{self.task_id.value}:artifact-scope",
        )
        scope = ReportArtifactScope(self.task_id, "report-uow-task")

        with self.report_uow() as unit_of_work:
            ReportResourceFactService(unit_of_work.resources).register(
                self.task_id,
                self.business_ref,
                scope,
            )
            # 不 commit，模拟进程在资源事实写入后、Step checkpoint 前退出。

        with self.report_uow() as verification:
            self.assertIsNone(verification.resources.get(self.task_id))
            step = verification.execution.get_step(self.task_id, active.step_key)
            assert step is not None
            self.assertEqual("running", step.state.value)

    def test_terminal_step_resource_task_and_callback_guard_commit_atomically(self) -> None:
        runtime = ReportStepRuntime(uow_factory=self.report_uow, clock=self.clock)
        scope_step = runtime.begin(
            self.context,
            step_key="artifact.scope.begin",
            idempotency_key=f"report:{self.task_id.value}:artifact-scope",
        )
        scope = ReportArtifactScope(self.task_id, "report-uow-task")
        runtime.succeed(
            self.context,
            scope_step,
            TaskStepCheckpoint(
                code="artifact_scope_registered_v1",
                result_ref=scope.namespace,
            ),
            resource_mutation=lambda facts: facts.register(
                self.task_id,
                self.business_ref,
                scope,
            ),
        )
        terminal = runtime.begin(
            self.context,
            step_key="terminal.commit",
            idempotency_key=f"report:{self.task_id.value}:terminal:result",
        )
        artifact = ReportArtifactRef(
            task_id=self.task_id,
            artifact_id="output/report.html",
            category=ReportArtifactCategory.REPORT_HTML,
            size_bytes=12,
            checksum="b" * 64,
        )
        runtime.finish(
            self.context,
            terminal,
            succeeded=True,
            public_status="1",
            message="报告生成完成",
            terminal_checkpoint=TaskStepCheckpoint(
                code="terminal_committed_v1",
                result_ref="report-terminal-result",
                result_digest="c" * 64,
            ),
            business_ref=self.business_ref,
            final_artifact=artifact,
        )

        with self.report_uow() as verification:
            task = verification.execution.get_task(self.task_id)
            step = verification.execution.get_step(self.task_id, "terminal.commit")
            record = verification.resources.get(self.task_id)
            guard = verification.callback_delivery.get_admission_conflict(
                self.business_ref
            )
        assert task is not None and step is not None and record is not None
        self.assertEqual("succeeded", task.state.value)
        self.assertEqual("succeeded", step.state.value)
        self.assertEqual(artifact, record.final_artifact)
        self.assertEqual("none", guard.value)

    def test_terminal_callback_failure_rolls_back_step_resource_and_task(self) -> None:
        """终态事务末尾故障不得留下部分成功的 terminal/Artifact 事实。"""

        runtime = ReportStepRuntime(uow_factory=self.report_uow, clock=self.clock)
        scope_step = runtime.begin(
            self.context,
            step_key="artifact.scope.begin",
            idempotency_key=f"report:{self.task_id.value}:artifact-scope",
        )
        scope = ReportArtifactScope(self.task_id, "report-uow-task")
        runtime.succeed(
            self.context,
            scope_step,
            TaskStepCheckpoint(code="scope_v1", result_ref=scope.namespace),
            resource_mutation=lambda facts: facts.register(
                self.task_id,
                self.business_ref,
                scope,
            ),
        )
        terminal = runtime.begin(
            self.context,
            step_key="terminal.commit",
            idempotency_key=f"report:{self.task_id.value}:terminal:rollback",
        )
        artifact = ReportArtifactRef(
            task_id=self.task_id,
            artifact_id="output/report.html",
            category=ReportArtifactCategory.REPORT_HTML,
            size_bytes=12,
            checksum="d" * 64,
        )

        class _RejectEligibility(SQLiteCallbackControlStore):
            def mark_eligible(self, command):
                return CallbackControlMutationOutcome.INVALID_STATE

        failing_factory = SQLiteReportExecutionUnitOfWorkFactory(
            self.manager,
            execution_builder=SQLiteTaskControlStore,
            callback_delivery_builder=_RejectEligibility,
            resource_builder=lambda connection: SQLiteReportResourceStoreAdapter(
                SQLiteReportResourceStore.from_connection(connection),
                clock=lambda: datetime(2026, 8, 13, 2, 0, 1, tzinfo=timezone.utc),
            ),
        )
        failing_runtime = ReportStepRuntime(
            uow_factory=failing_factory,
            clock=self.clock,
        )

        with self.assertRaisesRegex(Exception, "Callback eligibility"):
            failing_runtime.finish(
                self.context,
                terminal,
                succeeded=True,
                public_status="1",
                message="报告生成完成",
                terminal_checkpoint=TaskStepCheckpoint(
                    code="terminal_v1",
                    result_ref="terminal-result",
                    result_digest="e" * 64,
                ),
                business_ref=self.business_ref,
                final_artifact=artifact,
            )

        with self.report_uow() as verification:
            task = verification.execution.get_task(self.task_id)
            step = verification.execution.get_step(self.task_id, "terminal.commit")
            record = verification.resources.get(self.task_id)
        assert task is not None and step is not None and record is not None
        self.assertEqual("running", task.state.value)
        self.assertEqual("running", step.state.value)
        self.assertIsNone(record.final_artifact)


if __name__ == "__main__":
    unittest.main()
