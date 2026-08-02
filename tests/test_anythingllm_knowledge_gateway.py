"""AnythingLLM 永久知识库 Gateway 的离线状态机测试。"""

from __future__ import annotations

import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

from app.integrations.anythingllm.documents import AnythingLLMDocumentClient
from app.integrations.anythingllm.knowledge_gateway import (
    AnythingLLMKnowledgeGateway,
)
from app.integrations.anythingllm.models import (
    AnythingLLMDocument,
    AnythingLLMWorkspace,
)
from app.integrations.anythingllm.workspaces import AnythingLLMWorkspaceClient
from app.ports import (
    CollectionSpec,
    KnowledgeDocumentMetadata,
    KnowledgeIndexConflictError,
    KnowledgeIndexRecoveryRequiredError,
    KnowledgeIndexDocumentReleasedError,
    KnowledgeIndexRetentionRequiredError,
    KnowledgeOperationContext,
    PreparedDocumentRef,
)
from app.services.core.database import DatabaseService
from app.services.llm_service.knowledge_index_operation_service import (
    STATUS_COMMITTED,
    STATUS_COMPENSATED,
    STATUS_COMPENSATION_FAILED,
    STATUS_EXTERNAL_SUCCEEDED,
    STATUS_EXTERNAL_DETACHED,
    STATUS_SUPERSEDED,
)
from app.services.llm_service.task_service import LLMTaskService
from tests import workspace_tempdir


class _KnowledgeGatewayHarness:
    """使用有状态 Mock 模拟 Workspace 绑定和全局文档生命周期。"""

    def __init__(self, root: Path) -> None:
        """创建相互隔离的本地数据库、协调服务和原子 Client Mock。"""
        self.task_service = LLMTaskService(str(root / "tasks.sqlite3"))
        self.database_service = DatabaseService(str(root / "knowledge.sqlite3"))
        self.document_client = Mock(spec=AnythingLLMDocumentClient)
        self.workspace_client = Mock(spec=AnythingLLMWorkspaceClient)
        self.workspace = AnythingLLMWorkspace(
            id="workspace-1",
            slug="architectureid-100",
            name="architectureId-100",
        )
        self.workspace_client.create_workspace.return_value = self.workspace
        self.workspace_client.get_workspace.return_value = self.workspace
        self.workspace_client.update_workspace.return_value = self.workspace
        self.workspace_client.update_pin.return_value = None
        self._global_documents: dict[str, AnythingLLMDocument] = {}
        self._bound_documents: dict[str, AnythingLLMDocument] = {}
        self._upload_sequence = 0
        self.document_client.upload_document.side_effect = self._upload
        self.document_client.delete_document.side_effect = self._delete_global
        self.document_client.delete_document_artifact.side_effect = self._delete_global
        self.workspace_client.update_embeddings.side_effect = self._update_embeddings
        self.workspace_client.find_document.side_effect = self._find_document
        self.gateway = AnythingLLMKnowledgeGateway(
            self.document_client,
            self.workspace_client,
            self.task_service.knowledge_index_operations,
            self.database_service,
            operation_lock=threading.RLock(),
            user_id=1,
        )
        self.collection = self.gateway.ensure_collection(
            CollectionSpec(architecture_id=100, name="architectureId-100")
        )

    @staticmethod
    def metadata(**overrides: object) -> KnowledgeDocumentMetadata:
        """返回控制字段与可扩展属性分离的标准文档元数据。"""
        attributes: dict[str, object] = {"country": "中国", "channel": "陆基"}
        attributes.update(overrides)
        return KnowledgeDocumentMetadata(
            file_name="hash.pdf",
            original_name="装备手册.pdf",
            ingested_file_name="hash.pdf",
            attributes=attributes,
        )

    @staticmethod
    def context(execution_id: str = "execution-1") -> KnowledgeOperationContext:
        """构造同一文件任务的可变执行身份。"""
        return KnowledgeOperationContext(
            execution_id=execution_id,
            business_type="file",
            business_key="hash.pdf",
        )

    def register_prepared_document(
        self,
        *,
        suffix: str = "prepared-id",
        content_sha256: str = "a" * 64,
    ) -> PreparedDocumentRef:
        """注册一份已经由临时 RAG 上传、但尚未绑定永久集合的全局文档。"""
        document = AnythingLLMDocument(
            id=suffix,
            location=f"custom-documents/hash.pdf-{suffix}.json",
            title="hash.pdf",
            document_ref=f"document:{suffix}",
        )
        self._global_documents[document.location] = document
        return PreparedDocumentRef(
            document_ref=document.document_ref,
            external_location=document.location,
            content_sha256=content_sha256,
            ingested_file_name="hash.pdf",
            structured_source_key="docsense_ref:" + "a" * 32,
        )

    def _upload(self, _file_path: str, **_kwargs) -> AnythingLLMDocument:
        """每次真实上传生成不同身份，用于发现错误的重复上传。"""
        self._upload_sequence += 1
        document = AnythingLLMDocument(
            id=f"uploaded-{self._upload_sequence}",
            location=(
                "custom-documents/hash.pdf-"
                f"uploaded-{self._upload_sequence}.json"
            ),
            title="hash.pdf",
            document_ref=f"document:uploaded-{self._upload_sequence}",
        )
        self._global_documents[document.location] = document
        return document

    def _delete_global(self, location: str, **_kwargs) -> None:
        """模拟官方全局删除同时清除永久集合关联。"""
        self._global_documents.pop(location, None)
        self._bound_documents.pop(location, None)

    def _update_embeddings(
        self,
        _workspace_slug: str,
        *,
        adds=None,
        deletes=None,
        **_kwargs,
    ) -> AnythingLLMWorkspace:
        """按原子 Client 契约模拟集合绑定和解除绑定。"""
        for location in adds or ():
            document = self._global_documents.get(location)
            if document is None:
                raise RuntimeError("待绑定全局文档不存在")
            self._bound_documents[location] = document
        for location in deletes or ():
            self._bound_documents.pop(location, None)
        return self.workspace

    def _find_document(
        self,
        _workspace_slug: str,
        location: str,
        **_kwargs,
    ) -> AnythingLLMDocument | None:
        """按完整外部位置返回当前集合中的文档。"""
        return self._bound_documents.get(location)


class AnythingLLMKnowledgeGatewayTests(unittest.TestCase):
    """验证永久写入、幂等复用、恢复和补偿语义。"""

    def test_collection_identity_reuses_local_architecture_mapping(self):
        """新任务必须按本地 architecture 映射复用 Workspace，而不是按名称猜测。"""
        with workspace_tempdir() as tmp:
            harness = _KnowledgeGatewayHarness(Path(tmp))
            second_gateway = AnythingLLMKnowledgeGateway(
                harness.document_client,
                harness.workspace_client,
                harness.task_service.knowledge_index_operations,
                harness.database_service,
                operation_lock=threading.RLock(),
                user_id=1,
            )

            reused = second_gateway.ensure_collection(
                CollectionSpec(architecture_id=100, name="architectureId-100")
            )

            self.assertEqual(harness.collection, reused)
            harness.workspace_client.get_workspace.assert_called_with(
                "architectureid-100",
                user_id=1,
            )
            self.assertEqual(1, harness.workspace_client.create_workspace.call_count)

    def test_workspace_policy_update_failure_is_retried(self):
        """远程策略更新失败时不得提前标记版本，下一任务必须继续应用。"""
        with workspace_tempdir() as tmp:
            harness = _KnowledgeGatewayHarness(Path(tmp))
            with sqlite3.connect(harness.task_service.db_path) as connection:
                connection.execute(
                    "UPDATE knowledge_index_collections SET policy_version = 0"
                )
            harness.workspace_client.update_workspace.side_effect = [
                RuntimeError("workspace update failed"),
                harness.workspace,
            ]
            gateway = AnythingLLMKnowledgeGateway(
                harness.document_client,
                harness.workspace_client,
                harness.task_service.knowledge_index_operations,
                harness.database_service,
                operation_lock=threading.RLock(),
                user_id=1,
                workspace_settings={"chatMode": "query", "topN": 6},
            )
            spec = CollectionSpec(architecture_id=100, name="architectureId-100")

            with self.assertRaisesRegex(RuntimeError, "workspace update failed"):
                gateway.ensure_collection(spec)
            collection = gateway.ensure_collection(spec)

            self.assertEqual("architectureid-100", collection.ref)
            self.assertEqual(2, harness.workspace_client.update_workspace.call_count)

    def test_prepared_document_is_transferred_without_upload_or_metadata_api(self):
        """RAG 预备文档只执行绑定和本地登记，不得产生第二次上传。"""
        with workspace_tempdir() as tmp:
            harness = _KnowledgeGatewayHarness(Path(tmp))
            prepared = harness.register_prepared_document()

            result = harness.gateway.store_prepared_document(
                harness.collection,
                prepared,
                harness.metadata(),
                operation_context=harness.context(),
                idempotency_key="file:100:sha256",
            )

            self.assertTrue(result.created)
            harness.document_client.upload_document.assert_not_called()
            self.assertFalse(hasattr(AnythingLLMDocumentClient, "update_metadata"))
            record = harness.database_service.get_document_record(
                "hash.pdf",
                architecture_id=100,
            )
            self.assertEqual(record["doc_path"], prepared.external_location)
            self.assertEqual(record["ingested_file_name"], "hash.pdf")
            self.assertEqual(
                record["metadata"],
                {
                    "country": "中国",
                    "channel": "陆基",
                    "docSource": prepared.structured_source_key,
                },
            )
            operation = harness.task_service.knowledge_index_operations.get(
                harness.collection.ref,
                "file:100:sha256",
            )
            self.assertEqual(operation.status, STATUS_COMMITTED)
            self.assertEqual(operation.metadata["ingested_file_name"], "hash.pdf")

    def test_single_sheet_xlsx_transfer_accepts_workspace_payload_id_change(self):
        """同一 nested location 的 Workspace 新 docId 不得阻断永久转交。"""
        with workspace_tempdir() as tmp:
            harness = _KnowledgeGatewayHarness(Path(tmp))
            location = "prepared-hash.xlsx-6f2a/sheet-summary.json"
            uploaded = AnythingLLMDocument.from_payload(
                {"id": "collector-sheet-id", "location": location}
            )
            harness._global_documents[location] = uploaded
            prepared = PreparedDocumentRef(
                document_ref=uploaded.document_ref,
                external_location=location,
                content_sha256="b" * 64,
                ingested_file_name="prepared-hash.xlsx",
                structured_source_key="docsense_ref:" + "b" * 32,
            )

            def find_with_workspace_id(
                _workspace_slug: str,
                requested_location: str,
                **_kwargs,
            ) -> AnythingLLMDocument | None:
                if requested_location not in harness._bound_documents:
                    return None
                return AnythingLLMDocument.from_payload(
                    {
                        "docId": "workspace-generated-id",
                        "docpath": requested_location,
                    }
                )

            harness.workspace_client.find_document.side_effect = find_with_workspace_id

            result = harness.gateway.store_prepared_document(
                harness.collection,
                prepared,
                harness.metadata(),
                operation_context=harness.context(),
                idempotency_key="xlsx-single-sheet",
            )

            self.assertTrue(result.created)
            self.assertEqual(uploaded.document_ref, result.document_ref)
            record = harness.database_service.get_document_record(
                "hash.pdf",
                architecture_id=100,
            )
            self.assertEqual(location, record["doc_path"])
            harness.document_client.delete_document_artifact.assert_not_called()

    def test_exact_replay_reuses_committed_operation(self):
        """相同幂等键的第二次提交必须复用，不重复绑定或 Pin。"""
        with workspace_tempdir() as tmp:
            harness = _KnowledgeGatewayHarness(Path(tmp))
            prepared = harness.register_prepared_document()
            arguments = {
                "collection": harness.collection,
                "document": prepared,
                "metadata": harness.metadata(),
                "operation_context": harness.context(),
                "idempotency_key": "same-key",
            }

            first = harness.gateway.store_prepared_document(**arguments)
            bind_calls = harness.workspace_client.update_embeddings.call_count
            pin_calls = harness.workspace_client.update_pin.call_count
            second = harness.gateway.store_prepared_document(**arguments)

            self.assertTrue(first.created)
            self.assertTrue(second.reused)
            self.assertEqual(first.document_ref, second.document_ref)
            self.assertEqual(
                bind_calls,
                harness.workspace_client.update_embeddings.call_count,
            )
            self.assertEqual(pin_calls, harness.workspace_client.update_pin.call_count)

    def test_same_key_with_different_metadata_is_rejected(self):
        """协调记录的不可变 metadata 快照不能被后到请求覆盖。"""
        with workspace_tempdir() as tmp:
            harness = _KnowledgeGatewayHarness(Path(tmp))
            prepared = harness.register_prepared_document()
            harness.gateway.store_prepared_document(
                harness.collection,
                prepared,
                harness.metadata(),
                operation_context=harness.context(),
                idempotency_key="metadata-key",
            )
            changed = harness.metadata(country="美国")

            with self.assertRaisesRegex(
                KnowledgeIndexConflictError,
                "metadata",
            ):
                harness.gateway.store_prepared_document(
                    harness.collection,
                    prepared,
                    changed,
                    operation_context=harness.context("execution-2"),
                    idempotency_key="metadata-key",
                )

    def test_new_content_replaces_old_binding_with_recoverable_saga(self):
        """同名新版本提交后必须解除旧版本绑定，避免新旧内容同时参与检索。"""
        with workspace_tempdir() as tmp:
            harness = _KnowledgeGatewayHarness(Path(tmp))
            first = harness.register_prepared_document()
            harness.gateway.store_prepared_document(
                harness.collection,
                first,
                harness.metadata(),
                operation_context=harness.context(),
                idempotency_key="version-one",
            )
            second = harness.register_prepared_document(
                suffix="prepared-id-v2",
                content_sha256="b" * 64,
            )

            replaced = harness.gateway.store_prepared_document(
                harness.collection,
                second,
                harness.metadata(),
                operation_context=harness.context("execution-2"),
                idempotency_key="version-two",
            )

            self.assertNotIn(first.external_location, harness._bound_documents)
            self.assertIn(second.external_location, harness._bound_documents)
            record = harness.database_service.get_document_record(
                "hash.pdf",
                architecture_id=100,
            )
            self.assertEqual(second.external_location, record["doc_path"])
            self.assertEqual(second.document_ref, replaced.document_ref)
            self.assertIsNone(
                harness.gateway.reconcile_document(
                    harness.collection,
                    operation_context=harness.context("execution-3"),
                    idempotency_key="version-one",
                )
            )

    def test_replacing_single_sheet_xlsx_preserves_global_folder_artifact(self):
        """XLSX 旧版本只从当前 Workspace 解绑，全局目录保留且新版本正常提交。"""
        with workspace_tempdir() as tmp:
            harness = _KnowledgeGatewayHarness(Path(tmp))
            old_location = "prepared-old.xlsx-6f2a/sheet-summary.json"
            old_document = AnythingLLMDocument.from_payload(
                {"id": "collector-old", "location": old_location}
            )
            harness._global_documents[old_location] = old_document
            old_prepared = PreparedDocumentRef(
                document_ref=old_document.document_ref,
                external_location=old_location,
                content_sha256="a" * 64,
                ingested_file_name="prepared-old.xlsx",
                structured_source_key="docsense_ref:" + "a" * 32,
            )
            harness.gateway.store_prepared_document(
                harness.collection,
                old_prepared,
                harness.metadata(),
                operation_context=harness.context(),
                idempotency_key="xlsx-version-one",
            )
            new_location = "prepared-new.xlsx-7e3b/sheet-summary.json"
            new_document = AnythingLLMDocument.from_payload(
                {"id": "collector-new", "location": new_location}
            )
            harness._global_documents[new_location] = new_document
            new_prepared = PreparedDocumentRef(
                document_ref=new_document.document_ref,
                external_location=new_location,
                content_sha256="b" * 64,
                ingested_file_name="prepared-new.xlsx",
                structured_source_key="docsense_ref:" + "b" * 32,
            )
            harness.document_client.delete_document_artifact.side_effect = (
                AssertionError("替换旧 XLSX 不应删除全局目录")
            )

            replaced = harness.gateway.store_prepared_document(
                harness.collection,
                new_prepared,
                harness.metadata(),
                operation_context=harness.context("execution-2"),
                idempotency_key="xlsx-version-two",
            )

            harness.workspace_client.update_embeddings.assert_any_call(
                harness.collection.ref,
                deletes=(old_location,),
                user_id=1,
            )
            harness.document_client.delete_document_artifact.assert_not_called()
            self.assertIn(old_location, harness._global_documents)
            self.assertNotIn(old_location, harness._bound_documents)
            self.assertIn(new_prepared.external_location, harness._bound_documents)
            self.assertEqual(new_prepared.document_ref, replaced.document_ref)
            old_operation = harness.task_service.knowledge_index_operations.get(
                harness.collection.ref,
                "xlsx-version-one",
            )
            new_operation = harness.task_service.knowledge_index_operations.get(
                harness.collection.ref,
                "xlsx-version-two",
            )
            self.assertIsNotNone(old_operation)
            self.assertIsNotNone(new_operation)
            self.assertEqual(STATUS_SUPERSEDED, old_operation.status)
            self.assertEqual(STATUS_COMMITTED, new_operation.status)

    def test_local_commit_failure_is_reconciled_without_rebinding(self):
        """外部成功后的本地瞬时失败必须只重试 SQLite 提交。"""
        with workspace_tempdir() as tmp:
            harness = _KnowledgeGatewayHarness(Path(tmp))
            prepared = harness.register_prepared_document()
            original_commit = harness.database_service.commit_indexed_document
            commit_attempts = 0

            def fail_once(**kwargs) -> None:
                nonlocal commit_attempts
                commit_attempts += 1
                if commit_attempts == 1:
                    raise sqlite3.OperationalError("database is locked")
                original_commit(**kwargs)

            harness.database_service.commit_indexed_document = fail_once
            with self.assertRaises(KnowledgeIndexRetentionRequiredError) as raised:
                harness.gateway.store_prepared_document(
                    harness.collection,
                    prepared,
                    harness.metadata(),
                    operation_context=harness.context(),
                    idempotency_key="recover-local",
                )
            self.assertTrue(raised.exception.retain_document_required)
            operation = harness.task_service.knowledge_index_operations.get(
                harness.collection.ref,
                "recover-local",
            )
            self.assertEqual(operation.status, STATUS_EXTERNAL_SUCCEEDED)
            bind_calls = harness.workspace_client.update_embeddings.call_count
            find_calls = harness.workspace_client.find_document.call_count
            pin_calls = harness.workspace_client.update_pin.call_count

            recovered = harness.gateway.reconcile_document(
                harness.collection,
                operation_context=harness.context("execution-2"),
                idempotency_key="recover-local",
            )

            self.assertTrue(recovered.reused)
            self.assertEqual(
                bind_calls,
                harness.workspace_client.update_embeddings.call_count,
            )
            self.assertEqual(find_calls, harness.workspace_client.find_document.call_count)
            self.assertEqual(pin_calls, harness.workspace_client.update_pin.call_count)

    def test_uploaded_document_is_globally_deleted_when_binding_fails(self):
        """Gateway 自己上传的文档在转交失败后必须执行全局补偿删除。"""
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            harness = _KnowledgeGatewayHarness(root)
            file_path = root / "hash.pdf"
            file_path.write_bytes(b"document content")
            original_update = harness.workspace_client.update_embeddings.side_effect
            failed = False

            def fail_add_once(*args, **kwargs):
                nonlocal failed
                if kwargs.get("adds") and not failed:
                    failed = True
                    raise RuntimeError("embedding failed")
                return original_update(*args, **kwargs)

            harness.workspace_client.update_embeddings.side_effect = fail_add_once
            with self.assertRaisesRegex(RuntimeError, "embedding failed"):
                harness.gateway.store_document(
                    harness.collection,
                    str(file_path),
                    harness.metadata(),
                    operation_context=harness.context(),
                    idempotency_key="upload-failure",
                )

            harness.document_client.delete_document.assert_called_once()
            operation = harness.task_service.knowledge_index_operations.get(
                harness.collection.ref,
                "upload-failure",
            )
            self.assertEqual(operation.status, STATUS_COMPENSATED)

    def test_final_coordinator_failure_requires_document_retention(self):
        """本地记录已提交但 committed 写入失败时，上层必须保留全局文档。"""
        with workspace_tempdir() as tmp:
            harness = _KnowledgeGatewayHarness(Path(tmp))
            prepared = harness.register_prepared_document()
            operations = harness.task_service.knowledge_index_operations
            original_transition = operations.transition
            failed = False

            def fail_committed_once(**kwargs):
                nonlocal failed
                if kwargs.get("target_status") == STATUS_COMMITTED and not failed:
                    failed = True
                    raise sqlite3.OperationalError("database is locked")
                return original_transition(**kwargs)

            operations.transition = fail_committed_once
            with self.assertRaises(KnowledgeIndexRetentionRequiredError):
                harness.gateway.store_prepared_document(
                    harness.collection,
                    prepared,
                    harness.metadata(),
                    operation_context=harness.context(),
                    idempotency_key="final-transition",
                )

            record = harness.database_service.get_document_record(
                "hash.pdf",
                architecture_id=100,
            )
            self.assertEqual(prepared.external_location, record["doc_path"])
            operation = operations.get(harness.collection.ref, "final-transition")
            self.assertEqual(STATUS_EXTERNAL_SUCCEEDED, operation.status)

    def test_prepared_document_compensation_allows_session_global_delete(self):
        """集合解绑及状态提交成功后，应明确通知 Session 可以执行全局删除。"""
        with workspace_tempdir() as tmp:
            harness = _KnowledgeGatewayHarness(Path(tmp))
            prepared = harness.register_prepared_document()
            original_update = harness.workspace_client.update_embeddings.side_effect
            failed = False

            def fail_add_once(*args, **kwargs):
                nonlocal failed
                if kwargs.get("adds") and not failed:
                    failed = True
                    raise RuntimeError("embedding failed")
                return original_update(*args, **kwargs)

            harness.workspace_client.update_embeddings.side_effect = fail_add_once
            with self.assertRaises(KnowledgeIndexDocumentReleasedError) as raised:
                harness.gateway.store_prepared_document(
                    harness.collection,
                    prepared,
                    harness.metadata(),
                    operation_context=harness.context(),
                    idempotency_key="prepared-failure",
                )

            self.assertFalse(raised.exception.retain_document_required)
            harness.document_client.delete_document.assert_not_called()
            operation = harness.task_service.knowledge_index_operations.get(
                harness.collection.ref,
                "prepared-failure",
            )
            self.assertEqual(operation.status, STATUS_COMPENSATED)

    def test_compensation_failure_blocks_automatic_replay(self):
        """解绑补偿失败后必须保留外部引用并阻断自动重放。"""
        with workspace_tempdir() as tmp:
            harness = _KnowledgeGatewayHarness(Path(tmp))
            prepared = harness.register_prepared_document()

            def always_fail(*_args, **_kwargs):
                raise RuntimeError("workspace unavailable")

            harness.workspace_client.update_embeddings.side_effect = always_fail
            with self.assertRaises(KnowledgeIndexRetentionRequiredError):
                harness.gateway.store_prepared_document(
                    harness.collection,
                    prepared,
                    harness.metadata(),
                    operation_context=harness.context(),
                    idempotency_key="compensation-failed",
                )
            operation = harness.task_service.knowledge_index_operations.get(
                harness.collection.ref,
                "compensation-failed",
            )
            self.assertEqual(operation.status, STATUS_COMPENSATION_FAILED)

            with self.assertRaisesRegex(
                KnowledgeIndexRecoveryRequiredError,
                "补偿失败",
            ):
                harness.gateway.store_prepared_document(
                    harness.collection,
                    prepared,
                    harness.metadata(),
                    operation_context=harness.context("execution-2"),
                    idempotency_key="compensation-failed",
                )

    def test_concurrent_same_key_uploads_only_once(self):
        """同一进程并发提交同键时只能产生一份全局文档。"""
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            harness = _KnowledgeGatewayHarness(root)
            file_path = root / "hash.pdf"
            file_path.write_bytes(b"same content")

            def store(index: int):
                return harness.gateway.store_document(
                    harness.collection,
                    str(file_path),
                    harness.metadata(),
                    operation_context=harness.context(f"execution-{index}"),
                    idempotency_key="concurrent-key",
                )

            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(store, range(8)))

            self.assertEqual(1, harness.document_client.upload_document.call_count)
            self.assertEqual(1, sum(result.created for result in results))
            self.assertEqual(7, sum(result.reused for result in results))

    def test_detach_only_unbinds_and_updates_local_state(self):
        """集合解绑不得调用具有全局破坏性的文档删除 API。"""
        with workspace_tempdir() as tmp:
            harness = _KnowledgeGatewayHarness(Path(tmp))
            prepared = harness.register_prepared_document()
            harness.gateway.store_prepared_document(
                harness.collection,
                prepared,
                harness.metadata(),
                operation_context=harness.context(),
                idempotency_key="detach-key",
            )

            result = harness.gateway.detach_document(
                harness.collection,
                prepared.external_location,
                operation_context=harness.context(),
            )

            self.assertTrue(result.success)
            harness.document_client.delete_document.assert_not_called()
            self.assertIsNone(
                harness.database_service.get_document_record(
                    "hash.pdf",
                    architecture_id=100,
                )
            )

    def test_detach_recovers_local_failure_without_false_committed_result(self):
        """外部解绑成功、本地删除失败时必须从 external_detached 继续恢复。"""
        with workspace_tempdir() as tmp:
            harness = _KnowledgeGatewayHarness(Path(tmp))
            prepared = harness.register_prepared_document()
            harness.gateway.store_prepared_document(
                harness.collection,
                prepared,
                harness.metadata(),
                operation_context=harness.context(),
                idempotency_key="detach-recovery",
            )
            original_delete = harness.database_service.delete_document_by_location
            failed = False

            def fail_once(**kwargs):
                nonlocal failed
                if not failed:
                    failed = True
                    raise sqlite3.OperationalError("database is locked")
                return original_delete(**kwargs)

            harness.database_service.delete_document_by_location = fail_once
            first = harness.gateway.detach_document(
                harness.collection,
                prepared.external_location,
                operation_context=harness.context(),
            )
            operation = harness.task_service.knowledge_index_operations.get(
                harness.collection.ref,
                "detach-recovery",
            )
            self.assertFalse(first.success)
            self.assertEqual(STATUS_EXTERNAL_DETACHED, operation.status)
            remote_calls = harness.workspace_client.find_document.call_count

            second = harness.gateway.detach_document(
                harness.collection,
                prepared.external_location,
                operation_context=harness.context("execution-2"),
            )

            self.assertTrue(second.success)
            self.assertEqual(remote_calls, harness.workspace_client.find_document.call_count)
            self.assertIsNone(
                harness.gateway.reconcile_document(
                    harness.collection,
                    operation_context=harness.context("execution-2"),
                    idempotency_key="detach-key",
                )
            )


if __name__ == "__main__":
    unittest.main()
