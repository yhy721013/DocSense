"""报告回调 Guard 的 latest、fencing、过期冻结与并发验收。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest.mock import Mock, patch

import requests

from app.modules.report.adapters import (
    ReportTaskCommandCodec,
    SQLiteReportCallbackAdapter,
)
from app.modules.report.application import ReportTaskCompletion
from app.modules.report.domain import (
    ReportId,
    ReportSubmission,
    build_report_callback,
    build_report_result,
)
from app.modules.report.ports import (
    DeliverReportCallback,
    ReportArtifactCategory,
    ReportArtifactRef,
    ReportCallbackAcquire,
    ReportCallbackAcquireOutcome,
    ReportCallbackDeliveryOutcome,
    ReportCallbackDeliveryResult,
    ReportCallbackReleaseOutcome,
    ReportCallbackWaitOutcome,
    ReleaseUnknownReportCallback,
    WaitForReportCallbackRelease,
)
from app.modules.tasks.adapters import LegacyTaskCommandAdapter
from app.modules.tasks.domain import TaskBusinessRef
from app.modules.tasks.ports import (
    ExpectedTaskCompletion,
    TaskSubmissionCommand,
    TaskSubmissionOutcome,
)
from app.services.llm_service.task_service import LLMTaskService
from tests import workspace_tempdir


def _submission() -> ReportSubmission:
    return ReportSubmission(
        report_id=ReportId.from_public_value(132),
        source_urls=("http://files.local/a.pdf",),
        template_outline_url="http://files.local/template.docx",
        template_desc="模板说明",
        requirement="生成报告",
        trace_id="trace-report-132",
    )


def _command(submission: ReportSubmission) -> TaskSubmissionCommand[ReportSubmission]:
    return TaskSubmissionCommand(
        task_type="report",
        business_ref=TaskBusinessRef("report", submission.report_id.business_key),
        input_schema_version=1,
        submission=submission,
        trace_id=submission.trace_id,
    )


def _task_adapter(service: LLMTaskService):
    return LegacyTaskCommandAdapter(service, ReportTaskCommandCodec())


def _finish_success(adapter, execution) -> ReportTaskCompletion:
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
            f"{execution.task_id.value}:report.html",
            ReportArtifactCategory.REPORT_HTML,
            size_bytes=len(result.html_details.encode("utf-8")),
            checksum="report-checksum",
        ),
    )
    adapter.claim(execution.task_id)
    finished = adapter.finish_if_current(
        ExpectedTaskCompletion(
            expected_task_id=execution.task_id,
            business_ref=execution.business_ref,
            execution_state="succeeded",
            public_status="1",
            message="报告生成完成",
            result=completion,
        )
    )
    if not finished:
        raise AssertionError("测试前置报告终态未提交")
    return completion


class _MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class ReportCallbackGuardTests(unittest.TestCase):
    def test_released_old_worker_is_revalidated_and_never_reaches_transport(self) -> None:
        """旧 Worker 即使在解除后恢复，也必须在 HTTP 前被 latest/租约复核拦截。"""

        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            service = LLMTaskService(database)
            tasks = _task_adapter(service)
            first = tasks.create_if_allowed(_command(_submission()))
            assert first.execution is not None
            first_completion = _finish_success(tasks, first.execution)

            clock = _MutableClock(datetime(2026, 7, 16, tzinfo=timezone.utc))
            transport = Mock(
                return_value=ReportCallbackDeliveryResult(
                    ReportCallbackDeliveryOutcome.SUCCESS,
                    "http_status=200",
                )
            )
            callbacks = SQLiteReportCallbackAdapter(
                service,
                callback_url="http://callback.local/result",
                callback_timeout=5,
                lease_seconds=6,
                clock=clock,
                transport=transport,
            )
            acquired = callbacks.acquire(
                ReportCallbackAcquire(
                    first.execution.task_id,
                    first.execution.input_snapshot.report_id,
                )
            )
            assert acquired.lease is not None

            # 模拟旧 Worker 在 acquire 之后暂停。租约过期并由其他控制流观察、人工核验、
            # 隔离旧 Worker 后解除，随后同一业务键受理新任务。
            clock.value += timedelta(seconds=7)
            observed = callbacks.wait_until_released(
                WaitForReportCallbackRelease(
                    first.execution.input_snapshot.report_id,
                    timeout_seconds=0.01,
                )
            )
            released = callbacks.release_unknown(
                ReleaseUnknownReportCallback(
                    first.execution.input_snapshot.report_id,
                    released_by="operator-safe-release",
                    reason="已确认旧 Worker 停止并完成隔离",
                    worker_stopped_confirmed=True,
                )
            )
            second = tasks.create_if_allowed(_command(_submission()))

            stale_delivery = callbacks.deliver(
                DeliverReportCallback(
                    acquired.lease,
                    first_completion.callback_payload,
                )
            )

        self.assertEqual(
            ReportCallbackWaitOutcome.OUTCOME_UNKNOWN,
            observed.outcome,
        )
        self.assertEqual(ReportCallbackReleaseOutcome.RELEASED, released.outcome)
        self.assertEqual(TaskSubmissionOutcome.ACCEPTED, second.outcome)
        self.assertEqual(
            ReportCallbackDeliveryOutcome.STALE,
            stale_delivery.outcome,
        )
        transport.assert_not_called()

    def test_manual_release_preserves_unknown_fact_and_allows_new_submission(self) -> None:
        """人工解除只释放业务键，不得把旧投递改写为 pending 或再次发送。"""

        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            service = LLMTaskService(database)
            tasks = _task_adapter(service)
            created = tasks.create_if_allowed(_command(_submission()))
            assert created.execution is not None
            completion = _finish_success(tasks, created.execution)
            transport = Mock()
            callbacks = SQLiteReportCallbackAdapter(
                service,
                callback_url="http://callback.local/result",
                callback_timeout=5,
                transport=transport,
            )
            command = ReportCallbackAcquire(
                created.execution.task_id,
                created.execution.input_snapshot.report_id,
            )
            acquired = callbacks.acquire(command)
            assert acquired.lease is not None
            self.assertTrue(
                callbacks.complete(
                    acquired.lease,
                    ReportCallbackDeliveryResult(
                        ReportCallbackDeliveryOutcome.DELIVERY_OUTCOME_UNKNOWN,
                        "ReadTimeout",
                    ),
                    completion.callback_payload,
                )
            )
            blocked = tasks.create_if_allowed(_command(_submission()))
            before_release = service.get_task_execution(
                created.execution.task_id.value
            )

            released = callbacks.release_unknown(
                ReleaseUnknownReportCallback(
                    created.execution.input_snapshot.report_id,
                    released_by="operator-001",
                    reason="甲方确认未收到本次回调，允许重新提交",
                    worker_stopped_confirmed=True,
                )
            )
            after_release = service.get_task_execution(
                created.execution.task_id.value
            )
            accepted = tasks.create_if_allowed(_command(_submission()))
            stale_replay = callbacks.acquire(command)
            with sqlite3.connect(database) as connection:
                guard = connection.execute(
                    """
                    SELECT state, last_outcome, released_at,
                           released_by, release_reason
                    FROM callback_delivery_guards
                    WHERE business_type = 'report' AND business_key = '132'
                    """
                ).fetchone()
            audits = service.list_callback_delivery_guard_release_audits(
                business_type="report",
                business_key="132",
            )

        self.assertEqual(
            TaskSubmissionOutcome.CALLBACK_OUTCOME_UNKNOWN,
            blocked.outcome,
        )
        self.assertEqual(ReportCallbackReleaseOutcome.RELEASED, released.outcome)
        self.assertEqual("outcome_unknown", before_release["callback_status"])
        self.assertEqual("outcome_unknown", after_release["callback_status"])
        self.assertEqual(
            "delivery_outcome_unknown",
            after_release["callback_outcome"],
        )
        self.assertEqual(TaskSubmissionOutcome.ACCEPTED, accepted.outcome)
        self.assertEqual(ReportCallbackAcquireOutcome.STALE, stale_replay.outcome)
        self.assertEqual("idle", guard[0])
        self.assertEqual("delivery_outcome_unknown", guard[1])
        self.assertTrue(guard[2])
        self.assertEqual("operator-001", guard[3])
        self.assertEqual("甲方确认未收到本次回调，允许重新提交", guard[4])
        self.assertEqual(1, len(audits))
        self.assertEqual("operator-001", audits[0]["released_by"])
        transport.assert_not_called()

    def test_fifty_concurrent_manual_releases_preserve_first_audit(self) -> None:
        """同一 unknown 冻结只允许一次状态转换，其余命令幂等观察首次证据。"""

        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            service = LLMTaskService(database)
            tasks = _task_adapter(service)
            created = tasks.create_if_allowed(_command(_submission()))
            assert created.execution is not None
            completion = _finish_success(tasks, created.execution)
            callbacks = SQLiteReportCallbackAdapter(
                service,
                callback_url="http://callback.local/result",
                callback_timeout=5,
            )
            acquired = callbacks.acquire(
                ReportCallbackAcquire(
                    created.execution.task_id,
                    created.execution.input_snapshot.report_id,
                )
            )
            assert acquired.lease is not None
            callbacks.complete(
                acquired.lease,
                ReportCallbackDeliveryResult(
                    ReportCallbackDeliveryOutcome.DELIVERY_OUTCOME_UNKNOWN,
                    "ReadTimeout",
                ),
                completion.callback_payload,
            )
            barrier = threading.Barrier(50)

            def release(index: int):
                barrier.wait(timeout=20)
                return callbacks.release_unknown(
                    ReleaseUnknownReportCallback(
                        created.execution.input_snapshot.report_id,
                        released_by=f"operator-{index:02d}",
                        reason=f"review-{index:02d}",
                        worker_stopped_confirmed=True,
                    )
                ).outcome

            with ThreadPoolExecutor(max_workers=50) as executor:
                outcomes = list(executor.map(release, range(50)))
            with sqlite3.connect(database) as connection:
                guard = connection.execute(
                    """
                    SELECT state, released_by, release_reason
                    FROM callback_delivery_guards
                    WHERE business_type = 'report' AND business_key = '132'
                    """
                ).fetchone()
            audits = service.list_callback_delivery_guard_release_audits(
                business_type="report",
                business_key="132",
            )

        self.assertEqual(1, outcomes.count(ReportCallbackReleaseOutcome.RELEASED))
        self.assertEqual(
            49,
            outcomes.count(ReportCallbackReleaseOutcome.ALREADY_RELEASED),
        )
        self.assertEqual("idle", guard[0])
        winning_index = int(guard[1].rsplit("-", 1)[-1])
        self.assertEqual(f"review-{winning_index:02d}", guard[2])
        self.assertEqual(1, len(audits))
        self.assertEqual(guard[1], audits[0]["released_by"])
        self.assertEqual(guard[2], audits[0]["release_reason"])

    def test_manual_release_audit_survives_next_lease_and_success_is_not_frozen(self) -> None:
        """追加式解除审计不能被下一租约覆盖，普通成功 Guard 也不是“已人工解除”。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(
                str(Path(runtime_directory) / "tasks.sqlite3")
            )
            tasks = _task_adapter(service)
            first = tasks.create_if_allowed(_command(_submission()))
            assert first.execution is not None
            first_completion = _finish_success(tasks, first.execution)
            callbacks = SQLiteReportCallbackAdapter(
                service,
                callback_url="http://callback.local/result",
                callback_timeout=5,
            )
            first_acquire = callbacks.acquire(
                ReportCallbackAcquire(
                    first.execution.task_id,
                    first.execution.input_snapshot.report_id,
                )
            )
            assert first_acquire.lease is not None
            callbacks.complete(
                first_acquire.lease,
                ReportCallbackDeliveryResult(
                    ReportCallbackDeliveryOutcome.DELIVERY_OUTCOME_UNKNOWN,
                    "ReadTimeout",
                ),
                first_completion.callback_payload,
            )
            callbacks.release_unknown(
                ReleaseUnknownReportCallback(
                    first.execution.input_snapshot.report_id,
                    released_by="operator-audit",
                    reason="甲方人工核验后允许再次提交",
                    worker_stopped_confirmed=True,
                )
            )

            second = tasks.create_if_allowed(_command(_submission()))
            assert second.execution is not None
            second_completion = _finish_success(tasks, second.execution)
            second_acquire = callbacks.acquire(
                ReportCallbackAcquire(
                    second.execution.task_id,
                    second.execution.input_snapshot.report_id,
                )
            )
            assert second_acquire.lease is not None
            callbacks.complete(
                second_acquire.lease,
                ReportCallbackDeliveryResult(
                    ReportCallbackDeliveryOutcome.SUCCESS,
                    "http_status=200",
                ),
                second_completion.callback_payload,
            )
            not_frozen = callbacks.release_unknown(
                ReleaseUnknownReportCallback(
                    second.execution.input_snapshot.report_id,
                    released_by="operator-should-not-release",
                    reason="本次状态并非结果未知",
                    worker_stopped_confirmed=True,
                )
            )
            audits = service.list_callback_delivery_guard_release_audits(
                business_type="report",
                business_key="132",
            )

        self.assertEqual(ReportCallbackReleaseOutcome.NOT_FROZEN, not_frozen.outcome)
        self.assertEqual(1, len(audits))
        self.assertEqual(first.execution.task_id.value, audits[0]["owner_execution_id"])
        self.assertEqual("operator-audit", audits[0]["released_by"])
        self.assertEqual("甲方人工核验后允许再次提交", audits[0]["release_reason"])

    def test_acquire_rechecks_latest_after_precheck_race(self) -> None:
        """新任务在预检查后提交时，旧任务必须由 acquire 的事务判定为 stale。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            tasks = _task_adapter(service)
            first = tasks.create_if_allowed(_command(_submission()))
            assert first.execution is not None
            _finish_success(tasks, first.execution)
            callbacks = SQLiteReportCallbackAdapter(
                service,
                callback_url="",
                callback_timeout=5,
            )
            prechecked = threading.Event()
            allow_acquire = threading.Event()

            def old_worker() -> ReportCallbackAcquireOutcome:
                self.assertTrue(
                    tasks.is_latest(
                        first.execution.task_id,
                        first.execution.business_ref,
                    )
                )
                prechecked.set()
                self.assertTrue(allow_acquire.wait(timeout=10))
                return callbacks.acquire(
                    ReportCallbackAcquire(
                        first.execution.task_id,
                        first.execution.input_snapshot.report_id,
                    )
                ).outcome

            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(old_worker)
                self.assertTrue(prechecked.wait(timeout=10))
                second = tasks.create_if_allowed(_command(_submission()))
                allow_acquire.set()
                outcome = future.result(timeout=10)

        self.assertEqual(TaskSubmissionOutcome.ACCEPTED, second.outcome)
        self.assertEqual(ReportCallbackAcquireOutcome.STALE, outcome)

    def test_fifty_concurrent_acquires_have_one_owner(self) -> None:
        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            tasks = _task_adapter(service)
            created = tasks.create_if_allowed(_command(_submission()))
            assert created.execution is not None
            completion = _finish_success(tasks, created.execution)
            callbacks = SQLiteReportCallbackAdapter(
                service,
                callback_url="",
                callback_timeout=5,
            )
            barrier = threading.Barrier(50)

            def acquire():
                barrier.wait(timeout=20)
                return callbacks.acquire(
                    ReportCallbackAcquire(
                        created.execution.task_id,
                        created.execution.input_snapshot.report_id,
                    )
                )

            with ThreadPoolExecutor(max_workers=50) as executor:
                futures = [executor.submit(acquire) for _ in range(50)]
                results = [future.result(timeout=60) for future in futures]

            acquired = [
                item
                for item in results
                if item.outcome is ReportCallbackAcquireOutcome.ACQUIRED
            ]
            busy = [
                item
                for item in results
                if item.outcome is ReportCallbackAcquireOutcome.BUSY
            ]
            self.assertEqual(1, len(acquired))
            self.assertEqual(49, len(busy))
            assert acquired[0].lease is not None
            delivery = callbacks.deliver(
                DeliverReportCallback(acquired[0].lease, completion.callback_payload)
            )
            self.assertEqual(ReportCallbackDeliveryOutcome.SKIPPED, delivery.outcome)
            self.assertTrue(
                callbacks.complete(
                    acquired[0].lease,
                    delivery,
                    completion.callback_payload,
                )
            )

    def test_late_fencing_token_cannot_complete_newer_lease(self) -> None:
        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            service = LLMTaskService(database)
            tasks = _task_adapter(service)
            created = tasks.create_if_allowed(_command(_submission()))
            assert created.execution is not None
            completion = _finish_success(tasks, created.execution)
            callbacks = SQLiteReportCallbackAdapter(
                service,
                callback_url="http://callback.local/result",
                callback_timeout=5,
            )
            command = ReportCallbackAcquire(
                created.execution.task_id,
                created.execution.input_snapshot.report_id,
            )
            first = callbacks.acquire(command)
            assert first.lease is not None
            self.assertTrue(
                callbacks.complete(
                    first.lease,
                    ReportCallbackDeliveryResult(
                        ReportCallbackDeliveryOutcome.DEFINITELY_NOT_SENT,
                        "connect timeout",
                    ),
                    completion.callback_payload,
                )
            )
            # 阶段 1C 的普通 acquire 不允许自动重试明确失败；这里显式模拟未来受控
            # recovery command 已完成审计并重新授权投递，只为构造 version=2 的新租约，
            # 从而独立验证旧 Worker 的 fencing token 不得完成新租约。
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    UPDATE llm_task_executions
                    SET callback_status = 'pending', callback_outcome = ''
                    WHERE execution_id = ?
                    """,
                    (created.execution.task_id.value,),
                )
                connection.execute(
                    """
                    UPDATE llm_tasks
                    SET callback_status = 'pending', last_callback_error = ''
                    WHERE business_type = 'report' AND business_key = '132'
                    """
                )
            second = callbacks.acquire(command)
            assert second.lease is not None

            late_completed = callbacks.complete(
                first.lease,
                ReportCallbackDeliveryResult(
                    ReportCallbackDeliveryOutcome.SUCCESS
                ),
                completion.callback_payload,
            )
            with sqlite3.connect(database) as connection:
                row = connection.execute(
                    """
                    SELECT state, lease_version, lease_token
                    FROM callback_delivery_guards
                    WHERE business_type = 'report' AND business_key = '132'
                    """
                ).fetchone()

            self.assertFalse(late_completed)
            self.assertEqual("sending", row[0])
            self.assertEqual(second.lease.fencing_token, row[1])
            self.assertEqual(second.lease.token, row[2])
            self.assertTrue(
                callbacks.complete(
                    second.lease,
                    ReportCallbackDeliveryResult(
                        ReportCallbackDeliveryOutcome.SUCCESS
                    ),
                    completion.callback_payload,
                )
            )

    def test_definitive_failure_cannot_be_implicitly_retried(self) -> None:
        """明确拒绝已形成终局，重复 Worker 不得把首次 acquire 当成重试入口。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            tasks = _task_adapter(service)
            created = tasks.create_if_allowed(_command(_submission()))
            assert created.execution is not None
            completion = _finish_success(tasks, created.execution)
            callbacks = SQLiteReportCallbackAdapter(
                service,
                callback_url="http://callback.local/result",
                callback_timeout=5,
            )
            command = ReportCallbackAcquire(
                created.execution.task_id,
                created.execution.input_snapshot.report_id,
            )
            first = callbacks.acquire(command)
            assert first.lease is not None
            self.assertTrue(
                callbacks.complete(
                    first.lease,
                    ReportCallbackDeliveryResult(
                        ReportCallbackDeliveryOutcome.REJECTED,
                        "http_status=500",
                    ),
                    completion.callback_payload,
                )
            )
            replay = callbacks.acquire(command)

        self.assertEqual(
            ReportCallbackAcquireOutcome.ALREADY_COMPLETED,
            replay.outcome,
        )

    def test_default_http_transport_classifies_delivery_outcomes(self) -> None:
        """HTTP 明确结果与不确定异常必须形成不同的 Guard 完成语义。"""

        cases = (
            (Mock(status_code=204), None, ReportCallbackDeliveryOutcome.SUCCESS),
            # requests.Response.ok 会把 302 视为成功；报告契约必须严格只接受 2xx。
            (Mock(status_code=302), None, ReportCallbackDeliveryOutcome.REJECTED),
            (Mock(status_code=503), None, ReportCallbackDeliveryOutcome.REJECTED),
            (
                None,
                requests.exceptions.ConnectTimeout("connect timeout"),
                ReportCallbackDeliveryOutcome.DEFINITELY_NOT_SENT,
            ),
            (
                None,
                requests.exceptions.InvalidSchema("unsupported scheme"),
                ReportCallbackDeliveryOutcome.DEFINITELY_NOT_SENT,
            ),
            (
                None,
                requests.exceptions.ReadTimeout("read timeout"),
                ReportCallbackDeliveryOutcome.DELIVERY_OUTCOME_UNKNOWN,
            ),
        )
        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            callbacks = SQLiteReportCallbackAdapter(
                service,
                callback_url="http://callback.local/result",
                callback_timeout=5,
            )
            for response, error, expected in cases:
                with self.subTest(expected=expected.value), patch(
                    "app.modules.report.adapters.callback_guard."
                    "save_callback_history_payload"
                ), patch(
                    "app.modules.report.adapters.callback_guard.requests.post",
                    return_value=response,
                    side_effect=error,
                ):
                    # 此处直接验证 Adapter 自带的 HTTP 分类器；数据库 Guard 的 CAS 行为
                    # 已由本文件其他测试通过公开 acquire/complete 入口覆盖。
                    result = callbacks._deliver_http(  # noqa: SLF001
                        {"data": {"reportId": 132}}
                    )
                self.assertEqual(expected, result.outcome)

    def test_history_is_written_only_after_guard_completion_attempt(self) -> None:
        """运维历史不得先于权威 Guard 状态落盘，避免恢复判断看到相反事实。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            tasks = _task_adapter(service)
            created = tasks.create_if_allowed(_command(_submission()))
            assert created.execution is not None
            completion = _finish_success(tasks, created.execution)
            callbacks = SQLiteReportCallbackAdapter(
                service,
                callback_url="http://callback.local/result",
                callback_timeout=5,
                lease_seconds=30,
            )
            acquired = callbacks.acquire(
                ReportCallbackAcquire(
                    created.execution.task_id,
                    created.execution.input_snapshot.report_id,
                )
            )
            assert acquired.lease is not None
            order: list[str] = []
            original_complete = service.complete_callback_delivery_guard

            def complete_guard(**kwargs):
                order.append("guard")
                return original_complete(**kwargs)

            with patch.object(
                service,
                "complete_callback_delivery_guard",
                side_effect=complete_guard,
            ), patch(
                "app.modules.report.adapters.callback_guard."
                "save_callback_history_payload",
                side_effect=lambda *_args, **_kwargs: order.append("history"),
            ):
                completed = callbacks.complete(
                    acquired.lease,
                    ReportCallbackDeliveryResult(
                        ReportCallbackDeliveryOutcome.SUCCESS,
                        "http_status=204",
                    ),
                    completion.callback_payload,
                )

        self.assertTrue(completed)
        self.assertEqual(["guard", "history"], order)

    def test_history_is_skipped_when_guard_completion_loses_authority(self) -> None:
        """CAS 未提交时不得生成会被误读成已持久化成功的调试历史。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            tasks = _task_adapter(service)
            created = tasks.create_if_allowed(_command(_submission()))
            assert created.execution is not None
            completion = _finish_success(tasks, created.execution)
            callbacks = SQLiteReportCallbackAdapter(
                service,
                callback_url="http://callback.local/result",
                callback_timeout=5,
                lease_seconds=30,
            )
            acquired = callbacks.acquire(
                ReportCallbackAcquire(
                    created.execution.task_id,
                    created.execution.input_snapshot.report_id,
                )
            )
            assert acquired.lease is not None

            with patch.object(
                service,
                "complete_callback_delivery_guard",
                return_value=False,
            ), patch(
                "app.modules.report.adapters.callback_guard."
                "save_callback_history_payload"
            ) as save_history:
                completed = callbacks.complete(
                    acquired.lease,
                    ReportCallbackDeliveryResult(
                        ReportCallbackDeliveryOutcome.SUCCESS,
                        "http_status=204",
                    ),
                    completion.callback_payload,
                )

        self.assertFalse(completed)
        save_history.assert_not_called()

    def test_skipped_callback_does_not_create_delivery_history(self) -> None:
        """未配置 URL 时没有发生 HTTP，不能在回调调试目录伪造发送记录。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            tasks = _task_adapter(service)
            created = tasks.create_if_allowed(_command(_submission()))
            assert created.execution is not None
            completion = _finish_success(tasks, created.execution)
            callbacks = SQLiteReportCallbackAdapter(
                service,
                callback_url="",
                callback_timeout=5,
                lease_seconds=30,
            )
            acquired = callbacks.acquire(
                ReportCallbackAcquire(
                    created.execution.task_id,
                    created.execution.input_snapshot.report_id,
                )
            )
            assert acquired.lease is not None
            delivery = callbacks.deliver(
                DeliverReportCallback(acquired.lease, completion.callback_payload)
            )

            with patch(
                "app.modules.report.adapters.callback_guard."
                "save_callback_history_payload"
            ) as save_history:
                completed = callbacks.complete(
                    acquired.lease,
                    delivery,
                    completion.callback_payload,
                )

        self.assertTrue(completed)
        self.assertEqual(ReportCallbackDeliveryOutcome.SKIPPED, delivery.outcome)
        save_history.assert_not_called()

    def test_maintenance_sweep_freezes_expired_guard_without_resending(self) -> None:
        """即使没有请求线程观察 Guard，生产维护循环也能有界冻结失联租约。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            tasks = _task_adapter(service)
            created = tasks.create_if_allowed(_command(_submission()))
            assert created.execution is not None
            _finish_success(tasks, created.execution)
            clock = _MutableClock(datetime(2026, 7, 16, tzinfo=timezone.utc))
            transport_calls: list[dict[str, object]] = []
            callbacks = SQLiteReportCallbackAdapter(
                service,
                callback_url="http://callback.local/result",
                callback_timeout=5,
                lease_seconds=30,
                clock=clock,
                transport=lambda payload: (
                    transport_calls.append(payload)
                    or ReportCallbackDeliveryResult(
                        ReportCallbackDeliveryOutcome.SUCCESS,
                        "http_status=204",
                    )
                ),
            )
            acquired = callbacks.acquire(
                ReportCallbackAcquire(
                    created.execution.task_id,
                    created.execution.input_snapshot.report_id,
                )
            )
            self.assertEqual(ReportCallbackAcquireOutcome.ACQUIRED, acquired.outcome)

            clock.value += timedelta(seconds=31)
            sweep = callbacks.freeze_expired(limit=10)
            execution = service.get_task_execution(created.execution.task_id.value)
            projection = service.get_task("report", "132")

        self.assertEqual(1, sweep.scanned_count)
        self.assertEqual(1, sweep.frozen_count)
        self.assertEqual([], transport_calls)
        assert execution is not None and projection is not None
        self.assertEqual("outcome_unknown", execution["callback_status"])
        self.assertEqual("outcome_unknown", projection["callback_status"])

    def test_expired_sending_lease_freezes_business_key(self) -> None:
        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            tasks = _task_adapter(service)
            created = tasks.create_if_allowed(_command(_submission()))
            assert created.execution is not None
            _finish_success(tasks, created.execution)
            clock = _MutableClock(datetime(2026, 7, 16, tzinfo=timezone.utc))
            callbacks = SQLiteReportCallbackAdapter(
                service,
                callback_url="http://callback.local/result",
                callback_timeout=5,
                lease_seconds=30,
                clock=clock,
            )
            acquired = callbacks.acquire(
                ReportCallbackAcquire(
                    created.execution.task_id,
                    created.execution.input_snapshot.report_id,
                )
            )
            self.assertEqual(ReportCallbackAcquireOutcome.ACQUIRED, acquired.outcome)

            clock.value += timedelta(seconds=31)
            wait_result = callbacks.wait_until_released(
                WaitForReportCallbackRelease(
                    created.execution.input_snapshot.report_id,
                    timeout_seconds=1,
                )
            )
            new_submission = tasks.create_if_allowed(_command(_submission()))

        self.assertEqual(
            ReportCallbackWaitOutcome.OUTCOME_UNKNOWN,
            wait_result.outcome,
        )
        self.assertEqual(
            TaskSubmissionOutcome.CALLBACK_OUTCOME_UNKNOWN,
            new_submission.outcome,
        )


if __name__ == "__main__":
    unittest.main()
