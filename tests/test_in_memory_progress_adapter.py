"""阶段 1B-2 线程安全 Progress Hub/Adapter 离线并发测试。"""

from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from app.modules.tasks.adapters import InMemoryProgressAdapter
from app.modules.tasks.application import ProgressDeliveryBuffer
from app.modules.tasks.domain import ProgressKey
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


if __name__ == "__main__":
    unittest.main()
