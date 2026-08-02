"""文件对话本地权威历史的测试。"""

from __future__ import annotations

import tempfile
import unittest

from app.modules.chat import (
    ChatHistoryService,
    ChatSessionScopeBinding,
    ChatStore,
    MESSAGE_COMMITTED,
    MESSAGE_DISCARDED,
    MESSAGE_PENDING,
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_USER,
)
from app.modules.chat.domain.identity import FileChatIdentity, WeaponryChatIdentity


class ChatHistoryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tempdir.__enter__()
        self.store = ChatStore(f"{self.tmp}/chat.sqlite3")
        self.history = ChatHistoryService(self.store)

    def tearDown(self) -> None:
        self._tempdir.__exit__(None, None, None)

    def _create(self, identity: FileChatIdentity | WeaponryChatIdentity) -> str:
        """建立公开身份对应的内部 Conversation，并返回仅供仓储断言使用的 UUID。"""
        return self.store.identities.create_conversation(identity).conversation_id

    def test_empty_history_returns_empty_list(self) -> None:
        self.assertEqual(
            [],
            self.history.list_history(FileChatIdentity(chat_id=10001)),
        )

    def test_history_uses_committed_messages_and_user_file_snapshot(self) -> None:
        identity = FileChatIdentity(chat_id=10002)
        conversation_id = self._create(identity)
        self.store.runs.create(
            run_id="run-history",
            conversation_id=conversation_id,
        )
        self.store.messages.append(
            message_id="message-user",
            conversation_id=conversation_id,
            run_id="run-history",
            role=MESSAGE_ROLE_USER,
            content="请总结",
            status=MESSAGE_COMMITTED,
            files=(("b.pdf", "乙.pdf"), ("a.pdf", "")),
        )
        self.store.messages.append(
            message_id="message-assistant",
            conversation_id=conversation_id,
            run_id="run-history",
            role=MESSAGE_ROLE_ASSISTANT,
            content="总结完成",
            status=MESSAGE_COMMITTED,
        )

        result = self.history.list_history(identity)

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
        identity = FileChatIdentity(chat_id=10003)
        conversation_id = self._create(identity)
        self.store.runs.create(
            run_id="run-interrupted",
            conversation_id=conversation_id,
        )
        self.store.messages.append(
            message_id="message-user",
            conversation_id=conversation_id,
            run_id="run-interrupted",
            role=MESSAGE_ROLE_USER,
            content="继续",
            status=MESSAGE_COMMITTED,
        )
        self.store.messages.append(
            message_id="message-assistant-pending",
            conversation_id=conversation_id,
            run_id="run-interrupted",
            role=MESSAGE_ROLE_ASSISTANT,
            content="半截回答",
            status=MESSAGE_PENDING,
        )
        self.store.messages.append(
            message_id="message-assistant-discarded",
            conversation_id=conversation_id,
            run_id="run-interrupted",
            role=MESSAGE_ROLE_ASSISTANT,
            content="丢弃回答",
            status=MESSAGE_DISCARDED,
        )

        result = self.history.list_history(identity)

        self.assertEqual(1, len(result))
        self.assertEqual("user", result[0]["role"])
        self.assertEqual("继续", result[0]["content"])

    def test_weaponry_history_does_not_echo_public_identity_fields(self) -> None:
        """裸消息数组不回显 userId/architectureId，也不暴露内部 Conversation。"""
        identity = WeaponryChatIdentity(user_id=9, architecture_id=7)
        conversation_id = self._create(identity)
        self.store.session_scope_bindings.create(
            ChatSessionScopeBinding(
                conversation_id=conversation_id,
                scope_mode="architecture",
                architecture_id=7,
                created_at="2026-07-28T00:00:00+00:00",
            )
        )
        self.store.runs.create(
            run_id="run-architecture-history",
            conversation_id=conversation_id,
        )
        self.store.messages.append(
            message_id="message-architecture-user",
            conversation_id=conversation_id,
            run_id="run-architecture-history",
            role=MESSAGE_ROLE_USER,
            content="请总结类别",
            status=MESSAGE_COMMITTED,
            architecture_id=7,
        )
        self.store.messages.append(
            message_id="message-architecture-assistant",
            conversation_id=conversation_id,
            run_id="run-architecture-history",
            role=MESSAGE_ROLE_ASSISTANT,
            content="总结完成",
            status=MESSAGE_COMMITTED,
        )

        result = self.history.list_history(identity)

        self.assertNotIn("userId", result[0])
        self.assertNotIn("architectureId", result[0])
        self.assertNotIn("files", result[0])
        self.assertNotIn("conversationId", result[0])
        self.assertEqual([], result[1]["chunks"])
        self.assertNotIn("architectureId", result[1])
        self.assertNotIn("files", result[1])

    def test_title_messages_use_only_role_and_trimmed_content(self) -> None:
        identity = FileChatIdentity(chat_id=10004)
        conversation_id = self._create(identity)
        self.store.runs.create(
            run_id="run-title",
            conversation_id=conversation_id,
        )
        self.store.messages.append(
            message_id="message-title-user",
            conversation_id=conversation_id,
            run_id="run-title",
            role=MESSAGE_ROLE_USER,
            content="  这是用户问题  ",
            status=MESSAGE_COMMITTED,
        )
        self.store.messages.append(
            message_id="message-title-assistant",
            conversation_id=conversation_id,
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
            self.history.list_title_messages(
                conversation_id,
                max_content_chars=2,
            ),
        )


if __name__ == "__main__":
    unittest.main()
