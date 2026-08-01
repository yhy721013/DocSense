"""阶段 1G-4R：同步 Callback Recovery 组合适配器的身份与审计语义。"""

from __future__ import annotations

from dataclasses import replace
import unittest

from app.modules.tasks.adapters import SynchronousCallbackRecoveryRouterAdapter
from app.modules.tasks.application import CheckTaskStatusRequest, CheckTaskStatusService
from app.modules.tasks.domain import (
    CALLBACK_FAILED,
    CALLBACK_SUCCESS,
    TaskBusinessRef,
    TaskId,
    TaskLookupItem,
    TaskSnapshot,
)
from tests.fakes import FakeTaskReadPort


def _snapshot(*, callback_status: str, callback_attempts: int) -> TaskSnapshot:
    return TaskSnapshot(
        task_id=TaskId("callback-recovery-adapter-task"),
        task_type="file_task",
        business_ref=TaskBusinessRef("file", "adapter.pdf"),
        execution_state="legacy_status:2",
        public_status="2",
        progress=1.0,
        message="完成",
        callback_status=callback_status,
        created_at="2026-08-01T10:00:00+08:00",
        updated_at="2026-08-01T10:01:00+08:00",
        callback_attempts=callback_attempts,
    )


class SynchronousCallbackRecoveryRouterAdapterTests(unittest.TestCase):
    """验证 attempt 不能继续与严格 2xx 成功混为一谈。"""

    def test_rejected_delivery_is_attempted_but_not_replayed(self) -> None:
        before = _snapshot(callback_status=CALLBACK_FAILED, callback_attempts=1)
        reader = FakeTaskReadPort((before,))

        def recover(
            task_id: TaskId,
            business_ref: TaskBusinessRef,
            trace_id: str,
        ) -> bool:
            self.assertEqual(before.task_id, task_id)
            self.assertEqual(before.business_ref, business_ref)
            self.assertEqual("request-trace", trace_id)
            reader.replace_by_id(
                replace(before, callback_attempts=2, updated_at="updated")
            )
            return False

        adapter = SynchronousCallbackRecoveryRouterAdapter(
            task_reader=reader,
            routes={"file": recover},
        )

        result = adapter.recover_snapshot_with_context(
            before,
            trace_id="request-trace",
        )

        self.assertTrue(result.attempted)
        self.assertFalse(result.replayed)
        self.assertEqual(CALLBACK_FAILED, result.final_status)
        self.assertEqual(2, result.current_snapshot.callback_attempts)

    def test_success_without_persisted_attempt_increment_is_rejected(self) -> None:
        before = _snapshot(callback_status=CALLBACK_FAILED, callback_attempts=1)
        reader = FakeTaskReadPort((before,))
        adapter = SynchronousCallbackRecoveryRouterAdapter(
            task_reader=reader,
            routes={"file": lambda _task_id, _business_ref, _trace_id: True},
        )

        with self.assertRaisesRegex(RuntimeError, "attempt 未增加"):
            adapter.recover_snapshot_with_context(before, trace_id="trace")

    def test_application_reuses_adapter_current_snapshot_without_second_read(self) -> None:
        before = _snapshot(callback_status=CALLBACK_FAILED, callback_attempts=1)
        reader = FakeTaskReadPort((before,))

        def recover(
            _task_id: TaskId,
            _business_ref: TaskBusinessRef,
            _trace_id: str,
        ) -> bool:
            reader.replace_by_id(
                replace(
                    before,
                    callback_status=CALLBACK_SUCCESS,
                    callback_attempts=2,
                    updated_at="updated",
                )
            )
            return True

        adapter = SynchronousCallbackRecoveryRouterAdapter(
            task_reader=reader,
            routes={"file": recover},
        )
        service = CheckTaskStatusService(
            task_reader=reader,
            callback_recovery=adapter,
        )
        request = CheckTaskStatusRequest(
            (
                TaskLookupItem(
                    business_ref=before.business_ref,
                    response_key="fileName",
                    response_value="adapter.pdf",
                ),
            )
        )

        result = service.check(request, trace_id="request-trace")

        self.assertEqual(1, result.replayed_count)
        # 初始批量读取之后，生产恢复适配器只需按原 TaskId 重读一次；Application
        # 直接复用该权威快照，不再制造第三次重复读取。
        self.assertEqual([before.task_id], reader.by_id_calls)


if __name__ == "__main__":
    unittest.main()
