import unittest
from unittest.mock import patch

from app import create_app
from app.services.core.database import ChatDatabaseService, DatabaseService
from tests import workspace_tempdir


class ChatDebugRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()
        self._tempdir = workspace_tempdir()
        self.tmp = self._tempdir.__enter__()
        self.chat_db = ChatDatabaseService(db_path=f"{self.tmp}/chat.sqlite3")
        self.kb_service = DatabaseService(db_path=f"{self.tmp}/knowledge.sqlite3")
        self.chat_patch = patch("app.blueprints.debug.chat_db", self.chat_db)
        self.kb_patch = patch("app.blueprints.debug.kb_service", self.kb_service)
        self.chat_patch.start()
        self.kb_patch.start()

    def tearDown(self):
        self.chat_patch.stop()
        self.kb_patch.stop()
        self._tempdir.__exit__(None, None, None)

    def test_chat_bootstrap_api_returns_local_sessions_and_files(self):
        self.chat_db.create_chat("conv-001", ["alpha.pdf"], "ws-1", "th-1")
        self.kb_service.save_document_record(
            "alpha.pdf",
            12,
            "doc-alpha",
            "custom-documents/doc-alpha.json",
        )

        response = self.client.get("/debug/api/chat/bootstrap")
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["data"]["sessions"][0]["chatId"], "conv-001")
        self.assertEqual(data["data"]["availableFiles"][0]["fileName"], "alpha.pdf")

    def test_chat_page_renders_shell(self):
        response = self.client.get("/debug/chat")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("文件对话调试页", html)
        self.assertIn("<h2>聊天</h2>", html)
        self.assertIn('id="page-message"', html)
        self.assertIn('id="refresh-button"', html)
        self.assertIn('id="chat-session-list"', html)
        self.assertIn('id="chat-id-input"', html)
        self.assertIn('id="chat-file-select"', html)
        self.assertIn('id="chat-message-input"', html)
        self.assertIn('id="load-history-button"', html)
        self.assertIn('id="chat-thread"', html)
        self.assertIn('id="chat-events"', html)
        self.assertIn("/debug/api/chat/bootstrap", html)
        self.assertIn('const CHAT_SEND_URL = "/llm/chat";', html)
        self.assertIn('const CHAT_HISTORY_URL = "/llm/chat/history";', html)
        self.assertIn('const CHAT_DELETE_URL = "/llm/chat/delete";', html)
        self.assertIn("function loadBootstrap()", html)
        self.assertIn("function renderSessionList(sessions)", html)
        self.assertIn("function renderAvailableFiles(files)", html)
        self.assertIn("function loadHistory()", html)
        self.assertIn("function sendCurrentMessage()", html)
        self.assertIn("function consumeSseStream(response)", html)
        self.assertIn("function handleSseBlock(block)", html)
        self.assertIn("function handleSseEvent(eventName, data)", html)
        self.assertIn("function deleteCurrentChat()", html)
        self.assertIn('if (state.isStreaming)', html)
        self.assertIn('setMessage("当前流式响应尚未结束")', html)
        self.assertIn("align-items: start;", html)
        self.assertIn("box-sizing: border-box;", html)
        self.assertIn("display: block;", html)
        self.assertIn("width: 100%;", html)
        self.assertIn("text-align: left;", html)
        self.assertIn("font: inherit;", html)
        self.assertIn("appearance: none;", html)
