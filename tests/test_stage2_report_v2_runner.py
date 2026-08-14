"""阶段 2-4 Report v2 Runner 的临时 SQLite + Fake 定向验收。"""

from __future__ import annotations

from dataclasses import replace
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
from app.modules.report.application import (
    REPORT_STEP_REGISTRY,
    ReportResourceRecoveryService,
    ReportStepRuntime,
    RunReportOutcome,
    RunReportV2Workflow,
)
from app.modules.report.domain import (
    REPORT_EMPTY_RESULT_POLICY,
    REPORT_EXECUTION_PROFILE_SCHEMA_NAME,
    REPORT_EXECUTION_PROFILE_SCHEMA_VERSION,
    REPORT_INPUT_SCHEMA_VERSION_V2,
    ReportExecutionProfile,
    ReportId,
    ReportInputSnapshot,
    ReportPortContractError,
    ReportSubmission,
    ReportTaskPersistenceError,
)
from app.modules.report.ports import (
    ReportRagCleanupRef,
    ReportRagExecutionError,
    ReportResourceState,
)
from app.modules.tasks.adapters.sqlite import (
    SQLiteCallbackControlStore,
    SQLiteConnectionFactory,
    SQLiteTaskControlStore,
    SQLiteTransactionManager,
    build_sqlite_task_control_uow_factories,
)
from app.modules.tasks.application import TaskExecutionAuthoritySession, TaskWorkflowContext
from app.modules.tasks.domain import TaskBusinessRef, TaskExecutionSnapshot, TaskId, TaskOwnerIdentity
from app.modules.tasks.ports import (
    LoadedTaskExecutionInput,
    TaskAdmissionOutcome,
    TaskAdmissionRequest,
    TaskClaimRequest,
    TaskExecutionMutationOutcome,
    TaskExecutionStopRequested,
)
from tests import workspace_tempdir
from tests.fakes import (
    FakeClock,
    FakeProgressPublisherPort,
    FakeReportArtifactPort,
    FakeReportAuditPort,
    FakeReportCallbackPort,
    FakeReportFilePort,
    FakeReportRagPort,
    InvocationRecorder,
    sample_failed_report_trace,
)


_T0 = "2026-08-13T03:00:00.000000Z"
_T1 = "2026-08-13T03:00:01.000000Z"
_T30 = "2026-08-13T03:00:30.000000Z"


def _profile() -> ReportExecutionProfile:
    return ReportExecutionProfile(
        schema_name=REPORT_EXECUTION_PROFILE_SCHEMA_NAME,
        schema_version=REPORT_EXECUTION_PROFILE_SCHEMA_VERSION,
        source_transport_profile_id="report-http-source.v1",
        max_download_bytes=512 * 1024 * 1024,
        document_processing_profile_id="report-document-pipeline.v1",
        document_processing_fingerprint="1" * 64,
        template_extractor_profile_id="docx-template-text.v1",
        rag_provider_id="anythingllm",
        rag_provider_fingerprint="2" * 64,
        rag_model_fingerprint="3" * 64,
        rag_workspace_settings_fingerprint="4" * 64,
        rag_upload_policy_fingerprint="5" * 64,
        prompt_profile_id="report-prompt.v1",
        sanitizer_profile_id="report-public-sanitizer.v1",
        renderer_profile_id="report-html-renderer.v1",
        empty_result_policy=REPORT_EMPTY_RESULT_POLICY,
    )


class ReportV2RunnerTests(unittest.TestCase):
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
        self.fixed_datetime = datetime(2026, 8, 13, 3, 0, 1, tzinfo=timezone.utc)

        def resource_builder(connection):
            return SQLiteReportResourceStoreAdapter(
                SQLiteReportResourceStore.from_connection(connection),
                clock=lambda: self.fixed_datetime,
            )

        self.report_uow = SQLiteReportExecutionUnitOfWorkFactory(
            self.manager,
            execution_builder=SQLiteTaskControlStore,
            callback_delivery_builder=SQLiteCallbackControlStore,
            resource_builder=resource_builder,
        )
        self.task_id = TaskId("report-v2-runner")
        self.business_ref = TaskBusinessRef("report", "132")
        self.snapshot = self._snapshot()
        self.context = self._context()
        self.recorder = InvocationRecorder()
        self.progress = FakeProgressPublisherPort(self.recorder)
        self.files = FakeReportFilePort(self.recorder)
        self.artifacts = FakeReportArtifactPort(self.recorder)
        self.rag = FakeReportRagPort(self.recorder)
        self.audit = FakeReportAuditPort(self.recorder)
        self.callbacks = FakeReportCallbackPort(self.recorder)
        independent_store = SQLiteReportResourceStoreAdapter(
            SQLiteReportResourceStore(self.manager),
            clock=lambda: self.fixed_datetime,
        )
        self.resources = ReportResourceRecoveryService(
            store=independent_store,
            artifacts=self.artifacts,
            rag=self.rag,
            audit=self.audit,
        )
        self.maintenance_wakeup_count = 0

        def wake_maintenance() -> None:
            self.maintenance_wakeup_count += 1

        self.runner = RunReportV2Workflow(
            steps=ReportStepRuntime(
                uow_factory=self.report_uow,
                clock=FakeClock(_T1),
            ),
            progress_publisher=self.progress,
            files=self.files,
            artifacts=self.artifacts,
            rag=self.rag,
            audit=self.audit,
            callbacks=self.callbacks,
            resources=self.resources,
            execution_profile=_profile(),
            maintenance_wakeup=wake_maintenance,
        )

    def tearDown(self) -> None:
        self._directory.__exit__(None, None, None)

    def _snapshot(self) -> ReportInputSnapshot:
        return ReportInputSnapshot.from_submission(
            ReportSubmission(
                report_id=ReportId.from_public_value(132),
                source_urls=(
                    "https://example.invalid/a.pdf",
                    "https://example.invalid/b.mhtml",
                ),
                template_outline_url="https://example.invalid/template.docx",
                template_desc="模板说明",
                requirement="报告要求",
                trace_id="trace-report-v2-runner",
            ),
            task_id=self.task_id.value,
            accepted_at=_T0,
            schema_version=REPORT_INPUT_SCHEMA_VERSION_V2,
            execution_profile=_profile(),
        )

    def _context(self) -> TaskWorkflowContext:
        request = TaskAdmissionRequest(
            task_id=self.task_id,
            task_type="report",
            business_ref=self.business_ref,
            input_schema_version=REPORT_INPUT_SCHEMA_VERSION_V2,
            input_snapshot={"report_id": 132},
            input_payload={"report_id": 132},
            public_request_payload={"reportId": 132},
            initial_public_status="0",
            trace_id=self.snapshot.trace_id,
            accepted_at=_T0,
        )
        with self.task_factories.admission() as unit_of_work:
            admitted = unit_of_work.admission.admit_one(request)
            self.assertIs(TaskAdmissionOutcome.ACCEPTED, admitted.outcome)
            unit_of_work.commit()
        owner = TaskOwnerIdentity(
            instance_start_id="12345678-1234-4234-8234-123456789abc",
            process_id=401,
            executor_name="ReportExecutor",
            worker_slot="worker-0",
        )
        with self.task_factories.execution() as unit_of_work:
            claimed = unit_of_work.execution.claim(
                TaskClaimRequest(
                    task_id=self.task_id,
                    task_type="report",
                    owner=owner,
                    lease_token="report-v2-runner-lease",
                    claimed_at=_T1,
                    lease_expires_at=_T30,
                )
            )
            self.assertIs(TaskExecutionMutationOutcome.APPLIED, claimed.outcome)
            assert claimed.attempt is not None
            authority = claimed.attempt.authority
            self.assertIs(
                TaskExecutionMutationOutcome.APPLIED,
                unit_of_work.execution.start(authority, started_at=_T1),
            )
            unit_of_work.commit()
        loaded = LoadedTaskExecutionInput(
            snapshot=TaskExecutionSnapshot(
                task_id=self.task_id,
                task_type="report",
                business_ref=self.business_ref,
                execution_state="running",
                public_status="0",
                progress=0.0,
                message="",
                input_snapshot=self.snapshot,
                accepted_at=_T0,
                trace_id=self.snapshot.trace_id,
            ),
            input_schema_version=REPORT_INPUT_SCHEMA_VERSION_V2,
            input_payload_fingerprint="a" * 64,
        )
        return TaskWorkflowContext(
            session=TaskExecutionAuthoritySession(authority),
            loaded_input=loaded,
        )

    def test_stop_requested_exits_before_first_step_and_external_operation(self) -> None:
        """正常停机应在首个 Step 边界退出，且不得把 Task 错误终态化。"""

        self.assertTrue(self.context.request_cancellation())

        with self.assertRaises(TaskExecutionStopRequested) as raised:
            self.runner.run(self.context)

        self.assertEqual("stopped", raised.exception.result.outcome.value)
        with self.report_uow() as unit_of_work:
            task = unit_of_work.execution.get_task(self.task_id)
        with self.manager.begin(read_only=True) as transaction:
            step_count = transaction.connection.execute(
                "SELECT COUNT(*) FROM task_steps WHERE task_id = ?",
                (self.task_id.value,),
            ).fetchone()[0]
            transaction.commit()
        assert task is not None
        self.assertEqual("running", task.state.value)
        self.assertEqual(0, int(step_count))
        self.assertEqual([], self.files.source_downloads)

    def test_success_runs_every_registered_step_and_commits_terminal(self) -> None:
        self.runner.run(self.context)

        result = self.runner.last_result
        assert result is not None
        self.assertIs(RunReportOutcome.SUCCEEDED, result.outcome)
        with self.report_uow() as unit_of_work:
            task = unit_of_work.execution.get_task(self.task_id)
            resource = unit_of_work.resources.get(self.task_id)
        with self.manager.begin(read_only=True) as transaction:
            rows = transaction.connection.execute(
                "SELECT step_key, state FROM task_steps WHERE task_id = ?",
                (self.task_id.value,),
            ).fetchall()
            transaction.commit()
        assert task is not None and resource is not None
        self.assertEqual("succeeded", task.state.value)
        self.assertTrue(resource.final_artifact is not None)
        # Worker 只提交终态并发出可丢提示，不同步等待外部 DELETE。资源记录继续作为
        # 周期维护扫描的持久真相，清理失败也不可能反转已经提交的成功终态。
        self.assertIs(ReportResourceState.TRACKING, resource.state)
        self.assertEqual(1, self.maintenance_wakeup_count)
        actual = {str(row["step_key"]): str(row["state"]) for row in rows}
        expected = {
            definition.key_pattern
            for definition in REPORT_STEP_REGISTRY
            if "{" not in definition.key_pattern
        }
        expected.update(
            {
                "source.download:1",
                "source.download:2",
                "document.prepare:1",
                "document.prepare:2",
                "rag.document.upload:1",
                "rag.document.upload:2",
                "rag.document.bind:1",
                "rag.document.bind:2",
            }
        )
        self.assertEqual(expected, set(actual))
        self.assertEqual({"succeeded"}, set(actual.values()))

    def test_unknown_rag_write_is_audited_and_quarantined_without_terminal(self) -> None:
        class _UnknownRag(FakeReportRagPort):
            def generate(inner_self, request):
                inner_self.requests.append(request)
                assert request.step_observer is not None
                request.step_observer.begin(
                    "rag.session.open",
                    f"report:{request.task_id.value}:rag-session",
                )
                raise ReportRagExecutionError(
                    "workspace create outcome unknown",
                    trace=sample_failed_report_trace(
                        request.trace_id,
                        context_name=request.context_name,
                        failure_stage="context_create_outcome_unknown",
                    ),
                    cleanup_ref=ReportRagCleanupRef("cleanup:unknown-workspace"),
                    external_outcome_unknown=True,
                    active_step_key="rag.session.open",
                )

        unknown_rag = _UnknownRag(self.recorder)
        resources = ReportResourceRecoveryService(
            store=SQLiteReportResourceStoreAdapter(
                SQLiteReportResourceStore(self.manager),
                clock=lambda: self.fixed_datetime,
            ),
            artifacts=self.artifacts,
            rag=unknown_rag,
            audit=self.audit,
        )
        runner = RunReportV2Workflow(
            steps=ReportStepRuntime(
                uow_factory=self.report_uow,
                clock=FakeClock(_T1),
            ),
            progress_publisher=self.progress,
            files=self.files,
            artifacts=self.artifacts,
            rag=unknown_rag,
            audit=self.audit,
            callbacks=self.callbacks,
            resources=resources,
            execution_profile=_profile(),
        )

        runner.run(self.context)

        result = runner.last_result
        assert result is not None
        self.assertIs(RunReportOutcome.RECOVERY_REQUIRED, result.outcome)
        with self.report_uow() as unit_of_work:
            task = unit_of_work.execution.get_task(self.task_id)
            rag_step = unit_of_work.execution.get_step(
                self.task_id,
                "rag.session.open",
            )
            audit_step = unit_of_work.execution.get_step(
                self.task_id,
                "interaction_audit.commit",
            )
            terminal = unit_of_work.execution.get_step(
                self.task_id,
                "terminal.commit",
            )
            resource = unit_of_work.resources.get(self.task_id)
        assert task is not None and rag_step is not None
        assert audit_step is not None and resource is not None
        self.assertEqual("recovery_required", task.state.value)
        self.assertEqual("outcome_unknown", rag_step.state.value)
        self.assertEqual("succeeded", audit_step.state.value)
        self.assertIsNone(terminal)
        self.assertIs(ReportResourceState.QUARANTINED, resource.state)
        self.assertEqual([], self.callbacks.acquire_calls)
        self.assertEqual([], self.rag.cleanup_calls)

    def test_profile_mismatch_stops_before_first_external_operation(self) -> None:
        """受理快照与 Worker 当前能力不同，必须在 Artifact 作用域前 fail closed。"""

        incompatible_runner = RunReportV2Workflow(
            steps=ReportStepRuntime(
                uow_factory=self.report_uow,
                clock=FakeClock(_T1),
            ),
            progress_publisher=self.progress,
            files=self.files,
            artifacts=self.artifacts,
            rag=self.rag,
            audit=self.audit,
            callbacks=self.callbacks,
            resources=self.resources,
            execution_profile=replace(_profile(), max_download_bytes=1024),
        )

        with self.assertRaises(ReportPortContractError):
            incompatible_runner.run(self.context)

        self.assertEqual([], self.recorder.events)
        with self.manager.begin(read_only=True) as transaction:
            step_count = transaction.connection.execute(
                "SELECT COUNT(*) AS count FROM task_steps WHERE task_id = ?",
                (self.task_id.value,),
            ).fetchone()["count"]
            transaction.commit()
        self.assertEqual(0, step_count)

    def test_audit_commit_unknown_is_quarantined_without_terminal(self) -> None:
        """审计写异常无法证明未提交，必须隔离而不是补写失败终态。"""

        self.audit.persist_error = OSError("simulated audit connection loss")

        with self.assertRaises(ReportTaskPersistenceError):
            self.runner.run(self.context)

        with self.report_uow() as unit_of_work:
            task = unit_of_work.execution.get_task(self.task_id)
            audit_step = unit_of_work.execution.get_step(
                self.task_id,
                "interaction_audit.commit",
            )
            terminal = unit_of_work.execution.get_step(
                self.task_id,
                "terminal.commit",
            )
            resource = unit_of_work.resources.get(self.task_id)
        assert task is not None and audit_step is not None and resource is not None
        self.assertEqual("recovery_required", task.state.value)
        self.assertEqual("outcome_unknown", audit_step.state.value)
        self.assertIsNone(terminal)
        self.assertIs(ReportResourceState.QUARANTINED, resource.state)
        self.assertEqual([], self.callbacks.acquire_calls)
        self.assertEqual([], self.rag.cleanup_calls)

    def test_unclassified_exception_after_rag_intent_is_never_terminalized(self) -> None:
        """Port 漏分类型也不能让已开始的外部写被当作普通业务失败。"""

        class _UnclassifiedRag(FakeReportRagPort):
            def generate(inner_self, request):
                assert request.step_observer is not None
                request.step_observer.begin(
                    "rag.generate",
                    f"report:{request.task_id.value}:generation:test",
                )
                raise OSError("simulated connection reset after request write")

        unclassified_rag = _UnclassifiedRag(self.recorder)
        runner = RunReportV2Workflow(
            steps=ReportStepRuntime(
                uow_factory=self.report_uow,
                clock=FakeClock(_T1),
            ),
            progress_publisher=self.progress,
            files=self.files,
            artifacts=self.artifacts,
            rag=unclassified_rag,
            audit=self.audit,
            callbacks=self.callbacks,
            resources=ReportResourceRecoveryService(
                store=SQLiteReportResourceStoreAdapter(
                    SQLiteReportResourceStore(self.manager),
                    clock=lambda: self.fixed_datetime,
                ),
                artifacts=self.artifacts,
                rag=unclassified_rag,
                audit=self.audit,
            ),
            execution_profile=_profile(),
        )

        runner.run(self.context)

        with self.report_uow() as unit_of_work:
            task = unit_of_work.execution.get_task(self.task_id)
            rag_step = unit_of_work.execution.get_step(self.task_id, "rag.generate")
            terminal = unit_of_work.execution.get_step(self.task_id, "terminal.commit")
            resource = unit_of_work.resources.get(self.task_id)
        assert task is not None and rag_step is not None and resource is not None
        self.assertEqual("recovery_required", task.state.value)
        self.assertEqual("outcome_unknown", rag_step.state.value)
        self.assertIsNone(terminal)
        self.assertIs(ReportResourceState.QUARANTINED, resource.state)
        self.assertEqual([], self.callbacks.acquire_calls)
        self.assertEqual([], unclassified_rag.cleanup_calls)


if __name__ == "__main__":
    unittest.main()
