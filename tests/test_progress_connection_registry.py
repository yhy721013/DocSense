"""阶段 1B-2 Progress 连接 Registry 生命周期测试。"""

from __future__ import annotations

import unittest

from app.adapters.web.flask import ProgressConnectionRegistry
from app.modules.tasks.application import (
    ProgressSubscriptionReleaseError,
    ProgressSubscriptionService,
)
from app.modules.tasks.domain import ProgressKey, ProgressSubscriptionRequest
from tests.fakes import (
    FakeProgressSnapshotPort,
    FakeProgressSubscriptionPort,
    FakeTaskReadPort,
)


class ProgressConnectionRegistryTests(unittest.TestCase):
    @staticmethod
    def _service():
        subscriptions = FakeProgressSubscriptionPort()
        service = ProgressSubscriptionService(
            progress_snapshots=FakeProgressSnapshotPort(),
            progress_subscriptions=subscriptions,
            task_reader=FakeTaskReadPort(),
        )
        return service, subscriptions

    def test_result_tokens_are_registered_before_release_and_success_clears_all(self) -> None:
        service, subscriptions = self._service()
        registry = ProgressConnectionRegistry(connection_id="connection-success")
        result = service.subscribe(
            ProgressSubscriptionRequest((ProgressKey("file", "a.pdf"),)),
            delivery=registry.delivery,
        )

        registry.register_result(result)
        result.complete_initial_delivery()
        remaining = registry.close_and_release(service)

        self.assertEqual((), remaining)
        self.assertEqual(0, registry.active_count)
        self.assertEqual((), subscriptions.active_subscriptions)
        self.assertTrue(registry.delivery.closed)

    def test_failed_release_remains_registered_and_can_be_retried(self) -> None:
        service, subscriptions = self._service()
        registry = ProgressConnectionRegistry(connection_id="connection-retry")
        result = service.subscribe(
            ProgressSubscriptionRequest((ProgressKey("file", "retry.pdf"),)),
            delivery=registry.delivery,
        )
        registry.register_result(result)
        result.complete_initial_delivery()
        token = result.active_subscriptions[0]
        subscriptions.unsubscribe_errors[token.subscription_id] = RuntimeError(
            "temporary unsubscribe failure"
        )

        with self.assertRaises(ProgressSubscriptionReleaseError):
            registry.release_once(service)

        self.assertEqual((token,), registry.subscriptions)
        subscriptions.unsubscribe_errors.clear()
        registry.release_once(service)

        self.assertEqual((), registry.subscriptions)
        self.assertEqual((), subscriptions.active_subscriptions)

    def test_close_retries_only_failed_tokens_and_eventually_succeeds(self) -> None:
        service, subscriptions = self._service()
        registry = ProgressConnectionRegistry(connection_id="connection-auto-retry")
        result = service.subscribe(
            ProgressSubscriptionRequest(
                (
                    ProgressKey("file", "first.pdf"),
                    ProgressKey("file", "second.pdf"),
                )
            ),
            delivery=registry.delivery,
        )
        registry.register_result(result)
        result.complete_initial_delivery()
        first, second = result.active_subscriptions

        attempts = {first.subscription_id: 0}
        original_unsubscribe = subscriptions.unsubscribe

        def fail_once(token) -> None:
            if token.subscription_id == first.subscription_id:
                attempts[first.subscription_id] += 1
                if attempts[first.subscription_id] == 1:
                    raise RuntimeError("first attempt fails")
            original_unsubscribe(token)

        subscriptions.unsubscribe = fail_once  # type: ignore[method-assign]
        remaining = registry.close_and_release(service, max_attempts=3)

        self.assertEqual((), remaining)
        self.assertEqual(2, attempts[first.subscription_id])
        self.assertEqual(1, subscriptions.unsubscribe_calls.count(second.subscription_id))
        self.assertEqual((), subscriptions.active_subscriptions)

    def test_other_connection_token_is_rejected(self) -> None:
        service, _ = self._service()
        first = ProgressConnectionRegistry(connection_id="first")
        second = ProgressConnectionRegistry(connection_id="second")
        result = service.subscribe(
            ProgressSubscriptionRequest((ProgressKey("file", "owner.pdf"),)),
            delivery=first.delivery,
        )

        with self.assertRaisesRegex(ValueError, "其他连接"):
            second.retain(result.active_subscriptions)

        result.abort_initial_delivery()
        service.release(result.active_subscriptions)


if __name__ == "__main__":
    unittest.main()
