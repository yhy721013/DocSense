"""AnythingLLM 文档原子客户端的离线契约测试。

测试只使用 Fake Transport 和临时文件，不建立网络连接。覆盖上传 DTO 归一化、必填字段
校验、Document Processor 有限重试、全局文档永久删除以及元数据响应的业务失败识别。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from app.integrations.anythingllm.documents import AnythingLLMDocumentClient
from app.integrations.anythingllm.errors import (
    AnythingLLMHTTPError,
    AnythingLLMProtocolError,
)
from app.integrations.anythingllm.models import AnythingLLMDocument, AnythingLLMSource


class AnythingLLMDocumentClientTests(unittest.TestCase):
    """验证文档客户端只处理上传、永久删除和元数据更新原子接口。"""

    def setUp(self) -> None:
        """创建传输替身与测试文件，避免依赖真实 Document Processor。"""
        self.transport = MagicMock()
        self.sleep = MagicMock()
        self.client = AnythingLLMDocumentClient(
            self.transport,
            sleep=self.sleep,
        )
        self.temp_directory = tempfile.TemporaryDirectory()
        self.file_path = Path(self.temp_directory.name) / "示例.txt"
        self.file_path.write_text("测试内容", encoding="utf-8")

    def tearDown(self) -> None:
        """删除单元测试创建的临时文件目录。"""
        self.temp_directory.cleanup()

    def test_upload_returns_normalized_document_from_real_response_fields(self) -> None:
        """上传必须使用响应真实 ID/location，并统一历史字段别名与路径分隔符。"""
        self.transport.post_multipart.return_value = {
            "documents": [
                {
                    "docId": "doc-1",
                    "docpath": r"C:\storage\documents\custom-documents\示例.txt-doc-1.json",
                    "title": "示例.txt",
                }
            ]
        }

        with self.assertLogs(
            "app.integrations.anythingllm.documents",
            level="INFO",
        ) as captured_logs:
            document = self.client.upload_document(str(self.file_path), user_id=7)

        self.assertEqual(document.id, "doc-1")
        self.assertEqual(
            document.location,
            "custom-documents/示例.txt-doc-1.json",
        )
        self.assertEqual(
            document.document_ref,
            "document:doc-1",
        )
        request = self.transport.post_multipart.call_args
        self.assertEqual(request.args[0], "document/upload")
        self.assertEqual(request.kwargs["user_id"], 7)
        self.assertEqual(request.kwargs["files"]["file"][0], "示例.txt")
        logs = "\n".join(captured_logs.output)
        self.assertIn("开始上传 AnythingLLM 文档", logs)
        self.assertIn("AnythingLLM 文档上传完成", logs)
        self.assertNotIn("测试内容", logs)

    def test_upload_rejects_missing_id_or_location_without_guessing(self) -> None:
        """上传响应缺少 ID 或位置时必须协议失败，不能根据文件名构造内部路径。"""
        invalid_documents = (
            {"location": "custom-documents/a.json"},
            {"id": "doc-1"},
        )
        for invalid_document in invalid_documents:
            with self.subTest(invalid_document=invalid_document):
                self.transport.post_multipart.return_value = {
                    "documents": [invalid_document]
                }
                with self.assertRaises(AnythingLLMProtocolError):
                    self.client.upload_document(str(self.file_path))

    def test_document_identity_prefers_global_doc_id_over_workspace_row_id(self) -> None:
        """工作区记录同时含 id/docId 时必须使用可跨 Workspace 复用的 docId。"""
        document = AnythingLLMDocument.from_payload(
            {
                "id": 42,
                "docId": "global-document-id",
                "docpath": "custom-documents/example.pdf-global-document-id.json",
                "title": "example.pdf",
            }
        )

        self.assertEqual("global-document-id", document.id)
        self.assertEqual("document:global-document-id", document.document_ref)

    def test_document_identity_prefers_location_uuid_over_ambiguous_doc_id(self) -> None:
        """location 携带上传 UUID 时，不得把工作区本地行 ID 当作全局文档身份。"""
        document_id = "bbeea606-4f61-443e-b74a-737c6fad18f3"
        document = AnythingLLMDocument.from_payload(
            {
                "id": 942,
                "docId": "17",
                "docpath": f"custom-documents/sample-hash6.txt-{document_id}.json",
                "title": "sample-hash6.txt",
            }
        )

        self.assertEqual(document_id, document.id)
        self.assertEqual(f"document:{document_id}", document.document_ref)
        self.assertEqual("17", document.raw_document_id)
        self.assertEqual("location_uuid", document.identity_source)

    def test_upload_serializes_source_marker_as_structured_metadata(self) -> None:
        """来源标记必须放入 multipart metadata，不能污染文件名或正文。"""
        marker = "docsense_ref:0123456789abcdef0123456789abcdef"
        metadata = {"docSource": marker}
        self.transport.post_multipart.return_value = {
            "documents": [
                {
                    "id": "doc-marker",
                    "location": "custom-documents/示例.txt-doc-marker.json",
                    "title": "示例.txt",
                }
            ]
        }

        with self.assertLogs(
            "app.integrations.anythingllm.documents",
            level="INFO",
        ) as captured_logs:
            self.client.upload_document(
                str(self.file_path),
                user_id=7,
                metadata=metadata,
            )

        request_kwargs = self.transport.post_multipart.call_args.kwargs
        self.assertEqual(
            {"docSource": marker},
            json.loads(request_kwargs["data"]["metadata"]),
        )
        self.assertEqual("示例.txt", request_kwargs["files"]["file"][0])
        self.assertNotIn(marker, self.file_path.read_text(encoding="utf-8"))
        self.assertNotIn(marker, "\n".join(captured_logs.output))

    def test_upload_metadata_serialization_fails_before_http(self) -> None:
        """不可序列化元数据必须在任何上传副作用之前失败。"""
        with self.assertRaises(ValueError):
            self.client.upload_document(
                str(self.file_path),
                metadata={"docSource": object()},
            )

        self.transport.post_multipart.assert_not_called()

    def test_upload_metadata_rejects_non_finite_numbers_before_http(self) -> None:
        """NaN 不是严格 JSON，不能进入可重试上传请求或协调身份。"""
        with self.assertRaises(ValueError):
            self.client.upload_document(
                str(self.file_path),
                metadata={"score": float("nan")},
            )

        self.transport.post_multipart.assert_not_called()

    def test_upload_metadata_rejects_non_string_or_empty_keys(self) -> None:
        """元数据键不得通过隐式字符串转换产生碰撞或无名字段。"""
        for metadata in ({1: "value"}, {"": "value"}, {"   ": "value"}):
            with self.subTest(metadata=metadata):
                with self.assertRaises(ValueError):
                    self.client.upload_document(
                        str(self.file_path),
                        metadata=metadata,  # type: ignore[arg-type]
                    )

        self.transport.post_multipart.assert_not_called()

    def test_source_display_reference_is_not_equal_to_uploaded_document_identity(self) -> None:
        """同名 title/URL 不能与真实上传 ID 形成可信相等关系。"""
        document_id = "b40936e4-6d24-496c-8d23-bcb4c4d7e8b7"
        file_name = "JUMV0235-JUMV-07-Jun-2024 - hash.pdf"
        document = AnythingLLMDocument.from_payload(
            {
                "id": document_id,
                "location": f"custom-documents/{file_name}-{document_id}.json",
                "title": file_name,
            }
        )
        source = AnythingLLMSource.from_payload(
            {
                "id": document_id,
                "title": file_name,
                "url": (
                    "file://C:\\Users\\dev\\AppData\\Roaming\\anythingllm-desktop"
                    f"\\storage\\hotdir\\{file_name}"
                ),
                "text": "证据片段",
            }
        )

        self.assertEqual(document.document_ref, f"document:{document_id}")
        self.assertEqual(source.document_ref, f"name:{file_name.casefold()}")
        self.assertNotEqual(source.document_ref, document.document_ref)
        self.assertTrue(source.document_ref)

    def test_windows_file_url_without_title_still_generates_reference(self) -> None:
        """来源缺少标题时，非标准 Windows file URL 仍应回退到完整文件名。"""
        source = AnythingLLMSource.from_payload(
            {
                "url": r"file://C:\Users\dev\storage\hotdir\example.pdf",
                "text": "证据片段",
            }
        )

        self.assertEqual(source.document_ref, "name:example.pdf")

    def test_source_marker_is_read_only_from_structured_doc_source(self) -> None:
        """合法标记只允许来自结构化字段，正文中的同形文本不能伪造身份。"""
        marker = "docsense_ref:0123456789abcdef0123456789abcdef"
        structured = AnythingLLMSource.from_payload(
            {
                "metadata": {"docSource": marker, "title": "example.pdf"},
                "text": "证据片段",
            }
        )
        forged_in_text = AnythingLLMSource.from_payload(
            {
                "metadata": {"title": "example.pdf"},
                "text": f"正文主动写入 {marker}",
            }
        )

        self.assertEqual(marker, structured.source_marker)
        self.assertIsNone(forged_in_text.source_marker)

    def test_source_marker_rejects_malformed_or_foreign_doc_source(self) -> None:
        """普通来源描述和格式错误标记不得被误识别为 DocSense 关联标记。"""
        invalid_values = (
            "用户上传的 PDF",
            "docsense_ref:too-short",
            "DOCSENSE_REF:0123456789abcdef0123456789abcdef",
            "docsense_ref:0123456789abcdef0123456789abcdeg",
        )
        for value in invalid_values:
            with self.subTest(value=value):
                source = AnythingLLMSource.from_payload(
                    {"docSource": value, "text": "证据"}
                )
                self.assertIsNone(source.source_marker)

    def test_source_marker_rejects_conflicting_structured_fields(self) -> None:
        """顶层和 metadata 的 docSource 冲突时必须失败关闭而不是选择优先级。"""
        source = AnythingLLMSource.from_payload(
            {
                "docSource": "docsense_ref:0123456789abcdef0123456789abcdef",
                "metadata": {
                    "docSource": "docsense_ref:ffffffffffffffffffffffffffffffff"
                },
                "text": "证据",
            }
        )

        self.assertIsNone(source.source_marker)

    def test_upload_retries_only_known_processor_outage_with_backoff(self) -> None:
        """已识别的 Processor 500 故障应指数退避，成功后停止继续重试。"""
        temporary_error = AnythingLLMHTTPError(
            "处理器离线",
            status_code=500,
            response_summary="Document processing API is not online",
        )
        self.transport.post_multipart.side_effect = [
            temporary_error,
            temporary_error,
            {
                "documents": [
                    {
                        "id": "doc-2",
                        "location": "custom-documents/b.json",
                        "title": "b.txt",
                    }
                ]
            },
        ]

        with self.assertLogs(
            "app.integrations.anythingllm.documents",
            level="WARNING",
        ) as captured_logs:
            document = self.client.upload_document(str(self.file_path))

        self.assertEqual(document.id, "doc-2")
        self.assertEqual(self.transport.post_multipart.call_count, 3)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [3.0, 6.0])
        logs = "\n".join(captured_logs.output)
        self.assertIn("attempt=1/4", logs)
        self.assertIn("delay_seconds=3.0", logs)

    def test_upload_retries_reuse_the_original_serialized_source_marker(self) -> None:
        """调用方在退避期间修改 Mapping 也不能改变同一逻辑上传的来源身份。"""
        original_marker = "docsense_ref:0123456789abcdef0123456789abcdef"
        replacement_marker = "docsense_ref:ffffffffffffffffffffffffffffffff"
        metadata = {"docSource": original_marker}
        temporary_error = AnythingLLMHTTPError(
            "处理器离线",
            status_code=500,
            response_summary="Document processing API is not online",
        )
        self.transport.post_multipart.side_effect = [
            temporary_error,
            {
                "documents": [
                    {
                        "id": "doc-retry",
                        "location": "custom-documents/retry-doc-retry.json",
                    }
                ]
            },
        ]

        def mutate_metadata_during_backoff(_: float) -> None:
            metadata["docSource"] = replacement_marker

        client = AnythingLLMDocumentClient(
            self.transport,
            sleep=mutate_metadata_during_backoff,
        )
        client.upload_document(str(self.file_path), metadata=metadata)

        serialized_values = [
            call.kwargs["data"]["metadata"]
            for call in self.transport.post_multipart.call_args_list
        ]
        self.assertEqual(2, len(serialized_values))
        self.assertTrue(all(original_marker in value for value in serialized_values))
        self.assertTrue(all(replacement_marker not in value for value in serialized_values))

    def test_upload_does_not_retry_unrecognized_http_error(self) -> None:
        """非白名单 HTTP 错误必须立即抛出，避免自动重放未知副作用请求。"""
        self.transport.post_multipart.side_effect = AnythingLLMHTTPError(
            "服务器错误",
            status_code=500,
            response_summary="unexpected failure",
        )

        with self.assertRaises(AnythingLLMHTTPError):
            self.client.upload_document(str(self.file_path))

        self.transport.post_multipart.assert_called_once()
        self.sleep.assert_not_called()

    def test_upload_retry_count_rejects_value_above_hard_cap(self) -> None:
        """上传额外重试次数超过三次时必须在构造阶段失败。"""
        with self.assertRaises(ValueError):
            AnythingLLMDocumentClient(
                self.transport,
                upload_max_retries=4,
                sleep=self.sleep,
            )

    def test_delete_document_uses_official_global_purge_endpoint(self) -> None:
        """永久删除必须使用上传返回的 location 调用官方全局清理接口。"""
        self.transport.delete_json.return_value = {
            "success": True,
            "message": "Documents removed successfully",
        }

        self.client.delete_document(
            r"C:\storage\documents\custom-documents\示例.txt-doc-1.json",
            user_id=7,
        )

        self.transport.delete_json.assert_called_once_with(
            "system/remove-documents",
            {"names": ["custom-documents/示例.txt-doc-1.json"]},
            user_id=7,
        )

    def test_delete_document_rejects_untrusted_or_parent_paths(self) -> None:
        """删除接口不得接受全局文档目录之外或包含父目录跳转的位置。"""
        invalid_locations = (
            "",
            "other-documents/a.json",
            "custom-documents/../a.json",
            "custom-documents",
        )
        for location in invalid_locations:
            with self.subTest(location=location):
                with self.assertRaises(ValueError):
                    self.client.delete_document(location)

        self.transport.delete_json.assert_not_called()

    def test_delete_document_requires_explicit_success_confirmation(self) -> None:
        """2xx 响应缺少 success=true 时仍应视为协议失败，不能假定删除完成。"""
        for response in ({}, {"success": False}, {"error": "failed"}):
            with self.subTest(response=response):
                self.transport.delete_json.return_value = response
                with self.assertRaises(AnythingLLMProtocolError):
                    self.client.delete_document("custom-documents/a.json")

if __name__ == "__main__":
    unittest.main()
