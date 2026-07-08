"""Tests for aborting active file-chat runs."""

from __future__ import annotations

import tempfile
import unittest

from app.services.chat import (
    ChatAbortService,
    ChatCommandService,
    ChatRunLockService,
    ChatStreamEvent,
    ChatStore,
    RUN_SUCCEEDED,
)


class ChatAbortServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tempdir.__enter__()
        self.db_path = f"{self.tmp}/chat.sqlite3"
        self.store = ChatStore(self.db_path)
        self.commands = ChatCommandService(ChatRunLockService(self.db_path))
        self.abort = ChatAbortService(
            store=self.store,
            chat_commands=self.commands,
        )

    def tearDown(self) -> None:
        self._tempdir.__exit__(None, None, None)

    def test_no_active_run_returns_false(self) -> None:
        result = self.abort.abort_chat(chat_id="chat-empty")

        self.assertEqual(
            {
                "chatId": "chat-empty",
                "aborted": False,
                "msg": "当前无进行中的流式响应",
            },
            result.to_response(),
        )

    def test_active_run_sets_abort_requested(self) -> None:
        run = self.commands.start_chat_run(chat_id="chat-active")

        result = self.abort.abort_chat(chat_id="chat-active")
        stored = self.store.runs.get(run.run_id)

        self.assertTrue(result.aborted)
        self.assertEqual(run.run_id, result.run_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertTrue(stored.abort_requested)

    def test_repeated_abort_is_idempotent_while_run_is_active(self) -> None:
        run = self.commands.start_chat_run(chat_id="chat-repeat")

        first = self.abort.abort_chat(chat_id="chat-repeat")
        second = self.abort.abort_chat(chat_id="chat-repeat")

        self.assertTrue(first.aborted)
        self.assertTrue(second.aborted)
        self.assertEqual(run.run_id, second.run_id)

    def test_completed_run_returns_false(self) -> None:
        run = self.commands.start_chat_run(chat_id="chat-done")
        completed = self.commands.complete_chat_run(run_id=run.run_id)

        result = self.abort.abort_chat(chat_id="chat-done")

        self.assertEqual(RUN_SUCCEEDED, completed.status)
        self.assertFalse(result.aborted)
        self.assertEqual("当前无进行中的流式响应", result.msg)

    def test_build_abort_signal_returns_domain_event(self) -> None:
        self.assertEqual(
            ChatStreamEvent("aborted", {"chatId": "chat-signal"}),
            ChatAbortService.build_abort_signal(chat_id=" chat-signal "),
        )


if __name__ == "__main__":
    unittest.main()
