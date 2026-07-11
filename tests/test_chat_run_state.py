"""阶段 4 文件对话运行锁的离线测试。"""

from __future__ import annotations

import tempfile
import unittest
import sqlite3

from app.services.chat import (
    RUN_FAILED,
    RUN_ACCEPTED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    ChatRunBusyError,
    ChatRunInactiveError,
    ChatRunLockService,
    ChatStore,
    MESSAGE_DISCARDED,
)


class ChatRunLockServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tempdir.__enter__()
        self.db_path = f"{self.tmp}/chat.sqlite3"
        self.store = ChatStore(self.db_path)
        self.locks = ChatRunLockService(
            self.db_path,
            owner_instance_id="test-instance",
        )

    def tearDown(self) -> None:
        self._tempdir.__exit__(None, None, None)

    def test_same_chat_is_exclusive_until_run_completes(self) -> None:
        first = self.locks.try_acquire_chat_run(
            chat_id="chat-a",
            run_id="run-a",
        )

        with self.assertRaises(ChatRunBusyError) as error:
            self.locks.try_acquire_chat_run(
                chat_id="chat-a",
                run_id="run-b",
            )

        self.locks.issue_execution_lease(run_id="run-a")
        completed = self.locks.complete_run("run-a")
        second = self.locks.try_acquire_chat_run(
            chat_id="chat-a",
            run_id="run-b",
        )

        self.assertEqual(RUN_ACCEPTED, first.status)
        self.assertEqual("run-a", error.exception.active_run_id)
        self.assertEqual(RUN_SUCCEEDED, completed.status)
        self.assertEqual(RUN_ACCEPTED, second.status)

    def test_failed_run_releases_chat_for_next_attempt(self) -> None:
        self.locks.try_acquire_chat_run(
            chat_id="chat-fail",
            run_id="run-fail",
        )
        self.locks.issue_execution_lease(run_id="run-fail")
        failed = self.locks.fail_run("run-fail", error_message="stream failed")
        retry = self.locks.try_acquire_chat_run(
            chat_id="chat-fail",
            run_id="run-retry",
        )

        self.assertEqual("failed", failed.status)
        self.assertEqual(RUN_ACCEPTED, retry.status)

    def test_different_chats_do_not_block_each_other(self) -> None:
        first = self.locks.try_acquire_chat_run(
            chat_id="chat-one",
            run_id="run-one",
        )
        second = self.locks.try_acquire_chat_run(
            chat_id="chat-two",
            run_id="run-two",
        )

        self.assertEqual(RUN_ACCEPTED, first.status)
        self.assertEqual(RUN_ACCEPTED, second.status)

    def test_request_abort_sets_flag_on_active_run(self) -> None:
        self.locks.try_acquire_chat_run(
            chat_id="chat-abort",
            run_id="run-abort",
        )

        aborted = self.locks.request_abort("run-abort")

        self.assertTrue(aborted.abort_requested)

    def test_discard_unstarted_run_hides_the_accepted_user_message(self) -> None:
    """已受理状态从未领取执行权时，断开连接不应产生历史轮次。"""
        run = self.locks.try_acquire_chat_run(
            chat_id="chat-discard",
            run_id="run-discard",
            user_message="尚未执行",
        )

        discarded = self.locks.discard_unstarted_run(
            run_id=run.run_id,
            error_message="response closed before execution",
        )

        message = self.store.messages.list_by_chat("chat-discard")[0]
        self.assertEqual(RUN_FAILED, discarded.status)
        self.assertEqual(MESSAGE_DISCARDED, message.status)
        self.assertEqual((), self.store.runs.list_active("chat-discard"))

    def test_heartbeat_updates_active_run(self) -> None:
        started = self.locks.try_acquire_chat_run(
            chat_id="chat-heartbeat",
            run_id="run-heartbeat",
        )
        self.locks.issue_execution_lease(run_id="run-heartbeat")
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
                    "run-heartbeat",
                ),
            )

        touched = self.locks.heartbeat_run("run-heartbeat")

        self.assertEqual(RUN_ACCEPTED, started.status)
        self.assertEqual(RUN_RUNNING, touched.status)
        self.assertNotEqual("2000-01-01T00:00:00+00:00", touched.heartbeat_at)

    def test_stale_active_run_is_failed_before_retry(self) -> None:
        locks = ChatRunLockService(
            self.db_path,
            owner_instance_id="test-instance",
            stale_after_seconds=1,
        )
        locks.try_acquire_chat_run(
            chat_id="chat-stale",
            run_id="run-stale",
        )
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
                    "run-stale",
                ),
            )

        retry = locks.try_acquire_chat_run(
            chat_id="chat-stale",
            run_id="run-after-stale",
        )
        stale = self.store.runs.get("run-stale")

        self.assertEqual(RUN_ACCEPTED, retry.status)
        self.assertIsNotNone(stale)
        assert stale is not None
        self.assertEqual(RUN_FAILED, stale.status)
        self.assertEqual("chat run heartbeat expired", stale.error_message)

    def test_stale_active_run_can_be_expired_without_retry(self) -> None:
        locks = ChatRunLockService(
            self.db_path,
            owner_instance_id="test-instance",
            stale_after_seconds=1,
        )
        locks.try_acquire_chat_run(
            chat_id="chat-stale-explicit",
            run_id="run-stale-explicit",
        )
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
                    "run-stale-explicit",
                ),
            )

        expired = locks.expire_stale_runs_for_chat(chat_id="chat-stale-explicit")
        active = self.store.runs.list_active("chat-stale-explicit")

        self.assertEqual(["run-stale-explicit"], [run.run_id for run in expired])
        self.assertEqual(RUN_FAILED, expired[0].status)
        self.assertEqual("chat run heartbeat expired", expired[0].error_message)
        self.assertEqual((), active)

    def test_terminal_run_rejects_illegal_follow_up_state_changes(self) -> None:
        self.locks.try_acquire_chat_run(
            chat_id="chat-terminal",
            run_id="run-terminal",
        )
        self.locks.issue_execution_lease(run_id="run-terminal")
        completed = self.locks.complete_run("run-terminal")

        with self.assertRaises(ValueError):
            self.locks.fail_run("run-terminal", error_message="late failure")
        with self.assertRaises(ChatRunInactiveError):
            self.locks.request_abort("run-terminal")

        self.assertEqual(RUN_SUCCEEDED, completed.status)


if __name__ == "__main__":
    unittest.main()
