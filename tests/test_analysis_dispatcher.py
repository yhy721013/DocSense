"""阶段 1F-5A：文件分析本地 Dispatcher 的离线验收。"""

from __future__ import annotations

import threading
import time
import unittest

from app.modules.analysis.adapters import (
    AnalysisTaskSnapshotCorruptedError,
    LocalAnalysisTaskDispatcher,
)
from app.modules.analysis.application import (
    AnalysisResourceSweepResult,
    RunAnalysisOutcome,
    RunAnalysisResult,
)
from app.modules.analysis.ports import (
    AnalysisCallbackGuardSweepResult,
    AnalysisExecutionRef,
)
from app.modules.tasks.domain import TaskId
from app.modules.tasks.ports import TaskQueueSnapshot
from app.modules.analysis.adapters.runtime_config import AnalysisInfrastructureConfig


class _AnalysisTaskCommandsFake:
    """实现 Dispatcher 所需窄 Port 的严格内存 Fake，不模拟真实业务执行。"""

    def __init__(self, accepted: tuple[TaskId, ...] = ()) -> None:
        self._accepted = accepted
        self.backoff_calls: list[tuple[TaskId, float, float, str]] = []
        self.poison_calls: list[tuple[TaskId, str]] = []
        self.backoff_recorded = threading.Event()

    # 下列通用 TaskCommand 方法只用于通过 runtime Protocol 校验；本测试的 Worker
    # 从不进入真实 get/claim/finish 路径，避免用伪数据掩盖 Application 行为。
    def create_if_allowed(self, command):  # type: ignore[no-untyped-def]
        raise AssertionError("Dispatcher 测试不应调用 create_if_allowed")

    def get_execution(self, task_id: TaskId):  # type: ignore[no-untyped-def]
        raise AssertionError("Dispatcher 测试不应调用 get_execution")

    def claim(self, task_id: TaskId):  # type: ignore[no-untyped-def]
        raise AssertionError("Dispatcher 测试不应调用 claim")

    def update_progress_if_current(self, update):  # type: ignore[no-untyped-def]
        raise AssertionError("Dispatcher 测试不应调用 update_progress_if_current")

    def finish_if_current(self, completion):  # type: ignore[no-untyped-def]
        raise AssertionError("Dispatcher 测试不应调用 finish_if_current")

    def is_latest(self, task_id: TaskId, business_ref):  # type: ignore[no-untyped-def]
        raise AssertionError("Dispatcher 测试不应调用 is_latest")

    def list_accepted(self, task_type: str, *, limit: int) -> tuple[TaskId, ...]:
        if task_type != "file" or limit < 1:
            raise ValueError("测试 Fake 收到无效扫描参数")
        return self._accepted[:limit]

    def defer_accepted(
        self,
        task_id: TaskId,
        *,
        retry_at: str,
        reason: str,
    ) -> bool:
        raise AssertionError("Analysis Dispatcher 必须走事务级指数退避 Port")

    def inspect_queue(
        self,
        task_type: str,
        *,
        running_sample_limit: int,
    ) -> TaskQueueSnapshot:
        if task_type != "file" or running_sample_limit < 1:
            raise ValueError("测试 Fake 收到无效队列诊断参数")
        return TaskQueueSnapshot(
            task_type="file",
            accepted_count=len(self._accepted),
            running_count=0,
        )

    def defer_accepted_with_backoff(
        self,
        task_id: TaskId,
        *,
        retry_base_seconds: float,
        retry_max_seconds: float,
        reason: str,
    ) -> bool:
        self.backoff_calls.append(
            (task_id, retry_base_seconds, retry_max_seconds, reason)
        )
        # 模拟 Repository 已写入 next_dispatch_at：同一 accepted 不应在本测试中热循环。
        self._accepted = ()
        self.backoff_recorded.set()
        return True

    def fail_poisoned_accepted(
        self,
        task_id: TaskId,
        *,
        reason: str,
    ) -> AnalysisExecutionRef:
        self.poison_calls.append((task_id, reason))
        self._accepted = ()
        return AnalysisExecutionRef(
            task_id=task_id,
            file_name=f"{task_id.value}.pdf",
            batch_id="a" * 32,
            batch_sequence=1,
        )


class _ResourceMaintenanceFake:
    """返回强类型空扫描结果，证明维护线程无需外部 RAG 清理也可运行。"""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.calls = 0

    def run_once(self, *, limit: int) -> AnalysisResourceSweepResult:
        self.calls += 1
        if self._fail:
            raise RuntimeError("forced resource maintenance failure")
        return AnalysisResourceSweepResult(
            requested_limit=limit,
            scanned_count=0,
            cleaned_count=0,
            deferred_count=0,
            quarantined_count=0,
            pending_count=0,
        )


class _CallbackMaintenanceFake:
    """返回强类型 Guard 扫描结果，不触发真实 HTTP 或 SQLite。"""

    def __init__(self) -> None:
        self.calls = 0

    def run_once(self, *, limit: int) -> AnalysisCallbackGuardSweepResult:
        self.calls += 1
        return AnalysisCallbackGuardSweepResult(
            scanned_count=0,
            frozen_count=0,
        )


def _config(**overrides: object) -> AnalysisInfrastructureConfig:
    """创建适用于线程离线测试的短周期配置。"""

    values: dict[str, object] = {
        "runtime_mode": "single_instance",
        "scan_interval_seconds": 0.01,
        "accepted_batch_size": 10,
        "dispatch_retry_base_seconds": 0.05,
        "dispatch_retry_max_seconds": 0.10,
        "resource_sweep_interval_seconds": 0.01,
        "resource_sweep_batch_size": 5,
        "running_alert_seconds": 0.01,
        "stop_timeout_seconds": 1.0,
        "callback_http_timeout_seconds": 1.0,
        "callback_lease_seconds": 10.0,
    }
    values.update(overrides)
    return AnalysisInfrastructureConfig(**values)  # type: ignore[arg-type]


class LocalAnalysisTaskDispatcherTests(unittest.TestCase):
    """验证毒快照、持久退避、维护隔离和显式生命周期。"""

    def _dispatcher(
        self,
        *,
        commands: _AnalysisTaskCommandsFake,
        execute,
        resources: _ResourceMaintenanceFake | None = None,
        callbacks: _CallbackMaintenanceFake | None = None,
        poison_callback_recovery=None,
        fatal_error_handler=None,
    ) -> LocalAnalysisTaskDispatcher:
        return LocalAnalysisTaskDispatcher(
            task_commands=commands,
            queue_inspector=commands,
            poison_commands=commands,
            dispatch_failure_backoff=commands,
            execute=execute,
            resource_maintenance=resources or _ResourceMaintenanceFake(),
            callback_guard_maintenance=callbacks or _CallbackMaintenanceFake(),
            poison_callback_recovery=(
                poison_callback_recovery
                or (lambda _file_name: False)
            ),
            config=_config(),
            fatal_error_handler=fatal_error_handler,
        )

    def test_constructor_and_explicit_fake_do_not_start_background_threads(self) -> None:
        commands = _AnalysisTaskCommandsFake()
        dispatcher = self._dispatcher(
            commands=commands,
            execute=lambda task_id: RunAnalysisResult(
                task_id,
                RunAnalysisOutcome.MISSING,
            ),
        )

        snapshot = dispatcher.snapshot()

        self.assertEqual("new", snapshot.lifecycle_state)
        self.assertEqual(0, snapshot.worker_thread_count)
        self.assertEqual(0, snapshot.maintenance_thread_count)
        self.assertFalse(snapshot.ready)

    def test_poisoned_snapshot_is_finished_without_generic_backoff(self) -> None:
        task_id = TaskId("analysis-poison-0001")
        commands = _AnalysisTaskCommandsFake((task_id,))

        def execute(_: TaskId) -> RunAnalysisResult:
            raise AnalysisTaskSnapshotCorruptedError("bad persisted payload")

        recovered_file_names: list[str] = []
        dispatcher = self._dispatcher(
            commands=commands,
            execute=execute,
            poison_callback_recovery=lambda file_name: (
                recovered_file_names.append(file_name) is None
            ),
        )

        result = dispatcher._execute_once(task_id)

        self.assertIsNone(result)
        self.assertEqual(
            [(task_id, "analysis_task_snapshot_corrupted")],
            commands.poison_calls,
        )
        self.assertEqual([], commands.backoff_calls)
        self.assertEqual(1, dispatcher.snapshot().poisoned_snapshot_count)
        self.assertEqual([f"{task_id.value}.pdf"], recovered_file_names)

    def test_worker_failure_uses_analysis_transactional_backoff_and_stays_alive(self) -> None:
        task_id = TaskId("analysis-backoff-0001")
        commands = _AnalysisTaskCommandsFake((task_id,))

        def execute(_: TaskId) -> RunAnalysisResult:
            raise RuntimeError("forced pre-claim failure")

        dispatcher = self._dispatcher(commands=commands, execute=execute)
        try:
            dispatcher.start()
            self.assertTrue(
                commands.backoff_recorded.wait(timeout=2.0),
                "Worker 未在限定时间内写入持久退避",
            )
            snapshot = dispatcher.snapshot()
            self.assertTrue(snapshot.ready)
            self.assertEqual(1, snapshot.execution_failure_count)
            self.assertEqual(1, snapshot.accepted_deferral_count)
        finally:
            self.assertTrue(dispatcher.stop(timeout_seconds=1.0))

        self.assertEqual(
            [(task_id, 0.05, 0.10, "builtins.RuntimeError")],
            commands.backoff_calls,
        )

    def test_resource_maintenance_error_is_observed_without_stopping_other_maintenance(self) -> None:
        commands = _AnalysisTaskCommandsFake()
        resources = _ResourceMaintenanceFake(fail=True)
        callbacks = _CallbackMaintenanceFake()
        dispatcher = self._dispatcher(
            commands=commands,
            execute=lambda task_id: RunAnalysisResult(
                task_id,
                RunAnalysisOutcome.MISSING,
            ),
            resources=resources,
            callbacks=callbacks,
        )

        dispatcher._run_analysis_maintenance_once()
        snapshot = dispatcher.snapshot()

        self.assertEqual(1, callbacks.calls)
        self.assertEqual(1, resources.calls)
        self.assertEqual(1, snapshot.callback_guard_sweep_count)
        self.assertEqual(1, snapshot.resource_sweep_failure_count)
        self.assertEqual(0, snapshot.resource_sweep_count)

    def test_unexpected_worker_exit_notifies_process_fatal_handler_once(self) -> None:
        """Worker 不可恢复退出时必须让生产组合根有机会终止假健康进程。"""

        task_id = TaskId("analysis-fatal-0001")
        commands = _AnalysisTaskCommandsFake((task_id,))
        fatal_messages: list[str] = []
        notified = threading.Event()

        def execute(_: TaskId) -> RunAnalysisResult:
            raise KeyboardInterrupt("forced worker base exception")

        def on_fatal(message: str) -> None:
            fatal_messages.append(message)
            notified.set()

        dispatcher = self._dispatcher(
            commands=commands,
            execute=execute,
            fatal_error_handler=on_fatal,
        )
        try:
            dispatcher.start()
            self.assertTrue(
                notified.wait(timeout=2.0),
                "不可恢复 Worker 退出未通知进程级处理器",
            )
            snapshot = dispatcher.snapshot()
            self.assertFalse(snapshot.ready)
            self.assertTrue(snapshot.fatal_error)
            self.assertEqual(1, len(fatal_messages))
        finally:
            dispatcher.stop(timeout_seconds=1.0)


if __name__ == "__main__":
    unittest.main()
