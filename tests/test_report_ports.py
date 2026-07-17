"""阶段 1C-2 报告/任务端口 DTO 与严格 Fake 的协议测试。"""

from __future__ import annotations

import hashlib
import unittest

from app.modules.tasks.domain import ProgressKey, TaskBusinessRef, TaskId
from app.modules.tasks.ports import (
    ProgressPublication,
    ProgressPublisherPort,
    TaskCommandPort,
    TaskSubmissionCommand,
    TaskSubmissionOutcome,
    TaskSubmissionResult,
)

from app.modules.report.domain import ReportId, ReportSubmission, build_report_callback
from app.modules.report.application import ReportResourceRecoveryService
from app.modules.report.ports import (
    AppendReportLifecycleEvents,
    DeliverReportCallback,
    PersistReportRagTrace,
    ReportArtifactCategory,
    ReportArtifactPort,
    ReportArtifactRef,
    ReportArtifactScope,
    ReportAuditReceipt,
    ReportCallbackAcquire,
    ReportCallbackAcquireOutcome,
    ReportCallbackAcquireResult,
    ReportCallbackDeliveryOutcome,
    ReportCallbackDeliveryResult,
    ReportCallbackGuardLease,
    ReportCallbackPort,
    ReportCallbackReleaseOutcome,
    ReportCallbackWaitOutcome,
    ReportCallbackWaitResult,
    ReportFilePort,
    ReportInteractionAuditPort,
    ReportRagAttempt,
    ReportRagAuditOutcome,
    ReportRagExecutionError,
    ReportRagLifecycleEvent,
    ReportRagPort,
    ReportRagRequest,
    ReportRagSource,
    ReportRagTrace,
    ReportResourceRecoveryPort,
    ReportResourceStorePort,
    ReportTaskDispatcherPort,
    WaitForReportCallbackRelease,
    ReleaseUnknownReportCallback,
)
from tests.fakes import (
    FakeProgressPublisherPort,
    FakeReportArtifactPort,
    FakeReportAuditPort,
    FakeReportCallbackPort,
    FakeReportDispatcherPort,
    FakeReportFilePort,
    FakeReportRagPort,
    FakeReportResourceStorePort,
    FakeReportTaskCommandPort,
    InvocationRecorder,
    sample_report_trace,
)


def _submission() -> ReportSubmission:
    return ReportSubmission(
        report_id=ReportId.from_public_value(132),
        source_urls=("http://files.local/a.pdf",),
        template_outline_url="http://files.local/template.docx",
        template_desc="模板",
        requirement="要求",
        trace_id="trace-report-001",
    )


class ReportPortProtocolTests(unittest.TestCase):
    """验证 Fake 真正满足生产 Protocol，而不是只在测试中碰巧同名。"""

    def test_all_report_fakes_satisfy_runtime_protocols(self) -> None:
        recorder = InvocationRecorder()
        cases = (
            (FakeReportTaskCommandPort(recorder), TaskCommandPort),
            (FakeProgressPublisherPort(recorder), ProgressPublisherPort),
            (FakeReportDispatcherPort(recorder), ReportTaskDispatcherPort),
            (FakeReportFilePort(recorder), ReportFilePort),
            (FakeReportArtifactPort(recorder), ReportArtifactPort),
            (FakeReportRagPort(recorder), ReportRagPort),
            (FakeReportAuditPort(recorder), ReportInteractionAuditPort),
            (FakeReportCallbackPort(recorder), ReportCallbackPort),
            (FakeReportResourceStorePort(), ReportResourceStorePort),
        )
        for fake, protocol in cases:
            with self.subTest(protocol=protocol.__name__):
                self.assertIsInstance(fake, protocol)
        resources = ReportResourceRecoveryService(
            store=FakeReportResourceStorePort(),
            artifacts=FakeReportArtifactPort(recorder),
            rag=FakeReportRagPort(recorder),
            audit=FakeReportAuditPort(recorder),
        )
        self.assertIsInstance(resources, ReportResourceRecoveryPort)

    def test_task_submission_command_keeps_business_payload_typed(self) -> None:
        submission = _submission()
        command = TaskSubmissionCommand(
            task_type="report",
            business_ref=TaskBusinessRef("report", "132"),
            input_schema_version=1,
            submission=submission,
            trace_id=submission.trace_id,
        )

        self.assertIs(submission, command.submission)
        with self.assertRaises(ValueError):
            TaskSubmissionResult(TaskSubmissionOutcome.ACTIVE_CONFLICT, object())

    def test_progress_publication_has_internal_identity_without_public_dict(self) -> None:
        publication = ProgressPublication(
            key=ProgressKey("report", "132"),
            expected_task_id=TaskId("task-001"),
            progress=0.15,
            message="正在下载报告文件",
            internal_state="running",
        )

        self.assertEqual(0.15, publication.progress)
        self.assertFalse(hasattr(publication, "payload"))
        self.assertFalse(hasattr(publication, "report_id"))


class ReportPortDtoTests(unittest.TestCase):
    """验证 Artifact、RAG、Audit 和 Callback 的强身份约束。"""

    def test_rag_request_preserves_order_and_rejects_cross_task_artifact(self) -> None:
        task_id = TaskId("task-001")
        first = ReportArtifactRef(
            task_id,
            "source-1",
            ReportArtifactCategory.RAG_INPUT,
            sequence_no=1,
        )
        second = ReportArtifactRef(
            task_id,
            "source-2",
            ReportArtifactCategory.RAG_INPUT,
            sequence_no=2,
        )
        request = ReportRagRequest(
            task_id=task_id,
            trace_id="trace-001",
            ordered_source_files=(first, second),
            prompt="prompt",
            context_name="context",
            conversation_name="conversation",
        )

        self.assertEqual((first, second), request.ordered_source_files)
        foreign = ReportArtifactRef(
            TaskId("task-002"),
            "foreign",
            ReportArtifactCategory.RAG_INPUT,
        )
        with self.assertRaises(ValueError):
            ReportRagRequest(
                task_id=task_id,
                trace_id="trace-001",
                ordered_source_files=(first, foreign),
                prompt="prompt",
                context_name="context",
                conversation_name="conversation",
            )
        wrong_category = ReportArtifactRef(
            task_id,
            "not-rag-input",
            ReportArtifactCategory.SOURCE,
            sequence_no=1,
        )
        with self.assertRaisesRegex(ValueError, "rag_input"):
            ReportRagRequest(
                task_id=task_id,
                trace_id="trace-001",
                ordered_source_files=(wrong_category,),
                prompt="prompt",
                context_name="context",
                conversation_name="conversation",
            )

    def test_rag_trace_requires_strictly_increasing_sequences(self) -> None:
        digest = hashlib.sha256(b"prompt").hexdigest()
        source = ReportRagSource("document:001", "evidence")
        duplicate_attempts = tuple(
            ReportRagAttempt(
                sequence_no=1,
                operation=operation,
                attempt_no=1,
                prompt_kind="report_generation",
                prompt_digest=digest,
                raw_response="ok",
                sources=(source,),
            )
            for operation in ("upload", "query")
        )
        with self.assertRaisesRegex(ValueError, "严格递增"):
            ReportRagTrace(
                trace_id="trace",
                context_name="context",
                context_ref="context-ref",
                conversation_ref="conversation-ref",
                final_call_id="call",
                attempts=duplicate_attempts,
                lifecycle_events=(),
                summary="summary",
            )

    def test_failed_rag_trace_can_end_before_any_model_attempt(self) -> None:
        event = ReportRagLifecycleEvent(
            sequence_no=1,
            operation="context_create",
            attempt_no=1,
            success=False,
            failure_stage="context_create",
            error_message="context unavailable",
        )
        failed_trace = ReportRagTrace(
            trace_id="trace-001",
            context_name="context-001",
            context_ref=None,
            conversation_ref=None,
            attempts=(),
            lifecycle_events=(event,),
            failure_stage="context_create",
            error_message="context unavailable",
        )

        error = ReportRagExecutionError("rag failed", trace=failed_trace)
        self.assertIs(failed_trace, error.trace)
        with self.assertRaisesRegex(ValueError, "至少一次模型调用"):
            ReportRagTrace(
                trace_id="trace-001",
                context_name="context-001",
                context_ref=None,
                conversation_ref=None,
                attempts=(),
                lifecycle_events=(),
            )

    def test_audit_receipt_and_cleanup_append_keep_same_identity(self) -> None:
        task_id = TaskId("task-001")
        trace = sample_report_trace("trace-report-001")
        command = PersistReportRagTrace(
            task_id=task_id,
            business_ref=TaskBusinessRef("report", "132"),
            idempotency_key="report-rag:task-001",
            prompt="prompt",
            trace=trace,
            outcome=ReportRagAuditOutcome.SUCCEEDED,
        )
        receipt = ReportAuditReceipt(
            task_id,
            command.idempotency_key,
            "audit-001",
        )
        append = AppendReportLifecycleEvents(
            receipt,
            (
                ReportRagLifecycleEvent(
                    sequence_no=2,
                    operation="context_delete",
                    attempt_no=1,
                    success=True,
                ),
            ),
        )

        self.assertEqual(task_id, append.receipt.task_id)
        self.assertEqual(TaskBusinessRef("report", "132"), command.business_ref)
        self.assertEqual("prompt", command.prompt)
        with self.assertRaises(ValueError):
            PersistReportRagTrace(
                task_id=task_id,
                business_ref=TaskBusinessRef("report", "132"),
                idempotency_key="report-rag:task-001",
                prompt="prompt",
                trace=trace,
                outcome=ReportRagAuditOutcome.SUCCEEDED,
                error_code="must-be-empty",
            )
        with self.assertRaises(ValueError):
            PersistReportRagTrace(
                task_id=task_id,
                business_ref=TaskBusinessRef("report", "132"),
                idempotency_key="report-rag:task-001",
                prompt="prompt",
                trace=trace,
                outcome=ReportRagAuditOutcome.FAILED,
            )

    def test_callback_acquire_shape_prevents_lease_leak(self) -> None:
        task_id = TaskId("task-001")
        report_id = ReportId.from_public_value(132)
        lease = ReportCallbackGuardLease(
            task_id,
            report_id,
            "guard-001",
            1,
            "2026-07-16T00:01:00+00:00",
        )

        with self.assertRaises(TypeError):
            ReportCallbackAcquireResult(ReportCallbackAcquireOutcome.ACQUIRED)
        with self.assertRaises(ValueError):
            ReportCallbackAcquireResult(
                ReportCallbackAcquireOutcome.STALE,
                lease,
            )

    def test_callback_wait_command_has_positive_bounded_timeout(self) -> None:
        report_id = ReportId.from_public_value(132)
        command = WaitForReportCallbackRelease(report_id, 5)
        result = ReportCallbackWaitResult(ReportCallbackWaitOutcome.RELEASED)

        self.assertEqual(5.0, command.timeout_seconds)
        self.assertEqual(ReportCallbackWaitOutcome.RELEASED, result.outcome)
        for invalid in (0, -1, float("inf"), float("nan"), True):
            with self.subTest(invalid=invalid):
                with self.assertRaises((TypeError, ValueError)):
                    WaitForReportCallbackRelease(report_id, invalid)

    def test_callback_delivery_uses_exact_existing_payload(self) -> None:
        task_id = TaskId("task-001")
        report_id = ReportId.from_public_value(132)
        acquire = ReportCallbackAcquire(task_id, report_id)
        lease = ReportCallbackGuardLease(
            task_id,
            report_id,
            "guard-001",
            1,
            "2026-07-16T00:01:00+00:00",
        )
        payload = build_report_callback(report_id, "<div>ok</div>", status="1")
        delivery = DeliverReportCallback(lease, payload)
        outcome = ReportCallbackDeliveryResult(
            ReportCallbackDeliveryOutcome.SUCCESS
        )

        self.assertEqual(task_id, acquire.task_id)
        self.assertEqual(
            {
                "businessType": "report",
                "data": {
                    "reportId": 132,
                    "status": "1",
                    "details": "<div>ok</div>",
                },
                "msg": "生成成功",
            },
            delivery.payload.to_public_dict(),
        )
        self.assertEqual(ReportCallbackDeliveryOutcome.SUCCESS, outcome.outcome)

    def test_callback_manual_release_requires_bounded_audit_fields(self) -> None:
        report_id = ReportId.from_public_value(132)
        command = ReleaseUnknownReportCallback(
            report_id,
            released_by="operator-001",
            reason="已完成人工核验",
            worker_stopped_confirmed=True,
        )

        self.assertEqual("operator-001", command.released_by)
        self.assertTrue(command.worker_stopped_confirmed)
        self.assertEqual("released", ReportCallbackReleaseOutcome.RELEASED.value)
        with self.assertRaises(ValueError):
            ReleaseUnknownReportCallback(
                report_id,
                released_by=" ",
                reason="reason",
                worker_stopped_confirmed=True,
            )
        with self.assertRaises(ValueError):
            ReleaseUnknownReportCallback(
                report_id,
                released_by="operator",
                reason="x" * 513,
                worker_stopped_confirmed=True,
            )
        with self.assertRaises(ValueError):
            ReleaseUnknownReportCallback(
                report_id,
                released_by="operator",
                reason="尚未隔离旧 Worker",
                worker_stopped_confirmed=False,
            )

    def test_artifact_scope_and_ref_never_expose_real_path_field(self) -> None:
        task_id = TaskId("task-001")
        scope = ReportArtifactScope(task_id, "runtime/tasks/task-001")
        ref = ReportArtifactRef(
            task_id,
            "artifact-001",
            ReportArtifactCategory.REPORT_HTML,
            size_bytes=12,
            checksum="sha256:abc",
        )

        self.assertEqual(task_id, scope.task_id)
        self.assertFalse(hasattr(ref, "path"))
        self.assertFalse(hasattr(ref, "url"))


if __name__ == "__main__":
    unittest.main()
