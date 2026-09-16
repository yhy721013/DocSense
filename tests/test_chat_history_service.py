"""文件对话本地权威历史的测试。"""

from __future__ import annotations

import tempfile
import unittest

from app.services.chat import (
    ChatHistoryService,
    ChatSessionScopeBinding,
    ChatStore,
    MESSAGE_COMMITTED,
    MESSAGE_DISCARDED,
    MESSAGE_PENDING,
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_USER,
)


class ChatHistoryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tempdir.__enter__()
        self.store = ChatStore(f"{self.tmp}/chat.sqlite3")
        self.history = ChatHistoryService(self.store)

    def tearDown(self) -> None:
        self._tempdir.__exit__(None, None, None)

    def test_empty_history_returns_empty_list(self) -> None:
        self.assertEqual([], self.history.list_history("chat-empty"))

    def test_history_uses_committed_messages_and_user_file_snapshot(self) -> None:
        self.store.sessions.create_or_get(chat_id="chat-history")
        self.store.runs.create(run_id="run-history", chat_id="chat-history")
        self.store.messages.append(
            message_id="message-user",
            chat_id="chat-history",
            run_id="run-history",
            role=MESSAGE_ROLE_USER,
            content="请总结",
            status=MESSAGE_COMMITTED,
            files=(("b.pdf", "乙.pdf"), ("a.pdf", "")),
        )
        self.store.messages.append(
            message_id="message-assistant",
            chat_id="chat-history",
            run_id="run-history",
            role=MESSAGE_ROLE_ASSISTANT,
            content="总结完成",
            status=MESSAGE_COMMITTED,
        )

        result = self.history.list_history("chat-history")

        self.assertEqual("user", result[0]["role"])
        self.assertEqual("请总结", result[0]["content"])
        self.assertIsInstance(result[0]["timestamp"], int)
        self.assertEqual(
            [{"name": "a.pdf"}, {"name": "乙.pdf"}],
            result[0]["files"],
        )
        self.assertEqual(
            {
                "role": "assistant",
                "content": "总结完成",
                "timestamp": result[1]["timestamp"],
            },
            result[1],
        )
        self.assertIsInstance(result[1]["timestamp"], int)

    def test_uncommitted_or_discarded_assistant_is_not_returned(self) -> None:
        self.store.sessions.create_or_get(chat_id="chat-interrupted")
        self.store.runs.create(run_id="run-interrupted", chat_id="chat-interrupted")
        self.store.messages.append(
            message_id="message-user",
            chat_id="chat-interrupted",
            run_id="run-interrupted",
            role=MESSAGE_ROLE_USER,
            content="继续",
            status=MESSAGE_COMMITTED,
        )
        self.store.messages.append(
            message_id="message-assistant-pending",
            chat_id="chat-interrupted",
            run_id="run-interrupted",
            role=MESSAGE_ROLE_ASSISTANT,
            content="半截回答",
            status=MESSAGE_PENDING,
        )
        self.store.messages.append(
            message_id="message-assistant-discarded",
            chat_id="chat-interrupted",
            run_id="run-interrupted",
            role=MESSAGE_ROLE_ASSISTANT,
            content="丢弃回答",
            status=MESSAGE_DISCARDED,
        )

        result = self.history.list_history("chat-interrupted")

        self.assertEqual(1, len(result))
        self.assertEqual("user", result[0]["role"])
        self.assertEqual("继续", result[0]["content"])

    def test_architecture_user_history_exposes_id_without_files(self) -> None:
        """architecture user 与 assistant 的公开字段必须严格互斥。"""
        self.store.sessions.create_or_get(chat_id="chat-architecture-history")
        self.store.session_scope_bindings.create(
            ChatSessionScopeBinding(
                chat_id="chat-architecture-history",
                scope_mode="architecture",
                architecture_id=7,
                created_at="2026-07-28T00:00:00+00:00",
            )
        )
        self.store.runs.create(
            run_id="run-architecture-history",
            chat_id="chat-architecture-history",
        )
        self.store.messages.append(
            message_id="message-architecture-user",
            chat_id="chat-architecture-history",
            run_id="run-architecture-history",
            role=MESSAGE_ROLE_USER,
            content="请总结类别",
            status=MESSAGE_COMMITTED,
            architecture_id=7,
        )
        self.store.messages.append(
            message_id="message-architecture-assistant",
            chat_id="chat-architecture-history",
            run_id="run-architecture-history",
            role=MESSAGE_ROLE_ASSISTANT,
            content="总结完成",
            status=MESSAGE_COMMITTED,
        )

        result = self.history.list_history("chat-architecture-history")

        self.assertEqual(7, result[0]["architectureId"])
        self.assertIsInstance(result[0]["architectureId"], int)
        self.assertNotIn("files", result[0])
        self.assertNotIn("architectureId", result[1])
        self.assertNotIn("files", result[1])

    def test_title_messages_use_only_role_and_trimmed_content(self) -> None:
        self.store.sessions.create_or_get(chat_id="chat-title")
        self.store.runs.create(run_id="run-title", chat_id="chat-title")
        self.store.messages.append(
            message_id="message-title-user",
            chat_id="chat-title",
            run_id="run-title",
            role=MESSAGE_ROLE_USER,
            content="  这是用户问题  ",
            status=MESSAGE_COMMITTED,
        )
        self.store.messages.append(
            message_id="message-title-assistant",
            chat_id="chat-title",
            run_id="run-title",
            role=MESSAGE_ROLE_ASSISTANT,
            content="这是一个很长的回答",
            status=MESSAGE_COMMITTED,
        )

        self.assertEqual(
            [
                {"role": "user", "content": "这是"},
                {"role": "assistant", "content": "这是"},
            ],
            self.history.list_title_messages("chat-title", max_content_chars=2),
        )


if __name__ == "__main__":
    unittest.main()
