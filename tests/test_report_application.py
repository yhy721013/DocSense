"""阶段 1C-2：报告 Submit/Run Application 的无框架编排与故障测试。"""

from __future__ import annotations

import unittest

from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.tasks.ports import TaskClaimOutcome, TaskSubmissionOutcome

from app.modules.report.application import (
    ReportResourceRecoveryService,
    ReportTaskCompletion,
    RunReportOutcome,
    RunReportTask,
    SubmitReportTask,
)
from app.modules.report.domain import (
    REPORT_STATUS_FAILED,
    REPORT_STATUS_SUCCEEDED,
    ReportAuditError,
    ReportId,
    ReportPortContractError,
    ReportSourceNormalizationError,
    ReportSubmission,
    ReportTaskConflictError,
    ReportTaskPersistenceError,
)
from app.modules.report.ports import (
    ReportArtifactCategory,
    ReportArtifactRef,
    ReportAuditReceipt,
    ReportCallbackAcquireOutcome,
    ReportCallbackDeliveryOutcome,
    ReportRagAuditOutcome,
    ReportRagCleanupRef,
    ReportRagExecutionError,
    ReportRagResponse,
    ReportResourceState,
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
    sample_failed_report_trace,
    sample_report_trace,
)


def _submission(*, source_count: int = 2) -> ReportSubmission:
    """构造保留顺序与重复语义的报告提交命令。"""

    return ReportSubmission(
        report_id=ReportId.from_public_value(132),
        source_urls=tuple(
            f"http://files.local/source-{index}.mhtml"
            for index in range(1, source_count + 1)
        ),
        template_outline_url="http://files.local/template.docx",
        template_desc="正式模板",
        requirement="完整生成报告",
        trace_id="trace-report-001",
    )


class _ReportHarness:
    """为每个用例建立彼此隔离、无数据库/网络/文件的测试装配。"""

    def __init__(self, *, source_count: int = 2) -> None:
        self.recorder = InvocationRecorder()
        self.tasks = FakeReportTaskCommandPort(self.recorder)
        self.progress = FakeProgressPublisherPort(self.recorder)
        self.dispatcher = FakeReportDispatcherPort(self.recorder)
        self.files = FakeReportFilePort(self.recorder)
        self.artifacts = FakeReportArtifactPort(self.recorder)
        self.rag = FakeReportRagPort(self.recorder)
        self.audit = FakeReportAuditPort(self.recorder)
        self.callbacks = FakeReportCallbackPort(self.recorder)
        self.resource_store = FakeReportResourceStorePort(
            lambda task_id: self.tasks.executions.get(task_id)
        )
        self.resources = ReportResourceRecoveryService(
            store=self.resource_store,
            artifacts=self.artifacts,
            rag=self.rag,
            audit=self.audit,
        )
        self.submission = _submission(source_count=source_count)
        self.submit_service = SubmitReportTask(
            task_commands=self.tasks,
            progress_publisher=self.progress,
            dispatcher=self.dispatcher,
        )
        self.submit_result = self.submit_service.execute(self.submission)
        self.task_id = self.submit_result.task_id
        self.run_service = RunReportTask(
            task_commands=self.tasks,
            progress_publisher=self.progress,
            files=self.files,
            artifacts=self.artifacts,
            rag=self.rag,
            audit=self.audit,
            callbacks=self.callbacks,
            resources=self.resources,
        )
        # Run 测试只观察 Worker 阶段；Submit 自身的顺序由独立测试冻结。
        self.recorder.events.clear()
        self.progress.publications.clear()


class SubmitReportApplicationTests(unittest.TestCase):
    """验证持久化受理与通知副作用的边界。"""

    def test_submit_persists_before_progress_and_dispatch(self) -> None:
        harness = _ReportHarness(source_count=1)
        command = harness.tasks.submission_calls[0]
        execution = harness.tasks.executions[harness.task_id]

        self.assertEqual("report", command.task_type)
        self.assertEqual(TaskBusinessRef("report", "132"), command.business_ref)
        self.assertEqual(harness.submission, command.submission)
        self.assertEqual(harness.task_id.value, execution.input_snapshot.task_id)
        self.assertEqual(harness.submission.source_urls, execution.input_snapshot.source_urls)
        self.assertTrue(harness.submit_result.progress_notified)
        self.assertTrue(harness.submit_result.dispatcher_notified)

    def test_submit_global_call_order_is_create_then_progress_then_dispatch(self) -> None:
        recorder = InvocationRecorder()
        tasks = FakeReportTaskCommandPort(recorder)
        progress = FakeProgressPublisherPort(recorder)
        dispatcher = FakeReportDispatcherPort(recorder)
        service = SubmitReportTask(
            task_commands=tasks,
            progress_publisher=progress,
            dispatcher=dispatcher,
        )

        service.execute(_submission(source_count=1))

        self.assertEqual(
            ["task.create", "progress.publish:0.0", "dispatcher.dispatch"],
            recorder.events,
        )

    def test_active_and_unknown_conflicts_map_to_same_report_conflict(self) -> None:
        for outcome in (
            TaskSubmissionOutcome.ACTIVE_CONFLICT,
            TaskSubmissionOutcome.CALLBACK_OUTCOME_UNKNOWN,
        ):
            with self.subTest(outcome=outcome):
                recorder = InvocationRecorder()
                tasks = FakeReportTaskCommandPort(recorder)
                submission = _submission(source_count=1)
                tasks.submission_outcomes[
                    TaskBusinessRef("report", submission.report_id.business_key)
                ] = outcome
                service = SubmitReportTask(
                    task_commands=tasks,
                    progress_publisher=FakeProgressPublisherPort(recorder),
                    dispatcher=FakeReportDispatcherPort(recorder),
                )

                with self.assertRaisesRegex(
                    ReportTaskConflictError,
                    "任务正在处理中",
                ):
                    service.execute(submission)

                self.assertEqual(["task.create"], recorder.events)

    def test_callback_sending_is_rejected_immediately_without_blocking_web_thread(self) -> None:
        recorder = InvocationRecorder()
        tasks = FakeReportTaskCommandPort(recorder)
        tasks.submission_outcome_sequence = [TaskSubmissionOutcome.CALLBACK_SENDING]
        callbacks = FakeReportCallbackPort(recorder)
        service = SubmitReportTask(
            task_commands=tasks,
            progress_publisher=FakeProgressPublisherPort(recorder),
            dispatcher=FakeReportDispatcherPort(recorder),
        )

        with self.assertRaisesRegex(ReportTaskConflictError, "任务正在处理中"):
            service.execute(_submission(source_count=1))

        self.assertEqual(1, len(tasks.submission_calls))
        self.assertEqual([], callbacks.wait_calls)
        self.assertEqual(["task.create"], recorder.events)
        self.assertEqual({}, tasks.executions)

    def test_persistence_failure_never_publishes_or_dispatches(self) -> None:
        recorder = InvocationRecorder()
        tasks = FakeReportTaskCommandPort(recorder)
        tasks.errors["create"] = OSError("database unavailable")
        service = SubmitReportTask(
            task_commands=tasks,
            progress_publisher=FakeProgressPublisherPort(recorder),
            dispatcher=FakeReportDispatcherPort(recorder),
        )

        with self.assertRaisesRegex(OSError, "database unavailable"):
            service.execute(_submission(source_count=1))

        self.assertEqual(["task.create"], recorder.events)

    def test_notification_failures_do_not_rollback_accepted_fact(self) -> None:
        recorder = InvocationRecorder()
        tasks = FakeReportTaskCommandPort(recorder)
        progress = FakeProgressPublisherPort(recorder)
        dispatcher = FakeReportDispatcherPort(recorder)
        progress.error = RuntimeError("progress offline")
        dispatcher.error = RuntimeError("wakeup offline")
        service = SubmitReportTask(
            task_commands=tasks,
            progress_publisher=progress,
            dispatcher=dispatcher,
        )

        with self.assertLogs(
            "app.modules.report.application.submit_report",
            level="ERROR",
        ):
            result = service.execute(_submission(source_count=1))

        self.assertIn(result.task_id, tasks.executions)
        self.assertFalse(result.progress_notified)
        self.assertFalse(result.dispatcher_notified)

    def test_submit_rejects_malformed_task_port_result(self) -> None:
        recorder = InvocationRecorder()
        tasks = FakeReportTaskCommandPort(recorder)
        tasks.forced_create_result = object()
        service = SubmitReportTask(
            task_commands=tasks,
            progress_publisher=FakeProgressPublisherPort(recorder),
            dispatcher=FakeReportDispatcherPort(recorder),
        )

        with self.assertRaises(ReportPortContractError):
            service.execute(_submission(source_count=1))


class RunReportApplicationTests(unittest.TestCase):
    """验证按 TaskId 执行、审计门禁、stale 和补偿顺序。"""

    def test_success_path_preserves_order_and_audits_before_terminal(self) -> None:
        harness = _ReportHarness()

        result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunReportOutcome.SUCCEEDED, result.outcome)
        self.assertEqual(ReportCallbackDeliveryOutcome.SUCCESS.value, result.callback_outcome)
        self.assertEqual(
            [0.15, 0.25, 0.35, 1.0],
            [item.progress for item in harness.progress.publications],
        )
        self.assertEqual(
            [1, 2],
            [item.sequence_no for item in harness.files.source_downloads],
        )
        rag_request = harness.rag.requests[0]
        self.assertEqual(
            [1, 2],
            [item.sequence_no for item in rag_request.ordered_source_files],
        )
        self.assertEqual("llm-report-132-report-task-0001", rag_request.context_name)
        self.assertEqual("report-132", rag_request.conversation_name)
        self.assertEqual(harness.submission.trace_id, rag_request.trace_id)
        self.assertIn("模板大纲：Word模板大纲", rag_request.prompt)
        self.assertLess(
            harness.recorder.events.index("audit.persist"),
            harness.recorder.events.index("task.finish:1"),
        )
        self.assertLess(
            harness.recorder.events.index("task.finish:1"),
            harness.recorder.events.index("callback.deliver"),
        )
        self.assertLess(
            harness.recorder.events.index("callback.complete"),
            harness.recorder.events.index("rag.cleanup"),
        )
        completion = harness.tasks.completion_calls[0].result
        self.assertIsInstance(completion, ReportTaskCompletion)
        self.assertEqual(
            {
                "businessType": "report",
                "data": {
                    "reportId": 132,
                    "status": "1",
                    "details": "<section>报告内容</section>",
                },
                "msg": "生成成功",
            },
            completion.callback_payload.to_public_dict(),
        )
        self.assertEqual(1, len(harness.audit.append_calls))
        self.assertEqual(1, len(harness.artifacts.cleanup_calls))

    def test_empty_rag_result_remains_success_with_internal_signal(self) -> None:
        harness = _ReportHarness(source_count=1)
        harness.rag.raw_content = "   "

        with self.assertLogs(
            "app.modules.report.application.run_report",
            level="WARNING",
        ) as captured:
            result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunReportOutcome.SUCCEEDED, result.outcome)
        self.assertTrue(result.empty_rag_result)
        self.assertTrue(any("empty_rag_result=true" in item for item in captured.output))
        payload = harness.callbacks.delivery_calls[0].payload.to_public_dict()
        self.assertEqual(REPORT_STATUS_SUCCEEDED, payload["data"]["status"])
        self.assertNotIn("empty_rag_result", payload)

    def test_normalization_has_explicit_fallback_and_strict_artifact_contract(self) -> None:
        harness = _ReportHarness(source_count=1)
        harness.files.errors["normalize"] = ReportSourceNormalizationError("boom")

        with self.assertLogs(
            "app.modules.report.application.run_report",
            level="WARNING",
        ):
            result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunReportOutcome.SUCCEEDED, result.outcome)
        self.assertEqual(ReportArtifactCategory.SOURCE, harness.files.prepared[0].category)

        invalid_harness = _ReportHarness(source_count=1)
        invalid_harness.files.normalize_results["source-0001"] = ReportArtifactRef(
            invalid_harness.task_id,
            "invalid-normalized-category",
            ReportArtifactCategory.REPORT_HTML,
            sequence_no=1,
            size_bytes=1,
            checksum="invalid-for-category-test",
        )

        invalid_result = invalid_harness.run_service.execute(invalid_harness.task_id)

        self.assertEqual(RunReportOutcome.FAILED, invalid_result.outcome)
        self.assertEqual("report_port_contract_error", invalid_result.error_code)
        self.assertEqual([], invalid_harness.rag.requests)

    def test_empty_template_fails_before_rag_and_sends_failure_callback(self) -> None:
        harness = _ReportHarness(source_count=1)
        harness.files.template_text = "  "

        result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunReportOutcome.FAILED, result.outcome)
        self.assertEqual("report_template_error", result.error_code)
        self.assertEqual([], harness.rag.requests)
        completion = harness.tasks.completion_calls[0].result
        self.assertEqual(REPORT_STATUS_FAILED, completion.callback_payload.status)
        self.assertEqual("生成失败", completion.callback_payload.message)
        self.assertEqual([0.15, 0.25, 1.0], [item.progress for item in harness.progress.publications])
        self.assertEqual(1, len(harness.artifacts.cleanup_calls))

    def test_rag_failure_trace_is_audited_before_failed_terminal(self) -> None:
        harness = _ReportHarness(source_count=1)
        harness.rag.generate_error = ReportRagExecutionError(
            "rag failed",
            trace=sample_failed_report_trace(
                harness.submission.trace_id,
                context_name=f"llm-report-132-{harness.task_id.value}",
            ),
            cleanup_ref=ReportRagCleanupRef("cleanup:failed"),
        )

        result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunReportOutcome.FAILED, result.outcome)
        self.assertEqual("report_rag_error", result.error_code)
        self.assertEqual(ReportRagAuditOutcome.FAILED, harness.audit.persist_calls[0].outcome)
        self.assertLess(
            harness.recorder.events.index("audit.persist"),
            harness.recorder.events.index("task.finish:2"),
        )
        self.assertEqual(1, len(harness.rag.cleanup_calls))

    def test_unknown_rag_side_effect_is_audited_then_quarantined(self) -> None:
        """供应商写结果未知时仍可形成业务失败，但绝不能执行可能有误的自动清理。"""

        harness = _ReportHarness(source_count=1)
        harness.rag.generate_error = ReportRagExecutionError(
            "rag write outcome unknown",
            trace=sample_failed_report_trace(
                harness.submission.trace_id,
                context_name=f"llm-report-132-{harness.task_id.value}",
            ),
            cleanup_ref=ReportRagCleanupRef("cleanup:unknown"),
            external_outcome_unknown=True,
        )

        result = harness.run_service.execute(harness.task_id)
        record = harness.resource_store.records[harness.task_id]

        self.assertEqual(RunReportOutcome.FAILED, result.outcome)
        self.assertEqual(ReportResourceState.QUARANTINED, record.state)
        self.assertEqual("rag_side_effect_outcome_unknown", record.last_error_stage)
        self.assertEqual([], harness.rag.cleanup_calls)
        self.assertEqual([], harness.artifacts.cleanup_calls)

    def test_audit_failure_blocks_success_and_preserves_scene(self) -> None:
        harness = _ReportHarness(source_count=1)
        harness.audit.persist_error = ReportAuditError("audit offline")

        with self.assertLogs(
            "app.modules.report.application.run_report",
            level="CRITICAL",
        ) as captured:
            result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunReportOutcome.FAILED, result.outcome)
        self.assertEqual("report_audit_error", result.error_code)
        self.assertEqual([], harness.rag.cleanup_calls)
        self.assertEqual([], harness.artifacts.cleanup_calls)
        self.assertEqual([], harness.artifacts.persisted_html)
        self.assertEqual(REPORT_STATUS_FAILED, harness.callbacks.delivery_calls[0].payload.status)
        self.assertTrue(any("保留现场" in item for item in captured.output))

    def test_stale_progress_stops_before_any_file_io(self) -> None:
        harness = _ReportHarness(source_count=1)
        harness.tasks.progress_results = [False]

        result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunReportOutcome.STALE, result.outcome)
        self.assertEqual([], harness.files.source_downloads)
        self.assertEqual([], harness.callbacks.delivery_calls)
        self.assertEqual([], harness.progress.publications)

    def test_progress_fact_write_error_preserves_scene_without_failure_rewrite(self) -> None:
        harness = _ReportHarness(source_count=1)
        harness.tasks.errors["progress"] = OSError("commit outcome unknown")

        with self.assertLogs(
            "app.modules.report.application.run_report",
            level="CRITICAL",
        ):
            with self.assertRaises(ReportTaskPersistenceError):
                harness.run_service.execute(harness.task_id)

        self.assertEqual([], harness.tasks.completion_calls)
        self.assertEqual([], harness.callbacks.delivery_calls)
        self.assertEqual([], harness.artifacts.cleanup_calls)

    def test_stale_terminal_write_suppresses_terminal_progress_and_callback(self) -> None:
        harness = _ReportHarness(source_count=1)
        harness.tasks.finish_results = [False]

        result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunReportOutcome.STALE, result.outcome)
        self.assertEqual([0.15, 0.25, 0.35], [item.progress for item in harness.progress.publications])
        self.assertEqual([], harness.callbacks.acquire_calls)
        self.assertEqual(1, len(harness.artifacts.cleanup_calls))

    def test_terminal_write_error_never_attempts_second_failed_terminal(self) -> None:
        harness = _ReportHarness(source_count=1)
        harness.tasks.errors["finish"] = OSError("commit response lost")

        with self.assertLogs(
            "app.modules.report.application.run_report",
            level="CRITICAL",
        ):
            with self.assertRaises(ReportTaskPersistenceError):
                harness.run_service.execute(harness.task_id)

        self.assertEqual(1, len(harness.tasks.completion_calls))
        self.assertEqual(REPORT_STATUS_SUCCEEDED, harness.tasks.completion_calls[0].public_status)
        self.assertEqual([], harness.callbacks.delivery_calls)
        self.assertEqual([], harness.artifacts.cleanup_calls)

    def test_latest_recheck_skips_old_callback_without_network_call(self) -> None:
        harness = _ReportHarness(source_count=1)
        harness.tasks.latest_results = [False]

        result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunReportOutcome.SUCCEEDED, result.outcome)
        self.assertEqual(ReportCallbackDeliveryOutcome.STALE.value, result.callback_outcome)
        self.assertEqual([], harness.callbacks.acquire_calls)
        self.assertEqual([], harness.callbacks.delivery_calls)

    def test_guard_non_acquired_result_never_calls_delivery(self) -> None:
        for outcome in (
            ReportCallbackAcquireOutcome.STALE,
            ReportCallbackAcquireOutcome.BUSY,
            ReportCallbackAcquireOutcome.OUTCOME_UNKNOWN,
            ReportCallbackAcquireOutcome.ALREADY_COMPLETED,
        ):
            with self.subTest(outcome=outcome):
                harness = _ReportHarness(source_count=1)
                harness.callbacks.acquire_outcome = outcome

                result = harness.run_service.execute(harness.task_id)

                self.assertEqual(RunReportOutcome.SUCCEEDED, result.outcome)
                self.assertEqual(outcome.value, result.callback_outcome)
                self.assertEqual([], harness.callbacks.delivery_calls)

    def test_guard_completion_cas_miss_is_observable_port_error(self) -> None:
        harness = _ReportHarness(source_count=1)
        harness.callbacks.complete_result = False

        with self.assertLogs(
            "app.modules.report.application.run_report",
            level="ERROR",
        ):
            result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunReportOutcome.SUCCEEDED, result.outcome)
        self.assertEqual("port_error", result.callback_outcome)
        self.assertEqual(1, len(harness.callbacks.complete_calls))

    def test_duplicate_or_terminal_dispatch_is_idempotently_skipped(self) -> None:
        for outcome in (
            TaskClaimOutcome.ALREADY_RUNNING,
            TaskClaimOutcome.TERMINAL,
            TaskClaimOutcome.STALE,
        ):
            with self.subTest(outcome=outcome):
                harness = _ReportHarness(source_count=1)
                harness.tasks.claim_outcomes[harness.task_id] = outcome

                result = harness.run_service.execute(harness.task_id)

                self.assertEqual(RunReportOutcome.NOT_CLAIMED, result.outcome)
                self.assertEqual(["task.get", "task.claim"], harness.recorder.events)

    def test_missing_execution_stops_before_claim(self) -> None:
        harness = _ReportHarness(source_count=1)
        harness.tasks.executions.pop(harness.task_id)

        result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunReportOutcome.MISSING, result.outcome)
        self.assertEqual(["task.get"], harness.recorder.events)

    def test_progress_notification_failure_does_not_change_task_success(self) -> None:
        harness = _ReportHarness(source_count=1)
        harness.progress.error = RuntimeError("notification failed")

        with self.assertLogs(
            "app.modules.report.application.run_report",
            level="ERROR",
        ):
            result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunReportOutcome.SUCCEEDED, result.outcome)
        self.assertEqual("succeeded", harness.tasks.executions[harness.task_id].execution_state)

    def test_callback_or_cleanup_failure_never_reverses_business_success(self) -> None:
        harness = _ReportHarness(source_count=1)
        harness.callbacks.delivery_error = RuntimeError("callback failed")
        harness.artifacts.cleanup_error = RuntimeError("cleanup failed")

        with self.assertLogs(
            "app.modules.report.application.run_report",
            level="ERROR",
        ):
            result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunReportOutcome.SUCCEEDED, result.outcome)
        self.assertEqual("port_error", result.callback_outcome)
        self.assertEqual("succeeded", harness.tasks.executions[harness.task_id].execution_state)
        self.assertEqual(1, len(harness.tasks.completion_calls))

    def test_callback_control_plane_failure_never_writes_a_second_terminal(self) -> None:
        """latest/Guard 异常属于回调维度，不能把已提交成功终态改写成失败。"""

        for failing_step in ("latest", "acquire", "complete"):
            with self.subTest(failing_step=failing_step):
                harness = _ReportHarness(source_count=1)
                if failing_step == "latest":
                    harness.tasks.errors["latest"] = RuntimeError(
                        "latest store unavailable"
                    )
                elif failing_step == "acquire":
                    harness.callbacks.acquire_error = RuntimeError(
                        "guard store unavailable"
                    )
                else:
                    harness.callbacks.complete_error = RuntimeError(
                        "guard completion unavailable"
                    )

                with self.assertLogs(
                    "app.modules.report.application.run_report",
                    level="ERROR",
                ):
                    result = harness.run_service.execute(harness.task_id)

                self.assertEqual(RunReportOutcome.SUCCEEDED, result.outcome)
                self.assertEqual("port_error", result.callback_outcome)
                self.assertEqual(
                    "succeeded",
                    harness.tasks.executions[harness.task_id].execution_state,
                )
                self.assertEqual(1, len(harness.tasks.completion_calls))

    def test_wrong_trace_identity_becomes_failed_and_preserves_unknown_scene(self) -> None:
        harness = _ReportHarness(source_count=1)
        harness.rag.forced_response = ReportRagResponse(
            raw_content="report",
            trace=sample_report_trace("other-trace", raw_response="report"),
            cleanup_ref=ReportRagCleanupRef("cleanup:other"),
        )

        result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunReportOutcome.FAILED, result.outcome)
        self.assertEqual("report_port_contract_error", result.error_code)
        self.assertEqual([], harness.audit.persist_calls)
        self.assertEqual([], harness.rag.cleanup_calls)
        self.assertEqual([], harness.artifacts.cleanup_calls)

    def test_wrong_task_read_identity_is_rejected_before_claim(self) -> None:
        harness = _ReportHarness(source_count=1)
        original = harness.tasks.executions[harness.task_id]
        wrong_task_id = TaskId("wrong-task")
        harness.tasks.forced_get_result = type(original)(
            task_id=wrong_task_id,
            task_type=original.task_type,
            business_ref=original.business_ref,
            execution_state=original.execution_state,
            public_status=original.public_status,
            progress=original.progress,
            message=original.message,
            input_snapshot=original.input_snapshot,
            accepted_at=original.accepted_at,
            trace_id=original.trace_id,
        )

        with self.assertRaises(ReportPortContractError):
            harness.run_service.execute(harness.task_id)

        self.assertEqual([], harness.tasks.claim_calls)

    def test_wrong_audit_receipt_blocks_success(self) -> None:
        harness = _ReportHarness(source_count=1)
        harness.audit.forced_receipt = ReportAuditReceipt(
            task_id=TaskId("other-task"),
            idempotency_key="report-rag:other-task",
            audit_id="audit:other-task",
        )

        result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunReportOutcome.FAILED, result.outcome)
        self.assertEqual("report_port_contract_error", result.error_code)
        self.assertEqual([], harness.callbacks.delivery_calls[:-1])
        self.assertEqual(REPORT_STATUS_FAILED, harness.callbacks.delivery_calls[0].payload.status)

    def test_final_report_artifact_must_belong_to_current_task(self) -> None:
        harness = _ReportHarness(source_count=1)
        harness.artifacts.forced_report_artifact = ReportArtifactRef(
            TaskId("other-task"),
            "foreign-report",
            ReportArtifactCategory.REPORT_HTML,
            size_bytes=10,
            checksum="foreign-checksum",
        )

        result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunReportOutcome.FAILED, result.outcome)
        self.assertEqual("report_port_contract_error", result.error_code)
        self.assertEqual(REPORT_STATUS_FAILED, harness.tasks.completion_calls[0].result.callback_payload.status)


if __name__ == "__main__":
    unittest.main()
