"""持久化 ID 文件对话执行调度器的离线测试。"""

from __future__ import annotations

import unittest

from app.services.chat import (
    ChatRunDispatcher,
    ChatStreamEvent,
    InlineChatRunDispatcher,
)


class ChatRunDispatcherTests(unittest.TestCase):
    def test_inline_dispatcher_delegates_only_the_durable_run_id(self) -> None:
        received: list[str] = []

        def execute(run_id: str):
            received.append(run_id)
            yield ChatStreamEvent(
                "chatInfo",
                {"chatId": "chat-dispatch", "isNewChat": True},
            )
            yield ChatStreamEvent("done", {"chatId": "chat-dispatch"})

        dispatcher = InlineChatRunDispatcher(execute=execute)

        self.assertIsInstance(dispatcher, ChatRunDispatcher)
        self.assertEqual(
            ["chatInfo", "done"],
            [
                event.event_type
                for event in dispatcher.dispatch(run_id="run-dispatch")
            ],
        )
        self.assertEqual(["run-dispatch"], received)
        self.assertTrue(dispatcher.capabilities.supports_single_instance)
        self.assertFalse(dispatcher.capabilities.reliable_delivery)


if __name__ == "__main__":
    unittest.main()
