"""阶段 1C-3 报告 SQLite execution、原子受理与条件写验收。"""

from __future__ import annotations

import sqlite3
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
    SubmitReportTask,
)
from app.modules.report.domain import (
    REPORT_INPUT_SCHEMA_VERSION,
    ReportId,
    ReportSubmission,
    ReportTaskConflictError,
    build_report_callback,
    build_report_result,
)
from app.modules.report.ports import (
    ReportArtifactCategory,
    ReportArtifactRef,
    ReportCallbackDeliveryOutcome,
    ReportCallbackDeliveryResult,
)
from app.modules.tasks.adapters import LegacyTaskCommandAdapter
from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.tasks.ports import (
    ExpectedProgressUpdate,
    ExpectedTaskCompletion,
    TaskClaimOutcome,
    TaskCommandPort,
    TaskQueueInspectionPort,
    TaskSubmissionCommand,
    TaskSubmissionOutcome,
)
from app.services.llm_service.task_service import LLMTaskService
from tests import workspace_tempdir
from tests.task_service_fixtures import seed_legacy_file_task, seed_legacy_report_task
from tests.fakes import (
    FakeProgressPublisherPort,
    FakeReportCallbackPort,
    FakeReportDispatcherPort,
    InvocationRecorder,
)


def _submission(report_id: int = 132) -> ReportSubmission:
    return ReportSubmission(
        report_id=ReportId.from_public_value(report_id),
        source_urls=(
            f"http://files.local/{report_id}/a.mhtml",
            f"http://files.local/{report_id}/b.pdf",
        ),
        template_outline_url=f"http://files.local/{report_id}/template.docx",
        template_desc="模板说明",
        requirement="生成完整报告",
        trace_id=f"trace-report-{report_id}",
    )


def _command(submission: ReportSubmission) -> TaskSubmissionCommand[ReportSubmission]:
    return TaskSubmissionCommand(
        task_type="report",
        business_ref=TaskBusinessRef(
            "report",
            submission.report_id.business_key,
        ),
        input_schema_version=REPORT_INPUT_SCHEMA_VERSION,
        submission=submission,
        trace_id=submission.trace_id,
    )


def _adapter(
    service: LLMTaskService,
) -> LegacyTaskCommandAdapter[ReportSubmission, object, ReportTaskCompletion]:
    return LegacyTaskCommandAdapter(service, ReportTaskCommandCodec())


def _success_completion(
    task_id: TaskId,
    report_id: ReportId,
) -> ReportTaskCompletion:
    result = build_report_result(report_id, "<section>报告内容</section>")
    return ReportTaskCompletion(
        callback_payload=build_report_callback(
            report_id,
            result.html_details,
            status="1",
        ),
        report_result=result,
        report_artifact=ReportArtifactRef(
            task_id,
            f"{task_id.value}:report.html",
            ReportArtifactCategory.REPORT_HTML,
            size_bytes=len(result.html_details.encode("utf-8")),
            checksum="report-checksum",
        ),
    )


class ReportTaskAdapterPersistenceTests(unittest.TestCase):
    """验证 Schema、序列化、领取和双表原子条件写。"""

    def test_schema_is_idempotent_and_preserves_legacy_projection(self) -> None:
        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            first_service = LLMTaskService(database)
            legacy = seed_legacy_file_task(first_service,
                "legacy.pdf",
                {"businessType": "file"},
            )

            second_service = LLMTaskService(database)
            self.assertEqual(
                legacy["execution_id"],
                second_service.get_task("file", "legacy.pdf")["execution_id"],
            )
            with sqlite3.connect(database) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                indexes = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index'"
                    )
                }
                guard_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(callback_delivery_guards)"
                    )
                }
                journal_mode = connection.execute(
                    "PRAGMA journal_mode"
                ).fetchone()[0]

        self.assertIn("llm_task_executions", tables)
        self.assertIn("callback_delivery_guards", tables)
        self.assertIn("idx_llm_task_executions_scan", indexes)
        self.assertIn("idx_llm_task_executions_business", indexes)
        self.assertIn("idx_callback_delivery_guards_recovery", indexes)
        self.assertIn("lease_token", guard_columns)
        self.assertIn("lease_version", guard_columns)
        self.assertEqual("wal", str(journal_mode).lower())

    def test_queue_inspection_reports_running_without_mutating_it(self) -> None:
        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(
                str(Path(runtime_directory) / "tasks.sqlite3")
            )
            adapter = _adapter(service)
            self.assertIsInstance(adapter, TaskQueueInspectionPort)
            first = adapter.create_if_allowed(_command(_submission(201)))
            second = adapter.create_if_allowed(_command(_submission(202)))
            assert first.execution is not None
            assert second.execution is not None
            adapter.claim(first.execution.task_id)

            snapshot = adapter.inspect_queue(
                "report",
                running_sample_limit=1,
            )

            self.assertEqual(1, snapshot.accepted_count)
            self.assertEqual(1, snapshot.running_count)
            self.assertEqual((first.execution.task_id,), snapshot.running_task_ids)
            self.assertIsNotNone(snapshot.oldest_accepted_at)
            self.assertIsNotNone(snapshot.oldest_running_at)
            # 诊断查询必须严格只读，不能把无法判断归属的 running 重置为 accepted。
            self.assertEqual(
                "running",
                service.get_task_execution(first.execution.task_id.value)[
                    "execution_state"
                ],
            )

    def test_round_trip_claim_progress_and_terminal_are_task_id_bound(self) -> None:
        huge_report_id = 10**100 + 17
        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(
                str(Path(runtime_directory) / "tasks.sqlite3")
            )
            adapter = _adapter(service)
            self.assertIsInstance(adapter, TaskCommandPort)
            submission = _submission(huge_report_id)

            created = adapter.create_if_allowed(_command(submission))

            self.assertEqual(TaskSubmissionOutcome.ACCEPTED, created.outcome)
            assert created.execution is not None
            task_id = created.execution.task_id
            self.assertEqual(submission.source_urls, created.execution.input_snapshot.source_urls)
            self.assertEqual(
                huge_report_id,
                created.execution.input_snapshot.report_id.public_value,
            )
            self.assertEqual(created.execution, adapter.get_execution(task_id))

            premature_progress = adapter.update_progress_if_current(
                ExpectedProgressUpdate(
                    expected_task_id=task_id,
                    business_ref=created.execution.business_ref,
                    progress=0.15,
                    message="未领取不得更新",
                    execution_state="running",
                    public_status="0",
                )
            )
            claimed = adapter.claim(task_id)
            repeated_claim = adapter.claim(task_id)
            self.assertFalse(premature_progress)
            self.assertEqual(TaskClaimOutcome.CLAIMED, claimed.outcome)
            self.assertEqual(TaskClaimOutcome.ALREADY_RUNNING, repeated_claim.outcome)

            progress_updated = adapter.update_progress_if_current(
                ExpectedProgressUpdate(
                    expected_task_id=task_id,
                    business_ref=created.execution.business_ref,
                    progress=0.35,
                    message="正在生成报告",
                    execution_state="running",
                    public_status="0",
                )
            )
            completion = _success_completion(task_id, submission.report_id)
            finished = adapter.finish_if_current(
                ExpectedTaskCompletion(
                    expected_task_id=task_id,
                    business_ref=created.execution.business_ref,
                    execution_state="succeeded",
                    public_status="1",
                    message="报告生成完成",
                    result=completion,
                )
            )
            repeated_finish = adapter.finish_if_current(
                ExpectedTaskCompletion(
                    expected_task_id=task_id,
                    business_ref=created.execution.business_ref,
                    execution_state="failed",
                    public_status="2",
                    message="不得覆盖",
                    result=ReportTaskCompletion(
                        callback_payload=build_report_callback(
                            submission.report_id,
                            "",
                            status="2",
                        )
                    ),
                )
            )
            latest = service.get_task("report", submission.report_id.business_key)
            execution = service.get_task_execution(task_id.value)

        self.assertTrue(progress_updated)
        self.assertTrue(finished)
        self.assertFalse(repeated_finish)
        self.assertIsNotNone(latest)
        self.assertIsNotNone(execution)
        assert latest is not None and execution is not None
        self.assertEqual(task_id.value, latest["execution_id"])
        self.assertEqual("1", latest["status"])
        self.assertEqual(1.0, latest["progress"])
        self.assertEqual("succeeded", execution["execution_state"])
        self.assertEqual(
            {
                "businessType": "report",
                "data": {
                    "reportId": huge_report_id,
                    "status": "1",
                    "details": "<section>报告内容</section>",
                },
                "msg": "生成成功",
            },
            latest["result_payload"],
        )
        self.assertEqual(1, execution["result_payload"]["schema_version"])
        self.assertEqual("1", execution["result_payload"]["status"])
        self.assertNotIn("callback_payload", execution["result_payload"])
        self.assertNotIn("html_details", execution["result_payload"])

    def test_guard_and_active_projection_return_precise_internal_outcomes(self) -> None:
        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            service = LLMTaskService(database)
            adapter = _adapter(service)
            submission = _submission()
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    INSERT INTO callback_delivery_guards (
                        business_type, business_key, owner_execution_id,
                        state, deadline_at, updated_at
                    ) VALUES (
                        'report', '132', 'old-task', 'sending',
                        '2999-01-01T00:00:00+00:00',
                        '2026-07-17T00:00:00+00:00'
                    )
                    """
                )

            sending = adapter.create_if_allowed(_command(submission))
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    UPDATE callback_delivery_guards
                    SET state = 'outcome_unknown'
                    WHERE business_type = 'report' AND business_key = '132'
                    """
                )
            unknown = adapter.create_if_allowed(_command(submission))
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    UPDATE callback_delivery_guards
                    SET state = 'idle'
                    WHERE business_type = 'report' AND business_key = '132'
                    """
                )
            accepted = adapter.create_if_allowed(_command(submission))
            active = adapter.create_if_allowed(_command(submission))
            with sqlite3.connect(database) as connection:
                execution_count = connection.execute(
                    "SELECT COUNT(*) FROM llm_task_executions"
                ).fetchone()[0]

        self.assertEqual(TaskSubmissionOutcome.CALLBACK_SENDING, sending.outcome)
        self.assertEqual(
            TaskSubmissionOutcome.CALLBACK_OUTCOME_UNKNOWN,
            unknown.outcome,
        )
        self.assertEqual(TaskSubmissionOutcome.ACCEPTED, accepted.outcome)
        self.assertEqual(TaskSubmissionOutcome.ACTIVE_CONFLICT, active.outcome)
        self.assertEqual(1, execution_count)

    def test_submit_application_uses_atomic_adapter_before_notifications(self) -> None:
        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(
                str(Path(runtime_directory) / "tasks.sqlite3")
            )
            adapter = _adapter(service)
            recorder = InvocationRecorder()
            progress = FakeProgressPublisherPort(recorder)
            dispatcher = FakeReportDispatcherPort(recorder)
            submit = SubmitReportTask(
                task_commands=adapter,
                progress_publisher=progress,
                dispatcher=dispatcher,
            )

            accepted = submit.execute(_submission())
            with self.assertRaises(ReportTaskConflictError):
                submit.execute(_submission())

            projection = service.get_task("report", "132")

        assert projection is not None
        self.assertEqual(accepted.task_id.value, projection["execution_id"])
        self.assertEqual(1, len(progress.publications))
        self.assertEqual([accepted.task_id], dispatcher.task_ids)

    def test_committed_acceptance_survives_corrupt_repository_return_mapping(self) -> None:
        """事务已提交后即使读回映射损坏，也必须返回 202 语义并唤醒 Dispatcher。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(
                str(Path(runtime_directory) / "tasks.sqlite3")
            )
            adapter = _adapter(service)
            recorder = InvocationRecorder()
            progress = FakeProgressPublisherPort(recorder)
            dispatcher = FakeReportDispatcherPort(recorder)
            submit = SubmitReportTask(
                task_commands=adapter,
                progress_publisher=progress,
                dispatcher=dispatcher,
            )
            original_create = service.create_task_execution_if_allowed

            def committed_but_corrupt(**kwargs):
                raw = dict(original_create(**kwargs))
                raw["execution"] = {"corrupt": True}
                return raw

            with patch.object(
                service,
                "create_task_execution_if_allowed",
                side_effect=committed_but_corrupt,
            ), self.assertLogs(
                "app.modules.tasks.adapters.legacy_task_commands",
                level="CRITICAL",
            ):
                accepted = submit.execute(_submission())

            projection = service.get_task("report", "132")

        assert projection is not None
        self.assertEqual(accepted.task_id.value, projection["execution_id"])
        self.assertEqual([accepted.task_id], dispatcher.task_ids)
        self.assertEqual(1, len(progress.publications))

    def test_projection_failure_rolls_back_new_execution_without_orphan(self) -> None:
        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            service = LLMTaskService(database)
            adapter = _adapter(service)
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TRIGGER reject_report_projection
                    BEFORE INSERT ON llm_tasks
                    WHEN NEW.business_type = 'report'
                    BEGIN
                        SELECT RAISE(ABORT, 'forced projection failure');
                    END
                    """
                )

            with self.assertRaises(sqlite3.IntegrityError):
                adapter.create_if_allowed(_command(_submission()))

            with sqlite3.connect(database) as connection:
                execution_count = connection.execute(
                    "SELECT COUNT(*) FROM llm_task_executions"
                ).fetchone()[0]
                projection_count = connection.execute(
                    "SELECT COUNT(*) FROM llm_tasks WHERE business_type = 'report'"
                ).fetchone()[0]

        self.assertEqual(0, execution_count)
        self.assertEqual(0, projection_count)

    def test_projection_update_failure_rolls_back_execution_progress(self) -> None:
        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            service = LLMTaskService(database)
            adapter = _adapter(service)
            created = adapter.create_if_allowed(_command(_submission()))
            assert created.execution is not None
            adapter.claim(created.execution.task_id)
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TRIGGER reject_report_progress
                    BEFORE UPDATE OF progress ON llm_tasks
                    WHEN NEW.business_type = 'report'
                    BEGIN
                        SELECT RAISE(ABORT, 'forced progress failure');
                    END
                    """
                )

            with self.assertRaises(sqlite3.IntegrityError):
                adapter.update_progress_if_current(
                    ExpectedProgressUpdate(
                        expected_task_id=created.execution.task_id,
                        business_ref=created.execution.business_ref,
                        progress=0.35,
                        message="不得半提交",
                        execution_state="running",
                        public_status="0",
                    )
                )
            execution = service.get_task_execution(created.execution.task_id.value)
            projection = service.get_task("report", "132")

        assert execution is not None and projection is not None
        self.assertEqual(0.0, execution["progress"])
        self.assertEqual("", execution["message"])
        self.assertEqual(0.0, projection["progress"])
        self.assertEqual("", projection["message"])

    def test_legacy_projection_replacement_makes_old_execution_stale(self) -> None:
        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(
                str(Path(runtime_directory) / "tasks.sqlite3")
            )
            adapter = _adapter(service)
            created = adapter.create_if_allowed(_command(_submission()))
            assert created.execution is not None

            seed_legacy_report_task(
                service,
                132,
                {"businessType": "report", "source": "legacy-route"},
            )
            claim = adapter.claim(created.execution.task_id)
            execution = service.get_task_execution(created.execution.task_id.value)

        self.assertEqual(TaskClaimOutcome.STALE, claim.outcome)
        self.assertIsNotNone(execution)
        assert execution is not None
        self.assertEqual("stale", execution["execution_state"])

    def test_old_execution_cannot_overwrite_new_projection_owner(self) -> None:
        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(
                str(Path(runtime_directory) / "tasks.sqlite3")
            )
            adapter = _adapter(service)
            submission = _submission()
            first = adapter.create_if_allowed(_command(submission))
            assert first.execution is not None
            adapter.claim(first.execution.task_id)
            first_completion = _success_completion(
                first.execution.task_id,
                submission.report_id,
            )
            self.assertTrue(
                adapter.finish_if_current(
                    ExpectedTaskCompletion(
                        expected_task_id=first.execution.task_id,
                        business_ref=first.execution.business_ref,
                        execution_state="succeeded",
                        public_status="1",
                        message="第一次完成",
                        result=first_completion,
                    )
                )
            )
            second = adapter.create_if_allowed(_command(submission))
            assert second.execution is not None

            old_progress = adapter.update_progress_if_current(
                ExpectedProgressUpdate(
                    expected_task_id=first.execution.task_id,
                    business_ref=first.execution.business_ref,
                    progress=0.9,
                    message="旧进度不得写入",
                    execution_state="running",
                    public_status="0",
                )
            )
            old_finish = adapter.finish_if_current(
                ExpectedTaskCompletion(
                    expected_task_id=first.execution.task_id,
                    business_ref=first.execution.business_ref,
                    execution_state="failed",
                    public_status="2",
                    message="旧终态不得写入",
                    result=ReportTaskCompletion(
                        callback_payload=build_report_callback(
                            submission.report_id,
                            "",
                            status="2",
                        )
                    ),
                )
            )
            latest = service.get_task("report", "132")
            first_execution = service.get_task_execution(
                first.execution.task_id.value
            )

        self.assertFalse(old_progress)
        self.assertFalse(old_finish)
        assert latest is not None and first_execution is not None
        self.assertEqual(second.execution.task_id.value, latest["execution_id"])
        self.assertEqual("0", latest["status"])
        self.assertEqual(0.0, latest["progress"])
        self.assertEqual("succeeded", first_execution["execution_state"])
        self.assertEqual("第一次完成", first_execution["message"])

    def test_superseded_running_execution_is_atomically_marked_stale(self) -> None:
        """条件写拒绝旧 owner 时必须同时收敛 running 事实，不能留下幽灵任务。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(
                str(Path(runtime_directory) / "tasks.sqlite3")
            )
            adapter = _adapter(service)
            created = adapter.create_if_allowed(_command(_submission()))
            assert created.execution is not None
            task_id = created.execution.task_id
            adapter.claim(task_id)

            # 模拟切换窗口中仍存在的遗留路由覆盖 latest 投影。
            seed_legacy_report_task(
                service,
                132,
                {"businessType": "report", "source": "legacy-route"},
            )
            updated = adapter.update_progress_if_current(
                ExpectedProgressUpdate(
                    expected_task_id=task_id,
                    business_ref=created.execution.business_ref,
                    progress=0.5,
                    message="旧执行不得写入",
                    execution_state="running",
                    public_status="0",
                )
            )
            execution = service.get_task_execution(task_id.value)

        self.assertFalse(updated)
        assert execution is not None
        self.assertEqual("stale", execution["execution_state"])
        self.assertIsNotNone(execution["completed_at"])

    def test_legacy_recovery_posts_only_public_callback_projection(
        self,
    ) -> None:
        """check-task 同步恢复必须经过 Guard，且不得泄露 execution 内部 Schema。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(
                str(Path(runtime_directory) / "tasks.sqlite3")
            )
            adapter = _adapter(service)
            created = adapter.create_if_allowed(_command(_submission()))
            assert created.execution is not None
            adapter.claim(created.execution.task_id)
            adapter.finish_if_current(
                ExpectedTaskCompletion(
                    expected_task_id=created.execution.task_id,
                    business_ref=created.execution.business_ref,
                    execution_state="succeeded",
                    public_status="1",
                    message="报告生成完成",
                    result=_success_completion(
                        created.execution.task_id,
                        created.execution.input_snapshot.report_id,
                    ),
                )
            )

            posted_payloads: list[dict[str, object]] = []

            def transport(
                payload: dict[str, object],
            ) -> ReportCallbackDeliveryResult:
                posted_payloads.append(payload)
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
            replayed = recovery.execute(ReportId.from_public_value(132))

        self.assertTrue(replayed)
        self.assertEqual(1, len(posted_payloads))
        posted_payload = posted_payloads[0]
        self.assertEqual(
            {"businessType", "data", "msg"},
            set(posted_payload),
        )
        self.assertEqual(132, posted_payload["data"]["reportId"])


class ReportTaskAdapterConcurrencyTests(unittest.TestCase):
    """使用精确 Barrier 验证 50 并发，无随机 sleep。"""

    def test_fifty_same_business_keys_have_exactly_one_accepted(self) -> None:
        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            service = LLMTaskService(database)
            adapter = _adapter(service)
            command = _command(_submission())
            barrier = threading.Barrier(50)

            def submit() -> TaskSubmissionOutcome:
                barrier.wait(timeout=20)
                return adapter.create_if_allowed(command).outcome

            with ThreadPoolExecutor(max_workers=50) as executor:
                futures = [executor.submit(submit) for _ in range(50)]
                outcomes = [future.result(timeout=60) for future in futures]

            with sqlite3.connect(database) as connection:
                execution_count = connection.execute(
                    "SELECT COUNT(*) FROM llm_task_executions"
                ).fetchone()[0]
                projection = connection.execute(
                    """
                    SELECT execution_id, status
                    FROM llm_tasks
                    WHERE business_type = 'report' AND business_key = '132'
                    """
                ).fetchone()

        self.assertEqual(1, outcomes.count(TaskSubmissionOutcome.ACCEPTED))
        self.assertEqual(49, outcomes.count(TaskSubmissionOutcome.ACTIVE_CONFLICT))
        self.assertEqual(1, execution_count)
        self.assertIsNotNone(projection)
        assert projection is not None
        self.assertEqual("0", projection[1])

    def test_fifty_claims_have_exactly_one_owner(self) -> None:
        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(
                str(Path(runtime_directory) / "tasks.sqlite3")
            )
            adapter = _adapter(service)
            created = adapter.create_if_allowed(_command(_submission()))
            assert created.execution is not None
            task_id = created.execution.task_id
            barrier = threading.Barrier(50)

            def claim() -> TaskClaimOutcome:
                barrier.wait(timeout=20)
                return adapter.claim(task_id).outcome

            with ThreadPoolExecutor(max_workers=50) as executor:
                futures = [executor.submit(claim) for _ in range(50)]
                outcomes = [future.result(timeout=60) for future in futures]

            execution = service.get_task_execution(task_id.value)

        self.assertEqual(1, outcomes.count(TaskClaimOutcome.CLAIMED))
        self.assertEqual(49, outcomes.count(TaskClaimOutcome.ALREADY_RUNNING))
        assert execution is not None
        self.assertEqual("running", execution["execution_state"])

    def test_fifty_distinct_business_keys_are_all_persisted(self) -> None:
        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            service = LLMTaskService(database)
            adapter = _adapter(service)
            barrier = threading.Barrier(50)

            def submit(index: int) -> TaskSubmissionOutcome:
                barrier.wait(timeout=20)
                submission = _submission(1000 + index)
                return adapter.create_if_allowed(_command(submission)).outcome

            with ThreadPoolExecutor(max_workers=50) as executor:
                futures = [executor.submit(submit, index) for index in range(50)]
                outcomes = [future.result(timeout=60) for future in futures]

            accepted = adapter.list_accepted("report", limit=100)
            with sqlite3.connect(database) as connection:
                execution_count = connection.execute(
                    "SELECT COUNT(*) FROM llm_task_executions"
                ).fetchone()[0]
                projection_count = connection.execute(
                    "SELECT COUNT(*) FROM llm_tasks WHERE business_type = 'report'"
                ).fetchone()[0]

        self.assertEqual(
            [TaskSubmissionOutcome.ACCEPTED] * 50,
            sorted(outcomes, key=lambda item: item.value),
        )
        self.assertEqual(50, len(accepted))
        self.assertEqual(50, execution_count)
        self.assertEqual(50, projection_count)


if __name__ == "__main__":
    unittest.main()
