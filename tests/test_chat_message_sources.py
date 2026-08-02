"""assistant 来源 Chunk 与运行成功终态的原子提交测试。"""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from app.modules.chat.adapters.sqlite.event_repository import ChatRunEventRepository
from app.modules.chat.adapters.sqlite.locking.lock_service import ChatRunLockService
from app.modules.chat.adapters.sqlite.repositories import ChatMessageSourceRepository
from app.modules.chat.adapters.sqlite.store import ChatStore
from app.modules.chat.domain.events import ChatStreamEvent
from app.modules.chat.domain.identity import FileChatIdentity
from app.modules.chat.domain.models import (
    MESSAGE_PENDING,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    ChatMessageSourceChunk,
)
from app.modules.chat.ports.coordination import ChatRunInactiveError


class ChatMessageSourceAtomicityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = f"{self._tempdir.name}/chat.sqlite3"
        self.store = ChatStore(self.db_path)
        self.coordinator = ChatRunLockService(
            self.db_path,
            owner_instance_id="source-test-instance",
        )

    def tearDown(self) -> None:
        self._tempdir.cleanup()

    def _running(self, suffix: str = "one"):
        public_id = int.from_bytes(suffix.encode("utf-8"), "little") % 10**12 + 1
        run = self.coordinator.try_acquire_chat_run(
            identity=FileChatIdentity(chat_id=public_id),
            run_id=f"run-{suffix}",
            user_message="问题",
        )
        lease = self.coordinator.issue_execution_lease(run_id=run.run_id)
        return run, lease

    @staticmethod
    def _chunks(message_id: str) -> tuple[ChatMessageSourceChunk, ...]:
        return (
            ChatMessageSourceChunk(
                message_id=message_id,
                position=0,
                content="  第一段\r\n保留换行  ",
                file_name="stored-a.pdf",
                original_file_name="原文件甲.pdf",
                created_at="2026-08-02T00:00:00+00:00",
            ),
            ChatMessageSourceChunk(
                message_id=message_id,
                position=1,
                content="e\u0301 与 é 必须保持各自编码",
                file_name="stored-b.pdf",
                original_file_name="原文件乙.pdf",
                created_at="2026-08-02T00:00:00+00:00",
            ),
        )

    def test_reply_chunks_event_messages_and_run_commit_together(self) -> None:
        run, lease = self._running()
        assistant_id = f"{run.run_id}:assistant"
        chunks = self._chunks(assistant_id)

        completed = self.coordinator.complete_run_with_execution_lease(
            lease=lease,
            user_message_id=f"{run.run_id}:user",
            assistant_message_id=assistant_id,
            assistant_content="完整回答",
            source_chunks=chunks,
            terminal_event=ChatStreamEvent("done", {}),
        )

        self.assertEqual(RUN_SUCCEEDED, completed.status)
        self.assertEqual(chunks, self.store.message_sources.list_by_message(assistant_id))
        messages = self.store.messages.list_by_chat(run.conversation_id)
        self.assertEqual(["user", "assistant"], [item.role for item in messages])
        self.assertTrue(all(item.status == "committed" for item in messages))
        self.assertEqual(
            ["done"],
            [item.event_type for item in self.store.events.list_by_run(run.run_id)],
        )

    def test_chunk_insert_failure_rolls_back_assistant_user_and_run(self) -> None:
        run, lease = self._running("chunk-failure")
        assistant_id = f"{run.run_id}:assistant"
        with patch.object(
            ChatMessageSourceRepository,
            "append_many_in_transaction",
            side_effect=RuntimeError("forced chunk insert failure"),
        ), self.assertRaisesRegex(RuntimeError, "forced chunk"):
            self.coordinator.complete_run_with_execution_lease(
                lease=lease,
                user_message_id=f"{run.run_id}:user",
                assistant_message_id=assistant_id,
                assistant_content="不能部分提交",
                source_chunks=self._chunks(assistant_id),
                terminal_event=ChatStreamEvent("done", {}),
            )

        current = self.store.runs.get(run.run_id)
        self.assertEqual(RUN_RUNNING, current.status)
        messages = self.store.messages.list_by_chat(run.conversation_id)
        self.assertEqual(1, len(messages))
        self.assertEqual(MESSAGE_PENDING, messages[0].status)
        self.assertEqual((), self.store.message_sources.list_by_message(assistant_id))
        self.assertEqual((), self.store.events.list_by_run(run.run_id))

    def test_terminal_event_failure_rolls_back_chunks_and_messages(self) -> None:
        run, lease = self._running("event-failure")
        assistant_id = f"{run.run_id}:assistant"
        with patch.object(
            ChatRunEventRepository,
            "append_in_transaction",
            side_effect=RuntimeError("forced event failure"),
        ), self.assertRaisesRegex(RuntimeError, "forced event"):
            self.coordinator.complete_run_with_execution_lease(
                lease=lease,
                user_message_id=f"{run.run_id}:user",
                assistant_message_id=assistant_id,
                assistant_content="不能部分提交",
                source_chunks=self._chunks(assistant_id),
                terminal_event=ChatStreamEvent("done", {}),
            )
        self.assertEqual(RUN_RUNNING, self.store.runs.get(run.run_id).status)
        self.assertEqual(1, len(self.store.messages.list_by_chat(run.conversation_id)))
        self.assertEqual((), self.store.message_sources.list_by_message(assistant_id))

    def test_abort_wins_before_completion_and_prevents_all_reply_facts(self) -> None:
        run, lease = self._running("abort-first")
        assistant_id = f"{run.run_id}:assistant"
        self.coordinator.request_abort(run.run_id)
        with self.assertRaises(ChatRunInactiveError):
            self.coordinator.complete_run_with_execution_lease(
                lease=lease,
                user_message_id=f"{run.run_id}:user",
                assistant_message_id=assistant_id,
                assistant_content="不应提交",
                source_chunks=self._chunks(assistant_id),
                terminal_event=ChatStreamEvent("done", {}),
            )
        self.assertEqual(1, len(self.store.messages.list_by_chat(run.conversation_id)))
        self.assertEqual((), self.store.message_sources.list_by_message(assistant_id))

    def test_completion_wins_then_abort_cannot_reopen_terminal_run(self) -> None:
        run, lease = self._running("complete-first")
        assistant_id = f"{run.run_id}:assistant"
        self.coordinator.complete_run_with_execution_lease(
            lease=lease,
            user_message_id=f"{run.run_id}:user",
            assistant_message_id=assistant_id,
            assistant_content="已提交",
            source_chunks=self._chunks(assistant_id),
            terminal_event=ChatStreamEvent("done", {}),
        )
        with self.assertRaises(ChatRunInactiveError):
            self.coordinator.request_abort(run.run_id)
        self.assertEqual(RUN_SUCCEEDED, self.store.runs.get(run.run_id).status)


if __name__ == "__main__":
    unittest.main()
