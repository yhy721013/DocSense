"""阶段 2-4 第 7 步 Report 独立维护运行时的离线验收。"""

from __future__ import annotations

from pathlib import Path
import sqlite3
from threading import Event
import time
import unittest

from app.modules.report.adapters import (
    ReportRuntimeConfig,
    ReportV2Maintenance,
    ReportV2TaskDispatcher,
)
from app.modules.report.ports import (
    ReportResourceCleanupResult,
    ReportResourceSweepResult,
)
from app.modules.tasks.adapters import FairTaskExecutionPermitPool, LocalTaskExecutor
from app.modules.tasks.adapters.sqlite import (
    SQLiteConnectionFactory,
    SQLiteTransactionManager,
    bootstrap_task_control_database,
    build_sqlite_task_control_uow_factories,
)
from tests import workspace_tempdir
from tests.fakes import FakeClock, FakeReportCallbackPort, InvocationRecorder


class _FakeReportResources:
    """只模拟高层恢复 Port；不会执行文件、HTTP 或外部资源操作。"""

    def __init__(self) -> None:
        self.sweep_calls: list[int] = []
        self.sweep_error: Exception | None = None
        self.sweep_entered: Event | None = None
        self.sweep_release: Event | None = None

    # 下列方法是 ReportResourceRecoveryPort 的完整形状。维护用例只会调用 sweep，
    # 其余方法保留为显式断言入口，防止调度器越权进入业务登记或单任务清理路径。
    def register(self, task_id, business_ref, scope) -> None:
        raise AssertionError("维护调度器不得登记业务资源")

    def track_rag_cleanup(self, task_id, cleanup_ref) -> None:
        raise AssertionError("维护调度器不得写入 RAG 资源事实")

    def track_audit(self, receipt) -> None:
        raise AssertionError("维护调度器不得写入审计资源事实")

    def track_final_artifact(self, artifact) -> None:
        raise AssertionError("维护调度器不得写入最终产物事实")

    def cleanup(self, task_id) -> ReportResourceCleanupResult:
        raise AssertionError("维护调度器必须通过有界 sweep，不得按提示直接清理")

    def recover(self, task_id) -> ReportResourceCleanupResult:
        raise AssertionError("维护调度器必须通过有界 sweep，不得按提示直接恢复")

    def sweep(self, *, limit: int) -> ReportResourceSweepResult:
        self.sweep_calls.append(limit)
        if self.sweep_entered is not None:
            self.sweep_entered.set()
        if self.sweep_release is not None:
            self.sweep_release.wait(timeout=1.0)
        if self.sweep_error is not None:
            raise self.sweep_error
        return ReportResourceSweepResult(requested_limit=limit, scanned_count=0)

    def quarantine(self, task_id, *, stage: str, reason: str) -> None:
        raise AssertionError("维护调度器不得绕过恢复状态机直接隔离")


class ReportV2MaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.recorder = InvocationRecorder()
        self.callbacks = FakeReportCallbackPort(self.recorder)
        self.resources = _FakeReportResources()
        self.config = ReportRuntimeConfig(
            resource_sweep_interval_seconds=0.02,
            resource_sweep_limit=3,
            stop_timeout_seconds=1.0,
        )

    def _maintenance(self) -> ReportV2Maintenance:
        return ReportV2Maintenance(
            callbacks=self.callbacks,
            resources=self.resources,
            config=self.config,
        )

    def _wait_until(self, predicate, *, timeout_seconds: float = 1.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        self.fail("等待维护线程条件超时")

    def test_startup_periodic_and_wakeup_scan_both_state_machines(self) -> None:
        """启动即扫描；Event 只合并唤醒，两个 Job 都重新读取持久状态。"""

        maintenance = self._maintenance()
        try:
            maintenance.start()
            self._wait_until(
                lambda: len(self.callbacks.guard_sweep_calls) >= 1
                and len(self.resources.sweep_calls) >= 1
            )
            first_callback_count = len(self.callbacks.guard_sweep_calls)
            first_resource_count = len(self.resources.sweep_calls)

            maintenance.wake_up()
            self._wait_until(
                lambda: len(self.callbacks.guard_sweep_calls) > first_callback_count
                and len(self.resources.sweep_calls) > first_resource_count
            )

            snapshot = maintenance.snapshot()
            self.assertEqual(1, snapshot.thread_count)
            self.assertGreaterEqual(snapshot.callback_guard_sweep_count, 2)
            self.assertGreaterEqual(snapshot.resource_sweep_count, 2)
            self.assertEqual({3}, set(self.callbacks.guard_sweep_calls))
            self.assertEqual({3}, set(self.resources.sweep_calls))
            self.assertTrue(snapshot.healthy)
        finally:
            self.assertTrue(maintenance.stop(timeout_seconds=1.0))
        self.assertEqual(0, maintenance.snapshot().thread_count)

    def test_callback_failure_does_not_skip_resource_or_poison_scheduler(self) -> None:
        """单个状态机的暂态故障独立计数，不能关闭另一状态机或后续周期。"""

        self.callbacks.guard_sweep_error = OSError("simulated callback store busy")
        maintenance = self._maintenance()
        try:
            maintenance.start()
            self._wait_until(
                lambda: len(self.callbacks.guard_sweep_calls) >= 2
                and len(self.resources.sweep_calls) >= 2
            )
            snapshot = maintenance.snapshot()
            self.assertGreaterEqual(snapshot.callback_guard_sweep_failure_count, 2)
            self.assertGreaterEqual(snapshot.resource_sweep_count, 2)
            self.assertEqual(0, snapshot.resource_sweep_failure_count)
            self.assertTrue(snapshot.healthy)
        finally:
            self.assertTrue(maintenance.stop(timeout_seconds=1.0))

    def test_resource_failure_does_not_skip_callback_or_poison_scheduler(self) -> None:
        self.resources.sweep_error = OSError("simulated resource store busy")
        maintenance = self._maintenance()
        try:
            maintenance.start()
            self._wait_until(
                lambda: len(self.callbacks.guard_sweep_calls) >= 2
                and len(self.resources.sweep_calls) >= 2
            )
            snapshot = maintenance.snapshot()
            self.assertGreaterEqual(snapshot.callback_guard_sweep_count, 2)
            self.assertEqual(0, snapshot.callback_guard_sweep_failure_count)
            self.assertGreaterEqual(snapshot.resource_sweep_failure_count, 2)
            self.assertTrue(snapshot.healthy)
        finally:
            self.assertTrue(maintenance.stop(timeout_seconds=1.0))

    def test_invalid_timeout_does_not_break_later_valid_stop(self) -> None:
        maintenance = self._maintenance()
        maintenance.start()
        with self.assertRaises(ValueError):
            maintenance.stop(timeout_seconds=-1)
        self.assertTrue(maintenance.stop(timeout_seconds=1.0))

    def test_stop_timeout_keeps_live_thread_visible_and_allows_retry(self) -> None:
        """超时不得伪装成 stopped；释放阻塞 Fake 后可再次有限等待收敛。"""

        entered = Event()
        release = Event()
        self.resources.sweep_entered = entered
        self.resources.sweep_release = release
        maintenance = self._maintenance()
        maintenance.start()
        self.assertTrue(entered.wait(timeout=1.0))
        self.assertFalse(maintenance.stop(timeout_seconds=0))
        self.assertEqual(1, maintenance.snapshot().thread_count)
        release.set()
        self.assertTrue(maintenance.stop(timeout_seconds=1.0))
        self.assertEqual(0, maintenance.snapshot().thread_count)


class ReportV2DispatcherMaintenanceLifecycleTests(unittest.TestCase):
    def test_dispatcher_owns_one_executor_and_one_maintenance_thread(self) -> None:
        """临时 SQLite 空队列证明生产形状的启动、readiness 与有限停机。"""

        with workspace_tempdir() as directory:
            root = Path(directory)
            old_path = root / "old.sqlite3"
            sqlite3.connect(old_path).close()
            bootstrap = bootstrap_task_control_database(
                old_path,
                root / "task-control-v2.sqlite3",
            )
            manager = SQLiteTransactionManager(
                SQLiteConnectionFactory(bootstrap, busy_timeout_ms=100)
            )
            factories = build_sqlite_task_control_uow_factories(manager)
            executor = LocalTaskExecutor(
                task_type="report",
                worker_count=1,
                scan_interval_seconds=0.02,
                stop_grace_seconds=1.0,
                clock=FakeClock("2026-08-13T00:00:00.000000Z"),
                query_uow_factory=factories.queries,
                execution_uow_factory=factories.execution,
                permit=FairTaskExecutionPermitPool().for_business("report"),
                # 空队列不会创建 Runtime；若意外调用，测试应立即失败。
                runtime_factory=lambda _task_id: (_ for _ in ()).throw(
                    AssertionError("空队列不得构造 Runtime")
                ),
                thread_name_prefix="test-report-v2",
            )
            callbacks = FakeReportCallbackPort(InvocationRecorder())
            resources = _FakeReportResources()
            maintenance = ReportV2Maintenance(
                callbacks=callbacks,
                resources=resources,
                config=ReportRuntimeConfig(
                    scan_interval_seconds=0.02,
                    resource_sweep_interval_seconds=0.02,
                    stop_timeout_seconds=1.0,
                ),
            )
            dispatcher = ReportV2TaskDispatcher(
                executor=executor,
                maintenance=maintenance,
            )
            try:
                dispatcher.start()
                deadline = time.monotonic() + 1.0
                while not dispatcher.snapshot().ready and time.monotonic() < deadline:
                    time.sleep(0.005)
                snapshot = dispatcher.snapshot()
                self.assertTrue(snapshot.ready)
                self.assertEqual(1, snapshot.worker_thread_count)
                self.assertEqual(1, snapshot.maintenance_thread_count)
                self.assertIs(callbacks, dispatcher.callbacks)
                self.assertIs(resources, dispatcher.resources)

                with self.assertRaises(ValueError):
                    dispatcher.stop(timeout_seconds=0)
                self.assertTrue(dispatcher.stop(timeout_seconds=1.0))
            finally:
                dispatcher.close()
            stopped = dispatcher.snapshot()
            self.assertEqual("closed", stopped.lifecycle_state)
            self.assertEqual(0, stopped.worker_thread_count)
            self.assertEqual(0, stopped.maintenance_thread_count)


if __name__ == "__main__":
    unittest.main()
