"""Offline tests for the protocol-transparent chat execution dispatcher."""

from __future__ import annotations

import unittest

from app.services.chat import (
    ChatRunDispatcher,
    ChatRunExecutionLease,
    ChatRunStreamRequest,
    ChatStreamEvent,
    InlineChatRunDispatcher,
)


class ChatRunDispatcherTests(unittest.TestCase):
    def test_inline_dispatcher_delegates_the_internal_execution_lease(self) -> None:
        request = ChatRunStreamRequest(
            run_id="run-dispatch",
            chat_id="chat-dispatch",
            message="请总结",
        )
        lease = ChatRunExecutionLease(request=request)
        received: list[ChatRunExecutionLease] = []

        def execute(current: ChatRunExecutionLease):
            received.append(current)
            yield ChatStreamEvent("chatInfo", {"chatId": current.chat_id, "isNewChat": True})
            yield ChatStreamEvent("done", {"chatId": current.chat_id})

        dispatcher = InlineChatRunDispatcher(execute=execute)

        self.assertIsInstance(dispatcher, ChatRunDispatcher)
        self.assertEqual(
            ["chatInfo", "done"],
            [event.event_type for event in dispatcher.dispatch(lease)],
        )
        self.assertEqual([lease], received)
        self.assertEqual("run-dispatch", lease.run_id)
        self.assertEqual("chat-dispatch", lease.chat_id)


if __name__ == "__main__":
    unittest.main()
