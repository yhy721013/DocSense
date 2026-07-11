"""文件对话标题生成服务的测试。"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from typing import Iterator

from app.ports import ChatOperationResult, ChatSessionRefs
from app.services.chat import (
    ChatCleanupJobExecutor,
    ChatCommandService,
    ChatDeleteBusyError,
    ChatDeleteService,
    ChatHistoryService,
    ChatRunLockService,
    ChatStore,
    ChatTitleEmptyHistoryError,
    ChatTitleGenerationError,
    ChatTitleService,
    ChatTitleUnavailableError,
    MESSAGE_COMMITTED,
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_USER,
    RESOURCE_THREAD,
    chat_temporary_thread_lease_id,
)
from tests.fakes import FakeChatConversationFactory


class _FailingConversation:
    """模拟模型或供应商失败的对话替身。"""

    def open_temporary_conversation(
        self,
        *,
        context_ref: str,
        conversation_name: str,
    ) -> ChatSessionRefs:
        return ChatSessionRefs(context_ref, f"temporary-{conversation_name}")

    def generate_temporary_reply(
        self,
        *,
        session: ChatSessionRefs,
        prompt: str,
    ) -> str:
        raise RuntimeError("model boom")

    def delete_conversation(self, session: ChatSessionRefs) -> ChatOperationResult:
        return ChatOperationResult(success=True)


class _FailingConversationFactory:
    """满足运行期对话工厂协议的最小工厂。"""

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
        delete_conversation_error_message: str = "",
        max_title_chars: int = 20,
    ) -> tuple[ChatTitleService, FakeChatConversationFactory]:
        factory = FakeChatConversationFactory(
            standalone_reply=standalone_reply,
            delete_conversation_error_message=delete_conversation_error_message,
        )
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

    def test_deleting_session_cannot_create_a_title_resource(self) -> None:
        service, factory = self._service()
        self.store.sessions.create_or_get(
            chat_id="chat-deleting",
            workspace_ref="workspace-deleting",
            thread_ref="thread-deleting",
        )
        self.store.sessions.set_status(chat_id="chat-deleting", status="deleting")

        with self.assertRaises(ChatTitleUnavailableError):
            service.generate_title(chat_id="chat-deleting")

        self.assertEqual(0, len(factory.ports))

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

    def test_temporary_cleanup_failure_is_durable_and_can_be_retried(self) -> None:
        """标题线程删除失败不得被吞掉，并保留按 lease 重试的工作项。"""
        service, factory = self._service(
            standalone_reply="可恢复标题",
            delete_conversation_error_message="temporary delete failed",
        )
        self._create_session_with_known_context(chat_id="chat-cleanup-fail", factory=factory)
        self._append_committed_turn(chat_id="chat-cleanup-fail", run_id="run-cleanup")

        with self.assertRaises(ChatTitleGenerationError):
            service.generate_title(chat_id="chat-cleanup-fail")

        leases = self.store.resource_leases.list_by_chat("chat-cleanup-fail")
        temporary_lease = next(
            lease
            for lease in leases
            if lease.lease_id.startswith("chat:chat-cleanup-fail:temporary_thread:")
        )
        jobs = self.store.cleanup_jobs.list_by_chat("chat-cleanup-fail")
        self.assertEqual("cleanup_failed", temporary_lease.status)
        self.assertEqual(1, len(jobs))
        self.assertEqual("temporary_thread", jobs[0].reason)
        self.assertEqual("failed", jobs[0].status)
        self.assertEqual(1, jobs[0].attempt_count)

        # 后续维护工作进程只接收 ``job_id``，无需原始标题请求或捕获的回调，
        # 即可重新加载并恢复该租约。
        recovery = ChatCleanupJobExecutor(
            store=self.store,
            conversation_factory=FakeChatConversationFactory(),
        )
        requeued = self.store.cleanup_jobs.enqueue(
            chat_id="chat-cleanup-fail",
            reason="temporary_thread",
            lease_id=temporary_lease.lease_id,
        )
        completed = recovery.execute_cleanup_job(job_id=requeued.job_id)

        self.assertEqual("succeeded", completed.status)
        self.assertEqual(
            "closed",
            self.store.resource_leases.get(temporary_lease.lease_id).status,
        )

    def test_delete_is_rejected_while_title_has_a_planned_temporary_lease(self) -> None:
    """计划租约使标题创建与删除在 SQLite 临界区互斥。"""
        factory = FakeChatConversationFactory()
        self._create_session_with_known_context(chat_id="chat-title-race", factory=factory)
        lease_id = chat_temporary_thread_lease_id(
            chat_id="chat-title-race",
            attempt_id="race-test",
        )
        self.store.resource_leases.begin(
            lease_id=lease_id,
            chat_id="chat-title-race",
            resource_type=RESOURCE_THREAD,
            require_active_session=True,
        )
        delete_service = ChatDeleteService(
            store=self.store,
            chat_commands=ChatCommandService(
                ChatRunLockService(f"{self.tmp}/chat.sqlite3")
            ),
            conversation_factory=factory,
        )

        with self.assertRaises(ChatDeleteBusyError):
            delete_service.delete_chat(chat_id="chat-title-race")


if __name__ == "__main__":
    unittest.main()
