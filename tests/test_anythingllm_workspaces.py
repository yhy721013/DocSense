"""AnythingLLM 工作区原子客户端的离线契约测试。"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from app.integrations.anythingllm.errors import AnythingLLMProtocolError
from app.integrations.anythingllm.workspaces import AnythingLLMWorkspaceClient


class AnythingLLMWorkspaceClientTests(unittest.TestCase):
    """验证工作区 CRUD、文档集合、Pin 和检索的严格协议边界。"""

    def setUp(self) -> None:
        """为每个用例创建独立 Transport 替身和工作区客户端。"""
        self.transport = MagicMock()
        self.client = AnythingLLMWorkspaceClient(self.transport)

    def test_list_workspaces_normalizes_slug_and_id_aliases(self) -> None:
        """列表响应中的 slug/id 差异必须在适配层收敛。"""
        self.transport.get_json.return_value = {
            "workspaces": [
                {"id": 10, "slug": "alpha", "name": "Alpha"},
                {"id": "legacy-beta", "name": "Beta"},
            ]
        }

        workspaces = self.client.list_workspaces(user_id=1)

        self.assertEqual(workspaces[0].id, "10")
        self.assertEqual(workspaces[0].slug, "alpha")
        self.assertEqual(workspaces[1].slug, "legacy-beta")
        self.transport.get_json.assert_called_once_with("workspaces", user_id=1)

    def test_create_workspace_keeps_explicit_name_authoritative(self) -> None:
        """settings 中的同名字段不得覆盖调用方显式传入的工作区名称。"""
        self.transport.post_json.return_value = {
            "workspace": {"id": 8, "slug": "wanted", "name": "Wanted"}
        }

        workspace = self.client.create_workspace(
            "Wanted",
            settings={"name": "Unexpected", "chatMode": "query"},
            user_id=2,
        )

        self.assertEqual(workspace.slug, "wanted")
        request_payload = self.transport.post_json.call_args.args[1]
        self.assertEqual(request_payload["name"], "Wanted")
        self.assertEqual(request_payload["chatMode"], "query")

    def test_update_workspace_returns_normalized_updated_workspace(self) -> None:
        """工作区更新应使用独立原子端点，并规范化返回的工作区对象。"""
        self.transport.post_json.return_value = {
            "workspace": {"id": 8, "slug": "wanted", "name": "New Name"}
        }

        workspace = self.client.update_workspace(
            "wanted",
            {"name": "New Name", "topN": 10},
            user_id=2,
        )

        self.assertEqual(workspace.name, "New Name")
        self.transport.post_json.assert_called_once_with(
            "workspace/wanted/update",
            {"name": "New Name", "topN": 10},
            user_id=2,
        )

    def test_list_and_find_document_use_exact_normalized_reference(self) -> None:
        """文档查找应兼容别名与 URL 编码，但不得使用宽松子串匹配。"""
        self.transport.get_json.return_value = {
            "workspace": {
                "id": "ws-1",
                "slug": "ws-1",
                "documents": [
                    {
                        "docId": "doc-1",
                        "docpath": "custom-documents/%E7%A4%BA%E4%BE%8B.json",
                        "title": "示例",
                    }
                ],
            }
        }

        document = self.client.find_document(
            "ws-1",
            r"C:\storage\custom-documents\示例.json",
        )

        self.assertIsNotNone(document)
        self.assertEqual(document.id, "doc-1")
        missing = self.client.find_document("ws-1", "custom-documents/示例-副本.json")
        self.assertIsNone(missing)

    def test_find_document_derives_identity_from_exact_location_uuid(self) -> None:
        """工作区返回本地行 ID 时，精确 location 匹配仍应恢复全局上传文档身份。"""
        document_id = "bbeea606-4f61-443e-b74a-737c6fad18f3"
        location = f"custom-documents/sample-hash6.txt-{document_id}.json"
        self.transport.get_json.return_value = {
            "workspace": {
                "id": "architectureid-942",
                "slug": "architectureid-942",
                "documents": [
                    {
                        "id": 942,
                        "docId": "17",
                        "docpath": location,
                        "title": "sample-hash6.txt",
                    }
                ],
            }
        }

        document = self.client.find_document("architectureid-942", location)

        self.assertIsNotNone(document)
        self.assertEqual(document_id, document.id)
        self.assertEqual(f"document:{document_id}", document.document_ref)
        self.assertEqual("17", document.raw_document_id)
        self.assertEqual("location_uuid", document.identity_source)

    def test_update_embeddings_normalizes_paths_and_validates_workspace(self) -> None:
        """嵌入响应必须包含目标 workspace，且请求路径统一为相对文档位置。"""
        self.transport.post_json.return_value = {
            "workspace": {"id": 1, "slug": "target", "name": "Target"}
        }

        workspace = self.client.update_embeddings(
            "target",
            adds=[r"C:\storage\custom-documents\a.json"],
            deletes=["/custom-documents/b.json"],
            user_id=4,
        )

        self.assertEqual(workspace.slug, "target")
        self.transport.post_json.assert_called_once_with(
            "workspace/target/update-embeddings",
            {
                "adds": ["custom-documents/a.json"],
                "deletes": ["custom-documents/b.json"],
            },
            user_id=4,
        )

    def test_update_embeddings_rejects_missing_or_conflicting_workspace(self) -> None:
        """HTTP 2xx 不能替代业务校验：空 workspace 与 slug 冲突都必须失败。"""
        invalid_responses = (
            {},
            {"workspace": None},
            {"workspace": {}},
            {"workspace": {"id": 2, "slug": "other", "name": "Other"}},
        )
        for invalid_response in invalid_responses:
            with self.subTest(invalid_response=invalid_response):
                self.transport.post_json.return_value = invalid_response
                with self.assertRaises(AnythingLLMProtocolError):
                    self.client.update_embeddings("target", adds=["a.json"])

    def test_update_pin_requires_recognizable_success_message(self) -> None:
        """Pin 的 2xx JSON 必须包含成功语义，不能只依赖 HTTP 状态。"""
        self.transport.post_json.return_value = {
            "success": True,
            "message": "Document pin status updated successfully",
        }

        self.client.update_pin(
            "target",
            r"C:\storage\custom-documents\a.json",
            user_id=5,
        )

        self.transport.post_json.assert_called_once_with(
            "workspace/target/update-pin",
            {"docPath": "custom-documents/a.json", "pinStatus": True},
            user_id=5,
        )
        self.transport.post_json.return_value = {"success": True, "message": "accepted"}
        with self.assertRaises(AnythingLLMProtocolError):
            self.client.update_pin("target", "custom-documents/a.json")

    def test_vector_search_normalizes_source_and_allows_optional_fields(self) -> None:
        """检索结果应统一来源身份；缺失可选 ID、标题和 URL 不得导致失败。"""
        self.transport.post_json.return_value = {
            "results": [
                {
                    "pageContent": "片段文本",
                    "metadata": {
                        "sourceDocument": "custom-documents/%E7%A4%BA%E4%BE%8B.json",
                        "category": "demo",
                    },
                    "score": "0.85",
                    "distance": "0.15",
                }
            ]
        }

        sources = self.client.vector_search("target", "查询", top_n=3)

        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].document_ref, "name:示例.json")
        self.assertEqual(sources[0].text, "片段文本")
        self.assertEqual(sources[0].score, 0.85)
        self.assertEqual(sources[0].distance, 0.15)
        self.assertEqual(sources[0].metadata["category"], "demo")
        self.assertIsNone(sources[0].id)

    def test_delete_workspace_uses_status_only_transport_contract(self) -> None:
        """工作区删除应忽略不稳定正文，只委托 Transport 校验 HTTP 状态。"""

        with self.assertLogs(
            "app.integrations.anythingllm.workspaces",
            level="INFO",
        ) as captured_logs:
            self.client.delete_workspace("target", user_id=6)

        self.transport.delete_status.assert_called_once_with(
            "workspace/target",
            user_id=6,
        )
        self.assertIn(
            "workspace_slug=target",
            "\n".join(captured_logs.output),
        )


if __name__ == "__main__":
    unittest.main()
