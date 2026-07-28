"""甲方 check-task 同步报告回调恢复的 Guard、并发与 latest-wins 验收。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from app.modules.report.adapters import (
    ReportTaskCommandCodec,
    SQLiteReportCallbackAdapter,
    SQLiteReportCallbackRecoverySource,
)
from app.modules.report.application import (
    RecoverReportCallbackSynchronously,
    ReportTaskCompletion,
)
from app.modules.report.domain import (
    ReportId,
    ReportSubmission,
    build_report_callback,
    build_report_result,
)
from app.modules.report.ports import (
    ReportArtifactCategory,
    ReportArtifactRef,
    ReportCallbackDeliveryOutcome,
    ReportCallbackDeliveryResult,
    ReportCallbackRecoveryCandidate,
)
from app.modules.tasks.adapters import LegacyTaskCommandAdapter
from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.tasks.ports import ExpectedTaskCompletion, TaskSubmissionCommand
from app.services.llm_service.task_service import LLMTaskService
from tests import workspace_tempdir


def _submission(report_id: int = 132) -> ReportSubmission:
    return ReportSubmission(
        report_id=ReportId.from_public_value(report_id),
        source_urls=(f"http://files.local/{report_id}.pdf",),
        template_outline_url="http://files.local/template.docx",
        template_desc="模板说明",
        requirement="生成报告",
        trace_id=f"trace-report-recovery-{report_id}",
    )


def _command(submission: ReportSubmission) -> TaskSubmissionCommand[ReportSubmission]:
    return TaskSubmissionCommand(
        task_type="report",
        business_ref=TaskBusinessRef("report", submission.report_id.business_key),
        input_schema_version=1,
        submission=submission,
        trace_id=submission.trace_id,
    )


def _finish_terminal(adapter: LegacyTaskCommandAdapter, execution) -> None:
    result = build_report_result(
        execution.input_snapshot.report_id,
        "<section>报告内容</section>",
    )
    completion = ReportTaskCompletion(
        callback_payload=build_report_callback(
            execution.input_snapshot.report_id,
            result.html_details,
            status="1",
        ),
        report_result=result,
        report_artifact=ReportArtifactRef(
            execution.task_id,
            "output/report.html",
            ReportArtifactCategory.REPORT_HTML,
            size_bytes=len(result.html_details.encode("utf-8")),
            checksum="report-recovery-checksum",
        ),
    )
    adapter.claim(execution.task_id)
    if not adapter.finish_if_current(
        ExpectedTaskCompletion(
            expected_task_id=execution.task_id,
            business_ref=execution.business_ref,
            execution_state="succeeded",
            public_status="1",
            message="报告生成完成",
            result=completion,
        )
    ):
        raise AssertionError("测试前置报告终态未提交")


class _RacingRecoverySource:
    """读取旧候选后立即受理新任务，精确制造 latest owner 切换窗口。"""

    def __init__(self, source, accept_new) -> None:
        self._source = source
        self._accept_new = accept_new

    def load_recoverable(
        self,
        report_id: ReportId,
    ) -> ReportCallbackRecoveryCandidate | None:
        candidate = self._source.load_recoverable(report_id)
        if candidate is not None:
            self._accept_new()
        return candidate


class ReportCallbackRecoveryTests(unittest.TestCase):
    def test_new_check_task_can_explicitly_retry_outcome_unknown(self) -> None:
        """新的显式请求可重试 unknown；同一轮结果未知时仍保持冻结。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            adapter = LegacyTaskCommandAdapter(
                service,
                ReportTaskCommandCodec(),
                task_id_factory=lambda: TaskId("report-recovery-unknown"),
            )
            created = adapter.create_if_allowed(_command(_submission()))
            assert created.execution is not None
            _finish_terminal(adapter, created.execution)
            source = SQLiteReportCallbackRecoverySource(service)
            transport_outcomes = iter(
                (
                    ReportCallbackDeliveryOutcome.DELIVERY_OUTCOME_UNKNOWN,
                    ReportCallbackDeliveryOutcome.SUCCESS,
                )
            )
            transport_calls = 0

            def transport(_payload: dict[str, object]) -> ReportCallbackDeliveryResult:
                nonlocal transport_calls
                transport_calls += 1
                outcome = next(transport_outcomes)
                return ReportCallbackDeliveryResult(outcome, outcome.value)

            recovery = RecoverReportCallbackSynchronously(
                source=source,
                callbacks=SQLiteReportCallbackAdapter(
                    service,
                    callback_url="http://callback.local/result",
                    callback_timeout=5,
                    lease_seconds=30,
                    transport=transport,
                ),
            )
            with patch(
                "app.modules.report.adapters.callback_guard."
                "save_callback_history_payload"
            ):
                first = recovery.execute(
                    ReportId.from_public_value(132),
                    request_trace_id="report-unknown-first",
                )
                frozen = service.get_task("report", "132")
                second = recovery.execute(
                    ReportId.from_public_value(132),
                    request_trace_id="report-unknown-retry",
                )
                completed = service.get_task("report", "132")
                attempt_events = service.list_callback_delivery_attempt_events(
                    business_type="report",
                    business_key="132",
                )

        self.assertFalse(first)
        assert frozen is not None
        self.assertEqual("outcome_unknown", frozen["callback_status"])
        self.assertTrue(second)
        self.assertEqual(2, transport_calls)
        assert completed is not None
        self.assertEqual("success", completed["callback_status"])
        retry_authorization = next(
            event
            for event in attempt_events
            if event["event_type"] == "authorized"
            and event["callback_attempt"] == 2
        )
        self.assertEqual(
            "explicit_check_task_recovery",
            retry_authorization["trigger"],
        )
        self.assertEqual(
            "report-unknown-retry",
            retry_authorization["request_trace_id"],
        )

    def test_each_new_request_can_advance_one_unknown_or_failed_attempt(self) -> None:
        """unknown→rejected→success 每次只由一个新的显式请求推进一轮。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            adapter = LegacyTaskCommandAdapter(
                service,
                ReportTaskCommandCodec(),
                task_id_factory=lambda: TaskId("report-recovery-attempt-chain"),
            )
            created = adapter.create_if_allowed(_command(_submission()))
            assert created.execution is not None
            _finish_terminal(adapter, created.execution)
            outcomes = iter(
                (
                    ReportCallbackDeliveryOutcome.DELIVERY_OUTCOME_UNKNOWN,
                    ReportCallbackDeliveryOutcome.REJECTED,
                    ReportCallbackDeliveryOutcome.SUCCESS,
                )
            )
            transport_calls = 0

            def transport(_payload: dict[str, object]) -> ReportCallbackDeliveryResult:
                nonlocal transport_calls
                transport_calls += 1
                outcome = next(outcomes)
                return ReportCallbackDeliveryResult(outcome, outcome.value)

            recovery = RecoverReportCallbackSynchronously(
                source=SQLiteReportCallbackRecoverySource(service),
                callbacks=SQLiteReportCallbackAdapter(
                    service,
                    callback_url="http://callback.local/result",
                    callback_timeout=5,
                    lease_seconds=30,
                    transport=transport,
                ),
            )
            with patch(
                "app.modules.report.adapters.callback_guard."
                "save_callback_history_payload"
            ):
                results = tuple(
                    recovery.execute(
                        ReportId.from_public_value(132),
                        request_trace_id=f"report-attempt-{attempt}",
                    )
                    for attempt in range(1, 4)
                )
            projection = service.get_task("report", "132")
            events = service.list_callback_delivery_attempt_events(
                business_type="report",
                business_key="132",
            )

        self.assertEqual((False, False, True), results)
        self.assertEqual(3, transport_calls)
        assert projection is not None
        self.assertEqual(3, projection["callback_attempts"])
        self.assertEqual("success", projection["callback_status"])
        self.assertEqual(
            {1, 2, 3},
            {
                event["callback_attempt"]
                for event in events
                if event["event_type"] == "authorized"
            },
        )

    def test_fifty_concurrent_rejected_recoveries_do_not_roll_attempt(self) -> None:
        """同一活跃请求中的跟随者不得在 owner 明确失败后继续形成新 attempt。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            adapter = LegacyTaskCommandAdapter(
                service,
                ReportTaskCommandCodec(),
                task_id_factory=lambda: TaskId("report-recovery-rejected-flight"),
            )
            created = adapter.create_if_allowed(_command(_submission()))
            assert created.execution is not None
            _finish_terminal(adapter, created.execution)
            transport_entered = threading.Event()
            release_transport = threading.Event()
            transport_calls = 0

            def transport(_payload: dict[str, object]) -> ReportCallbackDeliveryResult:
                nonlocal transport_calls
                transport_calls += 1
                transport_entered.set()
                self.assertTrue(release_transport.wait(timeout=30))
                return ReportCallbackDeliveryResult(
                    ReportCallbackDeliveryOutcome.REJECTED,
                    "http_status=503",
                )

            recovery = RecoverReportCallbackSynchronously(
                source=SQLiteReportCallbackRecoverySource(service),
                callbacks=SQLiteReportCallbackAdapter(
                    service,
                    callback_url="http://callback.local/result",
                    callback_timeout=5,
                    lease_seconds=30,
                    transport=transport,
                ),
            )
            start = threading.Barrier(50)
            follower_lock = threading.Lock()
            followers_done = 0
            all_followers_done = threading.Event()

            def recover_once(index: int) -> bool:
                nonlocal followers_done
                start.wait(timeout=30)
                result = recovery.execute(
                    ReportId.from_public_value(132),
                    request_trace_id=f"report-concurrent-{index}",
                )
                with follower_lock:
                    followers_done += 1
                    if followers_done == 49:
                        all_followers_done.set()
                return result

            with patch(
                "app.modules.report.adapters.callback_guard."
                "save_callback_history_payload"
            ), ThreadPoolExecutor(max_workers=50) as executor:
                futures = [executor.submit(recover_once, index) for index in range(50)]
                self.assertTrue(transport_entered.wait(timeout=30))
                self.assertTrue(all_followers_done.wait(timeout=30))
                release_transport.set()
                results = tuple(future.result(timeout=30) for future in futures)
            projection = service.get_task("report", "132")

        self.assertEqual(0, sum(results))
        self.assertEqual(1, transport_calls)
        assert projection is not None
        self.assertEqual(1, projection["callback_attempts"])
        self.assertEqual("failed", projection["callback_status"])

    def test_fifty_concurrent_check_task_recoveries_send_exactly_once(self) -> None:
        """50 个同步查询可以并发进入，但同一 execution 只能有一个 HTTP owner。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            adapter = LegacyTaskCommandAdapter(
                service,
                ReportTaskCommandCodec(),
                task_id_factory=lambda: TaskId("report-recovery-concurrent"),
            )
            created = adapter.create_if_allowed(_command(_submission()))
            assert created.execution is not None
            _finish_terminal(adapter, created.execution)

            transport_payloads: list[dict[str, object]] = []
            transport_lock = threading.Lock()

            def transport(payload: dict[str, object]) -> ReportCallbackDeliveryResult:
                with transport_lock:
                    transport_payloads.append(payload)
                return ReportCallbackDeliveryResult(
                    ReportCallbackDeliveryOutcome.SUCCESS,
                    "http_status=204",
                )

            callbacks = SQLiteReportCallbackAdapter(
                service,
                callback_url="http://callback.local/result",
                callback_timeout=5,
                lease_seconds=30,
                transport=transport,
            )
            recovery = RecoverReportCallbackSynchronously(
                source=SQLiteReportCallbackRecoverySource(service),
                callbacks=callbacks,
            )
            barrier = threading.Barrier(50)

            def recover_once() -> bool:
                barrier.wait(timeout=60)
                return recovery.execute(ReportId.from_public_value(132))

            with patch(
                "app.modules.report.adapters.callback_guard."
                "save_callback_history_payload"
            ), ThreadPoolExecutor(max_workers=50) as executor:
                outcomes = tuple(executor.map(lambda _index: recover_once(), range(50)))

            projection = service.get_task("report", "132")

        self.assertEqual(1, sum(outcomes))
        self.assertEqual(1, len(transport_payloads))
        self.assertEqual(
            {"businessType", "data", "msg"},
            set(transport_payloads[0]),
        )
        assert projection is not None
        self.assertEqual("success", projection["callback_status"])

    def test_explicit_recovery_can_retry_a_definitive_rejection(self) -> None:
        """甲方再次调用 check-task 时允许恢复 failed，但普通 Worker 仍不得自动重试。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            adapter = LegacyTaskCommandAdapter(
                service,
                ReportTaskCommandCodec(),
                task_id_factory=lambda: TaskId("report-recovery-retry"),
            )
            created = adapter.create_if_allowed(_command(_submission()))
            assert created.execution is not None
            _finish_terminal(adapter, created.execution)
            source = SQLiteReportCallbackRecoverySource(service)

            rejected = RecoverReportCallbackSynchronously(
                source=source,
                callbacks=SQLiteReportCallbackAdapter(
                    service,
                    callback_url="http://callback.local/result",
                    callback_timeout=5,
                    lease_seconds=30,
                    transport=lambda _payload: ReportCallbackDeliveryResult(
                        ReportCallbackDeliveryOutcome.REJECTED,
                        "http_status=503",
                    ),
                ),
            )
            succeeded = RecoverReportCallbackSynchronously(
                source=source,
                callbacks=SQLiteReportCallbackAdapter(
                    service,
                    callback_url="http://callback.local/result",
                    callback_timeout=5,
                    lease_seconds=30,
                    transport=lambda _payload: ReportCallbackDeliveryResult(
                        ReportCallbackDeliveryOutcome.SUCCESS,
                        "http_status=204",
                    ),
                ),
            )
            with patch(
                "app.modules.report.adapters.callback_guard."
                "save_callback_history_payload"
            ):
                first = rejected.execute(ReportId.from_public_value(132))
                failed_projection = service.get_task("report", "132")
                second = succeeded.execute(ReportId.from_public_value(132))
                success_projection = service.get_task("report", "132")

        self.assertFalse(first)
        assert failed_projection is not None
        self.assertEqual("failed", failed_projection["callback_status"])
        self.assertTrue(second)
        assert success_projection is not None
        self.assertEqual("success", success_projection["callback_status"])

    def test_old_candidate_is_skipped_when_new_task_wins_before_acquire(self) -> None:
        """候选读取不是授权点；同名 reportId 新受理后，旧回调必须判 stale。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            task_ids = iter((TaskId("report-old"), TaskId("report-new")))
            adapter = LegacyTaskCommandAdapter(
                service,
                ReportTaskCommandCodec(),
                task_id_factory=lambda: next(task_ids),
            )
            submission = _submission()
            old = adapter.create_if_allowed(_command(submission))
            assert old.execution is not None
            _finish_terminal(adapter, old.execution)
            transport_payloads: list[dict[str, object]] = []

            def accept_new() -> None:
                accepted = adapter.create_if_allowed(_command(submission))
                if accepted.execution is None:
                    raise AssertionError("竞态测试的新任务未被受理")

            recovery = RecoverReportCallbackSynchronously(
                source=_RacingRecoverySource(
                    SQLiteReportCallbackRecoverySource(service),
                    accept_new,
                ),
                callbacks=SQLiteReportCallbackAdapter(
                    service,
                    callback_url="http://callback.local/result",
                    callback_timeout=5,
                    lease_seconds=30,
                    transport=lambda payload: (
                        transport_payloads.append(payload)
                        or ReportCallbackDeliveryResult(
                            ReportCallbackDeliveryOutcome.SUCCESS,
                            "http_status=204",
                        )
                    ),
                ),
            )

            replayed = recovery.execute(ReportId.from_public_value(132))
            latest = service.get_task("report", "132")

        self.assertFalse(replayed)
        self.assertEqual([], transport_payloads)
        assert latest is not None
        self.assertEqual("report-new", latest["execution_id"])


if __name__ == "__main__":
    unittest.main()
