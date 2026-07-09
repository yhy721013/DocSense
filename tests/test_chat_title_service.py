"""Tests for file-chat title generation service."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from typing import Iterator

from app.services.chat import (
    ChatHistoryService,
    ChatStore,
    ChatTitleEmptyHistoryError,
    ChatTitleService,
    MESSAGE_COMMITTED,
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_USER,
)
from tests.fakes import FakeChatConversationFactory


class _FailingConversation:
    """Conversation fake that simulates model/provider failure."""

    def generate_standalone_reply(self, *, context_ref: str, prompt: str) -> str:
        raise RuntimeError("model boom")


class _FailingConversationFactory:
    """Minimal factory satisfying the runtime chat factory protocol."""

    @contextmanager
    def create(self) -> Iterator[_FailingConversation]:
        yield _FailingConversation()


class ChatTitleServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tempdir.__enter__()
        self.store = ChatStore(f"{self.tmp}/chat.sqlite3")

    def tearDown(self) -> None:
        self._tempdir.__exit__(None, None, None)

    def _service(
        self,
        *,
        standalone_reply: str = "模拟标题",
        max_title_chars: int = 20,
    ) -> tuple[ChatTitleService, FakeChatConversationFactory]:
        factory = FakeChatConversationFactory(standalone_reply=standalone_reply)
        history = ChatHistoryService(self.store)
        return (
            ChatTitleService(
                store=self.store,
                history_service=history,
                conversation_factory=factory,
                max_title_chars=max_title_chars,
            ),
            factory,
        )

    def _create_session_with_known_context(
        self,
        *,
        chat_id: str,
        factory: FakeChatConversationFactory,
    ) -> None:
        with factory.create() as port:
            refs = port.open_conversation(
                context_name=f"context-{chat_id}",
                conversation_name=f"thread-{chat_id}",
            )
        self.store.sessions.create_or_get(
            chat_id=chat_id,
            workspace_ref=refs.context_ref,
            thread_ref=refs.conversation_ref,
        )

    def _append_committed_turn(self, *, chat_id: str, run_id: str = "run-title") -> None:
        self.store.runs.create(run_id=run_id, chat_id=chat_id)
        self.store.messages.append(
            message_id=f"{run_id}:user",
            chat_id=chat_id,
            run_id=run_id,
            role=MESSAGE_ROLE_USER,
            content="请总结这份国防战略文件",
            status=MESSAGE_COMMITTED,
        )
        self.store.messages.append(
            message_id=f"{run_id}:assistant",
            chat_id=chat_id,
            run_id=run_id,
            role=MESSAGE_ROLE_ASSISTANT,
            content="文件主要讨论美日国防战略协作和装备发展。",
            status=MESSAGE_COMMITTED,
        )

    def test_nonexistent_chat_returns_empty_title_without_model_call(self) -> None:
        service, factory = self._service()

        result = service.generate_title(chat_id="missing-chat")

        self.assertEqual({"chatId": "missing-chat", "title": ""}, result.to_response())
        self.assertEqual(0, len(factory.ports))

    def test_existing_chat_with_empty_history_is_rejected(self) -> None:
        service, factory = self._service()
        self._create_session_with_known_context(chat_id="chat-empty", factory=factory)

        with self.assertRaises(ChatTitleEmptyHistoryError):
            service.generate_title(chat_id="chat-empty")

        self.assertEqual(1, len(factory.ports))

    def test_title_is_cleaned_and_history_is_not_mutated(self) -> None:
        service, factory = self._service(standalone_reply=' 标题： "美日战略对比" \n说明忽略')
        self._create_session_with_known_context(chat_id="chat-title", factory=factory)
        self._append_committed_turn(chat_id="chat-title")
        history = ChatHistoryService(self.store)
        before = history.list_history("chat-title")

        result = service.generate_title(chat_id="chat-title")

        self.assertEqual("美日战略对比", result.title)
        self.assertEqual(before, history.list_history("chat-title"))
        prompts = factory.ports[-1].standalone_prompts
        self.assertEqual(1, len(prompts))
        self.assertIn("请总结这份国防战略文件", prompts[0][1])

    def test_title_is_truncated_to_configured_length(self) -> None:
        service, factory = self._service(
            standalone_reply="这是一段超过二十个字符的标题用于验证截断逻辑",
            max_title_chars=20,
        )
        self._create_session_with_known_context(chat_id="chat-long", factory=factory)
        self._append_committed_turn(chat_id="chat-long", run_id="run-long")

        result = service.generate_title(chat_id="chat-long")

        self.assertEqual("这是一段超过二十个字符的标题用于验证截断", result.title)
        self.assertEqual(20, len(result.title))

    def test_model_exception_is_propagated(self) -> None:
        history = ChatHistoryService(self.store)
        service = ChatTitleService(
            store=self.store,
            history_service=history,
            conversation_factory=_FailingConversationFactory(),
        )
        self.store.sessions.create_or_get(
            chat_id="chat-error",
            workspace_ref="workspace-error",
            thread_ref="thread-error",
        )
        self._append_committed_turn(chat_id="chat-error", run_id="run-error")

        with self.assertRaisesRegex(RuntimeError, "model boom"):
            service.generate_title(chat_id="chat-error")


if __name__ == "__main__":
    unittest.main()
