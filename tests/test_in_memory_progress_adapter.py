"""阶段 1B-2 线程安全 Progress Hub/Adapter 离线并发测试。"""

from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

from app.modules.tasks.adapters import (
    InMemoryProgressAdapter,
    LatestTaskProgressPublisherAdapter,
)
from app.modules.tasks.application import ProgressDeliveryBuffer
from app.modules.tasks.domain import ProgressKey, TaskBusinessRef, TaskId
from app.modules.tasks.ports import ProgressPublication, TaskCommandPort
from app.services.core.progress_hub import LLMProgressHub


def _payload(key: ProgressKey, progress: float) -> dict[str, object]:
    key_field = "fileName" if key.business_type == "file" else "reportId"
    key_value: object = key.business_key
    if key.business_type == "report":
        key_value = int(key.business_key)
    return {
        "businessType": key.business_type,
        "data": {key_field: key_value, "progress": progress},
    }


class InMemoryProgressAdapterConcurrencyTests(unittest.TestCase):
    def test_fifty_distinct_keys_can_subscribe_and_publish_concurrently(self) -> None:
        hub = LLMProgressHub()
        adapter = InMemoryProgressAdapter(hub)
        keys = tuple(ProgressKey("file", f"concurrent-{i}.pdf") for i in range(50))
        received: dict[ProgressKey, list[object]] = {key: [] for key in keys}
        received_lock = threading.Lock()
        subscribe_barrier = threading.Barrier(len(keys))

        def subscribe(key: ProgressKey):
            # 50 个工作线程必须全部到齐后再进入 Hub，防止快速任务被线程池以较低
            # 实际并发度串行复用，从而产生“50 并发”假阳性。
            subscribe_barrier.wait(timeout=10)

            def record(snapshot) -> None:
                with received_lock:
                    received[key].append(snapshot)

            return adapter.subscribe(key, record, delivery_id=f"delivery-{key.business_key}")

        with ThreadPoolExecutor(max_workers=50) as executor:
            subscriptions = tuple(executor.map(subscribe, keys))

        publish_barrier = threading.Barrier(len(keys))

        def publish(index_and_key: tuple[int, ProgressKey]) -> None:
            index, key = index_and_key
            publish_barrier.wait(timeout=10)
            # 人为加入浮点伪影，验证并发路径和 latest 都经过统一归一化。
            hub.publish(
                key.business_type,
                key.business_key,
                _payload(key, 0.28000000004 + index / 1000),
                task_id=f"task-{index}",
            )

        with ThreadPoolExecutor(max_workers=50) as executor:
            tuple(executor.map(publish, enumerate(keys)))

        for index, key in enumerate(keys):
            with self.subTest(key=key.business_key):
                self.assertEqual(1, len(received[key]))
                snapshot = received[key][0]
                self.assertEqual(key, snapshot.key)
                self.assertEqual(f"task-{index}", snapshot.task_id.value)
                self.assertEqual(round(0.28 + index / 1000, 4), snapshot.progress)
                self.assertEqual(snapshot, adapter.get_latest(key))

        with ThreadPoolExecutor(max_workers=50) as executor:
            tuple(executor.map(adapter.unsubscribe, subscriptions))
        # 重复释放是连接 finally 和异常路径的必要幂等保证。
        with ThreadPoolExecutor(max_workers=50) as executor:
            tuple(executor.map(adapter.unsubscribe, subscriptions))
        self.assertEqual(0, adapter.active_subscription_count)

    def test_delayed_hub_callback_cannot_requeue_older_same_task_sequence(self) -> None:
        """覆盖 Hub 锁外通知真实乱序，而不只单测连接缓冲的方法调用顺序。"""

        hub = LLMProgressHub()
        adapter = InMemoryProgressAdapter(hub)
        key = ProgressKey("file", "out-of-order-callback.pdf")
        delivery = ProgressDeliveryBuffer(
            delivery_id="out-of-order-connection",
            capacity=8,
        )
        old_callback_started = threading.Event()
        release_old_callback = threading.Event()

        def delayed_publish(snapshot) -> None:
            if snapshot.sequence_no == 1:
                old_callback_started.set()
                release_old_callback.wait(timeout=5)
            delivery.publish(snapshot)

        subscription = adapter.subscribe(
            key,
            delayed_publish,
            delivery_id=delivery.delivery_id,
        )

        old_thread = threading.Thread(
            target=hub.publish,
            args=(
                key.business_type,
                key.business_key,
                _payload(key, 0.2),
            ),
            kwargs={"task_id": "task-shared"},
            daemon=True,
        )
        try:
            old_thread.start()
            self.assertTrue(old_callback_started.wait(timeout=5))

            # seq=2 的 Hub 发布在 seq=1 subscriber 仍阻塞时完成，先进入并被连接取走。
            hub.publish(
                key.business_type,
                key.business_key,
                _payload(key, 0.8),
                task_id="task-shared",
            )
            newer = delivery.drain()
            self.assertEqual(1, len(newer))
            self.assertEqual(2, newer[0].sequence_no)

            release_old_callback.set()
            old_thread.join(timeout=5)
            self.assertFalse(old_thread.is_alive())
            self.assertEqual((), delivery.drain())
        finally:
            release_old_callback.set()
            old_thread.join(timeout=5)
            adapter.unsubscribe(subscription)
            delivery.close()

    def test_same_key_subscriber_failure_isolated_from_other_subscriber(self) -> None:
        hub = LLMProgressHub()
        adapter = InMemoryProgressAdapter(hub)
        key = ProgressKey("file", "shared.pdf")
        received = []

        def broken(_snapshot) -> None:
            raise RuntimeError("subscriber failed")

        first = adapter.subscribe(key, broken, delivery_id="broken-connection")
        second = adapter.subscribe(key, received.append, delivery_id="healthy-connection")

        with self.assertLogs(
            "app.modules.tasks.adapters.in_memory_progress",
            level="ERROR",
        ):
            hub.publish("file", "shared.pdf", _payload(key, 0.5), task_id="task-shared")

        self.assertEqual(1, len(received))
        self.assertEqual(0.5, received[0].progress)
        adapter.unsubscribe(first)
        adapter.unsubscribe(second)

    def test_publish_and_unsubscribe_race_leaves_no_active_callback(self) -> None:
        hub = LLMProgressHub()
        adapter = InMemoryProgressAdapter(hub)
        key = ProgressKey("file", "race.pdf")
        received = []
        subscription = adapter.subscribe(key, received.append, delivery_id="race-connection")
        barrier = threading.Barrier(2)

        def publisher() -> None:
            barrier.wait()
            for index in range(100):
                hub.publish(
                    "file",
                    "race.pdf",
                    _payload(key, index / 100),
                    task_id="task-race",
                )

        def releaser() -> None:
            barrier.wait()
            adapter.unsubscribe(subscription)

        first = threading.Thread(target=publisher)
        second = threading.Thread(target=releaser)
        first.start()
        second.start()
        first.join(timeout=5)
        second.join(timeout=5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        count_after_release = len(received)

        hub.publish("file", "race.pdf", _payload(key, 1.0), task_id="task-race")

        self.assertEqual(count_after_release, len(received))
        self.assertEqual(0, adapter.active_subscription_count)

    def test_slow_callback_does_not_hold_hub_lock_or_block_other_key(self) -> None:
        hub = LLMProgressHub()
        entered = threading.Event()
        release = threading.Event()

        def slow_callback(_message) -> None:
            entered.set()
            release.wait(timeout=5)

        hub.subscribe("file", "slow.pdf", slow_callback)
        worker = threading.Thread(
            target=hub.publish,
            args=(
                "file",
                "slow.pdf",
                {
                    "businessType": "file",
                    "data": {"fileName": "slow.pdf", "progress": 0.2},
                },
            ),
        )
        worker.start()
        self.assertTrue(entered.wait(timeout=2))

        started = time.monotonic()
        hub.publish(
            "file",
            "other.pdf",
            {
                "businessType": "file",
                "data": {"fileName": "other.pdf", "progress": 0.4},
            },
        )
        elapsed = time.monotonic() - started
        release.set()
        worker.join(timeout=5)

        self.assertLess(elapsed, 0.5)
        self.assertEqual(0.4, hub.get_latest("file", "other.pdf")["data"]["progress"])

    def test_slow_persistent_guard_does_not_block_an_unrelated_key(self) -> None:
        """未来 Repository 变慢时，只能串行同一业务键，不能冻结全部 Progress。"""

        hub = LLMProgressHub()
        adapter = InMemoryProgressAdapter(hub)
        guard_entered = threading.Event()
        release_guard = threading.Event()
        guarded_key = ProgressKey("file", "guarded.pdf")
        other_key = ProgressKey("file", "other-guard.pdf")

        def slow_guard() -> bool:
            guard_entered.set()
            if not release_guard.wait(timeout=5):
                raise TimeoutError("测试未释放持久化 Guard")
            return True

        worker = threading.Thread(
            target=adapter.publish_guarded,
            args=(
                ProgressPublication(
                    key=guarded_key,
                    expected_task_id=TaskId("guarded-task"),
                    progress=0.1,
                    message="",
                    internal_state="accepted",
                ),
            ),
            kwargs={"is_current": slow_guard},
            daemon=True,
        )
        worker.start()
        self.assertTrue(guard_entered.wait(timeout=2))

        started = time.monotonic()
        adapter.publish(
            ProgressPublication(
                key=other_key,
                expected_task_id=TaskId("other-task"),
                progress=0.2,
                message="",
                internal_state="accepted",
            )
        )
        elapsed = time.monotonic() - started
        release_guard.set()
        worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertLess(elapsed, 0.5)
        latest = adapter.get_latest(other_key)
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(0.2, latest.progress)

    def test_legacy_subscribers_receive_isolated_payload_copies(self) -> None:
        hub = LLMProgressHub()
        observed = []

        def mutating(message) -> None:
            message["data"]["progress"] = 1.0

        hub.subscribe("file", "copy.pdf", mutating)
        hub.subscribe("file", "copy.pdf", observed.append)
        hub.publish(
            "file",
            "copy.pdf",
            {
                "businessType": "file",
                "data": {"fileName": "copy.pdf", "progress": 0.25},
            },
        )

        self.assertEqual(0.25, observed[0]["data"]["progress"])
        self.assertEqual(0.25, hub.get_latest("file", "copy.pdf")["data"]["progress"])

    def test_new_task_id_resets_internal_sequence(self) -> None:
        hub = LLMProgressHub()
        adapter = InMemoryProgressAdapter(hub)
        key = ProgressKey("file", "rerun.pdf")

        hub.publish("file", "rerun.pdf", _payload(key, 0.2), task_id="task-old")
        hub.publish("file", "rerun.pdf", _payload(key, 0.4))
        old = adapter.get_latest(key)
        hub.publish("file", "rerun.pdf", _payload(key, 0.0), task_id="task-new")
        new = adapter.get_latest(key)

        self.assertEqual(2, old.sequence_no)
        self.assertEqual("task-old", old.task_id.value)
        self.assertEqual(1, new.sequence_no)
        self.assertEqual("task-new", new.task_id.value)

    def test_typed_publisher_rejects_old_task_after_new_acceptance(self) -> None:
        """旧执行的迟到终态不得覆盖新任务已经发布的 accepted 快照。"""

        hub = LLMProgressHub()
        adapter = InMemoryProgressAdapter(hub)
        key = ProgressKey("report", "132")

        adapter.publish(
            ProgressPublication(
                key=key,
                expected_task_id=TaskId("task-old"),
                progress=0.5,
                message="正在生成",
                internal_state="accepted",
            )
        )
        adapter.publish(
            ProgressPublication(
                key=key,
                expected_task_id=TaskId("task-new"),
                progress=0.0,
                message="",
                internal_state="accepted",
            )
        )
        with self.assertLogs(
            "app.modules.tasks.adapters.in_memory_progress",
            level="WARNING",
        ):
            adapter.publish(
                ProgressPublication(
                    key=key,
                    expected_task_id=TaskId("task-old"),
                    progress=1.0,
                    message="迟到完成",
                    internal_state="succeeded",
                )
            )

        latest = adapter.get_latest(key)
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(TaskId("task-new"), latest.task_id)
        self.assertEqual(0.0, latest.progress)
        self.assertEqual("accepted", latest.internal_state)

        adapter.publish(
            ProgressPublication(
                key=key,
                expected_task_id=TaskId("task-new"),
                progress=0.35,
                message="新任务运行中",
                internal_state="running",
            )
        )
        updated = adapter.get_latest(key)
        self.assertEqual(0.35, updated.progress)
        self.assertEqual("running", updated.internal_state)

    def test_persistent_latest_guard_blocks_delayed_old_accepted_publication(self) -> None:
        """即使旧 accepted 在线程调度后迟到，也必须先服从 SQLite latest owner。"""

        hub = LLMProgressHub()
        delegate = InMemoryProgressAdapter(hub)
        commands = MagicMock(spec=TaskCommandPort)
        commands.is_latest.side_effect = lambda task_id, _ref: (
            task_id == TaskId("task-new")
        )
        publisher = LatestTaskProgressPublisherAdapter(
            task_commands=commands,
            delegate=delegate,
        )
        key = ProgressKey("report", "132")

        with self.assertLogs(
            "app.modules.tasks.adapters.latest_progress",
            level="WARNING",
        ):
            publisher.publish(
                ProgressPublication(
                    key=key,
                    expected_task_id=TaskId("task-old"),
                    progress=0.0,
                    message="",
                    internal_state="accepted",
                )
            )
        self.assertIsNone(delegate.get_latest(key))

        publisher.publish(
            ProgressPublication(
                key=key,
                expected_task_id=TaskId("task-new"),
                progress=0.0,
                message="",
                internal_state="accepted",
            )
        )
        latest = delegate.get_latest(key)
        self.assertEqual(TaskId("task-new"), latest.task_id)

    def test_persistent_latest_guard_runs_inside_atomic_hub_publication(self) -> None:
        """Guard 拒绝时 Hub 必须保持为空，不能先写旧 accepted 再做补偿。"""

        hub = LLMProgressHub()
        delegate = InMemoryProgressAdapter(hub)
        commands = MagicMock(spec=TaskCommandPort)
        commands.is_latest.return_value = False
        publisher = LatestTaskProgressPublisherAdapter(
            task_commands=commands,
            delegate=delegate,
        )
        key = ProgressKey("report", "132")

        with self.assertLogs(
            "app.services.core.progress_hub",
            level="WARNING",
        ) as captured:
            publisher.publish(
                ProgressPublication(
                    key=key,
                    expected_task_id=TaskId("task-old"),
                    progress=0.0,
                    message="",
                    internal_state="accepted",
                )
            )

        self.assertIsNone(delegate.get_latest(key))
        commands.is_latest.assert_called_once_with(
            TaskId("task-old"),
            TaskBusinessRef("report", "132"),
        )
        self.assertTrue(
            any("persistent_owner_changed" in item for item in captured.output)
        )

    def test_latest_publisher_exposes_guarded_port_for_analysis_worker(self) -> None:
        """Analysis 可复用同一 latest Adapter，且调用方 Guard 仍先于持久化查询。"""

        hub = LLMProgressHub()
        delegate = InMemoryProgressAdapter(hub)
        commands = MagicMock(spec=TaskCommandPort)
        commands.is_latest.return_value = True
        publisher = LatestTaskProgressPublisherAdapter(
            task_commands=commands,
            delegate=delegate,
        )
        key = ProgressKey("file", "analysis-001.txt")
        publication = ProgressPublication(
            key=key,
            expected_task_id=TaskId("analysis-progress-task-1"),
            progress=0.1,
            message="开始处理",
            internal_state="accepted",
        )

        caller_guard = MagicMock(return_value=False)
        self.assertFalse(
            publisher.publish_guarded(
                publication,
                is_current=caller_guard,
            )
        )
        caller_guard.assert_called_once_with()
        commands.is_latest.assert_not_called()
        self.assertIsNone(delegate.get_latest(key))

        caller_guard = MagicMock(return_value=True)
        self.assertTrue(
            publisher.publish_guarded(
                publication,
                is_current=caller_guard,
            )
        )
        commands.is_latest.assert_called_once_with(
            TaskId("analysis-progress-task-1"),
            TaskBusinessRef("file", "analysis-001.txt"),
        )
        self.assertEqual(TaskId("analysis-progress-task-1"), delegate.get_latest(key).task_id)


if __name__ == "__main__":
    unittest.main()
