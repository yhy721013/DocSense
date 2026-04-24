"""文件对话接口（/llm/chat*）单元测试。"""
import json
import unittest
from unittest.mock import patch, MagicMock

from app import create_app
from app.services.core.database import ChatDatabaseService
from tests import workspace_tempdir


class ChatRouteValidationTests(unittest.TestCase):
    """参数校验类测试 — 不依赖 AnythingLLM。"""

    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()
        self._tempdir = workspace_tempdir()
        self.tmp = self._tempdir.__enter__()
        self.chat_db = ChatDatabaseService(db_path=f"{self.tmp}/chat.sqlite3")
        self.chat_db_patcher = patch("app.blueprints.llm.chat_db", self.chat_db)
        self.chat_db_patcher.start()

    def tearDown(self):
        self.chat_db_patcher.stop()
        self._tempdir.__exit__(None, None, None)

    # ── POST /llm/chat 参数校验 ──

    def test_chat_rejects_invalid_business_type(self):
        resp = self.client.post("/llm/chat", json={"businessType": "wrong", "params": {}})
        self.assertEqual(resp.status_code, 400)

    def test_chat_rejects_missing_params(self):
        resp = self.client.post("/llm/chat", json={"businessType": "chat"})
        self.assertEqual(resp.status_code, 400)

    def test_chat_rejects_empty_chat_id(self):
        resp = self.client.post("/llm/chat", json={
            "businessType": "chat",
            "params": {"chatId": "", "fileNames": ["a.pdf"], "message": "hi"},
        })
        self.assertEqual(resp.status_code, 400)

    def test_chat_rejects_empty_file_names_for_new_chat(self):
        resp = self.client.post("/llm/chat", json={
            "businessType": "chat",
            "params": {"chatId": "c1", "fileNames": [], "message": "hi"},
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("新对话", resp.get_json()["error"])

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
        self.chat_db.create_chat("c-exist", ["a.pdf"], "ws-slug", "th-slug")
        # 仍然会走到 handle_chat_stream，但不会报参数错误
        # 这里 mock handle_chat_stream 以避免实际调用 AnythingLLM
        with patch("app.blueprints.llm.handle_chat_stream", return_value=iter([
            'event: chatInfo\ndata: {"chatId": "c-exist", "isNewChat": false}\n\n',
            'event: done\ndata: {"chatId": "c-exist"}\n\n',
        ])):
            resp = self.client.post("/llm/chat", json={
                "businessType": "chat",
                "params": {"chatId": "c-exist", "fileNames": [], "message": "继续聊"},
            })
        self.assertEqual(resp.status_code, 200)

    # ── GET /llm/chat/history 参数校验 ──

    def test_history_rejects_missing_chat_id(self):
        resp = self.client.get("/llm/chat/history")
        self.assertEqual(resp.status_code, 400)

    def test_history_returns_404_for_nonexistent_chat(self):
        resp = self.client.get("/llm/chat/history?chatId=nonexistent")
        self.assertEqual(resp.status_code, 404)

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


class ChatDeleteTests(unittest.TestCase):
    """删除对话的行为测试。"""

    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()
        self._tempdir = workspace_tempdir()
        self.tmp = self._tempdir.__enter__()
        self.chat_db = ChatDatabaseService(db_path=f"{self.tmp}/chat.sqlite3")
        self.chat_db_patcher = patch("app.blueprints.llm.chat_db", self.chat_db)
        self.chat_db_patcher.start()

    def tearDown(self):
        self.chat_db_patcher.stop()
        self._tempdir.__exit__(None, None, None)

    @patch("app.services.llm_service.chat_service.AnythingLLMClient", autospec=True)
    def test_delete_existing_chat_returns_200(self, _mock_client_cls):
        # 先手动创建一条对话记录
        self.chat_db.create_chat("del-test", ["a.pdf"], "ws-slug", "th-slug")

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
