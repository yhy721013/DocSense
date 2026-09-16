"""阶段 1F-6：Analysis Callback Guard 与同步恢复离线验收。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import threading
import time
import unittest

from app.modules.analysis.adapters import (
    SQLiteAnalysisBatchCommandAdapter,
    SQLiteAnalysisCallbackAdapter,
    SQLiteAnalysisCallbackRecoverySource,
)
from app.modules.analysis.application import RecoverAnalysisCallbackSynchronously
from app.modules.analysis.application.run_analysis import AnalysisTaskCompletion
from app.modules.analysis.domain.task_inputs import (
    AnalysisPolicySnapshot,
    AnalysisSubmissionSnapshot,
    FrozenJsonObject,
)
from app.modules.analysis.ports import (
    AnalysisBatchCommand,
    AnalysisCallbackAcquireOutcome,
    AnalysisCallbackDelivery,
    AnalysisCallbackDeliveryOutcome,
    AnalysisCallbackDeliveryRequest,
    AnalysisCallbackRequest,
)
from app.modules.tasks.domain import TaskBusinessRef
from app.modules.tasks.ports import ExpectedTaskCompletion
from app.services.llm_service.task_service import LLMTaskService
from tests import workspace_tempdir


_CLOCK_VALUE = datetime(2030, 1, 1, tzinfo=timezone.utc)


def _command(prefix: str) -> AnalysisBatchCommand:
    raw_params = {
        "fileName": f"{prefix}.txt",
        "filePath": f"https://example.invalid/{prefix}.txt",
    }
    projection = FrozenJsonObject.from_mapping(
        {"businessType": "file", "params": [raw_params]},
        name="analysis_callback_test_request",
    )
    submission = AnalysisSubmissionSnapshot.from_request_params(
        raw_params,
        policy_snapshot=AnalysisPolicySnapshot.default(),
    )
    return AnalysisBatchCommand(
        request_projection=projection,
        submissions=(submission,),
        trace_id=f"analysis-callback-trace-{prefix}",
    )


def _terminal_execution(service: LLMTaskService, prefix: str):  # type: ignore[no-untyped-def]
    """创建并终结一条新 execution，确保恢复源不会误读旧 file 兼容投影。"""

    adapter = SQLiteAnalysisBatchCommandAdapter(service)
    admission = adapter.create_batch_if_allowed(_command(prefix))
    execution = admission.executions[0]
    claim = adapter.claim(execution.task_id)
    snapshot = claim.execution
    if snapshot is None:  # pragma: no cover - 测试夹具保护。
        raise AssertionError("新 execution 未能领取")
    payload = FrozenJsonObject.from_mapping(
        {
            "businessType": "file",
            "data": {"fileName": execution.file_name, "status": "2"},
            "msg": "解析完成",
        },
        name="analysis_callback_terminal_payload",
    )
    completed = adapter.finish_if_current(
        ExpectedTaskCompletion(
            expected_task_id=execution.task_id,
            business_ref=TaskBusinessRef("file", execution.file_name),
            execution_state="succeeded",
            public_status="2",
            message="解析完成",
            result=AnalysisTaskCompletion(
                callback_payload=payload,
                succeeded=True,
                mapped_result=FrozenJsonObject.from_mapping({"architectureId": 103}),
            ),
        )
    )
    if not completed:  # pragma: no cover - 测试夹具保护。
        raise AssertionError("新 execution 未能终结")
    return execution, payload


class AnalysisCallbackGuardTests(unittest.TestCase):
    """所有测试注入本地 transport，绝不访问真实 Callback URL。"""

    @staticmethod
    def _adapter(
        service: LLMTaskService,
        transport,
    ) -> SQLiteAnalysisCallbackAdapter:  # type: ignore[no-untyped-def]
        token_lock = threading.Lock()
        token_index = 0

        def token_factory() -> str:
            nonlocal token_index
            with token_lock:
                token_index += 1
                return f"analysis-callback-token-{token_index}"

        return SQLiteAnalysisCallbackAdapter(
            service,
            callback_timeout=1.0,
            lease_seconds=5.0,
            clock=lambda: _CLOCK_VALUE,
            token_factory=token_factory,
            transport=transport,
            # 历史文件不参与“是否发送”的权威判断；离线测试显式替身避免写入真实环境目录。
            history_writer=lambda _payload, *, callback_context: None,
        )

    def test_fifty_concurrent_sync_recoveries_send_exactly_one_http_request(self) -> None:
        """同一 fileName 的并发 check-task 恢复共享 Guard，最多一个线程进入 transport。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            execution, _payload = _terminal_execution(service, "callback-concurrent")
            calls_lock = threading.Lock()
            calls = 0

            def transport(request: AnalysisCallbackDeliveryRequest) -> AnalysisCallbackDelivery:
                nonlocal calls
                with calls_lock:
                    calls += 1
                return AnalysisCallbackDelivery(
                    execution=request.lease.execution,
                    lease_token=request.lease.lease_token,
                    lease_version=request.lease.lease_version,
                    outcome=AnalysisCallbackDeliveryOutcome.DELIVERED,
                )

            callbacks = self._adapter(service, transport)
            recovery = RecoverAnalysisCallbackSynchronously(
                source=SQLiteAnalysisCallbackRecoverySource(service),
                callbacks=callbacks,
                callback_url="https://callback.invalid/analysis",
                wait_timeout_seconds=1.0,
                wait_poll_seconds=0.01,
            )
            barrier = threading.Barrier(50)

            def recover_once() -> bool:
                barrier.wait(timeout=5)
                return recovery.execute(execution.file_name)

            with ThreadPoolExecutor(max_workers=50) as executor:
                results = tuple(executor.map(lambda _: recover_once(), range(50)))

            self.assertEqual(1, calls)
            self.assertEqual(1, sum(results))
            task = service.get_task("file", execution.file_name)
            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual("success", task["callback_status"])

    def test_active_same_process_recovery_is_coalesced_then_allows_next_attempt(self) -> None:
        """活跃恢复只允许一个本进程 owner，释放后下一次独立请求仍可明确重试。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            execution, _payload = _terminal_execution(service, "callback-local-flight")
            calls_lock = threading.Lock()
            calls = 0
            transport_entered = threading.Event()
            allow_first_completion = threading.Event()

            def transport(request: AnalysisCallbackDeliveryRequest) -> AnalysisCallbackDelivery:
                nonlocal calls
                with calls_lock:
                    calls += 1
                    current_call = calls
                if current_call == 1:
                    transport_entered.set()
                    if not allow_first_completion.wait(timeout=2.0):
                        raise TimeoutError("测试未允许首个回调完成")
                return AnalysisCallbackDelivery(
                    execution=request.lease.execution,
                    lease_token=request.lease.lease_token,
                    lease_version=request.lease.lease_version,
                    outcome=AnalysisCallbackDeliveryOutcome.REJECTED,
                    detail_code="http_status=503",
                )

            recovery = RecoverAnalysisCallbackSynchronously(
                source=SQLiteAnalysisCallbackRecoverySource(service),
                callbacks=self._adapter(service, transport),
                callback_url="https://callback.invalid/analysis",
                wait_timeout_seconds=1.0,
                wait_poll_seconds=0.01,
            )
            leader_results: list[bool] = []

            def execute_leader() -> None:
                leader_results.append(recovery.execute(execution.file_name))

            leader = threading.Thread(target=execute_leader)
            leader.start()
            self.assertTrue(transport_entered.wait(timeout=2.0))

            # follower 不等待 HTTP、不再读取新的 callback_attempts，也不把 owner 的
            # REJECTED 结果伪装成自己完成的成功。
            self.assertFalse(recovery.execute(execution.file_name))
            with calls_lock:
                self.assertEqual(1, calls)

            allow_first_completion.set()
            leader.join(timeout=2.0)
            self.assertFalse(leader.is_alive())
            self.assertEqual([False], leader_results)

            # 只有 owner 退出并在 finally 清理活跃键后，下一次独立请求才可以形成新的
            # 明确失败 attempt；这与原有 check-task 的显式重试语义保持一致。
            self.assertFalse(recovery.execute(execution.file_name))
            with calls_lock:
                self.assertEqual(2, calls)

    def test_fifty_concurrent_rejected_recoveries_do_not_roll_into_new_attempts(self) -> None:
        """同一轮并发补发即使明确失败，也只能消耗一次 callback attempt。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            execution, _payload = _terminal_execution(service, "callback-rejected")
            calls_lock = threading.Lock()
            calls = 0

            def transport(request: AnalysisCallbackDeliveryRequest) -> AnalysisCallbackDelivery:
                nonlocal calls
                with calls_lock:
                    calls += 1
                # 给其余调用者足够时间读取同一个 callback_attempts 快照，稳定复现
                # “等待者在明确失败后滚动重试”的历史竞态。
                time.sleep(0.05)
                return AnalysisCallbackDelivery(
                    execution=request.lease.execution,
                    lease_token=request.lease.lease_token,
                    lease_version=request.lease.lease_version,
                    outcome=AnalysisCallbackDeliveryOutcome.REJECTED,
                    detail_code="http_status=503",
                )

            recovery = RecoverAnalysisCallbackSynchronously(
                source=SQLiteAnalysisCallbackRecoverySource(service),
                callbacks=self._adapter(service, transport),
                callback_url="https://callback.invalid/analysis",
                wait_timeout_seconds=1.0,
                wait_poll_seconds=0.01,
            )
            barrier = threading.Barrier(50)

            def recover_once() -> bool:
                barrier.wait(timeout=5)
                return recovery.execute(execution.file_name)

            with ThreadPoolExecutor(max_workers=50) as executor:
                results = tuple(executor.map(lambda _: recover_once(), range(50)))

            self.assertEqual(1, calls)
            self.assertEqual(0, sum(results))
            task = service.get_task("file", execution.file_name)
            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual("failed", task["callback_status"])
            self.assertEqual(1, task["callback_attempts"])

            # 并发波次结束后的下一次独立 check-task 会读取 attempt=1 的新快照，仍可
            # 按公开合同显式发起下一轮补发，不会被上一轮的 fencing 永久冻结。
            self.assertFalse(recovery.execute(execution.file_name))
            self.assertEqual(2, calls)
            retried_task = service.get_task("file", execution.file_name)
            self.assertIsNotNone(retried_task)
            assert retried_task is not None
            self.assertEqual(2, retried_task["callback_attempts"])

    def test_recovery_projection_ignores_corrupt_request_and_execution_input(self) -> None:
        """回调恢复只读取结果投影，无关输入损坏不得阻断合法终态补发。"""

        with workspace_tempdir() as runtime_directory:
            database_path = Path(runtime_directory) / "tasks.sqlite3"
            service = LLMTaskService(str(database_path))
            execution, _payload = _terminal_execution(service, "callback-narrow-read")
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    """
                    UPDATE llm_tasks
                    SET request_payload = '{'
                    WHERE execution_id = ?
                    """,
                    (execution.task_id.value,),
                )
                connection.execute(
                    """
                    UPDATE llm_task_executions
                    SET input_payload = '{'
                    WHERE execution_id = ?
                    """,
                    (execution.task_id.value,),
                )

            candidate = SQLiteAnalysisCallbackRecoverySource(
                service
            ).load_recoverable(execution.file_name)

            self.assertIsNotNone(candidate)
            assert candidate is not None
            self.assertEqual(execution, candidate.execution)
            self.assertEqual(0, candidate.callback_attempts)

    def test_empty_callback_url_is_persisted_as_skipped_without_transport(self) -> None:
        """配置为空不是漏记：Guard 必须被完成为 skipped，且不能进入 HTTP transport。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            execution, payload = _terminal_execution(service, "callback-empty")
            transport_called = False

            def transport(request: AnalysisCallbackDeliveryRequest) -> AnalysisCallbackDelivery:
                nonlocal transport_called
                transport_called = True
                raise AssertionError("空 callback_url 不得进入 transport")

            callbacks = self._adapter(service, transport)
            acquired = callbacks.acquire(
                AnalysisCallbackRequest(
                    execution=execution,
                    callback_url="",
                    payload=payload,
                )
            )
            self.assertIsNotNone(acquired.lease)
            assert acquired.lease is not None
            delivery = callbacks.deliver(
                AnalysisCallbackDeliveryRequest(
                    lease=acquired.lease,
                    callback_url="",
                    payload=payload,
                )
            )
            self.assertEqual(AnalysisCallbackDeliveryOutcome.SKIPPED, delivery.outcome)
            self.assertTrue(callbacks.complete(acquired.lease, delivery, payload))
            self.assertFalse(transport_called)
            task = service.get_task("file", execution.file_name)
            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual("skipped", task["callback_status"])

    def test_completion_rolls_back_when_latest_projection_is_missing(self) -> None:
        """HTTP 后若公开投影异常丢失，Guard/execution 完成必须整体回滚。"""

        with workspace_tempdir() as runtime_directory:
            database_path = Path(runtime_directory) / "tasks.sqlite3"
            service = LLMTaskService(str(database_path))
            execution, payload = _terminal_execution(
                service,
                "callback-projection-missing",
            )

            def transport(
                request: AnalysisCallbackDeliveryRequest,
            ) -> AnalysisCallbackDelivery:
                # deliver 已完成发送前 Guard 复核；此处模拟 HTTP 返回成功后，投影被
                # 异常删除。complete 必须发现 rowcount=0 并回滚同一事务。
                with sqlite3.connect(database_path) as connection:
                    connection.execute(
                        "DELETE FROM llm_tasks WHERE execution_id = ?",
                        (execution.task_id.value,),
                    )
                return AnalysisCallbackDelivery(
                    execution=request.lease.execution,
                    lease_token=request.lease.lease_token,
                    lease_version=request.lease.lease_version,
                    outcome=AnalysisCallbackDeliveryOutcome.DELIVERED,
                )

            callbacks = self._adapter(service, transport)
            acquired = callbacks.acquire(
                AnalysisCallbackRequest(
                    execution=execution,
                    callback_url="https://callback.invalid/analysis",
                    payload=payload,
                )
            )
            self.assertIsNotNone(acquired.lease)
            assert acquired.lease is not None
            delivery = callbacks.deliver(
                AnalysisCallbackDeliveryRequest(
                    lease=acquired.lease,
                    callback_url="https://callback.invalid/analysis",
                    payload=payload,
                )
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "最新投影不存在或已切换",
            ):
                callbacks.complete(acquired.lease, delivery, payload)

            with sqlite3.connect(database_path) as connection:
                execution_status = connection.execute(
                    """
                    SELECT callback_status
                    FROM llm_task_executions
                    WHERE execution_id = ?
                    """,
                    (execution.task_id.value,),
                ).fetchone()[0]
                guard_state = connection.execute(
                    """
                    SELECT state
                    FROM callback_delivery_guards
                    WHERE business_type = 'file' AND business_key = ?
                    """,
                    (execution.file_name,),
                ).fetchone()[0]
            self.assertEqual("sending", execution_status)
            self.assertEqual("sending", guard_state)

    def test_unknown_delivery_only_allows_next_explicit_check_task_retry(self) -> None:
        """未知结果禁止 Worker 自动重试，但下一次 check-task 可明确补发。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            execution, _payload = _terminal_execution(service, "callback-unknown")
            calls = 0

            def transport(request: AnalysisCallbackDeliveryRequest) -> AnalysisCallbackDelivery:
                nonlocal calls
                calls += 1
                outcome = (
                    AnalysisCallbackDeliveryOutcome.OUTCOME_UNKNOWN
                    if calls == 1
                    else AnalysisCallbackDeliveryOutcome.DELIVERED
                )
                return AnalysisCallbackDelivery(
                    execution=request.lease.execution,
                    lease_token=request.lease.lease_token,
                    lease_version=request.lease.lease_version,
                    outcome=outcome,
                    detail_code=(
                        "response_read_interrupted"
                        if outcome
                        is AnalysisCallbackDeliveryOutcome.OUTCOME_UNKNOWN
                        else ""
                    ),
                )

            callbacks = self._adapter(service, transport)
            recovery = RecoverAnalysisCallbackSynchronously(
                source=SQLiteAnalysisCallbackRecoverySource(service),
                callbacks=callbacks,
                callback_url="https://callback.invalid/analysis",
            )
            self.assertFalse(recovery.execute(execution.file_name))
            # 普通 Worker 没有显式 check-task 授权，必须继续看到冻结结果且不得触网。
            worker_acquire = callbacks.acquire(
                AnalysisCallbackRequest(
                    execution=execution,
                    callback_url="https://callback.invalid/analysis",
                    payload=_payload,
                )
            )
            self.assertEqual(
                AnalysisCallbackAcquireOutcome.FROZEN,
                worker_acquire.outcome,
            )
            self.assertEqual(1, calls)

            self.assertTrue(recovery.execute(execution.file_name))
            self.assertEqual(2, calls)
            guard = service.observe_callback_delivery_guard(
                business_type="file",
                business_key=execution.file_name,
                observed_at=_CLOCK_VALUE.isoformat(),
            )
            self.assertEqual("idle", guard["state"])
            task = service.get_task("file", execution.file_name)
            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual("success", task["callback_status"])


if __name__ == "__main__":  # pragma: no cover - 仅供本地定向调试。
    unittest.main()
