"""活跃文件对话运行中断功能的测试。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
import zlib

from app.modules.chat import (
    ChatAbortService,
    ChatCommandService,
    ChatRunLockService,
    ChatStreamEvent,
    ChatStore,
    RUN_FAILED,
    RUN_SUCCEEDED,
)
from app.modules.chat.domain.identity import FileChatIdentity


def _identity(value: str | int) -> FileChatIdentity:
    """把旧测试标签稳定映射为合法文件对话身份。"""
    if isinstance(value, int) or str(value).isdigit():
        return FileChatIdentity(chat_id=int(value))
    return FileChatIdentity(chat_id=zlib.crc32(str(value).encode("utf-8")) + 1)


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
        result = self.abort.abort_chat(identity=_identity(10001))

        self.assertEqual(_identity(10001), result.identity)
        self.assertFalse(result.aborted)
        self.assertEqual("当前无进行中的流式响应", result.msg)

    def test_active_run_sets_abort_requested(self) -> None:
        identity = _identity("chat-active")
        run = self.commands.start_chat_run(identity=identity)

        result = self.abort.abort_chat(identity=identity)
        stored = self.store.runs.get(run.run_id)

        self.assertTrue(result.aborted)
        self.assertEqual(run.run_id, result.run_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertTrue(stored.abort_requested)

    def test_repeated_abort_is_idempotent_while_run_is_active(self) -> None:
        identity = _identity("chat-repeat")
        run = self.commands.start_chat_run(identity=identity)

        first = self.abort.abort_chat(identity=identity)
        second = self.abort.abort_chat(identity=identity)

        self.assertTrue(first.aborted)
        self.assertTrue(second.aborted)
        self.assertEqual(run.run_id, second.run_id)

    def test_completed_run_returns_false(self) -> None:
        identity = _identity("chat-done")
        run = self.commands.start_chat_run(identity=identity)
        self.commands.issue_execution_lease(run_id=run.run_id)
        completed = self.commands.complete_chat_run(run_id=run.run_id)

        result = self.abort.abort_chat(identity=identity)

        self.assertEqual(RUN_SUCCEEDED, completed.status)
        self.assertFalse(result.aborted)
        self.assertEqual("当前无进行中的流式响应", result.msg)

    def test_stale_active_run_is_expired_before_abort(self) -> None:
        short_commands = ChatCommandService(
            ChatRunLockService(
                self.db_path,
                owner_instance_id="test-instance",
                stale_after_seconds=1,
            )
        )
        abort = ChatAbortService(
            store=self.store,
            chat_commands=short_commands,
        )
        identity = _identity("chat-stale-abort")
        run = short_commands.start_chat_run(identity=identity)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE chat_runs
                SET heartbeat_at = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    "2000-01-01T00:00:00+00:00",
                    "2000-01-01T00:00:00+00:00",
                    run.run_id,
                ),
            )

        result = abort.abort_chat(identity=identity)
        stored = self.store.runs.get(run.run_id)

        self.assertFalse(result.aborted)
        self.assertEqual("当前无进行中的流式响应", result.msg)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(RUN_FAILED, stored.status)
        self.assertEqual("chat run heartbeat expired", stored.error_message)

    def test_unexpected_request_abort_value_error_is_not_masked(self) -> None:
        class FailingCommands:
            def expire_stale_chat_runs(self, *, conversation_id: str):
                return ()

            def request_abort(self, *, run_id: str):
                raise ValueError("unexpected persistence failure")

        identity = _identity("chat-corrupt")
        self.commands.start_chat_run(identity=identity)
        abort = ChatAbortService(
            store=self.store,
            chat_commands=FailingCommands(),  # type: ignore[arg-type]
        )

        with self.assertRaisesRegex(ValueError, "unexpected persistence failure"):
            abort.abort_chat(identity=identity)

    def test_build_abort_signal_returns_domain_event(self) -> None:
        self.assertEqual(
            ChatStreamEvent("aborted", {}),
            ChatAbortService.build_abort_signal(),
        )


if __name__ == "__main__":
    unittest.main()
