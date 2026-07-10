"""Offline tests for the internal file-chat event ledger."""

from __future__ import annotations

import tempfile
import unittest

from app.services.chat import (
    ChatRunEventRepository,
    ChatStore,
    ChatStreamEvent,
)


class ChatRunEventRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tempdir.__enter__()
        self.store = ChatStore(f"{self.tmp}/chat.sqlite3")
        self.store.sessions.create_or_get(chat_id="chat-events")
        self.store.runs.create(run_id="run-events", chat_id="chat-events")
        self.store.runs.mark_running("run-events")

    def tearDown(self) -> None:
        self._tempdir.__exit__(None, None, None)

    def test_events_are_persisted_in_order_with_original_payload(self) -> None:
        first = self.store.events.append(
            run_id="run-events",
            event=ChatStreamEvent("chatInfo", {"chatId": "chat-events", "isNewChat": True}),
        )
        second = self.store.events.append(
            run_id="run-events",
            event=ChatStreamEvent("textChunk", {"content": "第一段"}),
        )
        stored = self.store.events.list_by_run("run-events")

        self.assertEqual(1, first.event_seq)
        self.assertEqual(2, second.event_seq)
        self.assertEqual(
            ["chatInfo", "textChunk"],
            [event.event_type for event in stored],
        )
        self.assertEqual({"content": "第一段"}, dict(stored[1].data))

    def test_only_one_terminal_event_and_no_events_after_it_are_allowed(self) -> None:
        self.store.events.append(
            run_id="run-events",
            event=ChatStreamEvent("done", {"chatId": "chat-events"}),
        )

        with self.assertRaisesRegex(ValueError, "terminal"):
            self.store.events.append(
                run_id="run-events",
                event=ChatStreamEvent("error", {"error": "late"}),
            )
        with self.assertRaisesRegex(ValueError, "terminal"):
            self.store.events.append(
                run_id="run-events",
                event=ChatStreamEvent("textChunk", {"content": "late"}),
            )

    def test_unknown_run_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "chat_run"):
            self.store.events.append(
                run_id="missing-run",
                event=ChatStreamEvent("chatInfo", {"chatId": "missing"}),
            )

    def test_repository_implements_internal_event_store_contract(self) -> None:
        self.assertIsInstance(self.store.events, ChatRunEventRepository)


if __name__ == "__main__":
    unittest.main()
