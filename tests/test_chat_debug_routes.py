import unittest
import tempfile
from unittest.mock import patch

from app import create_app
from app.modules.chat.domain.identity import FileChatIdentity
from tests.test_chat import _build_test_services


class ChatDebugRouteTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tempdir.__enter__()
        self.services = _build_test_services(self.tmp)
        self.chat_store = self.services.chat_store
        self.kb_service = self.services.kb_service
        self.app = create_app(services=self.services)
        self.client = self.app.test_client()

    def tearDown(self):
        self._tempdir.__exit__(None, None, None)

    def test_chat_bootstrap_api_returns_local_sessions_and_files(self):
        resolution = self.chat_store.identities.create_conversation(
            FileChatIdentity(chat_id=10001)
        )
        self.chat_store.sessions.update_refs(
            conversation_id=resolution.conversation_id,
            workspace_ref="ws-1",
            thread_ref="th-1",
        )
        self.chat_store.document_bindings.add(
            conversation_id=resolution.conversation_id,
            file_name="alpha.pdf",
            original_name="alpha.pdf",
            document_ref="document:doc-alpha",
            external_location="custom-documents/doc-alpha.json",
        )
        self.kb_service.save_document_record(
            "alpha.pdf",
            12,
            "doc-alpha",
            "custom-documents/doc-alpha.json",
            ingested_file_name="alpha.pdf",
        )

        response = self.client.get("/debug/api/chat/bootstrap")
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["data"]["sessions"][0]["chatId"], 10001)
        self.assertEqual(data["data"]["availableFiles"][0]["fileName"], "alpha.pdf")

    def test_active_scope_replaces_debug_selection_while_bindings_accumulate(
        self,
    ):
        """调试选择跟随 Active Scope，历史与累计绑定保持各自语义。"""

        self.kb_service.save_document_record(
            "beta.pdf",
            2,
            "doc-beta",
            "custom-documents/doc-beta.json",
            original_name="Beta 原名.pdf",
            ingested_file_name="beta.pdf",
        )
        self.kb_service.save_document_record(
            "alpha.pdf",
            1,
            "doc-alpha",
            "custom-documents/doc-alpha.json",
            original_name="Alpha 原名.pdf",
            ingested_file_name="alpha.pdf",
        )
        chat_response = self.client.post(
            "/llm/chat",
            json={
                "businessType": "chat",
                "params": {
                    "chatId": 10002,
                    "fileNames": [],
                    "message": "请总结",
                },
            },
        )
        chat_response.get_data()
        replace_response = self.client.post(
            "/llm/chat",
            json={
                "businessType": "chat",
                "params": {
                    "chatId": 10002,
                    "fileNames": ["beta.pdf"],
                    "message": "只看 Beta",
                },
            },
        )
        replace_response.get_data()

        with self.assertLogs(
            "app.modules.debug.application.queries",
            level="INFO",
        ) as captured:
            bootstrap_response = self.client.get("/debug/api/chat/bootstrap")
        history_response = self.client.get(
            "/llm/chat/history",
            query_string={"chatId": "10002"},
        )

        self.assertEqual(200, bootstrap_response.status_code)
        bootstrap = bootstrap_response.get_json()
        session = next(
            item
            for item in bootstrap["data"]["sessions"]
            if item["chatId"] == 10002
        )
        self.assertEqual(["beta.pdf"], session["fileNames"])
        history = history_response.get_json()
        self.assertEqual([], history[0]["files"])
        self.assertEqual([{"name": "Beta 原名.pdf"}], history[2]["files"])
        response_log = next(
            message
            for message in captured.output
            if "调试初始化数据读取完成" in message
        )
        self.assertIn("session_count=1", response_log)
        self.assertIn("active_scope_member_count=1", response_log)
        self.assertIn("workspace_binding_count=2", response_log)
        self.assertNotIn("alpha.pdf", response_log)

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
        self.assertIn('type="number"', html)
        self.assertIn('id="selected-files"', html)
        self.assertIn('id="toggle-file-picker-button"', html)
        self.assertIn('id="file-picker-panel"', html)
        self.assertIn('id="available-file-options"', html)
        self.assertIn('id="chat-shell"', html)
        self.assertIn('id="chat-toolbar"', html)
        self.assertIn('id="chat-context"', html)
        self.assertIn('id="chat-scroll-area"', html)
        self.assertIn('id="chat-composer"', html)
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
        self.assertIn("活动范围文件数：", html)
        self.assertIn("function toggleFilePicker()", html)
        self.assertIn("function renderSelectedFiles()", html)
        self.assertIn("function renderFilePickerOptions(files)", html)
        self.assertIn("function toggleSelectedFile(fileName)", html)
        self.assertIn("function removeSelectedFile(fileName)", html)
        self.assertIn('id="debug-details"', html)
        self.assertIn('id="debug-summary"', html)
        self.assertIn("function loadHistory()", html)
        self.assertIn("function readChatId()", html)
        self.assertIn("function sendCurrentMessage()", html)
        self.assertIn("function consumeSseStream(response)", html)
        self.assertIn("function handleSseBlock(block)", html)
        self.assertIn("function handleSseEvent(eventName, data)", html)
        self.assertIn("function renderDebugEventList()", html)
        self.assertIn("function deleteCurrentChat()", html)
        self.assertNotIn('id="chat-file-select"', html)
        self.assertNotIn("<h2>聊天记录</h2>", html)
        self.assertNotIn("<h2>SSE 事件流</h2>", html)
        self.assertNotIn("function renderEventList()", html)
        self.assertIn('if (state.isStreaming)', html)
        self.assertIn('setMessage("当前流式响应尚未结束")', html)
        self.assertIn("state.selectedFileNames = [...item.fileNames];", html)
        self.assertIn("renderSelectedFiles();", html)
        self.assertIn("renderFilePickerOptions(state.availableFiles);", html)
        self.assertIn("state.selectedFileNames = state.selectedFileNames.filter((item) => item !== fileName);", html)
        self.assertIn(
            "const fileNames = state.selectedFileNames.filter((fn) => !sentSet.has(fn));",
            html,
        )
        self.assertIn("align-items: start;", html)
        self.assertIn("box-sizing: border-box;", html)
        self.assertIn("display: block;", html)
        self.assertIn("width: 100%;", html)
        self.assertIn("text-align: left;", html)
        self.assertIn("font: inherit;", html)
        self.assertIn("appearance: none;", html)

    def test_chat_bootstrap_query_failure_keeps_http_200_and_stable_shape(self):
        """内部只读查询失败不能改变既有 Debug HTTP 状态或字段集合。"""

        snapshots = self.services.debug_services.chat_bootstrap._snapshots
        with patch.object(
            snapshots,
            "read_snapshot",
            side_effect=RuntimeError("boom"),
        ):
            response = self.client.get("/debug/api/chat/bootstrap")

        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertFalse(payload["ok"])
        self.assertEqual(
            {"sessions": [], "availableFiles": []},
            payload["data"],
        )
        self.assertEqual("读取失败: boom", payload["message"])
