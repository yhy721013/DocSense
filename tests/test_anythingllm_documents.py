"""AnythingLLM 文档原子客户端的离线契约测试。

测试只使用 Fake Transport 和临时文件，不建立网络连接。覆盖上传 DTO 归一化、必填字段
校验、Document Processor 有限重试、全局文档永久删除以及元数据响应的业务失败识别。
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from app.integrations.anythingllm.documents import (
    AnythingLLMDocumentClient,
    XlsxFolderCleanupToken,
)
from app.integrations.anythingllm.errors import (
    AnythingLLMCleanupUncertainError,
    AnythingLLMHTTPError,
    AnythingLLMProtocolError,
    AnythingLLMTimeoutError,
    AnythingLLMUploadRejectedError,
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

    def test_xlsx_sheet_identity_uses_full_location_hash_across_payload_ids(self) -> None:
        """同一 Sheet 的 upload/workspace ID 不同也必须得到相同稳定身份。"""
        location = "prepared-a.xlsx-6f2a/sheet-summary.json"
        uploaded = AnythingLLMDocument.from_payload(
            {"id": "collector-sheet-id", "location": location}
        )
        workspace = AnythingLLMDocument.from_payload(
            {"docId": "workspace-row-id", "docpath": location}
        )
        expected_id = (
            "location-sha256-"
            + hashlib.sha256(location.encode("utf-8")).hexdigest()
        )

        self.assertEqual(expected_id, uploaded.id)
        self.assertEqual(uploaded.id, workspace.id)
        self.assertEqual(uploaded.document_ref, workspace.document_ref)
        self.assertEqual("collector-sheet-id", uploaded.raw_document_id)
        self.assertEqual("workspace-row-id", workspace.raw_document_id)
        self.assertEqual("location_sha256", uploaded.identity_source)
        self.assertEqual("location_sha256", workspace.identity_source)

    def test_xlsx_different_sheet_locations_have_different_identity(self) -> None:
        """稳定身份必须包含完整 Sheet location，而不是四位父目录 token。"""
        first = AnythingLLMDocument.from_payload(
            {
                "id": "same-payload-id",
                "location": "prepared-a.xlsx-6f2a/sheet-summary.json",
            }
        )
        second = AnythingLLMDocument.from_payload(
            {
                "id": "same-payload-id",
                "location": "prepared-a.xlsx-6f2a/sheet-details.json",
            }
        )

        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(first.document_ref, second.document_ref)

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

    def test_upload_accepts_exactly_one_xlsx_sheet(self) -> None:
        """单 Sheet XLSX 应返回 location-hash 身份且不触发临时清理。"""
        location = "prepared-a.xlsx-6f2a/sheet-summary.json"
        self.transport.post_multipart.return_value = {
            "documents": [
                {
                    "id": "collector-sheet-id",
                    "location": location,
                    "title": "prepared-a.xlsx - Sheet:summary",
                }
            ]
        }

        document = self.client.upload_document(str(self.file_path), user_id=7)

        self.assertEqual(location, document.location)
        self.assertEqual("location_sha256", document.identity_source)
        self.transport.get_json.assert_not_called()
        self.transport.delete_json.assert_not_called()

    def test_upload_rejects_multi_sheet_after_confirmed_folder_cleanup(self) -> None:
        """多 Sheet 必须完整识别成员、删除整次上传后再稳定失败。"""
        folder = "prepared-a.xlsx-6f2a"
        self.transport.post_multipart.return_value = {
            "documents": [
                {
                    "id": "sheet-a",
                    "location": f"{folder}/sheet-summary.json",
                },
                {
                    "id": "sheet-b",
                    "location": f"{folder}/sheet-details.json",
                },
            ]
        }
        self.transport.get_json.return_value = {
            "folder": folder,
            "documents": [
                {"name": "sheet-details.json"},
                {"name": "sheet-summary.json"},
            ],
            "error": None,
        }
        self.transport.delete_json.return_value = {
            "success": True,
            "message": "Folder removed successfully",
        }

        with self.assertRaises(AnythingLLMUploadRejectedError) as raised:
            self.client.upload_document(str(self.file_path), user_id=7)

        self.assertTrue(raised.exception.cleanup_attempted)
        self.assertTrue(raised.exception.cleanup_confirmed)
        self.assertTrue(raised.exception.folder_cleanup_token)
        self.transport.get_json.assert_called_once_with(
            f"documents/folder/{folder}",
            user_id=7,
        )
        self.transport.delete_json.assert_called_once_with(
            "document/remove-folder",
            {"name": folder},
            user_id=7,
        )

    def test_upload_multi_sheet_cleanup_unknown_is_explicit_and_recoverable(self) -> None:
        """删除响应超时且目录仍在时不得把多 Sheet 清理伪装成成功。"""
        folder = "prepared-a.xlsx-6f2a"
        self.transport.post_multipart.return_value = {
            "documents": [
                {"id": "sheet-a", "location": f"{folder}/sheet-summary.json"},
                {"id": "sheet-b", "location": f"{folder}/sheet-details.json"},
            ]
        }
        self.transport.get_json.side_effect = [
            {
                "folder": folder,
                "documents": [
                    {"name": "sheet-summary.json"},
                    {"name": "sheet-details.json"},
                ],
                "error": None,
            },
            {
                "localFiles": {
                    "items": [{"name": folder, "type": "folder"}],
                    "name": "documents",
                    "type": "folder",
                }
            },
        ]
        self.transport.delete_json.side_effect = AnythingLLMTimeoutError(
            "remove-folder timeout"
        )

        with self.assertRaises(AnythingLLMUploadRejectedError) as raised:
            self.client.upload_document(str(self.file_path))

        self.assertTrue(raised.exception.cleanup_attempted)
        self.assertFalse(raised.exception.cleanup_confirmed)
        self.assertTrue(raised.exception.folder_cleanup_token)
        self.assertIsInstance(
            raised.exception.__cause__,
            AnythingLLMCleanupUncertainError,
        )

    def test_xlsx_malformed_dto_is_cleaned_using_locations_collected_first(
        self,
    ) -> None:
        """成员缺少 id 时仍应使用预先冻结的精确 Sheet 集合清理整批上传。"""
        folder = "prepared-a.xlsx-6f2a"
        self.transport.post_multipart.return_value = {
            "documents": [
                {
                    "location": f"{folder}/sheet-summary.json",
                },
                {
                    "id": "sheet-details",
                    "location": f"{folder}/sheet-details.json",
                },
            ]
        }
        self.transport.get_json.return_value = {
            "folder": folder,
            "documents": [
                {"name": "sheet-details.json"},
                {"name": "sheet-summary.json"},
            ],
            "error": None,
        }
        self.transport.delete_json.return_value = {
            "success": True,
            "message": "Folder removed successfully",
        }

        with self.assertLogs(
            "app.integrations.anythingllm.documents",
            level="WARNING",
        ) as logs:
            with self.assertRaises(AnythingLLMUploadRejectedError) as raised:
                self.client.upload_document(str(self.file_path), user_id=7)

        error = raised.exception
        self.assertTrue(error.cleanup_attempted)
        self.assertTrue(error.cleanup_confirmed)
        self.assertTrue(error.folder_cleanup_token)
        self.assertIsInstance(error.__cause__, AnythingLLMProtocolError)
        self.assertNotIn(error.folder_cleanup_token, "\n".join(logs.output))
        self.transport.get_json.assert_called_once_with(
            f"documents/folder/{folder}",
            user_id=7,
        )
        self.transport.delete_json.assert_called_once_with(
            "document/remove-folder",
            {"name": folder},
            user_id=7,
        )

    def test_xlsx_malformed_dto_cleanup_unknown_keeps_recovery_token(
        self,
    ) -> None:
        """畸形 DTO 的目录删除结果未知时必须向上层交付同一受控恢复 token。"""
        folder = "prepared-a.xlsx-6f2a"
        self.transport.post_multipart.return_value = {
            "documents": [
                {
                    "location": f"{folder}/sheet-summary.json",
                },
            ]
        }
        self.transport.get_json.side_effect = [
            {
                "folder": folder,
                "documents": [{"name": "sheet-summary.json"}],
                "error": None,
            },
            {
                "localFiles": {
                    "items": [{"name": folder, "type": "folder"}],
                    "name": "documents",
                    "type": "folder",
                }
            },
        ]
        self.transport.delete_json.side_effect = AnythingLLMTimeoutError(
            "remove-folder timeout"
        )

        with self.assertLogs(
            "app.integrations.anythingllm.documents",
            level="ERROR",
        ) as logs:
            with self.assertRaises(AnythingLLMUploadRejectedError) as raised:
                self.client.upload_document(str(self.file_path))

        error = raised.exception
        self.assertTrue(error.cleanup_attempted)
        self.assertFalse(error.cleanup_confirmed)
        self.assertTrue(error.folder_cleanup_token)
        self.assertIsInstance(
            error.__cause__,
            AnythingLLMCleanupUncertainError,
        )
        self.assertNotIn(error.folder_cleanup_token, "\n".join(logs.output))

    def test_malformed_xlsx_mixed_or_duplicate_locations_never_guess_cleanup(
        self,
    ) -> None:
        """即使 DTO 也畸形，混合目录和重复成员仍不得签发 token 或猜测删除。"""
        invalid_document_sets = (
            [
                {
                    "location": "prepared-a.xlsx-6f2a/sheet-a.json",
                },
                {
                    "id": "b",
                    "location": "prepared-b.xlsx-7e3b/sheet-b.json",
                },
            ],
            [
                {
                    "location": "prepared-a.xlsx-6f2a/sheet-a.json",
                },
                {
                    "id": "b",
                    "location": "prepared-a.xlsx-6f2a/sheet-a.json",
                },
            ],
        )
        for documents in invalid_document_sets:
            with self.subTest(documents=documents):
                self.transport.reset_mock()
                self.transport.post_multipart.return_value = {
                    "documents": documents
                }

                with self.assertRaises(AnythingLLMUploadRejectedError) as raised:
                    self.client.upload_document(str(self.file_path))

                self.assertFalse(raised.exception.cleanup_attempted)
                self.assertFalse(raised.exception.cleanup_confirmed)
                self.assertEqual("", raised.exception.folder_cleanup_token)
                self.transport.get_json.assert_not_called()
                self.transport.delete_json.assert_not_called()

    def test_upload_rejects_duplicate_mixed_or_multi_custom_documents(self) -> None:
        """畸形集合不得被截断成首项，也不得发起猜测性的文件夹删除。"""
        invalid_document_sets = (
            [
                {"id": "a", "location": "prepared-a.xlsx-6f2a/sheet-a.json"},
                {"id": "b", "location": "prepared-a.xlsx-6f2a/sheet-a.json"},
            ],
            [
                {"id": "a", "location": "prepared-a.xlsx-6f2a/sheet-a.json"},
                {"id": "b", "location": "prepared-b.xlsx-7e3b/sheet-b.json"},
            ],
            [
                {"id": "a", "location": "custom-documents/a.json"},
                {"id": "b", "location": "custom-documents/b.json"},
            ],
        )
        for documents in invalid_document_sets:
            with self.subTest(documents=documents):
                self.transport.reset_mock()
                self.transport.post_multipart.return_value = {
                    "documents": documents
                }
                with self.assertRaises(AnythingLLMUploadRejectedError):
                    self.client.upload_document(str(self.file_path))
                self.transport.delete_json.assert_not_called()

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

    def test_xlsx_cleanup_token_rejects_encoded_traversal_and_unicode_controls(self) -> None:
        """Folder token 不得接受会被 URL/文件系统二次解释的危险组件。"""
        invalid_locations = (
            "/safe.xlsx-abcd/sheet-a.json",
            r"C:\safe.xlsx-abcd\sheet-a.json",
            "safe.xlsx-abcd/../sheet-a.json",
            "safe..xlsx-abcd/sheet-a.json",
            "safe.xlsx-abcd/sheet-%2e%2e.json",
            "safe.xlsx-abcd/sheet-%2fetc.json",
            "safe.xlsx-abcd/sheet-\x7f.json",
            "safe.xlsx-abcd/sheet-\u0085.json",
            "safe.xlsx-abcd/sheet-\u202e.json",
            "safe.xlsx-abcd/sheet-\u2028.json",
            "custom-documents/sheet-a.json",
            "safe.xlsx-abcd/nested/sheet-a.json",
            "safe.docx-abcd/sheet-a.json",
        )
        for location in invalid_locations:
            with self.subTest(location=repr(location)):
                with self.assertRaises(ValueError):
                    XlsxFolderCleanupToken.issue((location,))

    def test_delete_document_artifact_keeps_custom_document_contract(self) -> None:
        """新增 Artifact 删除入口不得改变普通 custom-documents 的官方端点。"""
        self.transport.delete_json.return_value = {
            "success": True,
            "message": "Documents removed successfully",
        }

        self.client.delete_document_artifact(
            "custom-documents/a.json",
            user_id=7,
        )

        self.transport.get_json.assert_not_called()
        self.transport.delete_json.assert_called_once_with(
            "system/remove-documents",
            {"names": ["custom-documents/a.json"]},
            user_id=7,
        )

    def test_delete_xlsx_folder_rejects_member_drift_before_delete(self) -> None:
        """目录当前成员与上传快照不一致时必须拒绝破坏性 remove-folder。"""
        folder = "safe.xlsx-abcd"
        token = XlsxFolderCleanupToken.issue(
            (f"{folder}/sheet-summary.json",)
        )
        self.transport.get_json.return_value = {
            "folder": folder,
            "documents": [
                {"name": "sheet-summary.json"},
                {"name": "sheet-unexpected.json"},
            ],
            "error": None,
        }

        with self.assertRaises(AnythingLLMCleanupUncertainError):
            self.client.delete_xlsx_folder(token, user_id=7)

        self.transport.delete_json.assert_not_called()

    def test_delete_xlsx_folder_uses_validated_folder_endpoint(self) -> None:
        """严格单成员快照通过核对后才能调用 remove-folder。"""
        folder = "safe.xlsx-abcd"
        token = XlsxFolderCleanupToken.issue(
            (f"{folder}/sheet-summary.json",)
        )
        self.transport.get_json.return_value = {
            "folder": folder,
            "documents": [{"name": "sheet-summary.json"}],
            "error": None,
        }
        self.transport.delete_json.return_value = {
            "success": True,
            "message": "Folder removed successfully",
        }

        self.client.delete_xlsx_folder(token, user_id=7)

        self.transport.delete_json.assert_called_once_with(
            "document/remove-folder",
            {"name": folder},
            user_id=7,
        )

    def test_xlsx_folder_allows_common_business_name_and_quotes_request_path(self) -> None:
        """普通空格、括号和中文可用，但 folder-list 动态段必须做 URL 编码。"""
        folder = "报告 (最终版).xlsx-abcd"
        sheet_name = "sheet-工作表 1.json"
        token = XlsxFolderCleanupToken.issue(
            (f"{folder}/{sheet_name}",)
        )
        self.transport.get_json.return_value = {
            "folder": folder,
            "documents": [{"name": sheet_name}],
            "error": None,
        }
        self.transport.delete_json.return_value = {
            "success": True,
            "message": "Folder removed successfully",
        }

        self.client.delete_xlsx_folder(token, user_id=7)

        self.transport.get_json.assert_called_once_with(
            (
                "documents/folder/"
                "%E6%8A%A5%E5%91%8A%20%28%E6%9C%80%E7%BB%88%E7%89%88%29"
                ".xlsx-abcd"
            ),
            user_id=7,
        )
        self.transport.delete_json.assert_called_once_with(
            "document/remove-folder",
            {"name": folder},
            user_id=7,
        )

    def test_folder_list_404_is_not_treated_as_absent_when_root_still_has_folder(self) -> None:
        """旧版本缺少 folder-list 时，根目录仍见目标必须触发版本门禁。"""
        folder = "safe.xlsx-abcd"
        token = XlsxFolderCleanupToken.issue(
            (f"{folder}/sheet-summary.json",)
        )
        self.transport.get_json.side_effect = [
            AnythingLLMHTTPError(
                "not found",
                status_code=404,
                response_summary="",
            ),
            {
                "localFiles": {
                    "name": "documents",
                    "type": "folder",
                    "items": [{"name": folder, "type": "folder"}],
                }
            },
        ]

        with self.assertRaises(AnythingLLMCleanupUncertainError):
            self.client.delete_xlsx_folder(token)

        self.transport.delete_json.assert_not_called()

    def test_folder_list_and_root_404_keep_cleanup_outcome_unknown(self) -> None:
        """两个读取端点均 404 不能证明目录缺失，必须保持 fail-closed。"""
        token = XlsxFolderCleanupToken.issue(
            ("safe.xlsx-abcd/sheet-summary.json",)
        )
        self.transport.get_json.side_effect = [
            AnythingLLMHTTPError(
                "folder not found",
                status_code=404,
                response_summary="",
            ),
            AnythingLLMHTTPError(
                "root not found",
                status_code=404,
                response_summary="",
            ),
        ]

        with self.assertRaises(AnythingLLMCleanupUncertainError):
            self.client.delete_xlsx_folder(token)

        self.transport.delete_json.assert_not_called()

if __name__ == "__main__":
    unittest.main()
