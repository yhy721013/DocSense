"""迁移期 AnythingLLMClient Facade 的离线兼容测试。

SSE 分帧和原子客户端协议分别由专用测试覆盖；本模块只验证旧方法签名、返回字典和委托
关系，确保阶段 2 不要求现有业务调用方同步迁移。
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.integrations.anythingllm.models import (
    AnythingLLMAnswer,
    AnythingLLMDocument,
    AnythingLLMSource,
    AnythingLLMWorkspace,
)
from app.services.core.config import AnythingLLMConfig
from app.services.utils.anythingllm_client import AnythingLLMClient


def _client() -> AnythingLLMClient:
    """创建不会主动发起网络请求的标准 Facade。"""
    return AnythingLLMClient(
        AnythingLLMConfig(
            base_url="http://anythingllm.local",
            api_key="test-key",
            timeout=30,
            storage_root=None,
        )
    )


class AnythingLLMClientFacadeTests(unittest.TestCase):
    """验证旧接口到原子客户端的兼容委托及结果转换。"""

    def setUp(self) -> None:
        """为每个用例创建任务独占 Facade，并替换三个原子客户端。"""
        self.client = _client()
        self.client.documents = MagicMock()
        self.client.workspaces = MagicMock()
        self.client.threads = MagicMock()

    def tearDown(self) -> None:
        """关闭真实但从未联网的底层 Session。"""
        self.client.close()

    def test_send_prompt_returns_legacy_cleaned_and_raw_response_keys(self) -> None:
        """同步问答 DTO 应转换为旧代码期望的三个返回字段。"""
        raw_answer = '<think>reasoning</think>```json\n{"summary":"摘要"}\n```'
        self.client.threads.ask.return_value = AnythingLLMAnswer(
            text='{"summary":"摘要"}',
            raw_text=raw_answer,
            sources=(
                AnythingLLMSource(
                    document_ref="custom-documents/example.json",
                    text="证据",
                ),
            ),
        )

        result = self.client.send_prompt_to_thread(
            "workspace-1",
            "thread-1",
            "提取摘要",
        )

        self.assertEqual(result["textResponse"], '{"summary":"摘要"}')
        self.assertEqual(result["rawTextResponse"], raw_answer)
        self.assertEqual(
            result["sources"][0]["document_ref"],
            "custom-documents/example.json",
        )

    def test_stream_chat_delegates_arguments_and_yields_original_chunks(self) -> None:
        """旧流式方法应保持生成器接口并完整转发工作区、线程和消息参数。"""
        self.client.threads.stream.return_value = iter(["你", "好"])

        chunks = list(
            self.client.stream_chat_to_thread(
                "workspace-1",
                "thread-1",
                "你好",
                user_id=2,
                mode="query",
                document_ids=["doc-1"],
            )
        )

        self.assertEqual(chunks, ["你", "好"])
        self.client.threads.stream.assert_called_once_with(
            "workspace-1",
            "thread-1",
            "你好",
            user_id=2,
            mode="query",
            document_ids=["doc-1"],
        )

    def test_upload_document_returns_both_new_and_legacy_aliases(self) -> None:
        """上传 DTO 应转换为同时包含 id/docId 与 location/docpath 的兼容字典。"""
        self.client.documents.upload_document.return_value = AnythingLLMDocument(
            id="doc-1",
            location="custom-documents/example.json",
            title="example.txt",
            document_ref="custom-documents/example.json",
        )

        result = self.client.upload_document("example.txt", user_id=3)

        self.assertEqual(result["id"], "doc-1")
        self.assertEqual(result["docId"], "doc-1")
        self.assertEqual(result["location"], "custom-documents/example.json")
        self.assertEqual(result["docpath"], "custom-documents/example.json")

    def test_vector_search_delegates_top_n_and_preserves_retrieval_fields(self) -> None:
        """Facade 应传递 top_n，并保留 weaponry 使用的 metadata 与 distance。"""
        self.client.workspaces.vector_search.return_value = [
            AnythingLLMSource(
                document_ref="name:example.pdf",
                text="检索片段",
                id="chunk-1",
                title="example.pdf",
                score=0.9,
                distance=0.1,
                metadata={"title": "example.pdf", "category": "demo"},
            )
        ]

        results = self.client.vector_search(
            "workspace-1",
            "检索问题",
            user_id=5,
            top_n=8,
        )

        self.client.workspaces.vector_search.assert_called_once_with(
            "workspace-1",
            "检索问题",
            top_n=8,
            user_id=5,
        )
        self.assertEqual(results[0]["metadata"]["category"], "demo")
        self.assertEqual(results[0]["distance"], 0.1)
        self.assertEqual(results[0]["document_ref"], "name:example.pdf")

    def test_list_workspace_documents_returns_legacy_aliases(self) -> None:
        """公开文档列表接口应委托 Workspace Client 并提供旧字段别名。"""
        self.client.workspaces.list_documents.return_value = [
            AnythingLLMDocument(
                id="doc-1",
                location="custom-documents/example.pdf-doc-1.json",
                title="example.pdf",
                document_ref="name:example.pdf",
            )
        ]

        documents = self.client.list_workspace_documents("workspace-1", user_id=6)

        self.client.workspaces.list_documents.assert_called_once_with(
            "workspace-1",
            user_id=6,
        )
        self.assertEqual(documents[0]["id"], "doc-1")
        self.assertEqual(documents[0]["docId"], "doc-1")
        self.assertEqual(
            documents[0]["docpath"],
            "custom-documents/example.pdf-doc-1.json",
        )

    def test_update_embeddings_keeps_legacy_best_effort_follow_up(self) -> None:
        """加入文档成功后应委托 Pin 和元数据；后两步失败不改变旧成功语义。"""
        self.client.workspaces.update_embeddings.return_value = AnythingLLMWorkspace(
            id="1",
            slug="workspace-1",
            name="Workspace",
        )
        self.client.workspaces.update_pin.side_effect = RuntimeError("pin failed")
        self.client.documents.update_metadata.side_effect = RuntimeError("meta failed")

        with self.assertLogs(
            "app.services.utils.anythingllm_client",
            level="WARNING",
        ):
            result = self.client.update_embeddings(
                r"C:\storage\custom-documents\example.json",
                "workspace-1",
                user_id=4,
                metadata={"file_name": "example.txt"},
            )

        self.assertTrue(result)
        self.client.workspaces.update_embeddings.assert_called_once_with(
            "workspace-1",
            adds=["custom-documents/example.json"],
            user_id=4,
        )
        self.client.workspaces.update_pin.assert_called_once()
        self.client.documents.update_metadata.assert_called_once()

    def test_list_workspaces_converts_dto_and_failure_keeps_empty_list_contract(self) -> None:
        """列表 DTO 应转换为字典；原子客户端异常仍按旧契约返回空列表。"""
        self.client.workspaces.list_workspaces.return_value = [
            AnythingLLMWorkspace(id="1", slug="alpha", name="Alpha")
        ]
        self.assertEqual(
            self.client.list_workspaces(),
            [{"id": "1", "slug": "alpha", "name": "Alpha"}],
        )

        self.client.workspaces.list_workspaces.side_effect = RuntimeError("failed")
        with self.assertLogs(
            "app.services.utils.anythingllm_client",
            level="ERROR",
        ):
            self.assertEqual(self.client.list_workspaces(), [])

    @patch("app.services.utils.anythingllm_client.Session")
    def test_constructor_closes_session_when_transport_configuration_is_invalid(
        self,
        session_class: MagicMock,
    ) -> None:
        """Transport 校验失败时 Facade 必须关闭尚未完成所有权转移的会话。"""
        session = MagicMock()
        session_class.return_value = session

        with self.assertRaises(ValueError):
            AnythingLLMClient(
                AnythingLLMConfig(
                    base_url="relative-url",
                    api_key="test-key",
                    timeout=30,
                    storage_root=None,
                )
            )

        session.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
