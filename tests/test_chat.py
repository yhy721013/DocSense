"""文件对话接口（/llm/chat*）单元测试。"""
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from app import create_app
from app.container import ApplicationServices, UploadTaskLimiter
from app.services.chat import (
    ChatCommandService,
    ChatHistoryService,
    ChatRunLockService,
    ChatStore,
    ChatStreamEvent,
    MESSAGE_COMMITTED,
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_USER,
)
from app.services.core.config import AnythingLLMConfig, LLMIntegrationConfig
from app.services.core.database import ChatDatabaseService, DatabaseService
from app.services.core.progress_hub import LLMProgressHub
from app.services.llm_service.task_service import LLMTaskService
from tests.fakes import (
    FakeChatConversationFactory,
    FakeDocumentRagFactory,
    FakeKnowledgeIndexFactory,
)


def _build_test_services(tmp: str) -> ApplicationServices:
    """构建完全离线的应用容器，避免路由测试触碰生产 SQLite 或网络依赖。"""
    chat_db_path = f"{tmp}/chat.sqlite3"
    chat_store = ChatStore(db_path=chat_db_path)
    return ApplicationServices(
        document_rag_factory=FakeDocumentRagFactory(),
        knowledge_index_factory=FakeKnowledgeIndexFactory(),
        chat_conversation_factory=FakeChatConversationFactory(),
        task_service=LLMTaskService(db_path=f"{tmp}/tasks.sqlite3"),
        kb_service=DatabaseService(db_path=f"{tmp}/knowledge.sqlite3"),
        chat_db=ChatDatabaseService(db_path=chat_db_path),
        chat_store=chat_store,
        chat_commands=ChatCommandService(ChatRunLockService(chat_db_path)),
        chat_history=ChatHistoryService(chat_store),
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


class ChatRouteValidationTests(unittest.TestCase):
    """参数校验类测试 — 不依赖 AnythingLLM。"""

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tempdir.__enter__()
        self.services = _build_test_services(self.tmp)
        self.chat_db = self.services.chat_db
        self.kb_service = self.services.kb_service
        self.app = create_app(services=self.services)
        self.client = self.app.test_client()

    def tearDown(self):
        self._tempdir.__exit__(None, None, None)

    @staticmethod
    def _stream_body(resp) -> str:
        return resp.get_data(as_text=True)

    def _save_document(
        self,
        file_name: str = "hash-alpha.pdf",
        *,
        original_name: str = "alpha原名.pdf",
        architecture_id: int = 1,
        anything_doc_id: str = "doc-alpha",
    ) -> None:
        self.kb_service.save_document_record(
            file_name,
            architecture_id,
            anything_doc_id,
            f"custom-documents/{anything_doc_id}.json",
            original_name=original_name,
        )

    # ── POST /llm/chat 参数校验 ──

    def test_chat_rejects_invalid_business_type(self):
        resp = self.client.post("/llm/chat", json={"businessType": "wrong", "params": {}})
        self.assertEqual(resp.status_code, 400)

    def test_chat_rejects_missing_params(self):
        resp = self.client.post("/llm/chat", json={"businessType": "chat"})
        self.assertEqual(resp.status_code, 400)

    def test_chat_rejects_file_names_that_are_not_list(self):
        resp = self.client.post("/llm/chat", json={
            "businessType": "chat",
            "params": {"chatId": "c1", "fileNames": "a.pdf", "message": "hi"},
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "fileNames必须为数组")

    def test_chat_rejects_empty_chat_id(self):
        resp = self.client.post("/llm/chat", json={
            "businessType": "chat",
            "params": {"chatId": "", "fileNames": ["a.pdf"], "message": "hi"},
        })
        self.assertEqual(resp.status_code, 400)

    def test_chat_accepts_empty_file_names_for_new_chat(self):
        """新对话传空 fileNames 不再报 400（允许创建不引用文件的对话）。"""
        with patch("app.blueprints.llm.AnythingLLMClient") as mock_client_cls, patch(
            "app.blueprints.llm.handle_chat_events",
            return_value=iter([
                ChatStreamEvent("chatInfo", {"chatId": "c1", "isNewChat": True}),
                ChatStreamEvent("done", {"chatId": "c1"}),
            ]),
        ) as mock_stream:
            resp = self.client.post("/llm/chat", json={
                "businessType": "chat",
                "params": {"chatId": "c1", "fileNames": [], "message": " hi "},
            })

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "text/event-stream")
        self.assertEqual(resp.headers["Cache-Control"], "no-cache")
        self.assertEqual(resp.headers["X-Accel-Buffering"], "no")
        self.assertEqual(
            self._stream_body(resp),
            'event: chatInfo\ndata: {"chatId": "c1", "isNewChat": true}\n\n'
            'event: done\ndata: {"chatId": "c1"}\n\n',
        )
        mock_stream.assert_called_once()
        kwargs = mock_stream.call_args.kwargs
        self.assertIs(kwargs["client"], mock_client_cls.return_value)
        self.assertEqual(kwargs["chat_id"], "c1")
        self.assertEqual(kwargs["file_names"], [])
        self.assertEqual(kwargs["file_original_names"], [])
        self.assertEqual(kwargs["message"], "hi")
        mock_client_cls.return_value.close.assert_called_once_with()
        self.assertEqual((), self.services.chat_store.runs.list_active("c1"))
        messages = self.services.chat_store.messages.list_by_chat("c1")
        self.assertEqual(1, len(messages))
        self.assertEqual(MESSAGE_ROLE_USER, messages[0].role)
        self.assertEqual(MESSAGE_COMMITTED, messages[0].status)
        self.assertEqual("hi", messages[0].content)

    def test_chat_returns_sse_events_for_resolved_file_request(self):
        """已解析文件进入 SSE 流，路由负责传入哈希文件名和原始文件名快照。"""
        self._save_document("hash-alpha.pdf", original_name="alpha原名.pdf")

        with patch("app.blueprints.llm.AnythingLLMClient") as mock_client_cls, patch(
            "app.blueprints.llm.handle_chat_events",
            return_value=iter([
                ChatStreamEvent("chatInfo", {"chatId": "c-sse", "isNewChat": True}),
                ChatStreamEvent("textChunk", {"content": "你好"}),
                ChatStreamEvent("done", {"chatId": "c-sse"}),
            ]),
        ) as mock_stream:
            resp = self.client.post("/llm/chat", json={
                "businessType": "chat",
                "params": {
                    "chatId": "c-sse",
                    "fileNames": ["hash-alpha.pdf"],
                    "message": " 请总结 ",
                },
            })

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "text/event-stream")
        body = self._stream_body(resp)
        self.assertIn('event: chatInfo\ndata: {"chatId": "c-sse", "isNewChat": true}', body)
        self.assertIn('event: textChunk\ndata: {"content": "你好"}', body)
        self.assertTrue(body.endswith('event: done\ndata: {"chatId": "c-sse"}\n\n'))
        kwargs = mock_stream.call_args.kwargs
        self.assertIs(kwargs["client"], mock_client_cls.return_value)
        self.assertIs(kwargs["chat_db"], self.chat_db)
        self.assertIs(kwargs["kb_service"], self.kb_service)
        self.assertEqual(kwargs["file_names"], ["hash-alpha.pdf"])
        self.assertEqual(kwargs["file_original_names"], ["alpha原名.pdf"])
        self.assertEqual(kwargs["message"], "请总结")

        mock_client_cls.return_value.close.assert_called_once_with()
        self.assertEqual((), self.services.chat_store.runs.list_active("c-sse"))
        messages = self.services.chat_store.messages.list_by_chat("c-sse")
        self.assertEqual(2, len(messages))
        self.assertEqual(MESSAGE_ROLE_USER, messages[0].role)
        self.assertEqual(MESSAGE_COMMITTED, messages[0].status)
        self.assertEqual("请总结", messages[0].content)
        self.assertEqual("hash-alpha.pdf", messages[0].files[0].file_name)
        self.assertEqual("alpha原名.pdf", messages[0].files[0].original_name)
        self.assertEqual(MESSAGE_ROLE_ASSISTANT, messages[1].role)
        self.assertEqual(MESSAGE_COMMITTED, messages[1].status)
        self.assertEqual("你好", messages[1].content)

    def test_chat_deduplicates_file_names_before_streaming(self):
        self._save_document("hash-alpha.pdf", original_name="alpha原名.pdf")

        with patch("app.blueprints.llm.AnythingLLMClient") as mock_client_cls, patch(
            "app.blueprints.llm.handle_chat_events",
            return_value=iter([
                ChatStreamEvent("chatInfo", {"chatId": "c-dedupe"}),
                ChatStreamEvent("done", {"chatId": "c-dedupe"}),
            ]),
        ) as mock_stream:
            resp = self.client.post("/llm/chat", json={
                "businessType": "chat",
                "params": {
                    "chatId": "c-dedupe",
                    "fileNames": ["hash-alpha.pdf", "hash-alpha.pdf"],
                    "message": "请总结",
                },
            })

        self.assertEqual(resp.status_code, 200)
        self._stream_body(resp)
        kwargs = mock_stream.call_args.kwargs
        self.assertIs(kwargs["client"], mock_client_cls.return_value)
        self.assertEqual(["hash-alpha.pdf"], kwargs["file_names"])
        self.assertEqual(["alpha原名.pdf"], kwargs["file_original_names"])
        messages = self.services.chat_store.messages.list_by_chat("c-dedupe")
        self.assertEqual(1, len(messages[0].files))
        self.assertEqual("hash-alpha.pdf", messages[0].files[0].file_name)

    def test_chat_rejects_duplicate_active_stream(self):
        self.services.chat_store.sessions.create_or_get(chat_id="c-busy")
        self.services.chat_store.runs.create(run_id="run-busy", chat_id="c-busy")
        self.services.chat_store.runs.mark_running("run-busy")

        with patch("app.blueprints.llm.AnythingLLMClient") as mock_client_cls, patch(
            "app.blueprints.llm.handle_chat_events",
            return_value=iter(()),
        ) as mock_stream:
            resp = self.client.post("/llm/chat", json={
                "businessType": "chat",
                "params": {
                    "chatId": "c-busy",
                    "fileNames": [],
                    "message": "hi",
                },
            })

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(
            resp.get_json(),
            {"error": "当前对话已有进行中的流式响应"},
        )
        mock_client_cls.assert_not_called()
        mock_stream.assert_not_called()

    def test_chat_error_event_releases_active_stream(self):
        with patch("app.blueprints.llm.AnythingLLMClient"), patch(
            "app.blueprints.llm.handle_chat_events",
            return_value=iter([
                ChatStreamEvent("error", {"error": "boom"}),
            ]),
        ):
            resp = self.client.post("/llm/chat", json={
                "businessType": "chat",
                "params": {
                    "chatId": "c-error",
                    "fileNames": [],
                    "message": "hi",
                },
            })

        self.assertEqual(resp.status_code, 200)
        self.assertIn("event: error", self._stream_body(resp))
        self.assertEqual((), self.services.chat_store.runs.list_active("c-error"))
        messages = self.services.chat_store.messages.list_by_chat("c-error")
        self.assertEqual(1, len(messages))
        self.assertEqual(MESSAGE_ROLE_USER, messages[0].role)
        self.assertEqual(MESSAGE_COMMITTED, messages[0].status)
        self.assertEqual("hi", messages[0].content)

    def test_chat_done_event_close_closes_stream_resource(self):
        from app.presenters.chat_stream import finalize_chat_run_stream

        on_close = MagicMock()
        generator = finalize_chat_run_stream(
            stream=iter([ChatStreamEvent("done", {"chatId": "c-close"})]),
            run_id="run-close",
            on_close=on_close,
        )

        self.assertEqual(
            'event: done\ndata: {"chatId": "c-close"}\n\n',
            next(generator),
        )
        generator.close()

        on_close.assert_called_once_with()

    def test_chat_client_construction_failure_releases_active_stream(self):
        self.app.config["PROPAGATE_EXCEPTIONS"] = False

        with patch(
            "app.blueprints.llm.AnythingLLMClient",
            side_effect=RuntimeError("client boom"),
        ):
            resp = self.client.post("/llm/chat", json={
                "businessType": "chat",
                "params": {
                    "chatId": "c-client-fail",
                    "fileNames": [],
                    "message": "hi",
                },
            })

        self.assertEqual(resp.status_code, 500)
        self.assertEqual(
            (),
            self.services.chat_store.runs.list_active("c-client-fail"),
        )
        with sqlite3.connect(self.services.chat_store.db_path) as connection:
            row = connection.execute(
                """
                SELECT status, error_message
                FROM chat_runs
                WHERE chat_id = ?
                """,
                ("c-client-fail",),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual("failed", row[0])
        self.assertIn("client boom", row[1])

    def test_chat_run_request_failure_releases_active_stream(self):
        self.app.config["PROPAGATE_EXCEPTIONS"] = False

        with patch(
            "app.blueprints.llm.ChatRunStreamRequest",
            side_effect=RuntimeError("request boom"),
        ), patch("app.blueprints.llm.AnythingLLMClient") as mock_client_cls:
            resp = self.client.post("/llm/chat", json={
                "businessType": "chat",
                "params": {
                    "chatId": "c-request-fail",
                    "fileNames": [],
                    "message": "hi",
                },
            })

        self.assertEqual(resp.status_code, 500)
        mock_client_cls.assert_not_called()
        self.assertEqual(
            (),
            self.services.chat_store.runs.list_active("c-request-fail"),
        )
        with sqlite3.connect(self.services.chat_store.db_path) as connection:
            row = connection.execute(
                """
                SELECT status, error_message
                FROM chat_runs
                WHERE chat_id = ?
                """,
                ("c-request-fail",),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual("failed", row[0])
        self.assertIn("request boom", row[1])

    def test_chat_rejects_empty_message(self):
        resp = self.client.post("/llm/chat", json={
            "businessType": "chat",
            "params": {"chatId": "c1", "fileNames": ["a.pdf"], "message": ""},
        })
        self.assertEqual(resp.status_code, 400)

    def test_chat_rejects_unresolved_file(self):
        """引用的文件未在 documents 表中，应返回 404。"""
        resp = self.client.post("/llm/chat", json={
            "businessType": "chat",
            "params": {"chatId": "c1", "fileNames": ["unknown.pdf"], "message": "hi"},
        })
        self.assertEqual(resp.status_code, 404)
        self.assertIn("尚未解析", resp.get_json()["error"])

    def test_chat_allows_empty_file_names_for_existing_chat(self):
        """已有会话时传空 fileNames 不报 400（增量语义：无新增文件）。"""
        self.chat_db.create_chat("c-exist", ["测试文件.pdf"], "ws-slug", "th-slug")
        # 仍然会走到 handle_chat_events，但不会报参数错误
        # 这里 mock handle_chat_events 以避免实际调用 AnythingLLM
        with patch("app.blueprints.llm.AnythingLLMClient") as mock_client_cls, patch(
            "app.blueprints.llm.handle_chat_events",
            return_value=iter([
                ChatStreamEvent("chatInfo", {"chatId": "c-exist", "isNewChat": False}),
                ChatStreamEvent("done", {"chatId": "c-exist"}),
            ]),
        ) as mock_stream:
            resp = self.client.post("/llm/chat", json={
                "businessType": "chat",
                "params": {"chatId": "c-exist", "fileNames": [], "message": "继续聊"},
            })
        self.assertEqual(resp.status_code, 200)
        self.assertIn('"isNewChat": false', self._stream_body(resp))
        kwargs = mock_stream.call_args.kwargs
        self.assertIs(kwargs["client"], mock_client_cls.return_value)
        self.assertEqual(kwargs["chat_id"], "c-exist")
        self.assertEqual(kwargs["file_names"], [])
        self.assertEqual(kwargs["file_original_names"], [])
        self.assertEqual(kwargs["message"], "继续聊")

    # ── GET /llm/chat/history 参数校验 ──

    def test_history_rejects_missing_chat_id(self):
        resp = self.client.get("/llm/chat/history")
        self.assertEqual(resp.status_code, 400)

    def test_history_returns_empty_list_for_nonexistent_chat(self):
        with patch("app.blueprints.llm.AnythingLLMClient") as mock_client_cls:
            resp = self.client.get("/llm/chat/history?chatId=nonexistent")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), [])
        mock_client_cls.assert_not_called()

    def test_history_returns_target_schema(self):
        self.services.chat_store.sessions.create_or_get(chat_id="conv-target")
        self.services.chat_store.runs.create(
            run_id="run-target",
            chat_id="conv-target",
        )
        self.services.chat_store.messages.append(
            message_id="message-target-user",
            chat_id="conv-target",
            run_id="run-target",
            role=MESSAGE_ROLE_USER,
            content="请总结该文件",
            status=MESSAGE_COMMITTED,
            files=(
                ("alpha.pdf", "alpha原名.pdf"),
                ("beta.pdf", "beta原名.pdf"),
            ),
        )
        self.services.chat_store.messages.append(
            message_id="message-target-assistant",
            chat_id="conv-target",
            run_id="run-target",
            role=MESSAGE_ROLE_ASSISTANT,
            content="抱歉，AI 服务出现错误",
            status=MESSAGE_COMMITTED,
        )

        with patch("app.blueprints.llm.AnythingLLMClient") as mock_client_cls:
            resp = self.client.get("/llm/chat/history?chatId=conv-target")

        self.assertEqual(resp.status_code, 200)
        mock_client_cls.assert_not_called()
        data = resp.get_json()
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["role"], "user")
        self.assertEqual(data[0]["content"], "请总结该文件")
        self.assertIsInstance(data[0]["timestamp"], int)
        self.assertEqual(
            data[0]["files"],
            [{"name": "alpha原名.pdf"}, {"name": "beta原名.pdf"}],
        )
        self.assertEqual(data[1]["role"], "assistant")
        self.assertEqual(data[1]["content"], "抱歉，AI 服务出现错误")
        self.assertIsInstance(data[1]["timestamp"], int)
        self.assertNotIn("files", data[1])

    def test_history_does_not_fallback_for_legacy_empty_local_history(self):
        self.chat_db.create_chat(
            "conv-legacy",
            ["测试文件.pdf"],
            "ws-slug",
            "th-slug",
        )

        with patch("app.blueprints.llm.AnythingLLMClient") as mock_client_cls:
            resp = self.client.get("/llm/chat/history?chatId=conv-legacy")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual([], resp.get_json())
        mock_client_cls.assert_not_called()

    # ── POST /llm/chat/delete 参数校验 ──

    def test_delete_rejects_invalid_business_type(self):
        resp = self.client.post("/llm/chat/delete", json={"businessType": "wrong", "params": {}})
        self.assertEqual(resp.status_code, 400)

    def test_delete_rejects_empty_chat_id(self):
        resp = self.client.post("/llm/chat/delete", json={
            "businessType": "chat",
            "params": {"chatId": ""},
        })
        self.assertEqual(resp.status_code, 400)

    def test_delete_returns_404_for_nonexistent_chat(self):
        resp = self.client.post("/llm/chat/delete", json={
            "businessType": "chat",
            "params": {"chatId": "nonexistent"},
        })
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["error"], "对话不存在")


class ChatDeleteTests(unittest.TestCase):
    """删除对话的行为测试。"""

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tempdir.__enter__()
        self.services = _build_test_services(self.tmp)
        self.chat_db = self.services.chat_db
        self.app = create_app(services=self.services)
        self.client = self.app.test_client()

    def tearDown(self):
        self._tempdir.__exit__(None, None, None)

    @patch("app.services.llm_service.chat_service.AnythingLLMClient", autospec=True)
    def test_delete_existing_chat_returns_200(self, _mock_client_cls):
        # 先手动创建一条对话记录
        self.chat_db.create_chat("del-test", ["测试文件.pdf"], "ws-slug", "th-slug")

        # mock AnythingLLMClient 实例方法
        mock_client = MagicMock()
        mock_client.delete_thread.return_value = True
        mock_client.delete_workspace.return_value = True

        with patch("app.blueprints.llm.AnythingLLMClient", return_value=mock_client):
            resp = self.client.post("/llm/chat/delete", json={
                "businessType": "chat",
                "params": {"chatId": "del-test"},
            })

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["deleted"])
        self.assertEqual(data["chatId"], "del-test")

        # 确认数据库记录已删除
        self.assertIsNone(self.chat_db.get_chat("del-test"))


if __name__ == "__main__":
    unittest.main()
