"""阶段 1F-4：文件分析批量原子受理与顺序协调的离线验收。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest.mock import patch

from app.modules.analysis.adapters import (
    AnalysisTaskInputCodec,
    AnalysisTaskSnapshotCorruptedError,
    SQLiteAnalysisBatchCommandAdapter,
)
from app.modules.analysis.application import (
    AnalysisBatchOrderContractError,
    AnalysisBatchOrderCoordinator,
    SubmitAnalysisBatch,
)
from app.modules.analysis.domain.task_inputs import (
    AnalysisPolicySnapshot,
    AnalysisSubmissionSnapshot,
    FrozenJsonArray,
    FrozenJsonObject,
)
from app.modules.analysis.ports import (
    AnalysisBatchAdmission,
    AnalysisBatchAdmissionOutcome,
    AnalysisBatchCommand,
    AnalysisBatchCommandPort,
    AnalysisExecutionRef,
    AnalysisPoisonTaskCommandPort,
)
from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.tasks.ports import (
    ExpectedProgressUpdate,
    TaskCommandPort,
    TaskQueueInspectionPort,
)
from app.services.llm_service.task_service import LLMTaskService, TaskAdmissionBusyError
from tests import workspace_tempdir
from tests.fakes.analysis import StrictAnalysisFakeScript, StrictAnalysisPortFake


def _raw_params(index: int, *, prefix: str = "batch") -> dict[str, object]:
    """构造一项最小合法文件分析参数，并保留未知字段以验证快照不被裁剪。"""

    file_name = f"{prefix}-{index:03d}.txt"
    return {
        "fileName": file_name,
        "filePath": f"https://example.invalid/{prefix}/{index}.txt",
        "unknownExtension": {"requestIndex": index, "empty": ""},
    }


def _command(
    count: int = 1,
    *,
    prefix: str = "batch",
    trace_id: str | None = None,
) -> AnalysisBatchCommand:
    """通过同一冻结请求投影构造命令，保持 params 与 submission 完全同源。"""

    raw_params = [_raw_params(index, prefix=prefix) for index in range(1, count + 1)]
    projection = FrozenJsonObject.from_mapping(
        {"businessType": "file", "params": raw_params},
        name="analysis_batch_request",
    )
    frozen_params = projection.get("params")
    if not isinstance(frozen_params, FrozenJsonArray):  # pragma: no cover - 夹具保护。
        raise AssertionError("测试夹具未冻结params数组")
    submissions = tuple(
        AnalysisSubmissionSnapshot.from_frozen_params(
            item,
            policy_snapshot=AnalysisPolicySnapshot.default(),
        )
        for item in frozen_params.values
        if isinstance(item, FrozenJsonObject)
    )
    if len(submissions) != count:  # pragma: no cover - 夹具保护。
        raise AssertionError("测试夹具未冻结params对象")
    return AnalysisBatchCommand(
        request_projection=projection,
        submissions=submissions,
        trace_id=trace_id or f"analysis-batch-trace-{prefix}",
    )


def _adapter(service: LLMTaskService) -> SQLiteAnalysisBatchCommandAdapter:
    """以真实临时 SQLite 验证 Adapter，不构造 Container 或后台线程。"""

    return SQLiteAnalysisBatchCommandAdapter(service)


class _WakeFailingDispatcher:
    """只模拟提交后 Event 失败；不创建后台线程或进程内任务队列。"""

    def wake_up(self) -> None:
        raise RuntimeError("forced wake failure")

    def start(self) -> None:
        return None

    def stop(self, *, timeout_seconds: float | None = None) -> bool:
        return True

    def close(self) -> None:
        return None


class AnalysisBatchSchemaAndAdapterTests(unittest.TestCase):
    """锁定追加 Schema、单事务写入、Codec 与 Worker 读取边界。"""

    def test_schema_is_additive_and_legacy_file_projection_remains_readable(self) -> None:
        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            first_service = LLMTaskService(database)
            legacy = first_service.create_file_task(
                "legacy-file.txt",
                {"businessType": "file", "params": []},
            )
            LLMTaskService(database)
            with sqlite3.connect(database) as connection:
                execution_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(llm_task_executions)"
                    )
                }
                index_sql = connection.execute(
                    """
                    SELECT sql
                    FROM sqlite_master
                    WHERE type = 'index'
                      AND name = 'idx_llm_task_executions_file_batch_sequence'
                    """
                ).fetchone()[0]

            self.assertIn("batch_id", execution_columns)
            self.assertIn("batch_sequence", execution_columns)
            self.assertIn("WHERE business_type = 'file' AND batch_id IS NOT NULL", index_sql)
            self.assertEqual(
                legacy["execution_id"],
                first_service.get_task("file", "legacy-file.txt")["execution_id"],
            )

    def test_thirty_two_items_are_committed_atomically_with_stable_batch_and_dispatch_order(self) -> None:
        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            service = LLMTaskService(database)
            adapter = _adapter(service)
            command = _command(32, prefix="complete")

            self.assertIsInstance(adapter, AnalysisBatchCommandPort)
            self.assertIsInstance(adapter, AnalysisPoisonTaskCommandPort)
            self.assertIsInstance(adapter, TaskCommandPort)
            admission = adapter.create_batch_if_allowed(command)
            self.assertEqual(AnalysisBatchAdmissionOutcome.ACCEPTED, admission.outcome)
            self.assertEqual(32, len(admission.executions))

            with sqlite3.connect(database) as connection:
                rows = connection.execute(
                    """
                    SELECT execution_id, business_key, batch_id, batch_sequence,
                           dispatch_sequence, public_status
                    FROM llm_task_executions
                    WHERE business_type = 'file'
                    ORDER BY dispatch_sequence
                    """
                ).fetchall()
                projections = connection.execute(
                    """
                    SELECT business_key, execution_id, status
                    FROM llm_tasks
                    WHERE business_type = 'file'
                    ORDER BY business_key
                    """
                ).fetchall()

            batch_ids = {execution.batch_id for execution in admission.executions}
            self.assertEqual(1, len(batch_ids))
            self.assertEqual(32, len(rows))
            self.assertEqual(tuple(range(1, 33)), tuple(row[3] for row in rows))
            self.assertEqual(tuple(range(1, 33)), tuple(row[4] for row in rows))
            self.assertEqual(
                tuple(execution.task_id.value for execution in admission.executions),
                tuple(row[0] for row in rows),
            )
            self.assertEqual(
                tuple(execution.file_name for execution in admission.executions),
                tuple(row[1] for row in rows),
            )
            self.assertEqual(("1",) + ("0",) * 31, tuple(row[5] for row in rows))
            self.assertEqual(32, len(projections))
            self.assertEqual({"1"}, {row[2] for row in projections if row[0] == "complete-001.txt"})
            self.assertEqual({"0"}, {row[2] for row in projections if row[0] != "complete-001.txt"})

            first = admission.executions[0]
            loaded = adapter.load_input(first.task_id)
            claimed = adapter.claim_if_accepted(first.task_id)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(first.task_id.value, loaded.task_id)
            self.assertEqual(first.batch_id, loaded.batch_id)
            self.assertEqual("complete-001.txt", loaded.file_name)
            self.assertEqual(first, claimed.execution)
            self.assertEqual("claimed", claimed.outcome.value)
            self.assertEqual(
                tuple(execution.task_id for execution in admission.executions[1:]),
                adapter.list_accepted("file", limit=64),
            )

    def test_poisoned_snapshot_converges_failed_without_decoding_payload(self) -> None:
        """坏输入必须按最新 owner 原子终结，不能无限冷却并占用持久积压。"""

        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            service = LLMTaskService(database)
            adapter = _adapter(service)
            admission = adapter.create_batch_if_allowed(
                _command(1, prefix="poison")
            )
            execution = admission.executions[0]
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    UPDATE llm_task_executions
                    SET input_payload = ?
                    WHERE execution_id = ?
                    """,
                    ('{"schema_version":1,"broken":true}', execution.task_id.value),
                )
                connection.commit()

            with self.assertRaises(AnalysisTaskSnapshotCorruptedError):
                adapter.get_execution(execution.task_id)
            self.assertEqual(
                execution,
                adapter.fail_poisoned_accepted(
                    execution.task_id,
                    reason="AnalysisTaskInputCodecError",
                ),
            )
            self.assertIsNone(
                adapter.fail_poisoned_accepted(
                    execution.task_id,
                    reason="AnalysisTaskInputCodecError",
                )
            )

            with sqlite3.connect(database) as connection:
                execution_row = connection.execute(
                    """
                    SELECT execution_state, public_status, progress,
                           result_payload, last_dispatch_error
                    FROM llm_task_executions
                    WHERE execution_id = ?
                    """,
                    (execution.task_id.value,),
                ).fetchone()
                projection_row = connection.execute(
                    """
                    SELECT status, progress, result_payload
                    FROM llm_tasks
                    WHERE business_type = 'file' AND business_key = ?
                    """,
                    (execution.file_name,),
                ).fetchone()

            assert execution_row is not None
            assert projection_row is not None
            self.assertEqual(("failed", "3", 1.0), execution_row[:3])
            self.assertEqual("AnalysisTaskInputCodecError", execution_row[4])
            self.assertFalse(json.loads(execution_row[3])["succeeded"])
            self.assertEqual(("3", 1.0), projection_row[:2])
            self.assertEqual(
                {
                    "businessType": "file",
                    "data": {
                        "fileName": execution.file_name,
                        "status": "3",
                    },
                    "msg": "解析失败",
                },
                json.loads(projection_row[2]),
            )

    def test_new_analysis_queue_excludes_legacy_file_and_persists_capped_backoff(self) -> None:
        """新 Dispatcher 的观测/退避不得误接管旧 file 兼容 execution。"""

        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            service = LLMTaskService(database)
            service.create_file_task(
                "legacy-only.txt",
                {"businessType": "file", "params": []},
            )
            clock_value = datetime(2026, 7, 27, tzinfo=timezone.utc).isoformat()
            adapter = SQLiteAnalysisBatchCommandAdapter(
                service,
                clock=lambda: clock_value,
            )
            execution = adapter.create_batch_if_allowed(
                _command(1, prefix="backoff")
            ).executions[0]

            self.assertIsInstance(adapter, TaskQueueInspectionPort)
            snapshot = adapter.inspect_queue("file", running_sample_limit=5)
            self.assertEqual(1, snapshot.accepted_count)
            self.assertEqual((), snapshot.running_task_ids)
            self.assertEqual((execution.task_id,), adapter.list_accepted("file", limit=5))

            for expected_count, expected_delay_seconds in (
                (1, 5.0),
                (2, 10.0),
                (3, 12.0),
            ):
                with self.subTest(expected_count=expected_count):
                    self.assertTrue(
                        adapter.defer_accepted_with_backoff(
                            execution.task_id,
                            retry_base_seconds=5.0,
                            retry_max_seconds=12.0,
                            reason="forced_dispatch_failure",
                        )
                    )
                    with sqlite3.connect(database) as connection:
                        row = connection.execute(
                            """
                            SELECT dispatch_failure_count, next_dispatch_at,
                                   last_dispatch_error
                            FROM llm_task_executions
                            WHERE execution_id = ?
                            """,
                            (execution.task_id.value,),
                        ).fetchone()
                    assert row is not None
                    self.assertEqual(expected_count, row[0])
                    self.assertEqual("forced_dispatch_failure", row[2])
                    self.assertEqual(
                        datetime.fromisoformat(clock_value)
                        + timedelta(seconds=expected_delay_seconds),
                        datetime.fromisoformat(row[1]),
                    )

    def test_expected_progress_uses_narrow_identity_without_snapshot_decode(self) -> None:
        """领取后的高频 expected 写不得反复重建大型领域树快照。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(
                str(Path(runtime_directory) / "tasks.sqlite3")
            )
            adapter = _adapter(service)
            execution = adapter.create_batch_if_allowed(
                _command(1, prefix="narrow")
            ).executions[0]
            claimed = adapter.claim(execution.task_id)
            self.assertEqual("claimed", claimed.outcome.value)

            with patch.object(
                AnalysisTaskInputCodec,
                "decode",
                side_effect=AssertionError("expected 写不应解码输入快照"),
            ):
                updated = adapter.update_progress_if_current(
                    ExpectedProgressUpdate(
                        expected_task_id=execution.task_id,
                        business_ref=TaskBusinessRef(
                            "file",
                            execution.file_name,
                        ),
                        progress=0.25,
                        message="窄身份条件写",
                        execution_state="running",
                        public_status="1",
                    )
                )

            self.assertTrue(updated)

    def test_first_middle_or_last_projection_failure_rolls_back_all_thirty_two_executions(self) -> None:
        """任一项持久化失败时，先前已插入 execution 必须随事务一并消失。"""

        for rejected_index in (1, 16, 32):
            with self.subTest(rejected_index=rejected_index), workspace_tempdir() as runtime_directory:
                database = str(Path(runtime_directory) / "tasks.sqlite3")
                service = LLMTaskService(database)
                adapter = _adapter(service)
                command = _command(32, prefix=f"rollback-{rejected_index}")
                rejected_key = command.submissions[rejected_index - 1].file_name
                with sqlite3.connect(database) as connection:
                    connection.execute(
                        f"""
                        CREATE TRIGGER reject_analysis_projection_{rejected_index}
                        BEFORE INSERT ON llm_tasks
                        WHEN NEW.business_type = 'file'
                         AND NEW.business_key = '{rejected_key}'
                        BEGIN
                            SELECT RAISE(ABORT, 'forced analysis projection failure');
                        END
                        """
                    )

                with self.assertRaises(sqlite3.IntegrityError):
                    adapter.create_batch_if_allowed(command)
                with sqlite3.connect(database) as connection:
                    execution_count = connection.execute(
                        "SELECT COUNT(*) FROM llm_task_executions"
                    ).fetchone()[0]
                    projection_count = connection.execute(
                        "SELECT COUNT(*) FROM llm_tasks WHERE business_type = 'file'"
                    ).fetchone()[0]
                self.assertEqual(0, execution_count)
                self.assertEqual(0, projection_count)

    def test_active_and_callback_guard_conflicts_return_zero_insert_results(self) -> None:
        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            service = LLMTaskService(database)
            adapter = _adapter(service)
            active_command = _command(2, prefix="active")
            service.create_file_task(
                active_command.submissions[0].file_name,
                {"businessType": "file", "params": []},
                status="1",
            )
            active = adapter.create_batch_if_allowed(active_command)

            callback_command = _command(1, prefix="guard")
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    INSERT INTO callback_delivery_guards (
                        business_type, business_key, owner_execution_id, state,
                        deadline_at, updated_at
                    ) VALUES ('file', ?, 'previous-execution', 'outcome_unknown',
                              NULL, '2026-07-26T00:00:00+00:00')
                    """,
                    (callback_command.submissions[0].file_name,),
                )
            callback = adapter.create_batch_if_allowed(callback_command)
            with sqlite3.connect(database) as connection:
                execution_count = connection.execute(
                    "SELECT COUNT(*) FROM llm_task_executions"
                ).fetchone()[0]

        self.assertEqual(AnalysisBatchAdmissionOutcome.CONFLICT_ACTIVE, active.outcome)
        self.assertEqual(
            AnalysisBatchAdmissionOutcome.CONFLICT_CALLBACK_PENDING,
            callback.outcome,
        )
        self.assertEqual(0, execution_count)

    def test_reusing_completed_projection_clears_only_expired_callback_claim(self) -> None:
        """新 execution 不能继承已失效的旧 callback 租约身份。"""

        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            service = LLMTaskService(database)
            adapter = _adapter(service)
            command = _command(1, prefix="expired-claim")
            service.create_file_task(
                command.submissions[0].file_name,
                {"businessType": "file", "params": []},
                status="1",
            )
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    UPDATE llm_tasks
                    SET status = '3',
                        callback_status = 'success',
                        callback_claim_id = 'expired-legacy-claim',
                        callback_claim_expires_at = 0
                    WHERE business_type = 'file' AND business_key = ?
                    """,
                    (command.submissions[0].file_name,),
                )

            admission = adapter.create_batch_if_allowed(command)

            with sqlite3.connect(database) as connection:
                projection = connection.execute(
                    """
                    SELECT execution_id, status, callback_claim_id,
                           callback_claim_expires_at
                    FROM llm_tasks
                    WHERE business_type = 'file' AND business_key = ?
                    """,
                    (command.submissions[0].file_name,),
                ).fetchone()

        self.assertEqual(AnalysisBatchAdmissionOutcome.ACCEPTED, admission.outcome)
        self.assertIsNotNone(projection)
        assert projection is not None
        self.assertEqual(admission.executions[0].task_id.value, projection[0])
        self.assertEqual("1", projection[1])
        self.assertEqual("", projection[2])
        self.assertEqual(0, projection[3])

    def test_busy_is_a_typed_internal_outcome_without_partial_execution(self) -> None:
        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            adapter = _adapter(service)
            with patch.object(
                service,
                "create_analysis_batch_if_allowed",
                side_effect=TaskAdmissionBusyError("busy"),
            ):
                admission = adapter.create_batch_if_allowed(_command(1, prefix="busy"))
            with sqlite3.connect(service.db_path) as connection:
                execution_count = connection.execute(
                    "SELECT COUNT(*) FROM llm_task_executions"
                ).fetchone()[0]

        self.assertEqual(AnalysisBatchAdmissionOutcome.BUSY, admission.outcome)
        self.assertEqual(0, execution_count)


class AnalysisBatchConcurrencyTests(unittest.TestCase):
    """验证 SQLite 单实例控制面下的批量原子性，不把它描述为多实例能力。"""

    def test_fifty_concurrent_same_key_requests_have_one_acceptance_and_forty_nine_conflicts(self) -> None:
        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            adapter = _adapter(LLMTaskService(database))
            command = _command(1, prefix="same-key")
            start = threading.Barrier(50)

            def submit(_: int) -> AnalysisBatchAdmissionOutcome:
                start.wait()
                return adapter.create_batch_if_allowed(command).outcome

            with ThreadPoolExecutor(max_workers=50) as executor:
                outcomes = list(executor.map(submit, range(50)))
            with sqlite3.connect(database) as connection:
                execution_count = connection.execute(
                    "SELECT COUNT(*) FROM llm_task_executions"
                ).fetchone()[0]

        self.assertEqual(1, outcomes.count(AnalysisBatchAdmissionOutcome.ACCEPTED))
        self.assertEqual(
            49,
            outcomes.count(AnalysisBatchAdmissionOutcome.CONFLICT_ACTIVE),
        )
        self.assertEqual(1, execution_count)

    def test_fifty_distinct_keys_are_all_accepted_without_duplicate_execution_or_loss(self) -> None:
        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            adapter = _adapter(LLMTaskService(database))
            commands = [_command(1, prefix=f"distinct-{index}") for index in range(50)]
            start = threading.Barrier(50)

            def submit(command: AnalysisBatchCommand) -> AnalysisBatchAdmission:
                start.wait()
                return adapter.create_batch_if_allowed(command)

            with ThreadPoolExecutor(max_workers=50) as executor:
                admissions = list(executor.map(submit, commands))
            with sqlite3.connect(database) as connection:
                rows = connection.execute(
                    """
                    SELECT execution_id, business_key, batch_id, dispatch_sequence
                    FROM llm_task_executions
                    WHERE business_type = 'file'
                    ORDER BY dispatch_sequence
                    """
                ).fetchall()

        self.assertTrue(
            all(item.outcome is AnalysisBatchAdmissionOutcome.ACCEPTED for item in admissions)
        )
        self.assertEqual(50, len(rows))
        self.assertEqual(50, len({row[0] for row in rows}))
        self.assertEqual(50, len({row[1] for row in rows}))
        self.assertEqual(50, len({row[2] for row in rows}))
        self.assertEqual(tuple(range(1, 51)), tuple(row[3] for row in rows))

    def test_two_concurrent_batches_keep_each_batch_contiguous_in_global_dispatch_sequence(self) -> None:
        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            adapter = _adapter(LLMTaskService(database))
            commands = (_command(2, prefix="left"), _command(2, prefix="right"))
            start = threading.Barrier(2)

            def submit(command: AnalysisBatchCommand) -> AnalysisBatchAdmission:
                start.wait()
                return adapter.create_batch_if_allowed(command)

            with ThreadPoolExecutor(max_workers=2) as executor:
                admissions = list(executor.map(submit, commands))
            with sqlite3.connect(database) as connection:
                rows = connection.execute(
                    """
                    SELECT batch_id, batch_sequence, dispatch_sequence
                    FROM llm_task_executions
                    WHERE business_type = 'file'
                    ORDER BY dispatch_sequence
                    """
                ).fetchall()

        self.assertTrue(
            all(item.outcome is AnalysisBatchAdmissionOutcome.ACCEPTED for item in admissions)
        )
        self.assertEqual((1, 2, 1, 2), tuple(row[1] for row in rows))
        self.assertEqual((1, 2, 3, 4), tuple(row[2] for row in rows))
        self.assertNotEqual(rows[0][0], rows[2][0])
        self.assertEqual(rows[0][0], rows[1][0])
        self.assertEqual(rows[2][0], rows[3][0])


class SubmitAnalysisBatchTests(unittest.TestCase):
    """验证 Application 只调用一次原子 Port 和一次有界唤醒。"""

    def _accepted_admission(self, command: AnalysisBatchCommand) -> AnalysisBatchAdmission:
        executions = tuple(
            AnalysisExecutionRef(
                task_id=TaskId(f"submit-analysis-{index}"),
                file_name=submission.file_name,
                batch_id="a" * 32,
                batch_sequence=index,
            )
            for index, submission in enumerate(command.submissions, start=1)
        )
        return AnalysisBatchAdmission(AnalysisBatchAdmissionOutcome.ACCEPTED, executions)

    def test_acceptance_preserves_request_order_and_wakes_once_without_task_precheck(self) -> None:
        command = _command(2, prefix="submit")
        admission = self._accepted_admission(command)
        script = StrictAnalysisFakeScript()
        port = StrictAnalysisPortFake(script)
        script.expect_for(
            f"batch:{command.trace_id}",
            "batch.create",
            admission,
            argument=command,
        )
        script.expect("dispatcher.wake_up", None)

        result = SubmitAnalysisBatch(batch_commands=port, dispatcher=port).execute(command)

        self.assertEqual(admission, result)
        self.assertEqual(
            tuple(item.file_name for item in command.submissions),
            tuple(item.file_name for item in AnalysisBatchOrderCoordinator.from_admission(command, result).executions),
        )
        script.assert_exhausted()
        source = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "modules"
            / "analysis"
            / "application"
            / "submit_analysis.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("get_task", source)
        self.assertNotIn("get_latest", source)
        self.assertNotIn("threading.Thread", source)

    def test_wake_failure_does_not_revoke_committed_acceptance(self) -> None:
        command = _command(1, prefix="wake-failure")
        admission = self._accepted_admission(command)
        script = StrictAnalysisFakeScript()
        port = StrictAnalysisPortFake(script)
        script.expect_for(
            f"batch:{command.trace_id}",
            "batch.create",
            admission,
            argument=command,
        )
        script.expect("dispatcher.wake_up", RuntimeError("wake failed"))

        with self.assertLogs(
            "app.modules.analysis.application.submit_analysis",
            level="ERROR",
        ):
            result = SubmitAnalysisBatch(batch_commands=port, dispatcher=port).execute(command)

        self.assertEqual(AnalysisBatchAdmissionOutcome.ACCEPTED, result.outcome)
        script.assert_exhausted()

    def test_wake_failure_keeps_committed_batch_available_to_persistent_scan(self) -> None:
        """1F-4 尚未启动 Dispatcher；以持久扫描入口证明唤醒不是唯一投递事实。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            adapter = _adapter(service)
            command = _command(2, prefix="scan-after-wake-failure")
            with self.assertLogs(
                "app.modules.analysis.application.submit_analysis",
                level="ERROR",
            ):
                admission = SubmitAnalysisBatch(
                    batch_commands=adapter,
                    dispatcher=_WakeFailingDispatcher(),
                ).execute(command)
            available = adapter.list_accepted("file", limit=10)

        self.assertEqual(AnalysisBatchAdmissionOutcome.ACCEPTED, admission.outcome)
        self.assertEqual(
            tuple(item.task_id for item in admission.executions),
            available,
        )

    def test_conflict_does_not_wake_dispatcher_and_order_mismatch_fails_closed(self) -> None:
        command = _command(2, prefix="conflict")
        script = StrictAnalysisFakeScript()
        port = StrictAnalysisPortFake(script)
        conflict = AnalysisBatchAdmission(AnalysisBatchAdmissionOutcome.CONFLICT_ACTIVE)
        script.expect_for(
            f"batch:{command.trace_id}",
            "batch.create",
            conflict,
            argument=command,
        )

        result = SubmitAnalysisBatch(batch_commands=port, dispatcher=port).execute(command)

        self.assertEqual(AnalysisBatchAdmissionOutcome.CONFLICT_ACTIVE, result.outcome)
        script.assert_exhausted()
        accepted = self._accepted_admission(command)
        # ``AnalysisBatchAdmission`` 自身已禁止 sequence 倒序，因此这里保持合法的
        # sequence，只交换 fileName，验证协调器仍会拒绝“身份正确但请求顺序错位”的 Port。
        reversed_executions = tuple(
            AnalysisExecutionRef(
                task_id=item.task_id,
                file_name=accepted.executions[len(accepted.executions) - index].file_name,
                batch_id=item.batch_id,
                batch_sequence=item.batch_sequence,
            )
            for index, item in enumerate(accepted.executions, start=1)
        )
        with self.assertRaises(AnalysisBatchOrderContractError):
            AnalysisBatchOrderCoordinator.from_admission(
                command,
                AnalysisBatchAdmission(
                    AnalysisBatchAdmissionOutcome.ACCEPTED,
                    reversed_executions,
                ),
            )


if __name__ == "__main__":
    unittest.main()
