"""阶段 1A-3：任务检查应用服务的框架无关契约测试。

测试只注入内存 Fake Port，不创建 Flask 应用、SQLite 数据库、网络会话或后台线程。
重点验证有序批量读取、缺失位置、按 TaskId 恢复后重读和异常传播。
"""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, replace

from app.modules.tasks.application import (
    CallbackRecoveryConsistencyError,
    CallbackRecoveryContractError,
    CheckTaskStatusRequest,
    CheckTaskStatusService,
    TaskReadContractError,
    TaskSnapshotUnavailableError,
)
from app.modules.tasks.domain import (
    CALLBACK_FAILED,
    CALLBACK_PENDING,
    CALLBACK_SENDING,
    CALLBACK_SKIPPED,
    CALLBACK_SUCCESS,
    TaskBusinessRef,
    TaskId,
    TaskLookupItem,
    TaskSnapshot,
)
from app.modules.tasks.ports import (
    CallbackRecoveryResult,
    DELIVERY_OUTCOME_UNKNOWN,
)
from tests.fakes import FakeCallbackRecoveryPort, FakeTaskReadPort


def _snapshot(
    business_key: str,
    *,
    task_id: str | None = None,
    callback_status: str = CALLBACK_PENDING,
    progress: float = 0.25,
) -> TaskSnapshot:
    return TaskSnapshot(
        task_id=TaskId(task_id or f"task-{business_key}"),
        task_type="file_analysis",
        business_ref=TaskBusinessRef("file", business_key),
        execution_state="running",
        public_status="1",
        progress=progress,
        message="处理中",
        callback_status=callback_status,
        created_at="2026-07-16T10:00:00+08:00",
        updated_at="2026-07-16T10:01:00+08:00",
    )


def _lookup(business_key: str) -> TaskLookupItem:
    return TaskLookupItem(
        business_ref=TaskBusinessRef("file", business_key),
        response_key="fileName",
        response_value=business_key,
    )


def _no_recovery(status: str = CALLBACK_PENDING) -> CallbackRecoveryResult:
    return CallbackRecoveryResult(
        attempted=False,
        replayed=False,
        final_status=status,
    )


class CheckTaskDomainContractTests(unittest.TestCase):
    """验证 DTO 在进入 Port 前已经不可变且类型稳定。"""

    def test_snapshot_is_immutable_and_normalizes_progress(self) -> None:
        snapshot = _snapshot("normalized.pdf", progress=0.28000000004)

        self.assertEqual(0.28, snapshot.progress)
        with self.assertRaises(FrozenInstanceError):
            snapshot.progress = 0.5  # type: ignore[misc]

    def test_snapshot_and_recovery_result_accept_active_sending_status(self) -> None:
        """Guard owner 活跃期间的 sending 必须能穿过内部查询模型。"""

        snapshot = _snapshot("sending.pdf", callback_status=CALLBACK_SENDING)
        result = CallbackRecoveryResult(
            attempted=False,
            replayed=False,
            final_status=CALLBACK_SENDING,
        )

        self.assertEqual(CALLBACK_SENDING, snapshot.callback_status)
        self.assertEqual(CALLBACK_SENDING, result.final_status)

    def test_request_rejects_empty_or_mixed_business_types(self) -> None:
        with self.assertRaises(ValueError):
            CheckTaskStatusRequest(())

        report_lookup = TaskLookupItem(
            business_ref=TaskBusinessRef("report", "132"),
            response_key="reportId",
            response_value=132,
        )
        with self.assertRaises(ValueError):
            CheckTaskStatusRequest((_lookup("a.pdf"), report_lookup))

    def test_callback_result_preserves_unknown_delivery_outcome(self) -> None:
        result = CallbackRecoveryResult(
            attempted=True,
            replayed=False,
            final_status=CALLBACK_FAILED,
            delivery_outcome=DELIVERY_OUTCOME_UNKNOWN,
        )

        self.assertEqual(DELIVERY_OUTCOME_UNKNOWN, result.delivery_outcome)
        with self.assertRaises(ValueError):
            CallbackRecoveryResult(
                attempted=False,
                replayed=True,
                final_status=CALLBACK_SUCCESS,
            )


class CheckTaskStatusServiceTests(unittest.TestCase):
    """使用 Fake Port 验证应用编排，不依赖当前生产 Task Service。"""

    def _service(
        self,
        snapshots: tuple[TaskSnapshot, ...] = (),
    ) -> tuple[
        CheckTaskStatusService,
        FakeTaskReadPort,
        FakeCallbackRecoveryPort,
    ]:
        reader = FakeTaskReadPort(snapshots)
        recovery = FakeCallbackRecoveryPort(reader)
        service = CheckTaskStatusService(
            task_reader=reader,
            callback_recovery=recovery,
        )
        return service, reader, recovery

    def test_batch_preserves_order_missing_positions_and_read_boundary(self) -> None:
        first = _snapshot("first.pdf")
        last = _snapshot("last.pdf")
        service, reader, recovery = self._service((first, last))
        recovery.configure(first.task_id, result=_no_recovery())
        recovery.configure(last.task_id, result=_no_recovery())
        request = CheckTaskStatusRequest(
            (_lookup("last.pdf"), _lookup("missing.pdf"), _lookup("first.pdf"))
        )

        result = service.check(request, trace_id="trace-1a3")

        self.assertEqual([True, False, True], [item.found for item in result.ordered_items])
        self.assertEqual(
            ["last.pdf", "missing.pdf", "first.pdf"],
            [item.lookup.response_value for item in result.ordered_items],
        )
        self.assertEqual(
            [last.task_id, first.task_id],
            recovery.recovery_calls,
        )
        self.assertEqual([last.task_id, first.task_id], reader.by_id_calls)
        self.assertEqual(
            [tuple(item.business_ref for item in request.ordered_items)],
            reader.latest_many_calls,
        )
        self.assertFalse(result.single_missing)

    def test_single_missing_is_left_for_presenter_to_map_to_404(self) -> None:
        service, _, _ = self._service()

        result = service.check(CheckTaskStatusRequest((_lookup("missing.pdf"),)))

        self.assertTrue(result.is_single)
        self.assertTrue(result.single_missing)
        self.assertIsNone(result.ordered_items[0].recovery)

    def test_successful_recovery_rereads_exact_same_task_id(self) -> None:
        initial = _snapshot("callback.pdf", callback_status=CALLBACK_FAILED)
        updated = replace(
            initial,
            callback_status=CALLBACK_SUCCESS,
            updated_at="2026-07-16T10:02:00+08:00",
        )
        service, reader, recovery = self._service((initial,))
        recovery.configure(
            initial.task_id,
            result=CallbackRecoveryResult(
                attempted=True,
                replayed=True,
                final_status=CALLBACK_SUCCESS,
            ),
            updated_snapshot=updated,
        )

        result = service.check(CheckTaskStatusRequest((_lookup("callback.pdf"),)))

        item = result.ordered_items[0]
        self.assertEqual(CALLBACK_FAILED, item.initial_snapshot.callback_status)
        self.assertEqual(CALLBACK_SUCCESS, item.current_snapshot.callback_status)
        self.assertEqual([initial.task_id], reader.by_id_calls)
        self.assertEqual(1, result.replayed_count)

    def test_recovery_result_must_match_persisted_callback_status(self) -> None:
        initial = _snapshot("inconsistent.pdf", callback_status=CALLBACK_FAILED)
        service, reader, recovery = self._service((initial,))
        recovery.configure(
            initial.task_id,
            result=CallbackRecoveryResult(
                attempted=True,
                replayed=True,
                final_status=CALLBACK_SUCCESS,
            ),
            # 模拟 Adapter 对外声称成功，但数据库中的同一 TaskId 仍停留在 failed。
            updated_snapshot=initial,
        )

        with self.assertLogs(
            "app.modules.tasks.application.check_status",
            level="ERROR",
        ):
            with self.assertRaises(CallbackRecoveryConsistencyError):
                service.check(
                    CheckTaskStatusRequest((_lookup("inconsistent.pdf"),))
                )

        self.assertEqual([initial.task_id], reader.by_id_calls)

    def test_no_attempt_result_cannot_hide_an_unexpected_persisted_change(self) -> None:
        initial = _snapshot("unexpected-change.pdf", callback_status=CALLBACK_FAILED)
        persisted = replace(initial, callback_status=CALLBACK_SUCCESS)
        service, reader, recovery = self._service((initial,))
        recovery.configure(
            initial.task_id,
            result=_no_recovery(CALLBACK_FAILED),
            updated_snapshot=persisted,
        )

        with self.assertLogs(
            "app.modules.tasks.application.check_status",
            level="ERROR",
        ):
            with self.assertRaises(CallbackRecoveryConsistencyError):
                service.check(
                    CheckTaskStatusRequest((_lookup("unexpected-change.pdf"),))
                )

        self.assertEqual([initial.task_id], reader.by_id_calls)

    def test_pending_to_skipped_rereads_even_without_network_attempt(self) -> None:
        initial = _snapshot("skipped.pdf", callback_status=CALLBACK_PENDING)
        updated = replace(initial, callback_status=CALLBACK_SKIPPED)
        service, reader, recovery = self._service((initial,))
        recovery.configure(
            initial.task_id,
            result=CallbackRecoveryResult(
                attempted=False,
                replayed=False,
                final_status=CALLBACK_SKIPPED,
            ),
            updated_snapshot=updated,
        )

        result = service.check(CheckTaskStatusRequest((_lookup("skipped.pdf"),)))

        self.assertEqual(CALLBACK_SKIPPED, result.ordered_items[0].current_snapshot.callback_status)
        self.assertEqual([initial.task_id], reader.by_id_calls)

    def test_no_attempt_and_unchanged_status_is_still_verified_from_storage(self) -> None:
        initial = _snapshot("running.pdf", callback_status=CALLBACK_PENDING)
        service, reader, recovery = self._service((initial,))
        recovery.configure(initial.task_id, result=_no_recovery())

        result = service.check(CheckTaskStatusRequest((_lookup("running.pdf"),)))

        self.assertIs(
            result.ordered_items[0].initial_snapshot,
            result.ordered_items[0].current_snapshot,
        )
        self.assertEqual([initial.task_id], reader.by_id_calls)

    def test_recovery_error_is_not_swallowed_as_success(self) -> None:
        initial = _snapshot("error.pdf")
        service, _, recovery = self._service((initial,))
        recovery.configure(
            initial.task_id,
            error=RuntimeError("callback transport failed"),
        )

        with self.assertLogs(
            "app.modules.tasks.application.check_status",
            level="ERROR",
        ):
            with self.assertRaisesRegex(RuntimeError, "transport failed"):
                service.check(CheckTaskStatusRequest((_lookup("error.pdf"),)))

    def test_recovery_port_must_return_typed_result(self) -> None:
        initial = _snapshot("invalid-result.pdf")
        reader = FakeTaskReadPort((initial,))

        class InvalidRecoveryPort:
            def recover_if_needed(self, task_id: TaskId) -> object:
                self.task_id = task_id
                return object()

        service = CheckTaskStatusService(
            task_reader=reader,
            callback_recovery=InvalidRecoveryPort(),
        )

        with self.assertRaises(CallbackRecoveryContractError):
            service.check(
                CheckTaskStatusRequest((_lookup("invalid-result.pdf"),))
            )

    def test_reread_missing_is_explicit_consistency_error(self) -> None:
        initial = _snapshot("lost.pdf", callback_status=CALLBACK_FAILED)
        service, _, recovery = self._service((initial,))
        recovery.configure(
            initial.task_id,
            result=CallbackRecoveryResult(
                attempted=True,
                replayed=False,
                final_status=CALLBACK_FAILED,
            ),
            updated_snapshot=None,
        )

        with self.assertRaises(TaskSnapshotUnavailableError):
            service.check(CheckTaskStatusRequest((_lookup("lost.pdf"),)))

    def test_batch_reader_must_preserve_response_length(self) -> None:
        service, reader, _ = self._service()
        reader.forced_many_result = ()

        with self.assertRaises(TaskReadContractError):
            service.check(CheckTaskStatusRequest((_lookup("a.pdf"),)))

    def test_batch_reader_must_return_snapshot_for_requested_business_ref(self) -> None:
        wrong = _snapshot("wrong.pdf")
        service, reader, _ = self._service()
        reader.forced_many_result = (wrong,)

        with self.assertRaises(TaskReadContractError):
            service.check(CheckTaskStatusRequest((_lookup("expected.pdf"),)))


if __name__ == "__main__":
    unittest.main()
