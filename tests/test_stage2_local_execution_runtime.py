"""阶段 2-3E 只读输入、配置、时钟、公平容量与本地执行器离线验收。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
from threading import Event, Lock, Thread
import time
import unittest

from app.modules.tasks.adapters import (
    CodecTaskExecutionSnapshotLoader,
    FairTaskExecutionPermitPool,
    LocalMaintenanceJob,
    LocalMaintenanceScheduler,
    LocalTaskExecutor,
    SystemSafeClock,
    TaskRuntimeConfig,
)
from app.modules.tasks.adapters.sqlite import (
    SQLiteConnectionFactory,
    SQLiteTransactionManager,
    bootstrap_task_control_database,
    build_sqlite_task_control_uow_factories,
)
from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.tasks.domain import TaskOwnerIdentity
from app.modules.tasks.application import ConservativeTaskReaper
from app.modules.tasks.ports import (
    ClockAnomalyError,
    TaskAdmissionRequest,
    TaskExecutionRuntimeOutcome,
    TaskExecutionRuntimeResult,
    TaskDispatchDeferralCommand,
    TaskClaimRequest,
    TaskExecutionMutationOutcome,
)
from tests.fakes import FakeClock


_T0 = "2026-08-13T00:00:00.000000Z"


class _ReportTupleCodec:
    task_type = "report"

    def decode_input(self, *, schema_version, payload):
        if schema_version != 1 or set(payload) != {"report_id"}:
            raise ValueError("冻结输入不符合 report tuple v1")
        return (payload["report_id"],)


class Stage2ReadOnlySnapshotLoaderTests(unittest.TestCase):
    def test_snapshot_loader_uses_read_only_uow_and_business_codec(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_path = root / "old.sqlite3"
            database_path = root / "task-control-v2.sqlite3"
            sqlite3.connect(old_path).close()
            bootstrap = bootstrap_task_control_database(old_path, database_path)
            manager = SQLiteTransactionManager(
                SQLiteConnectionFactory(bootstrap, busy_timeout_ms=100)
            )
            factories = build_sqlite_task_control_uow_factories(manager)
            task_id = TaskId("task-snapshot-loader")
            with factories.admission() as unit_of_work:
                unit_of_work.admission.admit_one(
                    TaskAdmissionRequest(
                        task_id=task_id,
                        task_type="report",
                        business_ref=TaskBusinessRef("report", "report-1"),
                        input_schema_version=1,
                        input_snapshot=("report-1",),
                        input_payload={"report_id": "report-1"},
                        public_request_payload={"reportId": "report-1"},
                        initial_public_status="waiting",
                        trace_id="trace-report-1",
                        accepted_at=_T0,
                    )
                )
                unit_of_work.commit()

            loader = CodecTaskExecutionSnapshotLoader(
                query_uow_factory=factories.queries,
                codec=_ReportTupleCodec(),
            )
            loaded = loader.load(task_id)

            self.assertEqual(("report-1",), loaded.snapshot.input_snapshot)
            self.assertEqual("trace-report-1", loaded.snapshot.trace_id)
            self.assertEqual(64, len(loaded.input_payload_fingerprint))
            with factories.queries() as query_uow:
                self.assertFalse(hasattr(query_uow, "commit"))
                self.assertEqual((task_id,), query_uow.queries.scan_runnable("report", not_after=_T0, limit=1))

    def test_conservative_reaper_only_persists_defer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_path = root / "old.sqlite3"
            database_path = root / "task-control-v2.sqlite3"
            sqlite3.connect(old_path).close()
            bootstrap = bootstrap_task_control_database(old_path, database_path)
            manager = SQLiteTransactionManager(SQLiteConnectionFactory(bootstrap, busy_timeout_ms=100))
            factories = build_sqlite_task_control_uow_factories(manager)
            task_id = TaskId("task-reaper-defer")
            request = TaskAdmissionRequest(
                task_id=task_id,
                task_type="report",
                business_ref=TaskBusinessRef("report", "report-reaper"),
                input_schema_version=1,
                input_snapshot=("report-reaper",),
                input_payload={"report_id": "report-reaper"},
                public_request_payload={"reportId": "report-reaper"},
                initial_public_status="waiting",
                trace_id="trace-reaper",
                accepted_at=_T0,
            )
            with factories.admission() as unit_of_work:
                unit_of_work.admission.admit_one(request)
                unit_of_work.commit()
            owner = TaskOwnerIdentity(
                instance_start_id="12345678-1234-4234-8234-123456789abc",
                process_id=1,
                executor_name="report",
                worker_slot="worker-0",
            )
            with factories.execution() as unit_of_work:
                claim = unit_of_work.execution.claim(
                    TaskClaimRequest(
                        task_id=task_id,
                        task_type="report",
                        owner=owner,
                        lease_token="test-reaper-token",
                        claimed_at=_T0,
                        lease_expires_at="2026-08-13T00:00:30.000000Z",
                    )
                )
                self.assertIs(TaskExecutionMutationOutcome.APPLIED, claim.outcome)
                unit_of_work.commit()
            with factories.execution() as unit_of_work:
                outcome = unit_of_work.execution.start(claim.attempt.authority, started_at=_T0)
                self.assertIs(TaskExecutionMutationOutcome.APPLIED, outcome)
                unit_of_work.commit()

            clock = FakeClock("2026-08-13T00:00:31.000000Z")
            reaper = ConservativeTaskReaper(
                clock=clock,
                query_uow_factory=factories.queries,
                recovery_uow_factory=factories.recovery,
                defer_seconds=5,
            )
            self.assertEqual(1, reaper.run_once())
            connection = sqlite3.connect(database_path)
            row = connection.execute(
                "SELECT execution_state, next_recovery_at FROM llm_task_executions WHERE execution_id = ?",
                (task_id.value,),
            ).fetchone()
            connection.close()
            self.assertEqual(("running", "2026-08-13T00:00:36.000000Z"), row)

    def test_dispatch_failure_cooldown_is_persisted_not_kept_in_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_path = root / "old.sqlite3"
            database_path = root / "task-control-v2.sqlite3"
            sqlite3.connect(old_path).close()
            bootstrap = bootstrap_task_control_database(old_path, database_path)
            factories = build_sqlite_task_control_uow_factories(
                SQLiteTransactionManager(SQLiteConnectionFactory(bootstrap, busy_timeout_ms=100))
            )
            task_id = TaskId("task-dispatch-cooldown")
            with factories.admission() as unit_of_work:
                unit_of_work.admission.admit_one(
                    TaskAdmissionRequest(
                        task_id=task_id,
                        task_type="report",
                        business_ref=TaskBusinessRef("report", "report-cooldown"),
                        input_schema_version=1,
                        input_snapshot=("report-cooldown",),
                        input_payload={"report_id": "report-cooldown"},
                        public_request_payload={"reportId": "report-cooldown"},
                        initial_public_status="waiting",
                        trace_id="trace-cooldown",
                        accepted_at=_T0,
                    )
                )
                unit_of_work.commit()
            with factories.execution() as unit_of_work:
                outcome = unit_of_work.execution.defer_dispatch(
                    TaskDispatchDeferralCommand(
                        task_id=task_id,
                        task_type="report",
                        reason_code="runtime_input_error",
                        deferred_at=_T0,
                        next_dispatch_at="2026-08-13T00:00:30.000000Z",
                    )
                )
                self.assertIs(TaskExecutionMutationOutcome.APPLIED, outcome)
                unit_of_work.commit()
            with factories.queries() as query_uow:
                self.assertEqual(
                    (),
                    query_uow.queries.scan_runnable("report", not_after=_T0, limit=10),
                )
            connection = sqlite3.connect(database_path)
            persisted = connection.execute(
                "SELECT dispatch_failure_count, next_dispatch_at, "
                "last_dispatch_error FROM llm_task_executions "
                "WHERE execution_id = ?",
                (task_id.value,),
            ).fetchone()
            connection.close()
            self.assertEqual(
                (1, "2026-08-13T00:00:30.000000Z", "runtime_input_error"),
                persisted,
            )


class Stage2SafeClockAndConfigTests(unittest.TestCase):
    def test_safe_clock_sticks_in_degraded_state_after_wall_clock_jump(self) -> None:
        wall = [datetime(2026, 8, 13, tzinfo=timezone.utc)]
        mono = [100.0]
        clock = SystemSafeClock(
            max_jitter_seconds=1,
            wall_clock=lambda: wall[0],
            monotonic_clock=lambda: mono[0],
        )
        mono[0] = 101.0
        wall[0] = datetime(2026, 8, 13, 0, 0, 1, tzinfo=timezone.utc)
        self.assertEqual("2026-08-13T00:00:01.000000Z", clock.now_utc())
        mono[0] = 102.0
        wall[0] = datetime(2026, 8, 13, 0, 0, 9, tzinfo=timezone.utc)
        with self.assertRaises(ClockAnomalyError):
            clock.now_utc()
        self.assertFalse(clock.is_safe())
        with self.assertRaises(ClockAnomalyError):
            clock.now_utc()

    def test_runtime_config_validates_all_inequalities_before_threads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = TaskRuntimeConfig.from_mapping(
                {},
                runtime_directory=root,
                legacy_task_database_path=root / "old.sqlite3",
            )
            self.assertEqual(1, config.heavy_concurrency)
            self.assertEqual(1, config.file_worker_count)
            with self.assertRaisesRegex(ValueError, "heavy concurrency"):
                TaskRuntimeConfig.from_mapping(
                    {"DOCSENSE_TASK_HEAVY_CONCURRENCY": "2"},
                    runtime_directory=root,
                    legacy_task_database_path=root / "old.sqlite3",
                )


class Stage2FairCapacityTests(unittest.TestCase):
    def test_business_permits_are_distinct_but_share_one_capacity_fact(self) -> None:
        """组合根按业务注入不同 Permit，同时必须能证明它们属于同一 Pool。"""

        pool = FairTaskExecutionPermitPool(capacity=1)
        report = pool.for_business("report")
        weaponry = pool.for_business("weaponry")
        analysis = pool.for_business("file")

        self.assertIsNot(report, weaponry)
        self.assertIsNot(weaponry, analysis)
        self.assertTrue(pool.owns(report))
        self.assertTrue(pool.owns(weaponry))
        self.assertTrue(pool.owns(analysis))
        self.assertFalse(FairTaskExecutionPermitPool().owns(report))
        self.assertEqual(1, analysis.max_concurrency)

    def test_waiting_businesses_receive_round_robin_grants(self) -> None:
        pool = FairTaskExecutionPermitPool()
        report = pool.for_business("report")
        weaponry = pool.for_business("weaponry")
        self.assertTrue(report.acquire_interruptibly(lambda: False, poll_interval_seconds=0.01))
        order: list[str] = []

        def wait_and_release(name, permit):
            if permit.acquire_interruptibly(lambda: False, poll_interval_seconds=0.01):
                order.append(name)
                permit.release()

        report_thread = Thread(target=wait_and_release, args=("report", report))
        weaponry_thread = Thread(target=wait_and_release, args=("weaponry", weaponry))
        report_thread.start()
        weaponry_thread.start()
        deadline = time.monotonic() + 2
        while sum(pool.waiting_counts.values()) < 2 and time.monotonic() < deadline:
            Event().wait(0.01)
        report.release()
        report_thread.join(timeout=2)
        weaponry_thread.join(timeout=2)
        self.assertEqual(["weaponry", "report"], order)


class _QueryPort:
    def __init__(self, task_ids: list[TaskId]) -> None:
        self._task_ids = task_ids
        self.lock = Lock()

    def scan_runnable(self, _task_type, *, not_after, limit):
        with self.lock:
            selected = tuple(self._task_ids[:limit])
            del self._task_ids[:limit]
            return selected


class _QueryUow:
    def __init__(self, queries) -> None:
        self.queries = queries

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Runtime:
    def __init__(self, completed: list[TaskId], lock: Lock, done: Event, expected: int) -> None:
        self._completed = completed
        self._lock = lock
        self._done = done
        self._expected = expected

    def run(self, task_id):
        with self._lock:
            self._completed.append(task_id)
            if len(self._completed) == self._expected:
                self._done.set()
        return TaskExecutionRuntimeResult(task_id, TaskExecutionRuntimeOutcome.WORKFLOW_RETURNED)

    def request_cancellation(self):
        return True


class _ExecutionPort:
    """只记录派发冷却，避免 Executor 单测依赖真实 SQLite。"""

    def __init__(self) -> None:
        self.deferred: list[TaskDispatchDeferralCommand] = []

    def defer_dispatch(self, command):
        self.deferred.append(command)
        return TaskExecutionMutationOutcome.APPLIED


class _ExecutionUow:
    def __init__(self, execution) -> None:
        self.execution = execution
        self.committed = False

    def __enter__(self):
        return self

    def commit(self):
        self.committed = True

    def __exit__(self, *_args):
        return False


class _CancellationRecordingRuntime:
    def __init__(self) -> None:
        self.cancel_count = 0
        self.run_count = 0

    def request_cancellation(self):
        self.cancel_count += 1
        return self.cancel_count == 1

    def run(self, task_id):
        self.run_count += 1
        return TaskExecutionRuntimeResult(
            task_id,
            TaskExecutionRuntimeOutcome.WORKFLOW_RETURNED,
        )


class Stage2LocalExecutorAndMaintenanceTests(unittest.TestCase):
    def test_executor_periodic_scan_drains_backlog_with_bounded_inflight(self) -> None:
        task_ids = [TaskId(f"task-{index}") for index in range(50)]
        queries = _QueryPort(task_ids.copy())
        completed: list[TaskId] = []
        lock = Lock()
        done = Event()
        pool = FairTaskExecutionPermitPool()
        executor = LocalTaskExecutor(
            task_type="report",
            worker_count=3,
            scan_interval_seconds=0.01,
            stop_grace_seconds=1,
            clock=FakeClock(_T0),
            query_uow_factory=lambda: _QueryUow(queries),
            execution_uow_factory=lambda: None,
            permit=pool.for_business("report"),
            runtime_factory=lambda _slot: _Runtime(completed, lock, done, 50),
            thread_name_prefix="test-report-executor",
        )
        executor.start()
        self.assertTrue(done.wait(timeout=5))
        self.assertLessEqual(executor.inflight_count, 3)
        executor.stop()
        self.assertTrue(executor.is_healthy())
        self.assertEqual(50, len(completed))

    def test_unclassified_worker_error_stops_executor_and_persists_cooldown(self) -> None:
        """Runtime 构造持续失败时不得热循环，也不能继续报告 healthy。"""

        task_id = TaskId("task-worker-contract-error")
        queries = _QueryPort([task_id])
        execution = _ExecutionPort()
        uows: list[_ExecutionUow] = []
        factory_calls = []
        failed = Event()

        def execution_uow_factory():
            unit_of_work = _ExecutionUow(execution)
            uows.append(unit_of_work)
            return unit_of_work

        def broken_runtime_factory(_slot):
            factory_calls.append(1)
            failed.set()
            raise RuntimeError("simulated runtime factory failure")

        executor = LocalTaskExecutor(
            task_type="report",
            worker_count=1,
            scan_interval_seconds=0.01,
            stop_grace_seconds=1,
            clock=FakeClock(_T0),
            query_uow_factory=lambda: _QueryUow(queries),
            execution_uow_factory=execution_uow_factory,
            permit=FairTaskExecutionPermitPool().for_business("report"),
            runtime_factory=broken_runtime_factory,
            thread_name_prefix="test-worker-contract-error",
        )

        executor.start()
        self.assertTrue(failed.wait(timeout=2))
        self.assertTrue(executor.stop(timeout_seconds=1))

        self.assertEqual(1, len(factory_calls))
        self.assertFalse(executor.is_healthy())
        self.assertEqual(1, len(execution.deferred))
        self.assertTrue(uows[0].committed)

    def test_stop_race_cancels_constructed_runtime_without_running_it(self) -> None:
        """permit 后、claim 前收到 stop 时，不得建立新的 Task Attempt。"""

        task_id = TaskId("task-stop-before-runtime-run")
        queries = _QueryPort([task_id])
        factory_entered = Event()
        allow_factory_return = Event()
        runtime = _CancellationRecordingRuntime()

        def blocking_runtime_factory(_slot):
            factory_entered.set()
            self.assertTrue(allow_factory_return.wait(timeout=2))
            return runtime

        executor = LocalTaskExecutor(
            task_type="report",
            worker_count=1,
            scan_interval_seconds=0.01,
            stop_grace_seconds=1,
            clock=FakeClock(_T0),
            query_uow_factory=lambda: _QueryUow(queries),
            execution_uow_factory=lambda: None,
            permit=FairTaskExecutionPermitPool().for_business("report"),
            runtime_factory=blocking_runtime_factory,
            thread_name_prefix="test-stop-before-runtime-run",
        )
        stopped: list[bool] = []
        executor.start()
        self.assertTrue(factory_entered.wait(timeout=2))
        stop_thread = Thread(
            target=lambda: stopped.append(executor.stop(timeout_seconds=1))
        )
        stop_thread.start()
        self.assertTrue(executor._stopping.wait(timeout=2))
        allow_factory_return.set()
        stop_thread.join(timeout=2)

        self.assertFalse(stop_thread.is_alive())
        self.assertEqual([True], stopped)
        self.assertEqual(1, runtime.cancel_count)
        self.assertEqual(0, runtime.run_count)

    def test_maintenance_startup_scan_and_wakeup_are_independent(self) -> None:
        called = Event()
        count = [0]

        def action():
            count[0] += 1
            called.set()

        scheduler = LocalMaintenanceScheduler(
            jobs=(LocalMaintenanceJob("task_reaper", 60, action),),
            stop_grace_seconds=1,
        )
        scheduler.start()
        self.assertTrue(called.wait(timeout=2))
        called.clear()
        scheduler.wake_up()
        self.assertTrue(called.wait(timeout=2))
        scheduler.stop()
        self.assertTrue(scheduler.is_healthy())
        self.assertGreaterEqual(count[0], 2)


if __name__ == "__main__":
    unittest.main()
