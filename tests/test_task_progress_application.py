"""阶段 1A-3：Progress 应用服务的框架无关契约测试。

测试验证快照优先级、Task Read 回退、顺序、重复 key 去重订阅、补偿释放与幂等
清理。subscriber 只接收类型化 ``ProgressSnapshot``，不会构造或发送 WebSocket 帧。
"""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from app.modules.tasks.application import (
    ProgressDeliveryBuffer,
    ProgressInitialBatchStateError,
    ProgressPortContractError,
    ProgressSnapshotSource,
    ProgressSubscriptionReleaseError,
    ProgressSubscriptionRollbackError,
    ProgressSubscriptionService,
)
from app.modules.tasks.domain import (
    CALLBACK_PENDING,
    ProgressKey,
    ProgressSnapshot,
    ProgressSubscriptionRequest,
    TaskBusinessRef,
    TaskId,
    TaskSnapshot,
)
from app.modules.tasks.ports import ProgressSubscription
from tests.fakes import (
    FakeProgressSnapshotPort,
    FakeProgressSubscriptionPort,
    FakeTaskReadPort,
)


def _task_snapshot(key: ProgressKey, *, progress: float = 0.25) -> TaskSnapshot:
    return TaskSnapshot(
        task_id=TaskId(f"task-{key.business_type}-{key.business_key}"),
        task_type=f"{key.business_type}_task",
        business_ref=key.business_ref,
        execution_state="running",
        public_status="1",
        progress=progress,
        message="任务投影",
        callback_status=CALLBACK_PENDING,
        created_at="2026-07-16T10:00:00+08:00",
        updated_at="2026-07-16T10:01:00+08:00",
    )


def _progress_snapshot(
    key: ProgressKey,
    *,
    progress: float = 0.8,
    sequence_no: int = 2,
) -> ProgressSnapshot:
    return ProgressSnapshot(
        key=key,
        task_id=TaskId(f"task-{key.business_type}-{key.business_key}"),
        progress=progress,
        message="通知层快照",
        internal_state="running",
        sequence_no=sequence_no,
        updated_at="2026-07-16T10:02:00+08:00",
    )


def _delivery(*, capacity: int = 16) -> ProgressDeliveryBuffer:
    return ProgressDeliveryBuffer(
        delivery_id="test-progress-connection",
        capacity=capacity,
    )


class ProgressDomainContractTests(unittest.TestCase):
    """验证 Progress DTO 不接受空请求、越界进度或运行期修改。"""

    def test_subscription_request_requires_typed_non_empty_keys(self) -> None:
        with self.assertRaises(ValueError):
            ProgressSubscriptionRequest(())
        with self.assertRaises(TypeError):
            ProgressSubscriptionRequest(("file:a.pdf",))  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            ProgressSubscriptionRequest(
                (ProgressKey("file", "a.pdf"), ProgressKey("report", "132"))
            )

    def test_progress_snapshot_is_immutable_and_normalized(self) -> None:
        key = ProgressKey("file", "normalized.pdf")
        snapshot = _progress_snapshot(key, progress=0.28000000004)

        self.assertEqual(0.28, snapshot.progress)
        with self.assertRaises(FrozenInstanceError):
            snapshot.progress = 0.5  # type: ignore[misc]
        with self.assertRaises(ValueError):
            _progress_snapshot(key, progress=1.1)


class ProgressSubscriptionServiceTests(unittest.TestCase):
    """通过三个独立 Fake Port 验证 Progress 应用编排。"""

    def _service(
        self,
        *,
        tasks: tuple[TaskSnapshot, ...] = (),
        progress: tuple[ProgressSnapshot, ...] = (),
    ) -> tuple[
        ProgressSubscriptionService,
        FakeTaskReadPort,
        FakeProgressSnapshotPort,
        FakeProgressSubscriptionPort,
    ]:
        task_reader = FakeTaskReadPort(tasks)
        snapshots = FakeProgressSnapshotPort(progress)
        subscriptions = FakeProgressSubscriptionPort()
        service = ProgressSubscriptionService(
            progress_snapshots=snapshots,
            progress_subscriptions=subscriptions,
            task_reader=task_reader,
        )
        return service, task_reader, snapshots, subscriptions

    def test_selects_progress_then_task_then_missing_and_preserves_order(self) -> None:
        latest_key = ProgressKey("file", "latest.pdf")
        task_key = ProgressKey("file", "task-only.pdf")
        missing_key = ProgressKey("file", "missing.pdf")
        service, task_reader, snapshots, subscriptions = self._service(
            tasks=(_task_snapshot(task_key, progress=0.35),),
            progress=(_progress_snapshot(latest_key, progress=0.85),),
        )
        delivery = _delivery()
        request = ProgressSubscriptionRequest(
            (latest_key, task_key, missing_key, latest_key)
        )

        result = service.subscribe(
            request,
            delivery=delivery,
            connection_id="connection-1",
        )

        self.assertEqual(
            [latest_key, task_key, missing_key, latest_key],
            [item.key for item in result.current_items],
        )
        self.assertEqual(
            [
                ProgressSnapshotSource.PROGRESS,
                ProgressSnapshotSource.TASK,
                ProgressSnapshotSource.MISSING,
                ProgressSnapshotSource.PROGRESS,
            ],
            [item.source for item in result.current_items],
        )
        self.assertEqual([True, True, False, True], [item.exists for item in result.current_items])
        self.assertEqual(0.85, result.current_items[0].snapshot.progress)
        self.assertEqual(0.35, result.current_items[1].snapshot.progress)
        self.assertEqual(0, result.current_items[1].snapshot.sequence_no)
        self.assertEqual(
            [latest_key, task_key, missing_key],
            subscriptions.subscribe_calls,
        )
        self.assertEqual(3, len(result.added_subscriptions))
        self.assertEqual(3, len(result.active_subscriptions))
        self.assertEqual([latest_key, task_key, missing_key, latest_key], snapshots.calls)
        self.assertEqual(
            [task_key.business_ref, missing_key.business_ref],
            task_reader.latest_calls,
        )
        self.assertTrue(delivery.buffering_initial_batch)
        self.assertEqual((), delivery.drain())
        result.complete_initial_delivery()
        self.assertFalse(delivery.buffering_initial_batch)
        self.assertEqual((), delivery.drain())

    def test_existing_subscription_is_reused_without_duplicate_registration(self) -> None:
        first_key = ProgressKey("file", "first.pdf")
        second_key = ProgressKey("file", "second.pdf")
        service, _, _, subscriptions = self._service()
        delivery = _delivery()
        first = service.subscribe(
            ProgressSubscriptionRequest((first_key,)),
            delivery=delivery,
        )
        first.complete_initial_delivery()
        subscriptions.subscribe_calls.clear()

        second = service.subscribe(
            ProgressSubscriptionRequest((first_key, second_key)),
            delivery=delivery,
            existing_subscriptions=first.active_subscriptions,
        )
        second.complete_initial_delivery()

        self.assertEqual([second_key], subscriptions.subscribe_calls)
        self.assertEqual(2, len(second.active_subscriptions))
        self.assertEqual(1, len(second.added_subscriptions))

    def test_release_can_be_repeated_and_deduplicates_same_token(self) -> None:
        key = ProgressKey("file", "close.pdf")
        service, _, _, subscriptions = self._service()
        delivery = _delivery()
        result = service.subscribe(
            ProgressSubscriptionRequest((key,)),
            delivery=delivery,
        )
        result.complete_initial_delivery()
        token = result.active_subscriptions[0]

        service.release((token, token), connection_id="connection-close")
        service.release((token,), connection_id="connection-close")

        self.assertEqual((), subscriptions.active_subscriptions)
        self.assertEqual(
            [token.subscription_id, token.subscription_id],
            subscriptions.unsubscribe_calls,
        )

    def test_later_subscribe_failure_rolls_back_earlier_new_token(self) -> None:
        first_key = ProgressKey("file", "first.pdf")
        second_key = ProgressKey("file", "second.pdf")
        service, _, _, subscriptions = self._service()
        delivery = _delivery()
        subscriptions.subscribe_errors[second_key] = RuntimeError("subscribe failed")

        with self.assertLogs(
            "app.modules.tasks.application.progress",
            level="ERROR",
        ):
            with self.assertRaisesRegex(RuntimeError, "subscribe failed"):
                service.subscribe(
                    ProgressSubscriptionRequest((first_key, second_key)),
                    delivery=delivery,
                )

        self.assertEqual((), subscriptions.active_subscriptions)
        self.assertEqual(1, len(subscriptions.unsubscribe_calls))
        self.assertFalse(delivery.buffering_initial_batch)

    def test_snapshot_failure_rolls_back_every_new_token(self) -> None:
        first_key = ProgressKey("file", "first.pdf")
        second_key = ProgressKey("file", "second.pdf")
        service, _, snapshots, subscriptions = self._service()
        delivery = _delivery()
        snapshots.errors[first_key] = RuntimeError("snapshot failed")

        with self.assertLogs(
            "app.modules.tasks.application.progress",
            level="ERROR",
        ):
            with self.assertRaisesRegex(RuntimeError, "snapshot failed"):
                service.subscribe(
                    ProgressSubscriptionRequest((first_key, second_key)),
                    delivery=delivery,
                )

        self.assertEqual((), subscriptions.active_subscriptions)
        self.assertEqual(2, len(subscriptions.unsubscribe_calls))
        self.assertFalse(delivery.buffering_initial_batch)

    def test_rollback_failure_exposes_tokens_for_registry_retry(self) -> None:
        first_key = ProgressKey("file", "first.pdf")
        second_key = ProgressKey("file", "second.pdf")
        service, _, _, subscriptions = self._service()
        delivery = _delivery()
        subscriptions.subscribe_errors[second_key] = RuntimeError("subscribe failed")
        subscriptions.unsubscribe_errors["fake-progress-1"] = RuntimeError(
            "rollback failed"
        )

        with self.assertLogs(
            "app.modules.tasks.application.progress",
            level="ERROR",
        ):
            with self.assertRaises(ProgressSubscriptionRollbackError) as context:
                service.subscribe(
                    ProgressSubscriptionRequest((first_key, second_key)),
                    delivery=delivery,
                )

        error = context.exception
        self.assertIsInstance(error.original_error, RuntimeError)
        self.assertEqual(("fake-progress-1",), error.failed_subscription_ids)
        self.assertEqual(error.failed_subscriptions, subscriptions.active_subscriptions)

        # Registry 保留异常携带的完整令牌后，可以在底层故障恢复时定向重试。
        subscriptions.unsubscribe_errors.clear()
        service.release(error.failed_subscriptions)
        self.assertEqual((), subscriptions.active_subscriptions)

    def test_subscription_port_must_return_token_for_requested_key(self) -> None:
        requested_key = ProgressKey("file", "requested.pdf")
        wrong_key = ProgressKey("file", "wrong.pdf")
        service, _, _, subscriptions = self._service()
        delivery = _delivery()
        subscriptions.forced_subscriptions[requested_key] = ProgressSubscription(
            subscription_id="wrong-key-token",
            key=wrong_key,
            delivery_id=delivery.delivery_id,
        )

        with self.assertLogs(
            "app.modules.tasks.application.progress",
            level="ERROR",
        ):
            with self.assertRaises(ProgressPortContractError):
                service.subscribe(
                    ProgressSubscriptionRequest((requested_key,)),
                    delivery=delivery,
                )

        self.assertEqual((), subscriptions.active_subscriptions)

    def test_snapshot_port_must_return_snapshot_for_requested_key(self) -> None:
        requested_key = ProgressKey("file", "requested.pdf")
        wrong_key = ProgressKey("file", "wrong.pdf")
        service, _, snapshots, subscriptions = self._service()
        delivery = _delivery()
        snapshots.set_for_key(requested_key, _progress_snapshot(wrong_key))

        with self.assertLogs(
            "app.modules.tasks.application.progress",
            level="ERROR",
        ):
            with self.assertRaises(ProgressPortContractError):
                service.subscribe(
                    ProgressSubscriptionRequest((requested_key,)),
                    delivery=delivery,
                )

        self.assertEqual((), subscriptions.active_subscriptions)

    def test_task_fallback_must_match_requested_business_ref(self) -> None:
        requested_key = ProgressKey("file", "requested.pdf")
        wrong_snapshot = _task_snapshot(ProgressKey("file", "wrong.pdf"))
        service, task_reader, _, subscriptions = self._service()
        delivery = _delivery()
        task_reader.set_latest_for_ref(requested_key.business_ref, wrong_snapshot)

        with self.assertLogs(
            "app.modules.tasks.application.progress",
            level="ERROR",
        ):
            with self.assertRaises(ProgressPortContractError):
                service.subscribe(
                    ProgressSubscriptionRequest((requested_key,)),
                    delivery=delivery,
                )

        self.assertEqual((), subscriptions.active_subscriptions)

    def test_release_attempts_remaining_tokens_before_reporting_failure(self) -> None:
        first_key = ProgressKey("file", "first.pdf")
        second_key = ProgressKey("file", "second.pdf")
        service, _, _, subscriptions = self._service()
        delivery = _delivery()
        result = service.subscribe(
            ProgressSubscriptionRequest((first_key, second_key)),
            delivery=delivery,
        )
        result.complete_initial_delivery()
        first_token, second_token = result.active_subscriptions
        subscriptions.unsubscribe_errors[first_token.subscription_id] = RuntimeError(
            "unsubscribe failed"
        )

        with self.assertLogs(
            "app.modules.tasks.application.progress",
            level="ERROR",
        ):
            with self.assertRaises(ProgressSubscriptionReleaseError) as context:
                service.release(result.active_subscriptions)

        self.assertEqual(
            (first_token.subscription_id,),
            context.exception.failed_subscription_ids,
        )
        self.assertEqual(
            (first_token,),
            context.exception.failed_subscriptions,
        )
        self.assertEqual(
            (first_token,),
            subscriptions.active_subscriptions,
        )
        self.assertIn(second_token.subscription_id, subscriptions.unsubscribe_calls)

        subscriptions.unsubscribe_errors.clear()
        service.release(context.exception.failed_subscriptions)
        self.assertEqual((), subscriptions.active_subscriptions)

    def test_fake_publish_delivers_only_typed_snapshot_for_matching_key(self) -> None:
        subscribed_key = ProgressKey("file", "subscribed.pdf")
        other_key = ProgressKey("file", "other.pdf")
        service, _, _, subscriptions = self._service()
        delivery = _delivery()
        result = service.subscribe(
            ProgressSubscriptionRequest((subscribed_key,)),
            delivery=delivery,
        )
        result.complete_initial_delivery()

        subscriptions.publish(_progress_snapshot(other_key))
        expected = _progress_snapshot(subscribed_key, sequence_no=3)
        subscriptions.publish(expected)

        self.assertEqual((expected,), delivery.drain())

    def test_initial_snapshot_barrier_holds_concurrent_newer_notification(self) -> None:
        key = ProgressKey("file", "racing.pdf")
        current = _progress_snapshot(key, sequence_no=2, progress=0.2)
        concurrent = _progress_snapshot(key, sequence_no=3, progress=0.3)
        service, _, snapshots, subscriptions = self._service(progress=(current,))
        delivery = _delivery()
        original_get_latest = snapshots.get_latest

        def get_latest_during_publish(requested_key: ProgressKey) -> ProgressSnapshot | None:
            selected = original_get_latest(requested_key)
            subscriptions.publish(concurrent)
            return selected

        snapshots.get_latest = get_latest_during_publish  # type: ignore[method-assign]

        result = service.subscribe(
            ProgressSubscriptionRequest((key,)),
            delivery=delivery,
        )

        self.assertEqual(2, result.current_items[0].snapshot.sequence_no)
        self.assertEqual((), delivery.drain(), "初始快照发送前不得放行并发通知")
        result.complete_initial_delivery()
        self.assertEqual((concurrent,), delivery.drain())
        with self.assertRaises(ProgressInitialBatchStateError):
            result.complete_initial_delivery()

    def test_repeated_initial_batch_blocks_and_filters_already_queued_stale_item(self) -> None:
        key = ProgressKey("file", "repeat.pdf")
        current = _progress_snapshot(key, sequence_no=5, progress=0.5)
        stale = _progress_snapshot(key, sequence_no=4, progress=0.4)
        service, _, _, subscriptions = self._service(progress=(current,))
        delivery = _delivery()
        first = service.subscribe(
            ProgressSubscriptionRequest((key,)),
            delivery=delivery,
        )
        first.complete_initial_delivery()
        subscriptions.publish(stale)
        self.assertEqual(1, delivery.queued_count)

        second = service.subscribe(
            ProgressSubscriptionRequest((key,)),
            delivery=delivery,
            existing_subscriptions=first.active_subscriptions,
        )

        self.assertEqual((), delivery.drain(), "屏障期间不得取出既有排队通知")
        second.complete_initial_delivery()
        self.assertEqual((), delivery.drain(), "初始快照之后不得发送更旧 sequence")

    def test_slow_connection_buffer_is_bounded_and_keeps_latest_snapshots(self) -> None:
        keys = tuple(ProgressKey("file", f"slow-{index}.pdf") for index in range(8))
        service, _, _, subscriptions = self._service()
        delivery = _delivery(capacity=3)
        result = service.subscribe(
            ProgressSubscriptionRequest(keys),
            delivery=delivery,
        )
        result.complete_initial_delivery()

        # 模拟发送循环完全不消费；发布线程仍只做有界入队，不执行任何网络调用。
        with self.assertLogs(
            "app.modules.tasks.application.progress_delivery",
            level="WARNING",
        ):
            for index, key in enumerate(keys, start=1):
                subscriptions.publish(
                    _progress_snapshot(key, sequence_no=index, progress=index / 10)
                )

        self.assertEqual(3, delivery.queued_count)
        self.assertEqual(5, delivery.dropped_count)
        self.assertEqual(keys[-3:], tuple(item.key for item in delivery.drain()))

    def test_existing_tokens_cannot_switch_to_another_connection_delivery(self) -> None:
        key = ProgressKey("file", "owner.pdf")
        service, _, _, _ = self._service()
        first_delivery = _delivery()
        first = service.subscribe(
            ProgressSubscriptionRequest((key,)),
            delivery=first_delivery,
        )
        first.complete_initial_delivery()
        other_delivery = ProgressDeliveryBuffer(
            delivery_id="other-progress-connection",
            capacity=16,
        )

        with self.assertRaisesRegex(ValueError, "其他连接"):
            service.subscribe(
                ProgressSubscriptionRequest((key,)),
                delivery=other_delivery,
                existing_subscriptions=first.active_subscriptions,
            )


if __name__ == "__main__":
    unittest.main()
