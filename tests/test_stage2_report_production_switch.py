"""阶段 2-4 第 6 步 Report 一次切换的定向离线验收。"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app.modules.report.adapters import (
    LocalReportArtifactAdapter,
    ReportTaskCommandCodec,
    SQLiteReportV2CallbackRecoverySource,
    TaskControlReportCallbackAdapter,
)
from app.modules.report.adapters.sqlite import bootstrap_report_task_control_database
from app.modules.report.application import SubmitReportV2Task
from app.modules.report.domain import (
    REPORT_EMPTY_RESULT_POLICY,
    REPORT_EXECUTION_PROFILE_SCHEMA_NAME,
    REPORT_EXECUTION_PROFILE_SCHEMA_VERSION,
    ReportExecutionProfile,
    ReportId,
    ReportSubmission,
    build_report_callback,
)
from app.modules.report.ports import (
    DeliverReportCallback,
    ReportCallbackAcquire,
    ReportCallbackDeliveryOutcome,
    ReportCallbackDeliveryResult,
    ReportResourceRecord,
)
from app.modules.tasks.adapters import SQLiteTaskControlReadAdapter
from app.modules.tasks.adapters.sqlite import (
    SQLiteConnectionFactory,
    SQLiteTransactionManager,
    build_sqlite_task_control_uow_factories,
)
from app.modules.tasks.domain import (
    TaskBusinessRef,
    TaskId,
    TaskOwnerIdentity,
    TaskTransition,
    TaskSnapshot,
    add_persisted_utc_seconds,
)
from app.modules.tasks.ports import (
    CallbackControlMutationOutcome,
    CallbackEligibilityCommand,
    TaskClaimRequest,
    TaskExecutionMutationOutcome,
    TaskTerminalCommand,
)
from tests.fakes import (
    FakeClock,
    FakeProgressPublisherPort,
    FakeReportResourceStorePort,
    FakeReportDispatcherPort,
    FakeTaskReadPort,
    InvocationRecorder,
)


def _profile() -> ReportExecutionProfile:
    return ReportExecutionProfile(
        schema_name=REPORT_EXECUTION_PROFILE_SCHEMA_NAME,
        schema_version=REPORT_EXECUTION_PROFILE_SCHEMA_VERSION,
        source_transport_profile_id="report-http-source.v1",
        max_download_bytes=1024 * 1024,
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


def _submission() -> ReportSubmission:
    return ReportSubmission(
        report_id=ReportId.from_public_value(4206),
        source_urls=("https://example.invalid/source.pdf",),
        template_outline_url="https://example.invalid/template.docx",
        template_desc="模板说明",
        requirement="报告要求",
        trace_id="trace-report-production-switch",
    )


class ReportProductionSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        root = Path(self._directory.name)
        self.old_path = root / "legacy.sqlite3"
        self.v2_path = root / "task-control-v2.sqlite3"
        sqlite3.connect(self.old_path).close()
        bootstrap = bootstrap_report_task_control_database(
            self.old_path,
            self.v2_path,
        )
        self.manager = SQLiteTransactionManager(
            SQLiteConnectionFactory(bootstrap, busy_timeout_ms=100)
        )
        self.factories = build_sqlite_task_control_uow_factories(self.manager)
        self.reader = SQLiteTaskControlReadAdapter(self.manager)
        self.clock = FakeClock("2026-08-13T00:00:00.000000Z")

    def tearDown(self) -> None:
        self._directory.cleanup()

    def test_submit_writes_only_v2_and_read_adapter_returns_same_task(self) -> None:
        recorder = InvocationRecorder()
        dispatcher = FakeReportDispatcherPort(recorder)
        submit = SubmitReportV2Task(
            admission_uow_factory=self.factories.admission,
            codec=ReportTaskCommandCodec.for_v2(_profile()),
            clock=self.clock,
            progress_publisher=FakeProgressPublisherPort(recorder),
            dispatcher=dispatcher,
            task_id_factory=lambda: TaskId(
                "12345678-1234-4234-8234-123456789abc"
            ),
        )

        result = submit.execute(_submission())
        snapshot = self.reader.get_latest(TaskBusinessRef("report", "4206"))

        self.assertEqual(result.task_id, snapshot.task_id)  # type: ignore[union-attr]
        self.assertEqual("accepted", snapshot.execution_state)  # type: ignore[union-attr]
        self.assertEqual("0", snapshot.public_status)  # type: ignore[union-attr]
        self.assertEqual([result.task_id], dispatcher.task_ids)
        with closing(sqlite3.connect(self.v2_path)) as connection:
            self.assertEqual(
                2,
                connection.execute(
                    "SELECT input_schema_version FROM llm_task_executions"
                ).fetchone()[0],
            )
        # 首次切换旧库是空现场；v2 受理后仍不得反向创建旧 Report Task 表或双写行。
        with closing(sqlite3.connect(self.old_path)) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_schema WHERE name='llm_tasks'"
                ).fetchone()
            )

    def test_callback_http_is_authorized_and_completed_by_v2_guard(self) -> None:
        submit = SubmitReportV2Task(
            admission_uow_factory=self.factories.admission,
            codec=ReportTaskCommandCodec.for_v2(_profile()),
            clock=self.clock,
            progress_publisher=FakeProgressPublisherPort(InvocationRecorder()),
            dispatcher=FakeReportDispatcherPort(InvocationRecorder()),
            task_id_factory=lambda: TaskId(
                "12345678-1234-4234-8234-123456789abd"
            ),
        )
        task_id = submit.execute(_submission()).task_id
        self.clock.advance(seconds=1)
        with self.factories.execution() as unit_of_work:
            claim = unit_of_work.execution.claim(
                TaskClaimRequest(
                    task_id=task_id,
                    task_type="report",
                    owner=TaskOwnerIdentity(
                        instance_start_id="12345678-1234-4234-8234-123456789abe",
                        process_id=1,
                        executor_name="ReportExecutor",
                        worker_slot="worker-0",
                    ),
                    lease_token="task-lease-secret",
                    claimed_at=self.clock.now_utc(),
                    lease_expires_at=add_persisted_utc_seconds(
                        self.clock.now_utc(),
                        seconds=30,
                    ),
                )
            )
            self.assertIs(TaskExecutionMutationOutcome.APPLIED, claim.outcome)
            authority = claim.attempt.authority  # type: ignore[union-attr]
            self.assertIs(
                TaskExecutionMutationOutcome.APPLIED,
                unit_of_work.execution.start(
                    authority,
                    started_at=self.clock.now_utc(),
                ),
            )
            unit_of_work.commit()
        self.clock.advance(seconds=1)
        with self.factories.execution() as unit_of_work:
            completed_at = self.clock.now_utc()
            self.assertIs(
                TaskExecutionMutationOutcome.APPLIED,
                unit_of_work.execution.finish(
                    TaskTerminalCommand(
                        authority=authority,
                        transition=TaskTransition.BUSINESS_SUCCEEDED,
                        public_status="1",
                        message="报告生成完成",
                        result_ref="report-artifact:v1:test",
                        completed_at=completed_at,
                    )
                ),
            )
            self.assertIs(
                CallbackControlMutationOutcome.APPLIED,
                unit_of_work.callback_delivery.mark_eligible(
                    CallbackEligibilityCommand(
                        authority=authority,
                        business_ref=TaskBusinessRef("report", "4206"),
                        eligible_at=completed_at,
                    )
                ),
            )
            unit_of_work.commit()

        callback = TaskControlReportCallbackAdapter(
            self.factories.callback_delivery,
            clock=self.clock,
            callback_url="https://callback.invalid/report",
            callback_timeout=1.0,
            lease_seconds=30.0,
            token_factory=lambda: "callback-lease-secret",
            transport=lambda payload: ReportCallbackDeliveryResult(
                ReportCallbackDeliveryOutcome.SUCCESS,
                "http_status=200",
            ),
        )
        acquired = callback.acquire(
            ReportCallbackAcquire(task_id, ReportId.from_public_value(4206))
        )
        payload = build_report_callback(
            ReportId.from_public_value(4206),
            "<html><body>报告</body></html>",
            status="1",
        )
        delivery = callback.deliver(DeliverReportCallback(acquired.lease, payload))  # type: ignore[arg-type]
        with patch(
            "app.modules.report.adapters.v2_callback.save_callback_history_payload"
        ):
            self.assertTrue(callback.complete(acquired.lease, delivery, payload))  # type: ignore[arg-type]
        snapshot = self.reader.get_by_id(task_id)
        self.assertEqual("success", snapshot.callback_status)  # type: ignore[union-attr]
        self.assertEqual(1, snapshot.callback_attempts)  # type: ignore[union-attr]

        # 同一 ReportId 后续允许产生新的 latest execution。旧 TaskId 的公开快照必须仍
        # 保留自己的 Callback attempt，不能因为离开 llm_tasks latest 行就回落为 0。
        next_task_id = TaskId("12345678-1234-4234-8234-123456789ac0")
        SubmitReportV2Task(
            admission_uow_factory=self.factories.admission,
            codec=ReportTaskCommandCodec.for_v2(_profile()),
            clock=self.clock,
            progress_publisher=FakeProgressPublisherPort(InvocationRecorder()),
            dispatcher=FakeReportDispatcherPort(InvocationRecorder()),
            task_id_factory=lambda: next_task_id,
        ).execute(_submission())
        self.assertEqual(next_task_id, self.reader.get_latest(
            TaskBusinessRef("report", "4206")
        ).task_id)  # type: ignore[union-attr]
        old_snapshot = self.reader.get_by_id(task_id)
        self.assertEqual("success", old_snapshot.callback_status)  # type: ignore[union-attr]
        self.assertEqual(1, old_snapshot.callback_attempts)  # type: ignore[union-attr]

    def test_check_task_recovery_rebuilds_exact_public_html_from_artifact(self) -> None:
        task_id = TaskId("12345678-1234-4234-8234-123456789abf")
        business_ref = TaskBusinessRef("report", "4206")
        task_reader = FakeTaskReadPort(
            (
                TaskSnapshot(
                    task_id=task_id,
                    task_type="report",
                    business_ref=business_ref,
                    execution_state="succeeded",
                    public_status="1",
                    progress=1.0,
                    message="报告生成完成",
                    callback_status="failed",
                    created_at="2026-08-13T00:00:00.000000Z",
                    updated_at="2026-08-13T00:00:02.000000Z",
                    callback_attempts=1,
                ),
            )
        )
        artifacts = LocalReportArtifactAdapter(
            Path(self._directory.name) / "artifacts"
        )
        scope = artifacts.begin(task_id)
        expected_html = "<html><body><h1>精确恢复</h1></body></html>"
        final_artifact = artifacts.persist_report_html(scope, expected_html)
        resources = FakeReportResourceStorePort()
        resources.create(
            ReportResourceRecord(
                task_id=task_id,
                business_ref=business_ref,
                scope=scope,
                final_artifact=final_artifact,
            )
        )
        source = SQLiteReportV2CallbackRecoverySource(
            task_reader=task_reader,
            resources=resources,
            artifacts=artifacts,
        )

        candidate = source.load_recoverable(ReportId.from_public_value(4206))

        self.assertIsNotNone(candidate)
        self.assertEqual(expected_html, candidate.payload.details)  # type: ignore[union-attr]
        self.assertEqual("1", candidate.payload.status)  # type: ignore[union-attr]
        self.assertEqual(1, candidate.callback_attempts)  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
