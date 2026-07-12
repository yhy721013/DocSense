"""阶段 10 文件对话链路的离线路由受理测试。"""

from __future__ import annotations

import tempfile
import unittest

from app import create_app
from app.container import ApplicationServices, UploadTaskLimiter
from app.services.chat import (
    ChatAbortService,
    ChatCleanupJobExecutor,
    ChatCommandService,
    ChatDeleteService,
    ChatHistoryService,
    ChatRunLockService,
    ChatStore,
    ChatTitleService,
    DatabaseChatDocumentResolver,
    SynchronousChatRunExecutor,
    InlineChatRunDispatcher,
    InlineChatCleanupDispatcher,
    MESSAGE_COMMITTED,
    RUN_FAILED,
)
from app.services.core.config import AnythingLLMConfig, LLMIntegrationConfig
from app.services.core.database import DatabaseService
from app.services.core.progress_hub import LLMProgressHub
from app.services.llm_service.task_service import LLMTaskService
from tests.fakes import (
    FakeChatConversationFactory,
    FakeDocumentRagFactory,
    FakeKnowledgeIndexFactory,
)


def _build_test_services(tmp: str) -> ApplicationServices:
    """创建文件对话路径不依赖网络的隔离容器。"""
    chat_db_path = f"{tmp}/chat.sqlite3"
    chat_store = ChatStore(db_path=chat_db_path)
    chat_commands = ChatCommandService(ChatRunLockService(chat_db_path))
    chat_history = ChatHistoryService(chat_store)
    chat_conversation_factory = FakeChatConversationFactory(
        stream_contents=("第一段", "第二段")
    )
    kb_service = DatabaseService(db_path=f"{tmp}/knowledge.sqlite3")
    chat_run_executor = SynchronousChatRunExecutor(
        store=chat_store,
        chat_commands=chat_commands,
        conversation_factory=chat_conversation_factory,
        document_resolver=DatabaseChatDocumentResolver(kb_service),
    )
    chat_cleanup_executor = ChatCleanupJobExecutor(
        store=chat_store,
        conversation_factory=chat_conversation_factory,
    )
    chat_cleanup_dispatcher = InlineChatCleanupDispatcher(
        execute=chat_cleanup_executor.execute_cleanup_job,
    )
    chat_dispatcher = InlineChatRunDispatcher(
        execute=chat_run_executor.execute_chat_run,
    )
    return ApplicationServices(
        document_rag_factory=FakeDocumentRagFactory(),
        knowledge_index_factory=FakeKnowledgeIndexFactory(),
        chat_conversation_factory=chat_conversation_factory,
        task_service=LLMTaskService(db_path=f"{tmp}/tasks.sqlite3"),
        kb_service=kb_service,
        chat_store=chat_store,
        chat_commands=chat_commands,
        chat_run_executor=chat_run_executor,
        chat_dispatcher=chat_dispatcher,
        chat_history=chat_history,
        chat_title=ChatTitleService(
            store=chat_store,
            history_service=chat_history,
            conversation_factory=chat_conversation_factory,
            cleanup_dispatcher=chat_cleanup_dispatcher,
            cleanup_executor=chat_cleanup_executor,
        ),
        chat_abort=ChatAbortService(
            store=chat_store,
            chat_commands=chat_commands,
        ),
        chat_delete=ChatDeleteService(
            store=chat_store,
            chat_commands=chat_commands,
            conversation_factory=chat_conversation_factory,
            cleanup_dispatcher=chat_cleanup_dispatcher,
            cleanup_executor=chat_cleanup_executor,
        ),
        chat_cleanup_executor=chat_cleanup_executor,
        progress_hub=LLMProgressHub(),
        upload_task_limiter=UploadTaskLimiter(max_concurrency=1),
        llm_config=LLMIntegrationConfig(
            callback_url=None,
            callback_timeout=5.0,
            task_db_path=f"{tmp}/tasks.sqlite3",
            download_timeout=5.0,
            download_dir=tmp,
        ),
        anythingllm_config=AnythingLLMConfig(
            base_url="http://anythingllm.invalid/api/v1",
            api_key="test-key",
            timeout=5.0,
            storage_root=None,
        ),
    )


class ChatRouteAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tempdir.__enter__()
        self.services = _build_test_services(self.tmp)
        self.kb_service = self.services.kb_service
        self.app = create_app(services=self.services)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self._tempdir.__exit__(None, None, None)

    def _save_document(
        self,
        file_name: str = "hash-alpha.pdf",
        *,
        original_name: str = "alpha原名.pdf",
        document_id: str = "doc-alpha",
    ) -> None:
        self.kb_service.save_document_record(
            file_name,
            1,
            document_id,
            f"custom-documents/{document_id}.json",
            original_name=original_name,
            ingested_file_name=file_name,
        )

    def _chat(self, *, chat_id: int, file_names: list[str], message: str):
        return self.client.post(
            "/llm/chat",
            json={
                "businessType": "chat",
                "params": {
                    "chatId": chat_id,
                    "fileNames": file_names,
                    "message": message,
                },
            },
        )

    def test_rejects_protocol_invalid_request_before_run_acceptance(self) -> None:
        response = self.client.post(
            "/llm/chat",
            json={"businessType": "chat", "params": {"chatId": 1000, "message": "hi"}},
        )

        self.assertEqual(400, response.status_code)
        self.assertEqual((), self.services.chat_store.runs.list_active("1000"))

    def test_chat_routes_strictly_reject_invalid_chat_id_values(self) -> None:
        """所有公开文件对话路由都必须在业务处理前拒绝非正整数。"""

        invalid_values = ("1001", True, False, 0, -1, 1.5)
        json_paths = (
            "/llm/chat",
            "/llm/chat/title",
            "/llm/chat/abort",
            "/llm/chat/delete",
        )
        for chat_id in invalid_values:
            for path in json_paths:
                with self.subTest(path=path, chat_id=chat_id):
                    response = self.client.post(
                        path,
                        json={
                            "businessType": "chat",
                            "params": {"chatId": chat_id},
                        },
                    )
                    self.assertEqual(400, response.status_code)
                    self.assertEqual(
                        {"error": "chatId必须为正整数"},
                        response.get_json(),
                    )

        for raw_chat_id in ("", "0", "-1", "1.5", "001", "legacy-chat"):
            with self.subTest(path="/llm/chat/history", chat_id=raw_chat_id):
                response = self.client.get(
                    "/llm/chat/history",
                    query_string={"chatId": raw_chat_id},
                )
                self.assertEqual(400, response.status_code)
                self.assertEqual(
                    {"error": "chatId必须为正整数"},
                    response.get_json(),
                )

    def test_chat_related_routes_echo_numeric_chat_id(self) -> None:
        title_response = self.client.post(
            "/llm/chat/title",
            json={"businessType": "chat", "params": {"chatId": 1011}},
        )
        abort_response = self.client.post(
            "/llm/chat/abort",
            json={"businessType": "chat", "params": {"chatId": 1011}},
        )
        history_response = self.client.get(
            "/llm/chat/history",
            query_string={"chatId": "1011"},
        )

        self.assertEqual({"chatId": 1011, "title": ""}, title_response.get_json())
        self.assertEqual(1011, abort_response.get_json()["chatId"])
        self.assertIsInstance(abort_response.get_json()["chatId"], int)
        self.assertEqual([], history_response.get_json())

    def test_new_empty_file_chat_uses_new_executor_and_commits_history(self) -> None:
        response = self._chat(chat_id=1001, file_names=[], message=" 你好 ")

        self.assertEqual(200, response.status_code)
        self.assertEqual("text/event-stream", response.mimetype)
        body = response.get_data(as_text=True)
        self.assertIn('event: chatInfo\ndata: {"chatId": 1001, "isNewChat": true}', body)
        self.assertIn('event: textChunk\ndata: {"content": "第一段"}', body)
        self.assertIn('event: done\ndata: {"chatId": 1001}', body)

        session = self.services.chat_store.sessions.get("1001")
        messages = self.services.chat_store.messages.list_by_chat("1001")
        runs = self.services.chat_store.runs.list_active("1001")
        self.assertIsNotNone(session)
        assert session is not None
        self.assertTrue(session.workspace_ref)
        self.assertTrue(session.thread_ref)
        self.assertEqual([], list(runs))
        self.assertEqual(["user", "assistant"], [item.role for item in messages])
        self.assertEqual("你好", messages[0].content)
        self.assertEqual("第一段第二段", messages[1].content)
        self.assertEqual(
            ["chatInfo", "textChunk", "textChunk", "done"],
            [
                event.event_type
                for event in self.services.chat_store.events.list_by_run(
                    messages[0].run_id
                )
            ],
        )
        self.assertFalse(hasattr(self.services, "chat_db"))

    def test_chat_sse_contract_does_not_expose_internal_run_identity(self) -> None:
        response = self._chat(
            chat_id=1002,
            file_names=[],
            message="验证既有 SSE 协议",
        )

        body = response.get_data(as_text=True)
        self.assertEqual(
            'event: chatInfo\ndata: {"chatId": 1002, "isNewChat": true}\n\n'
            'event: textChunk\ndata: {"content": "第一段"}\n\n'
            'event: textChunk\ndata: {"content": "第二段"}\n\n'
            'event: done\ndata: {"chatId": 1002}\n\n',
            body,
        )
        self.assertNotIn("runId", body)
        self.assertNotIn("requestId", body)
        self.assertNotIn("\nid:", body)
        self.assertNotIn("X-Chat-Run-Id", response.headers)
        self.assertNotIn("X-Request-Id", response.headers)

    def test_document_snapshot_is_resolved_inside_application_layer(self) -> None:
        self._save_document()

        response = self._chat(
            chat_id=1003,
            file_names=["hash-alpha.pdf"],
            message="请总结",
        )

        self.assertEqual(200, response.status_code)
        response.get_data()
        documents = self.services.chat_store.document_bindings.list_current_by_chat(
            "1003"
        )
        messages = self.services.chat_store.messages.list_by_chat("1003")
        run = next(
            message.run_id
            for message in messages
            if message.role == "user"
        )
        input_snapshot = self.services.chat_store.run_inputs.get(run)
        self.assertEqual(1, len(documents))
        self.assertEqual("document:doc-alpha", documents[0].document_ref)
        self.assertEqual("alpha原名.pdf", documents[0].original_name)
        self.assertEqual("alpha原名.pdf", messages[0].files[0].original_name)
        self.assertIsNotNone(input_snapshot)
        assert input_snapshot is not None
        self.assertEqual("请总结", input_snapshot.message)
        self.assertEqual("document:doc-alpha", input_snapshot.files[0].document_ref)

    def test_unresolved_document_is_404_without_creating_session_or_run(self) -> None:
        response = self._chat(
            chat_id=1004,
            file_names=["missing.pdf"],
            message="请总结",
        )

        self.assertEqual(404, response.status_code)
        self.assertIsNone(self.services.chat_store.sessions.get("1004"))

    def test_active_chat_request_is_rejected_with_409(self) -> None:
        self.services.chat_commands.start_chat_run(
            chat_id="1005",
            user_message="first",
        )

        response = self._chat(
            chat_id=1005,
            file_names=[],
            message="second",
        )

        self.assertEqual(409, response.status_code)

    def test_sse_close_after_execution_starts_preserves_user_turn(self) -> None:
        """已领取执行权后连接关闭，用户轮次仍按失败语义保留。"""
        response = self.client.post(
            "/llm/chat",
            json={
                "businessType": "chat",
                "params": {
                    "chatId": 1006,
                    "fileNames": [],
                    "message": "执行已开始后保留",
                },
            },
            buffered=False,
        )

        response.close()

        messages = self.services.chat_store.messages.list_by_chat(
            "1006"
        )
        self.assertEqual(1, len(messages))
        self.assertEqual(MESSAGE_COMMITTED, messages[0].status)
        run = self.services.chat_store.runs.get(messages[0].run_id)
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(RUN_FAILED, run.status)
        self.assertEqual((), self.services.chat_store.runs.list_active("1006"))

    def test_global_stream_capacity_returns_429_before_run_acceptance(self) -> None:
        executor = self.services.chat_run_executor
        acquired = [executor.try_acquire_stream_slot() for _ in range(executor.max_concurrent_streams)]
        self.assertEqual([True] * executor.max_concurrent_streams, acquired)
        try:
            response = self._chat(
                chat_id=1007,
                file_names=[],
                message="queued?",
            )
        finally:
            for _ in range(sum(acquired)):
                executor.release_stream_slot()

        self.assertEqual(429, response.status_code)
        self.assertIsNone(self.services.chat_store.sessions.get("1007"))

    def test_continue_chat_reuses_session_and_reports_not_new(self) -> None:
        first = self._chat(chat_id=1008, file_names=[], message="first")
        first.get_data()
        second = self._chat(chat_id=1008, file_names=[], message="second")

        self.assertEqual(200, second.status_code)
        self.assertIn('"isNewChat": false', second.get_data(as_text=True))
        self.assertEqual(
            4,
            len(self.services.chat_store.messages.list_by_chat("1008")),
        )

    def test_replaced_business_file_creates_a_new_document_binding_revision(self) -> None:
        self._save_document(document_id="doc-v1")
        first = self._chat(
            chat_id=1009,
            file_names=["hash-alpha.pdf"],
            message="first",
        )
        first.get_data()
        self._save_document(document_id="doc-v2")

        second = self._chat(
            chat_id=1009,
            file_names=["hash-alpha.pdf"],
            message="second",
        )
        second.get_data()

        documents = self.services.chat_store.document_bindings.list_by_chat(
            "1009"
        )
        document = self.services.chat_store.document_bindings.list_current_by_chat(
            "1009"
        )[0]
        binding_leases = [
            lease
            for lease in self.services.chat_store.resource_leases.list_by_chat(
                "1009"
            )
            if lease.resource_type == "document_binding"
        ]
        self.assertEqual("document:doc-v2", document.document_ref)
        self.assertEqual(2, len(documents))
        self.assertEqual(2, len(binding_leases))

    def test_delete_succeeds_for_leases_created_by_the_new_executor(self) -> None:
        response = self._chat(
            chat_id=1010,
            file_names=[],
            message="delete after this",
        )
        response.get_data()

        deleted = self.client.post(
            "/llm/chat/delete",
            json={
                "businessType": "chat",
                "params": {"chatId": 1010},
            },
        )

        self.assertEqual(200, deleted.status_code)
        self.assertTrue(deleted.get_json()["deleted"])
        self.assertEqual(1010, deleted.get_json()["chatId"])
        self.assertIsInstance(deleted.get_json()["chatId"], int)


if __name__ == "__main__":
    unittest.main()
