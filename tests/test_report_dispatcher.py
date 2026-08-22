"""阶段 1C-6 本地报告 Dispatcher 的持久积压、容量与生命周期验收。"""

from __future__ import annotations

from itertools import count
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import unittest

from app.modules.report.adapters import (
    FileProcessSingletonGuard,
    LocalReportTaskDispatcher,
    ReportTaskCommandCodec,
)
from app.modules.report.domain import ReportId, ReportSubmission
from app.modules.report.ports import (
    ReportCallbackGuardSweepResult,
    ReportResourceCleanupOutcome,
    ReportResourceCleanupResult,
    ReportResourceSweepResult,
)
from app.modules.tasks.adapters import LegacyTaskCommandAdapter, UploadTaskLimiter
from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.tasks.ports import (
    TaskClaimOutcome,
    TaskSubmissionCommand,
    TaskSubmissionOutcome,
)
from app.services.core.config import ReportInfrastructureConfig
from app.services.llm_service.task_service import LLMTaskService
from tests import workspace_tempdir
from tests.fakes import FakeReportCallbackPort, InvocationRecorder


class _ResourceRecoveryStub:
    """只为 Dispatcher 验证提供的完整资源恢复 Port 替身。"""

    def __init__(self, *, fail_first_sweep: bool = False) -> None:
        self.fail_first_sweep = fail_first_sweep
        self.sweep_calls: list[int] = []

    def register(self, task_id, business_ref, scope) -> None:
        return None

    def track_rag_cleanup(self, task_id, cleanup_ref) -> None:
        return None

    def track_audit(self, receipt) -> None:
        return None

    def track_final_artifact(self, artifact) -> None:
        return None

    def cleanup(self, task_id) -> ReportResourceCleanupResult:
        return ReportResourceCleanupResult(ReportResourceCleanupOutcome.CLEANED)

    def recover(self, task_id) -> ReportResourceCleanupResult:
        return ReportResourceCleanupResult(ReportResourceCleanupOutcome.CLEANED)

    def sweep(self, *, limit: int) -> ReportResourceSweepResult:
        self.sweep_calls.append(limit)
        if self.fail_first_sweep and len(self.sweep_calls) == 1:
            raise RuntimeError("forced resource sweep failure")
        return ReportResourceSweepResult(
            requested_limit=limit,
            scanned_count=0,
        )

    def quarantine(self, task_id, *, stage: str, reason: str) -> None:
        return None


class _QueueInspectorStub:
    """注入只读诊断故障，验证异常不会杀死任务扫描 Worker。"""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.call_count = 0

    def inspect_queue(self, task_type: str, *, running_sample_limit: int):
        self.call_count += 1
        raise self.error


class _BrokenReleaseLimiter:
    """只在归还许可时失败，用于验证 Dispatcher 的 fail-closed 就绪状态。"""

    def acquire_interruptibly(
        self,
        cancel_requested,
        *,
        poll_interval_seconds: float,
    ) -> bool:
        return not cancel_requested()

    def release(self) -> None:
        raise RuntimeError("forced limiter release failure")


def _config(**overrides: object) -> ReportInfrastructureConfig:
    values: dict[str, object] = {
        "scan_interval_seconds": 0.02,
        "accepted_batch_size": 7,
        "resource_sweep_interval_seconds": 0.04,
        "resource_sweep_limit": 5,
        "running_sample_limit": 5,
        "stop_timeout_seconds": 0.5,
        "cleanup_http_timeout_seconds": 1.0,
        "cleanup_lease_seconds": 7.0,
    }
    values.update(overrides)
    return ReportInfrastructureConfig(**values)  # type: ignore[arg-type]


def _wait_until(predicate, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class LocalReportTaskDispatcherTests(unittest.TestCase):
    @staticmethod
    def _callbacks() -> FakeReportCallbackPort:
        return FakeReportCallbackPort(InvocationRecorder())

    def _commands(self, root: Path):
        service = LLMTaskService(str(root / "tasks.sqlite3"))
        task_numbers = count(1)
        clock_numbers = count(0)
        commands = LegacyTaskCommandAdapter(
            service,
            ReportTaskCommandCodec(),
            task_id_factory=lambda: TaskId(
                f"report-dispatch-{next(task_numbers):04d}"
            ),
            clock=lambda: (
                "2026-07-17T00:00:"
                f"{next(clock_numbers):02d}+00:00"
            ),
        )
        return service, commands

    @staticmethod
    def _accept(commands, report_id: int) -> TaskId:
        submission = ReportSubmission(
            report_id=ReportId.from_public_value(report_id),
            source_urls=(f"http://files.local/{report_id}.pdf",),
            template_outline_url="http://files.local/template.docx",
            template_desc="",
            requirement="",
            trace_id=f"trace-dispatch-{report_id}",
        )
        result = commands.create_if_allowed(
            TaskSubmissionCommand(
                task_type="report",
                business_ref=TaskBusinessRef("report", str(report_id)),
                input_schema_version=1,
                submission=submission,
                trace_id=submission.trace_id,
            )
        )
        if result.outcome is not TaskSubmissionOutcome.ACCEPTED:
            raise AssertionError(f"测试任务受理失败: {result.outcome}")
        assert result.execution is not None
        return result.execution.task_id

    def test_fifty_persisted_tasks_use_one_worker_zero_buffer_and_fifo_scans(self) -> None:
        with workspace_tempdir() as tmp:
            service, commands = self._commands(Path(tmp))
            expected_ids = tuple(
                self._accept(commands, 1000 + index) for index in range(50)
            )
            resources = _ResourceRecoveryStub()
            executed: list[TaskId] = []
            lock = threading.Lock()
            all_executed = threading.Event()

            def execute(task_id: TaskId) -> None:
                claim = commands.claim(task_id)
                if claim.outcome is not TaskClaimOutcome.CLAIMED:
                    raise AssertionError(f"未取得执行权: {claim.outcome}")
                with lock:
                    executed.append(task_id)
                    if len(executed) == 50:
                        all_executed.set()

            dispatcher = LocalReportTaskDispatcher(
                task_commands=commands,
                queue_inspector=commands,
                resources=resources,
                callbacks=self._callbacks(),
                execute=execute,
                config=_config(),
            )
            for task_id in expected_ids:
                dispatcher.dispatch(task_id)

            before_start = dispatcher.snapshot()
            self.assertEqual(0, before_start.worker_thread_count)
            self.assertEqual(0, before_start.buffered_task_count)
            self.assertEqual(50, before_start.dispatch_count)
            self.assertEqual(49, before_start.merged_wakeup_count)

            try:
                dispatcher.start()
                dispatcher.start()  # 幂等，不得创建第二条 Worker。
                self.assertTrue(all_executed.wait(timeout=10))
                snapshot = dispatcher.snapshot()
                self.assertEqual(1, snapshot.worker_thread_count)
                self.assertEqual(0, snapshot.buffered_task_count)
                self.assertEqual(50, snapshot.execution_count)
                self.assertEqual(0, snapshot.execution_failure_count)
                self.assertGreaterEqual(snapshot.scan_count, 8)
                self.assertEqual(expected_ids, tuple(executed))
                self.assertEqual(
                    (),
                    service.list_accepted_task_execution_ids(
                        "report",
                        limit=100,
                    ),
                )
                self.assertTrue(
                    all(
                        service.get_task_execution(task_id.value)[
                            "execution_state"
                        ]
                        == "running"
                        for task_id in expected_ids
                    )
                )
            finally:
                self.assertTrue(dispatcher.stop(timeout_seconds=1.0))
                dispatcher.close()

    def test_periodic_scan_recovers_accepted_task_without_dispatch_wakeup(self) -> None:
        with workspace_tempdir() as tmp:
            _service, commands = self._commands(Path(tmp))
            processed = threading.Event()

            def execute(task_id: TaskId) -> None:
                commands.claim(task_id)
                processed.set()

            dispatcher = LocalReportTaskDispatcher(
                task_commands=commands,
                queue_inspector=commands,
                resources=_ResourceRecoveryStub(),
                callbacks=self._callbacks(),
                execute=execute,
                config=_config(),
            )
            try:
                dispatcher.start()
                # 启动后直接写 accepted，故意不调用 dispatch，模拟提交后唤醒丢失或重启。
                task_id = self._accept(commands, 2001)
                self.assertTrue(processed.wait(timeout=5))
                self.assertEqual("running", commands.get_execution(task_id).execution_state)
            finally:
                dispatcher.close()

    def test_fifo_uses_transaction_sequence_when_application_clock_moves_backward(self) -> None:
        """受理时钟回拨不得把后提交任务排到先提交任务前面。"""

        with workspace_tempdir() as tmp:
            service = LLMTaskService(str(Path(tmp) / "tasks.sqlite3"))
            task_ids = iter((TaskId("sequence-first"), TaskId("sequence-second")))
            accepted_times = iter(
                (
                    "2026-07-17T01:00:00+00:00",
                    "2026-07-16T01:00:00+00:00",
                )
            )
            commands = LegacyTaskCommandAdapter(
                service,
                ReportTaskCommandCodec(),
                task_id_factory=lambda: next(task_ids),
                clock=lambda: next(accepted_times),
            )
            first = self._accept(commands, 2101)
            second = self._accept(commands, 2102)

            self.assertEqual(
                (first, second),
                commands.list_accepted("report", limit=10),
            )

    def test_startup_and_periodic_resource_sweep_failure_do_not_kill_worker(self) -> None:
        with workspace_tempdir() as tmp:
            _service, commands = self._commands(Path(tmp))
            resources = _ResourceRecoveryStub(fail_first_sweep=True)
            dispatcher = LocalReportTaskDispatcher(
                task_commands=commands,
                queue_inspector=commands,
                resources=resources,
                callbacks=self._callbacks(),
                execute=lambda task_id: commands.claim(task_id),
                config=_config(resource_sweep_interval_seconds=0.02),
            )
            try:
                with self.assertLogs(
                    "app.modules.report.adapters.local_dispatcher",
                    level="ERROR",
                ):
                    dispatcher.start()
                    self.assertTrue(
                        _wait_until(lambda: len(resources.sweep_calls) >= 2)
                    )
                snapshot = dispatcher.snapshot()
                self.assertEqual(1, snapshot.resource_sweep_failure_count)
                self.assertGreaterEqual(snapshot.resource_sweep_count, 1)
                self.assertEqual(1, snapshot.worker_thread_count)
            finally:
                dispatcher.close()

    def test_callback_guard_sweep_is_periodic_bounded_and_observable(self) -> None:
        with workspace_tempdir() as tmp:
            _service, commands = self._commands(Path(tmp))
            callbacks = self._callbacks()
            callbacks.guard_sweep_result = ReportCallbackGuardSweepResult(1, 1)
            dispatcher = LocalReportTaskDispatcher(
                task_commands=commands,
                queue_inspector=commands,
                resources=_ResourceRecoveryStub(),
                callbacks=callbacks,
                execute=lambda task_id: commands.claim(task_id),
                config=_config(
                    resource_sweep_interval_seconds=0.02,
                    resource_sweep_limit=3,
                ),
            )
            try:
                dispatcher.start()
                self.assertTrue(
                    _wait_until(
                        lambda: dispatcher.snapshot().callback_guard_sweep_count >= 2
                    )
                )
                snapshot = dispatcher.snapshot()
                observed_guard_calls = tuple(callbacks.guard_sweep_calls)
            finally:
                dispatcher.close()

        self.assertTrue(all(item == 3 for item in observed_guard_calls))
        self.assertGreaterEqual(
            len(observed_guard_calls),
            snapshot.callback_guard_sweep_count,
        )
        self.assertEqual(
            snapshot.callback_guard_sweep_count,
            snapshot.callback_guard_frozen_count,
        )
        self.assertEqual(0, snapshot.callback_guard_sweep_failure_count)
        self.assertTrue(snapshot.ready)

    def test_callback_guard_sweep_failure_does_not_skip_resource_recovery(self) -> None:
        with workspace_tempdir() as tmp:
            _service, commands = self._commands(Path(tmp))
            callbacks = self._callbacks()
            callbacks.guard_sweep_error = RuntimeError("forced guard sweep failure")
            resources = _ResourceRecoveryStub()
            dispatcher = LocalReportTaskDispatcher(
                task_commands=commands,
                queue_inspector=commands,
                resources=resources,
                callbacks=callbacks,
                execute=lambda task_id: commands.claim(task_id),
                config=_config(resource_sweep_interval_seconds=0.02),
            )
            try:
                with self.assertLogs(
                    "app.modules.report.adapters.local_dispatcher",
                    level="ERROR",
                ):
                    dispatcher.start()
                    self.assertTrue(
                        _wait_until(
                            lambda: (
                                len(callbacks.guard_sweep_calls) >= 2
                                and len(resources.sweep_calls) >= 2
                            )
                        )
                    )
                snapshot = dispatcher.snapshot()
            finally:
                dispatcher.close()

        self.assertGreaterEqual(snapshot.callback_guard_sweep_failure_count, 2)
        self.assertGreaterEqual(snapshot.resource_sweep_count, 2)
        self.assertTrue(snapshot.ready)

    def test_limiter_release_failure_clears_readiness_and_exposes_fatal_reason(self) -> None:
        with workspace_tempdir() as tmp:
            _service, commands = self._commands(Path(tmp))
            task_id = self._accept(commands, 2501)
            executed: list[TaskId] = []
            dispatcher = LocalReportTaskDispatcher(
                task_commands=commands,
                queue_inspector=commands,
                resources=_ResourceRecoveryStub(),
                callbacks=self._callbacks(),
                execute=executed.append,
                config=_config(),
                execution_limiter=_BrokenReleaseLimiter(),
            )
            try:
                with self.assertLogs(
                    "app.modules.report.adapters.local_dispatcher",
                    level="CRITICAL",
                ):
                    dispatcher.start()
                    self.assertTrue(
                        _wait_until(
                            lambda: bool(dispatcher.snapshot().fatal_error)
                        )
                    )
                snapshot = dispatcher.snapshot()
            finally:
                dispatcher.close()

        self.assertEqual([task_id], executed)
        self.assertFalse(snapshot.ready)
        self.assertEqual(
            "execution_limiter_release_failed",
            snapshot.fatal_error,
        )

    def test_running_execution_is_only_reported_and_never_requeued(self) -> None:
        with workspace_tempdir() as tmp:
            service, commands = self._commands(Path(tmp))
            task_id = self._accept(commands, 3001)
            self.assertEqual(TaskClaimOutcome.CLAIMED, commands.claim(task_id).outcome)
            execute_calls: list[TaskId] = []
            dispatcher = LocalReportTaskDispatcher(
                task_commands=commands,
                queue_inspector=commands,
                resources=_ResourceRecoveryStub(),
                callbacks=self._callbacks(),
                execute=execute_calls.append,
                config=_config(),
            )
            try:
                with self.assertLogs(
                    "app.modules.report.adapters.local_dispatcher",
                    level="WARNING",
                ) as captured:
                    dispatcher.start()
                    self.assertTrue(
                        _wait_until(
                            lambda: any(
                                "禁止自动重置" in message
                                for message in captured.output
                            )
                        )
                    )
                    # 扫描周期仅 20ms；等待多个周期，证明同一批 running 不会每轮刷屏。
                    time.sleep(0.12)
                running_warnings = [
                    message
                    for message in captured.output
                    if "禁止自动重置" in message
                ]
                self.assertEqual(1, len(running_warnings))
                # accepted 扫描仍按 20ms 运行，但历史聚合诊断最多 30 秒一次。
                snapshot = dispatcher.snapshot()
                self.assertEqual(1, snapshot.queue_inspection_count)
                self.assertEqual(0, snapshot.queue_inspection_failure_count)
                self.assertEqual([], execute_calls)
                self.assertEqual(
                    "running",
                    service.get_task_execution(task_id.value)["execution_state"],
                )
            finally:
                dispatcher.close()

    def test_queue_inspection_failure_is_counted_and_worker_survives(self) -> None:
        with workspace_tempdir() as tmp:
            _service, commands = self._commands(Path(tmp))
            inspector = _QueueInspectorStub(RuntimeError("forced inspection failure"))
            dispatcher = LocalReportTaskDispatcher(
                task_commands=commands,
                queue_inspector=inspector,
                resources=_ResourceRecoveryStub(),
                callbacks=self._callbacks(),
                execute=lambda _task_id: None,
                config=_config(),
            )
            try:
                with self.assertLogs(
                    "app.modules.report.adapters.local_dispatcher",
                    level="ERROR",
                ):
                    dispatcher.start()
                    self.assertTrue(
                        _wait_until(
                            lambda: dispatcher.snapshot().queue_inspection_failure_count
                            == 1
                        )
                    )
                snapshot = dispatcher.snapshot()
                self.assertEqual(0, snapshot.queue_inspection_count)
                self.assertEqual(1, snapshot.queue_inspection_failure_count)
                self.assertEqual(1, inspector.call_count)
                self.assertEqual(1, snapshot.worker_thread_count)
            finally:
                dispatcher.close()

    def test_poison_accepted_is_deferred_without_starving_the_next_task(self) -> None:
        """领取前永久异常不得形成满批热循环，也不得一直挡住下一条 accepted。"""

        with workspace_tempdir() as tmp:
            service, commands = self._commands(Path(tmp))
            poison_task_id = self._accept(commands, 3501)
            healthy_task_id = self._accept(commands, 3502)
            healthy_processed = threading.Event()

            def execute(task_id: TaskId) -> None:
                if task_id == poison_task_id:
                    raise RuntimeError("permanent pre-claim failure")
                claim = commands.claim(task_id)
                if claim.outcome is not TaskClaimOutcome.CLAIMED:
                    raise AssertionError(f"健康任务未取得执行权: {claim.outcome}")
                healthy_processed.set()

            dispatcher = LocalReportTaskDispatcher(
                task_commands=commands,
                queue_inspector=commands,
                resources=_ResourceRecoveryStub(),
                callbacks=self._callbacks(),
                execute=execute,
                config=_config(
                    accepted_batch_size=1,
                    dispatch_failure_retry_seconds=1.0,
                ),
            )
            try:
                dispatcher.start()
                self.assertTrue(healthy_processed.wait(timeout=5))
                time.sleep(0.12)
                snapshot = dispatcher.snapshot()
                self.assertEqual(1, snapshot.execution_failure_count)
                self.assertEqual(1, snapshot.accepted_deferral_count)
                self.assertEqual(0, snapshot.accepted_deferral_failure_count)
                # 120ms 内只会按 20ms 固定周期空扫，不再出现每毫秒数百次的满批热循环。
                self.assertLess(snapshot.scan_count, 20)
                self.assertEqual(
                    "accepted",
                    service.get_task_execution(poison_task_id.value)[
                        "execution_state"
                    ],
                )
                self.assertEqual(
                    "running",
                    service.get_task_execution(healthy_task_id.value)[
                        "execution_state"
                    ],
                )
                with sqlite3.connect(service.db_path) as connection:
                    retry_fact = connection.execute(
                        """
                        SELECT dispatch_failure_count, next_dispatch_at
                        FROM llm_task_executions
                        WHERE execution_id = ?
                        """,
                        (poison_task_id.value,),
                    ).fetchone()
                self.assertEqual(1, retry_fact[0])
                self.assertTrue(retry_fact[1])
            finally:
                dispatcher.close()

    def test_resource_sweep_continues_while_report_execution_is_blocked(self) -> None:
        """重型模型任务不能延迟独立的 cleanup 恢复周期。"""

        with workspace_tempdir() as tmp:
            _service, commands = self._commands(Path(tmp))
            self._accept(commands, 3601)
            entered = threading.Event()
            release = threading.Event()
            resources = _ResourceRecoveryStub()

            def blocked_execute(task_id: TaskId) -> None:
                commands.claim(task_id)
                entered.set()
                release.wait(timeout=5)

            dispatcher = LocalReportTaskDispatcher(
                task_commands=commands,
                queue_inspector=commands,
                resources=resources,
                callbacks=self._callbacks(),
                execute=blocked_execute,
                config=_config(resource_sweep_interval_seconds=0.02),
            )
            try:
                dispatcher.start()
                self.assertTrue(entered.wait(timeout=5))
                self.assertTrue(
                    _wait_until(lambda: len(resources.sweep_calls) >= 3)
                )
                snapshot = dispatcher.snapshot()
                self.assertEqual(1, snapshot.worker_thread_count)
                self.assertEqual(2, snapshot.maintenance_thread_count)
                self.assertGreaterEqual(snapshot.resource_sweep_count, 3)
            finally:
                release.set()
                dispatcher.close()

    def test_stop_cancels_shared_limiter_wait_before_business_execution(self) -> None:
        """analysis 占用共享许可时，报告不得在 stop 返回后才开始执行。"""

        with workspace_tempdir() as tmp:
            service, commands = self._commands(Path(tmp))
            task_id = self._accept(commands, 3701)
            limiter = UploadTaskLimiter(max_concurrency=1)
            limiter_held = False
            self.assertTrue(
                limiter.acquire_interruptibly(
                    lambda: False,
                    poll_interval_seconds=0.01,
                )
            )
            limiter_held = True
            executed = threading.Event()
            dispatcher = LocalReportTaskDispatcher(
                task_commands=commands,
                queue_inspector=commands,
                resources=_ResourceRecoveryStub(),
                callbacks=self._callbacks(),
                execute=lambda _task_id: executed.set(),
                execution_limiter=limiter,
                config=_config(),
            )
            try:
                dispatcher.start()
                self.assertTrue(
                    _wait_until(
                        lambda: dispatcher.snapshot().waiting_task_id == task_id
                    )
                )
                self.assertTrue(dispatcher.stop(timeout_seconds=0.5))
                limiter.release()
                limiter_held = False
                time.sleep(0.1)
                self.assertFalse(executed.is_set())
                self.assertEqual(
                    "accepted",
                    service.get_task_execution(task_id.value)["execution_state"],
                )
            finally:
                # 若断言在归还许可前失败，确保测试仍不会泄漏信号量或后台线程。
                if limiter_held:
                    limiter.release()
                dispatcher.close()

    def test_stop_timeout_reports_current_task_and_returns_without_requeue(self) -> None:
        with workspace_tempdir() as tmp:
            service, commands = self._commands(Path(tmp))
            task_id = self._accept(commands, 4001)
            entered = threading.Event()
            release = threading.Event()

            def blocked_execute(current_task_id: TaskId) -> None:
                commands.claim(current_task_id)
                entered.set()
                release.wait(timeout=5)

            dispatcher = LocalReportTaskDispatcher(
                task_commands=commands,
                queue_inspector=commands,
                resources=_ResourceRecoveryStub(),
                callbacks=self._callbacks(),
                execute=blocked_execute,
                config=_config(),
            )
            dispatcher.start()
            self.assertTrue(entered.wait(timeout=5))
            try:
                with self.assertLogs(
                    "app.modules.report.adapters.local_dispatcher",
                    level="CRITICAL",
                ):
                    self.assertFalse(dispatcher.stop(timeout_seconds=0.05))
                self.assertEqual(task_id, dispatcher.snapshot().current_task_id)
                self.assertEqual(
                    "running",
                    service.get_task_execution(task_id.value)["execution_state"],
                )
            finally:
                release.set()
                self.assertTrue(
                    _wait_until(
                        lambda: dispatcher.snapshot().worker_thread_count == 0
                    )
                )
                dispatcher.close()

    def test_close_timeout_keeps_stopping_until_worker_really_exits(self) -> None:
        """close 不得在执行函数仍存活时制造 CLOSED 假阳性。"""

        with workspace_tempdir() as tmp:
            _service, commands = self._commands(Path(tmp))
            self._accept(commands, 4101)
            entered = threading.Event()
            release = threading.Event()

            def blocked_execute(task_id: TaskId) -> None:
                commands.claim(task_id)
                entered.set()
                release.wait(timeout=5)

            dispatcher = LocalReportTaskDispatcher(
                task_commands=commands,
                queue_inspector=commands,
                resources=_ResourceRecoveryStub(),
                callbacks=self._callbacks(),
                execute=blocked_execute,
                config=_config(stop_timeout_seconds=0.05),
            )
            dispatcher.start()
            self.assertTrue(entered.wait(timeout=5))
            try:
                with self.assertLogs(
                    "app.modules.report.adapters.local_dispatcher",
                    level="CRITICAL",
                ):
                    dispatcher.close()
                timed_out = dispatcher.snapshot()
                self.assertEqual("stopping", timed_out.lifecycle_state)
                self.assertEqual(1, timed_out.worker_thread_count)
                self.assertFalse(dispatcher.stop(timeout_seconds=0.05))
            finally:
                release.set()
                self.assertTrue(
                    _wait_until(
                        lambda: dispatcher.snapshot().worker_thread_count == 0
                        and dispatcher.snapshot().maintenance_thread_count == 0
                    )
                )
                dispatcher.close()
            self.assertEqual("closed", dispatcher.snapshot().lifecycle_state)
            self.assertTrue(dispatcher.stop(timeout_seconds=0.05))


class FileProcessSingletonGuardTests(unittest.TestCase):
    def test_second_owner_is_rejected_until_first_releases(self) -> None:
        with workspace_tempdir() as tmp:
            lock_path = Path(tmp) / "locks" / "report.lock"
            first = FileProcessSingletonGuard(lock_path)
            second = FileProcessSingletonGuard(lock_path)

            self.assertTrue(first.acquire())
            try:
                with self.assertLogs(
                    "app.modules.report.adapters.process_guard",
                    level="ERROR",
                ):
                    self.assertFalse(second.acquire())
            finally:
                first.release()

            self.assertTrue(second.acquire())
            second.release()

    def test_separate_process_cannot_acquire_the_same_lock(self) -> None:
        """使用真实子进程证明门禁不是仅在当前 Python 进程内生效。"""

        with workspace_tempdir() as tmp:
            lock_path = Path(tmp) / "locks" / "report-cross-process.lock"
            owner = FileProcessSingletonGuard(lock_path)
            self.assertTrue(owner.acquire())
            try:
                blocked = self._probe_from_child(lock_path)
                self.assertEqual("blocked", blocked.stdout.strip())
                self.assertEqual(0, blocked.returncode, blocked.stderr)
            finally:
                owner.release()

            acquired = self._probe_from_child(lock_path)
            self.assertEqual("acquired", acquired.stdout.strip())
            self.assertEqual(0, acquired.returncode, acquired.stderr)

    @staticmethod
    def _probe_from_child(lock_path: Path) -> subprocess.CompletedProcess[str]:
        """在隔离解释器中尝试获取锁；子进程始终自行释放成功取得的锁。"""

        script = "\n".join(
            (
                "import sys",
                "from app.modules.report.adapters import FileProcessSingletonGuard",
                "guard = FileProcessSingletonGuard(sys.argv[1])",
                "acquired = guard.acquire()",
                "print('acquired' if acquired else 'blocked')",
                "guard.release() if acquired else None",
            )
        )
        return subprocess.run(
            [sys.executable, "-c", script, str(lock_path)],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            # Windows 全仓回归会同时创建大量线程、SQLite 连接和子进程；导入完整
            # Report Adapter 在高负载机器上可能接近 10 秒。本测试验证的是跨进程锁
            # 语义，不是模块导入性能，因此给探针一个仍然有界的 30 秒窗口。
            timeout=30,
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
