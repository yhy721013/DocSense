"""AnythingLLM 永久知识库 Gateway 与可恢复写入状态机。

本模块实现供应商无关 ``KnowledgeIndexPort``。它负责永久 Workspace 的确保、文档上传或
所有权转交、绑定、Pin、来源实体确认、本地权威记录提交以及失败补偿。业务层只传递集合
引用、预备文档句柄、业务元数据和幂等身份，不接触 AnythingLLM HTTP 路径或响应字段。
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, ContextManager, Mapping, Optional

from app.integrations.anythingllm.documents import AnythingLLMDocumentClient
from app.integrations.anythingllm.models import (
    DOCSENSE_SOURCE_MARKER_PREFIX,
    AnythingLLMDocument,
    parse_xlsx_sheet_location,
)
from app.integrations.anythingllm.policies import DOCUMENT_RAG_WORKSPACE_POLICY_VERSION
from app.integrations.anythingllm.workspaces import AnythingLLMWorkspaceClient
from app.ports import (
    CollectionRef,
    CollectionSpec,
    IndexedDocument,
    KnowledgeDocumentMetadata,
    KnowledgeIndexConflictError,
    KnowledgeIndexDocumentReleasedError,
    KnowledgeIndexError,
    KnowledgeIndexRecoveryRequiredError,
    KnowledgeIndexRetentionRequiredError,
    KnowledgeOperationContext,
    OperationResult,
    PreparedDocumentRef,
)
from app.services.core.database import DatabaseService
from app.services.llm_service.knowledge_index_operation_service import (
    STATUS_COMMITTED,
    STATUS_COMPENSATED,
    STATUS_COMPENSATION_FAILED,
    STATUS_DOCUMENT_READY,
    STATUS_DETACHING,
    STATUS_EXTERNAL_DETACHED,
    STATUS_EXTERNAL_SUCCEEDED,
    STATUS_PENDING,
    STATUS_REPLACEMENT_CLEANUP_PENDING,
    STATUS_SUPERSEDED,
    STATUS_UPLOADING,
    KnowledgeIndexOperationRecord,
    KnowledgeIndexOperationService,
)


logger = logging.getLogger(__name__)


class AnythingLLMKnowledgeGateway:
    """把永久知识库业务契约编排为可恢复的 AnythingLLM 原子操作。"""

    def __init__(
        self,
        document_client: AnythingLLMDocumentClient,
        workspace_client: AnythingLLMWorkspaceClient,
        operation_service: KnowledgeIndexOperationService,
        database_service: DatabaseService,
        *,
        operation_lock: ContextManager[object] | None = None,
        operation_lock_factory: Callable[[str], ContextManager[object]] | None = None,
        user_id: int | None = 1,
        workspace_settings: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """保存任务级 Client 和共享协调依赖，不执行网络请求。"""
        if not isinstance(operation_service, KnowledgeIndexOperationService):
            raise TypeError("operation_service 类型无效")
        if not isinstance(database_service, DatabaseService):
            raise TypeError("database_service 类型无效")
        if operation_lock_factory is None:
            if operation_lock is None or not (
                hasattr(operation_lock, "__enter__")
                and hasattr(operation_lock, "__exit__")
            ):
                raise TypeError("必须提供 operation_lock 或 operation_lock_factory")
            fixed_operation_lock = operation_lock
            operation_lock_factory = lambda _key: fixed_operation_lock
        elif not callable(operation_lock_factory):
            raise TypeError("operation_lock_factory 必须可调用")
        if user_id is not None and (
            isinstance(user_id, bool)
            or not isinstance(user_id, int)
            or user_id < 1
        ):
            raise ValueError("user_id 必须是正整数或 None")
        self._document_client = document_client
        self._workspace_client = workspace_client
        self._operation_service = operation_service
        self._database_service = database_service
        self._operation_lock_factory = operation_lock_factory
        self._user_id = user_id
        self._workspace_settings = dict(workspace_settings or {})
        self._known_collections: dict[str, CollectionRef] = {}

    def ensure_collection(self, spec: CollectionSpec) -> CollectionRef:
        """按本地 architecture 权威映射复用或协调创建永久 Workspace。

        显示名称不再参与既有 Workspace 的身份推断。已有本地映射优先；没有映射时先在
        任务数据库中取得跨进程创建预留，再调用 AnythingLLM。创建结果写入预留后即使本地
        知识库暂时不可写，后续重试也只会复用同一 slug，不会再次创建同名 Workspace。
        """
        if not isinstance(spec, CollectionSpec):
            raise TypeError("spec 必须是 CollectionSpec")
        with self._operation_lock_factory(f"architecture:{spec.architecture_id}"):
            cached = next(
                (collection for collection in self._known_collections.values()
                 if collection.architecture_id == spec.architecture_id),
                None,
            )
            if cached is not None:
                if cached.name.casefold() != spec.name.casefold():
                    raise KnowledgeIndexConflictError(
                        "同一 architecture_id 在当前任务中使用了不同集合名称"
                    )
                return cached

            mapped_slug = self._database_service.get_workspace_slug(spec.architecture_id)
            current_policy_version = DOCUMENT_RAG_WORKSPACE_POLICY_VERSION
            if mapped_slug:
                current_policy_version = self._operation_service.register_existing_collection(
                    architecture_id=spec.architecture_id,
                    collection_name=spec.name,
                    workspace_slug=mapped_slug,
                )
                workspace = self._workspace_client.get_workspace(
                    mapped_slug,
                    user_id=self._user_id,
                )
                reused = True
            else:
                reservation = self._operation_service.reserve_collection(
                    architecture_id=spec.architecture_id,
                    collection_name=spec.name,
                )
                current_policy_version = reservation.policy_version
                if reservation.owns_reservation:
                    workspace = self._workspace_client.create_workspace(
                        spec.name,
                        settings=self._workspace_settings or None,
                        user_id=self._user_id,
                    )
                    self._operation_service.complete_collection_reservation(
                        reservation=reservation,
                        workspace_slug=workspace.slug,
                        policy_version=DOCUMENT_RAG_WORKSPACE_POLICY_VERSION,
                    )
                    reused = False
                else:
                    workspace = self._workspace_client.get_workspace(
                        reservation.workspace_slug,
                        user_id=self._user_id,
                    )
                    reused = True
                self._database_service.add_workspace(
                    spec.architecture_id,
                    workspace.slug,
                )

            if (
                reused
                and current_policy_version < DOCUMENT_RAG_WORKSPACE_POLICY_VERSION
                and self._workspace_settings
            ):
                workspace = self._workspace_client.update_workspace(
                    workspace.slug,
                    self._workspace_settings,
                    user_id=self._user_id,
                )
                self._operation_service.mark_collection_policy_applied(
                    architecture_id=spec.architecture_id,
                    workspace_slug=workspace.slug,
                    policy_version=DOCUMENT_RAG_WORKSPACE_POLICY_VERSION,
                )

            collection = CollectionRef(
                ref=workspace.slug,
                name=spec.name,
                architecture_id=spec.architecture_id,
            )
            self._known_collections[collection.ref] = collection
            logger.info(
                "永久知识集合已就绪: architecture_id=%s collection_name_chars=%d "
                "has_collection_ref=%s reused=%s",
                collection.architecture_id,
                len(collection.name),
                bool(collection.ref),
                reused,
            )
            return collection

    def store_document(
        self,
        collection: CollectionRef,
        file_path: str,
        metadata: KnowledgeDocumentMetadata,
        *,
        operation_context: KnowledgeOperationContext,
        idempotency_key: str,
    ) -> IndexedDocument:
        """上传新文档并以协调状态机提交到永久知识集合。

        文件摘要在任何外部调用前计算并保存到协调记录。首次上传只写入用于来源追踪的
        ``docSource``；业务 metadata 始终以 DocSense 本地数据库为权威，不调用部署中不
        支持的上传后元数据更新接口。
        """
        known_collection = self._require_collection(collection)
        normalized_path = str(Path(file_path))
        path = Path(normalized_path)
        if not path.is_file():
            raise FileNotFoundError(f"待索引文件不存在或不是普通文件: {path}")
        metadata_snapshot = self._normalize_metadata(metadata)
        normalized_key = self._required_text(
            idempotency_key,
            name="idempotency_key",
        )
        requested_marker = (
            f"{DOCSENSE_SOURCE_MARKER_PREFIX}{secrets.token_hex(16)}"
        )

        # 先把调用方可能并发改写的源文件复制到任务私有目录。摘要与 multipart 上传都针对
        # 同一个不可变副本，彻底消除“摘要完成后源路径内容被替换”的 TOCTOU 窗口。
        with tempfile.TemporaryDirectory(prefix="docsense-index-") as temporary_dir:
            snapshot_path = Path(temporary_dir) / path.name
            shutil.copyfile(path, snapshot_path)
            source_digest = self._sha256_file(snapshot_path)
            with snapshot_path.open("rb"):
                pass

            with self._operation_lock_factory(
                f"architecture:{known_collection.architecture_id}"
            ):
                existing = self._operation_service.get(
                    known_collection.ref,
                    normalized_key,
                )
                record = self._operation_service.begin(
                    collection_ref=known_collection.ref,
                    idempotency_key=normalized_key,
                    operation_context=operation_context,
                    source_kind="upload",
                    source_digest=source_digest,
                    metadata=metadata_snapshot,
                    source_marker=requested_marker,
                )
                if record.status == STATUS_COMMITTED:
                    return self._indexed_result(record, created=False)
                if record.status == STATUS_UPLOADING:
                    raise KnowledgeIndexRecoveryRequiredError(
                        "既有上传操作停留在 uploading，无法证明上游是否已创建文档"
                    )
                if record.status == STATUS_PENDING and not record.external_location:
                    record = self._operation_service.transition(
                        collection_ref=record.collection_ref,
                        idempotency_key=record.idempotency_key,
                        expected_statuses={STATUS_PENDING},
                        target_status=STATUS_UPLOADING,
                        last_error="",
                    )
                    try:
                        document = self._document_client.upload_document(
                            str(snapshot_path),
                            user_id=self._user_id,
                            metadata={"docSource": record.source_marker},
                        )
                    except Exception as exc:
                        self._handle_unknown_upload_failure(record, exc)
                        raise
                    try:
                        record = self._operation_service.transition(
                            collection_ref=record.collection_ref,
                            idempotency_key=record.idempotency_key,
                            expected_statuses={STATUS_UPLOADING},
                            target_status=STATUS_DOCUMENT_READY,
                            document_ref=document.document_ref,
                            external_document_id=document.id,
                            external_location=document.location,
                            last_error="",
                        )
                    except Exception as exc:
                        self._compensate_unrecorded_upload(record, document, exc)
                        raise

                return self._complete_store(
                    known_collection,
                    record,
                    created=(existing is None or existing.status == STATUS_COMPENSATED),
                )

    def store_prepared_document(
        self,
        collection: CollectionRef,
        document: PreparedDocumentRef,
        metadata: KnowledgeDocumentMetadata,
        *,
        operation_context: KnowledgeOperationContext,
        idempotency_key: str,
    ) -> IndexedDocument:
        """转交 RAG 已上传的同一全局文档，不再次上传或改写元数据。"""
        known_collection = self._require_collection(collection)
        if not isinstance(document, PreparedDocumentRef):
            raise TypeError("document 必须是 PreparedDocumentRef")
        metadata_snapshot = self._normalize_metadata(metadata)
        normalized_key = self._required_text(
            idempotency_key,
            name="idempotency_key",
        )
        source_digest = document.content_sha256

        with self._operation_lock_factory(
            f"architecture:{known_collection.architecture_id}"
        ):
            existing = self._operation_service.get(
                known_collection.ref,
                normalized_key,
            )
            # 即使记录已经 committed，也必须再次经过 begin 的不可变身份比较。直接返回会
            # 让“同一幂等键 + 不同 metadata/文档位置”的后到请求绕过冲突校验，并把旧结果
            # 误报成安全复用；begin 已负责精确重放和 last_execution_id 更新。
            record = self._operation_service.begin(
                collection_ref=known_collection.ref,
                idempotency_key=normalized_key,
                operation_context=operation_context,
                source_kind="prepared",
                source_digest=source_digest,
                metadata=metadata_snapshot,
                document_ref=document.document_ref,
                external_location=document.external_location,
            )
            if record.status == STATUS_COMMITTED:
                return self._indexed_result(record, created=False)
            return self._complete_store(
                known_collection,
                record,
                created=(existing is None or existing.status == STATUS_COMPENSATED),
            )

    def reconcile_document(
        self,
        collection: CollectionRef,
        *,
        operation_context: KnowledgeOperationContext,
        idempotency_key: str,
    ) -> IndexedDocument | None:
        """按协调记录复用或完成部分成功操作，不盲目发起新上传。"""
        known_collection = self._require_collection(collection)
        if not isinstance(operation_context, KnowledgeOperationContext):
            raise TypeError("operation_context 必须是 KnowledgeOperationContext")
        normalized_key = self._required_text(idempotency_key, name="idempotency_key")
        with self._operation_lock_factory(
            f"architecture:{known_collection.architecture_id}"
        ):
            record = self._operation_service.get(
                known_collection.ref,
                normalized_key,
            )
            if record is None or record.status in {STATUS_COMPENSATED, STATUS_SUPERSEDED}:
                return None
            if (
                record.business_type != operation_context.business_type
                or record.business_key != operation_context.business_key
            ):
                raise KnowledgeIndexError("知识库协调记录与当前业务身份不一致")
            if record.status == STATUS_COMPENSATION_FAILED:
                raise KnowledgeIndexRecoveryRequiredError(
                    "知识库操作存在未完成补偿，禁止自动 reconcile"
                )
            if record.status in {STATUS_DETACHING, STATUS_EXTERNAL_DETACHED}:
                raise KnowledgeIndexRecoveryRequiredError(
                    "知识库文档正在解绑，必须先完成解绑恢复"
                )
            if record.status == STATUS_COMMITTED:
                return self._indexed_result(record, created=False)
            if record.status == STATUS_PENDING and record.source_kind == "upload":
                # 没有外部引用意味着上一进程可能在上传请求期间退出。无法证明上游是否已
                # 创建全局文档时，自动重传会制造重复实体，因此必须转人工恢复。
                raise KnowledgeIndexRecoveryRequiredError(
                    "上传型知识库操作需要由 store_document 提供原文件后继续"
                )
            if record.status == STATUS_UPLOADING:
                raise KnowledgeIndexRecoveryRequiredError(
                    "上传型知识库操作停留在 uploading，外部结果不确定"
                )
            return self._complete_store(
                known_collection,
                record,
                created=False,
            )

    def detach_document(
        self,
        collection: CollectionRef,
        external_location: str,
        *,
        operation_context: KnowledgeOperationContext,
    ) -> OperationResult:
        """解除永久集合绑定并同步删除本地记录，但不全局删除文档。"""
        known_collection = self._require_collection(collection)
        if not isinstance(operation_context, KnowledgeOperationContext):
            raise TypeError("operation_context 必须是 KnowledgeOperationContext")
        normalized_location = self._required_text(
            external_location,
            name="external_location",
        )
        with self._operation_lock_factory(
            f"architecture:{known_collection.architecture_id}"
        ):
            try:
                detach_stage = self._operation_service.begin_detach(
                    collection_ref=known_collection.ref,
                    external_location=normalized_location,
                )
                existing = None
                if detach_stage != STATUS_EXTERNAL_DETACHED:
                    existing = self._workspace_client.find_document(
                        known_collection.ref,
                        normalized_location,
                        user_id=self._user_id,
                    )
                    if existing is not None:
                        self._workspace_client.update_embeddings(
                            known_collection.ref,
                            deletes=(normalized_location,),
                            user_id=self._user_id,
                        )
                    self._operation_service.mark_external_detached(
                        collection_ref=known_collection.ref,
                        external_location=normalized_location,
                    )
                deleted_rows = self._database_service.delete_document_by_location(
                    workspace_slug=known_collection.ref,
                    doc_path=normalized_location,
                )
                coordinated_rows = self._operation_service.mark_detached(
                    collection_ref=known_collection.ref,
                    external_location=normalized_location,
                )
                already_applied = (
                    existing is None and deleted_rows == 0 and coordinated_rows == 0
                )
                logger.info(
                    "永久知识文档已解除集合绑定: architecture_id=%s "
                    "has_document_location=%s already_applied=%s business_type=%s "
                    "business_key_chars=%d",
                    known_collection.architecture_id,
                    bool(normalized_location),
                    already_applied,
                    operation_context.business_type,
                    len(operation_context.business_key),
                )
                return OperationResult(
                    success=True,
                    already_applied=already_applied,
                )
            except Exception as exc:
                error_message = self._safe_error(exc, fallback="解除知识库文档绑定失败")
                logger.error(
                    "永久知识文档解除集合绑定失败: architecture_id=%s "
                    "has_document_location=%s error_type=%s business_type=%s "
                    "business_key_chars=%d",
                    known_collection.architecture_id,
                    bool(normalized_location),
                    type(exc).__name__,
                    operation_context.business_type,
                    len(operation_context.business_key),
                )
                return OperationResult(
                    success=False,
                    already_applied=False,
                    error_message=error_message,
                )

    def _complete_store(
        self,
        collection: CollectionRef,
        record: KnowledgeIndexOperationRecord,
        *,
        created: bool,
    ) -> IndexedDocument:
        """从已知协调阶段继续绑定、确认和本地提交。"""
        if record.status not in {
            STATUS_PENDING,
            STATUS_DOCUMENT_READY,
            STATUS_EXTERNAL_SUCCEEDED,
            STATUS_REPLACEMENT_CLEANUP_PENDING,
        }:
            raise KnowledgeIndexError(
                f"知识库操作无法从当前状态继续: {record.status}"
            )

        # ``external_succeeded`` 是永久集合已经接管文档的持久化证明。恢复该状态时只允许
        # 完成本地 SQLite 提交，不再依赖 AnythingLLM 在线，也不重复执行绑定或 Pin。
        if record.status in {
            STATUS_EXTERNAL_SUCCEEDED,
            STATUS_REPLACEMENT_CLEANUP_PENDING,
        }:
            return self._commit_recovered_external_success(
                collection,
                record,
                created=created,
            )

        try:
            record = self._prepare_replacement(collection, record)
        except Exception as exc:
            compensated = self._compensate_external_failure(collection, record, exc)
            if not compensated and record.source_kind == "prepared":
                raise KnowledgeIndexRetentionRequiredError(
                    "替换准备失败且补偿结果不确定，必须保留全局文档"
                ) from exc
            if compensated and record.source_kind == "prepared":
                raise KnowledgeIndexDocumentReleasedError(
                    "替换准备失败，但永久集合变更已经完成补偿"
                ) from exc
            raise

        try:
            existing_before_mutation = self._workspace_client.find_document(
                collection.ref,
                record.external_location,
                user_id=self._user_id,
            )
        except Exception as exc:
            # 首次读取失败时尚未产生本轮外部副作用。此时补偿删除既不必要，也可能误删
            # 上一轮已经成功绑定的文档，因此只记录错误并等待安全重试。
            self._record_error_best_effort(record, exc)
            if record.source_kind == "prepared":
                raise KnowledgeIndexRetentionRequiredError(
                    "无法确认预备文档是否已进入永久集合，必须保留全局文档"
                ) from exc
            raise
        try:
            document = self._ensure_external_document(
                collection,
                record,
                existing=existing_before_mutation,
            )
            record = self._operation_service.transition(
                collection_ref=record.collection_ref,
                idempotency_key=record.idempotency_key,
                expected_statuses={
                    STATUS_PENDING,
                    STATUS_DOCUMENT_READY,
                    STATUS_EXTERNAL_SUCCEEDED,
                },
                target_status=STATUS_EXTERNAL_SUCCEEDED,
                document_ref=document.document_ref,
                external_document_id=document.id,
                external_location=document.location,
                last_error="",
            )
        except Exception as exc:
            if existing_before_mutation is not None:
                self._record_error_best_effort(record, exc)
                if record.source_kind == "prepared":
                    raise KnowledgeIndexRetentionRequiredError(
                        "文档已存在于永久集合，失败后必须保留全局文档"
                    ) from exc
            else:
                compensated = self._compensate_external_failure(collection, record, exc)
                if not compensated and record.source_kind == "prepared":
                    raise KnowledgeIndexRetentionRequiredError(
                        "永久知识库补偿未完成，必须保留全局文档"
                    ) from exc
                if compensated and record.source_kind == "prepared":
                    raise KnowledgeIndexDocumentReleasedError(
                        "永久知识库写入失败，但集合绑定已经完成补偿"
                    ) from exc
            raise

        try:
            self._commit_local_record(collection, record)
        except Exception as exc:
            # 外部绑定已经确定成功。此处禁止反向删除外部文档，否则本地瞬时故障会丢失
            # 可恢复成果；协调记录保持 external_succeeded，重试只执行本地提交。
            self._record_error_best_effort(record, exc)
            logger.error(
                "永久知识库本地提交失败，保留外部成功状态等待重试: "
                "idempotency_key=%s error_type=%s",
                record.idempotency_key,
                type(exc).__name__,
            )
            if record.source_kind == "prepared":
                raise KnowledgeIndexRetentionRequiredError(
                    "永久集合已经接管文档，本地提交失败后必须保留全局文档"
                ) from exc
            raise KnowledgeIndexRecoveryRequiredError(
                "永久知识库本地提交失败，等待协调重试"
            ) from exc

        record = self._finalize_commit(collection, record)
        logger.info(
            "永久知识库操作已提交: architecture_id=%s idempotency_key=%s "
            "has_document_ref=%s created=%s source_kind=%s",
            collection.architecture_id,
            record.idempotency_key,
            bool(record.document_ref),
            created,
            record.source_kind,
        )
        return self._indexed_result(record, created=created)

    def _ensure_external_document(
        self,
        collection: CollectionRef,
        record: KnowledgeIndexOperationRecord,
        *,
        existing: AnythingLLMDocument | None,
    ) -> AnythingLLMDocument:
        """确保目标文档已绑定并 Pin，随后通过 Workspace 记录确认真实实体身份。"""
        if not record.external_location or not record.document_ref:
            raise KnowledgeIndexRecoveryRequiredError(
                "协调记录缺少可恢复的外部文档引用"
            )
        if existing is None:
            self._workspace_client.update_embeddings(
                collection.ref,
                adds=(record.external_location,),
                user_id=self._user_id,
            )
        self._workspace_client.update_pin(
            collection.ref,
            record.external_location,
            pinned=True,
            user_id=self._user_id,
        )
        verified = self._workspace_client.find_document(
            collection.ref,
            record.external_location,
            user_id=self._user_id,
        )
        if verified is None:
            raise KnowledgeIndexError(
                "AnythingLLM 未在永久知识集合中返回刚绑定的文档"
            )
        if verified.document_ref != record.document_ref:
            logger.error(
                "永久知识集合文档身份校验失败: architecture_id=%s "
                "idempotency_key=%s expected_ref_present=%s actual_ref_present=%s "
                "location_matched=%s has_document_id=%s has_raw_document_id=%s "
                "identity_source=%s",
                collection.architecture_id,
                record.idempotency_key,
                bool(record.document_ref),
                bool(verified.document_ref),
                verified.location == record.external_location,
                bool(verified.id),
                bool(verified.raw_document_id),
                verified.identity_source,
            )
            raise KnowledgeIndexError(
                "永久知识集合中的文档身份与协调记录不一致"
            )
        return verified

    def _commit_local_record(
        self,
        collection: CollectionRef,
        record: KnowledgeIndexOperationRecord,
    ) -> None:
        """把业务控制字段和本地权威 metadata 原子写入知识库数据库。"""
        metadata = dict(record.metadata)
        file_name = str(metadata.pop("file_name")).strip()
        # original_name 是请求 originalFileName 的业务原值。只在下游数据库提交时判空，
        # 此处不能 strip，否则会破坏“回调 source 严格返回原值”的链路。
        original_name = str(metadata.pop("original_name", file_name))
        ingested_file_name = str(metadata.pop("ingested_file_name")).strip()
        attributes = metadata.pop("attributes")
        if metadata:
            raise KnowledgeIndexError("协调记录包含未知文档控制字段")
        if not record.external_document_id:
            raise KnowledgeIndexRecoveryRequiredError(
                "协调记录缺少本地提交所需的外部文档 ID"
            )
        self._database_service.commit_indexed_document(
            architecture_id=collection.architecture_id,
            workspace_slug=collection.ref,
            file_name=file_name,
            original_name=original_name,
            ingested_file_name=ingested_file_name,
            anything_doc_id=record.external_document_id,
            doc_path=record.external_location,
            metadata=attributes,
        )

    def _commit_recovered_external_success(
        self,
        collection: CollectionRef,
        record: KnowledgeIndexOperationRecord,
        *,
        created: bool,
    ) -> IndexedDocument:
        """只依赖协调快照恢复本地提交，不重新访问 AnythingLLM。"""
        try:
            self._commit_local_record(collection, record)
            committed = self._finalize_commit(collection, record)
        except Exception as exc:
            self._record_error_best_effort(record, exc)
            if record.source_kind == "prepared":
                raise KnowledgeIndexRetentionRequiredError(
                    "永久文档正在恢复本地提交，必须继续保留全局实体"
                ) from exc
            raise KnowledgeIndexRecoveryRequiredError(
                "永久知识库本地恢复尚未完成"
            ) from exc
        return self._indexed_result(committed, created=created)

    def _prepare_replacement(
        self,
        collection: CollectionRef,
        record: KnowledgeIndexOperationRecord,
    ) -> KnowledgeIndexOperationRecord:
        """在外部绑定前持久化同名旧版本位置，建立可恢复替换 Saga。

        后续顺序固定为“绑定新版本、本地切换、解绑旧版本”。即使进程在本地切换后退出，
        协调记录仍保留旧位置，重试可以继续清理，旧文档不会永久残留在检索集合中。
        """
        file_name = str(record.metadata.get("file_name") or "").strip()
        existing = self._database_service.get_document_record(
            file_name,
            architecture_id=collection.architecture_id,
        )
        if existing is None:
            return record
        existing_location = str(existing.get("doc_path") or "").strip()
        if existing_location and existing_location != record.external_location:
            return self._operation_service.record_replacement_target(
                collection_ref=record.collection_ref,
                idempotency_key=record.idempotency_key,
                superseded_location=existing_location,
            )
        return record

    def _finalize_commit(
        self,
        collection: CollectionRef,
        record: KnowledgeIndexOperationRecord,
    ) -> KnowledgeIndexOperationRecord:
        """从当前集合解绑被替换旧版本，并把协调记录推进到 committed。"""
        if record.superseded_location:
            try:
                # Workspace 删除绑定是幂等操作。若上一次请求成功但状态写入失败，重放同一
                # deletes 仍只会保持目标文档不存在，不会产生额外破坏。
                self._workspace_client.update_embeddings(
                    collection.ref,
                    deletes=(record.superseded_location,),
                    user_id=self._user_id,
                )
                self._operation_service.mark_superseded(
                    collection_ref=collection.ref,
                    external_location=record.superseded_location,
                )
            except Exception as exc:
                try:
                    record = self._operation_service.transition(
                        collection_ref=record.collection_ref,
                        idempotency_key=record.idempotency_key,
                        expected_statuses={
                            STATUS_EXTERNAL_SUCCEEDED,
                            STATUS_REPLACEMENT_CLEANUP_PENDING,
                        },
                        target_status=STATUS_REPLACEMENT_CLEANUP_PENDING,
                        last_error=self._safe_error(
                            exc,
                            fallback="解绑被替换旧版本失败",
                        ),
                    )
                except Exception:
                    logger.critical(
                        "旧版本清理失败后，无法写入待恢复状态: idempotency_key=%s",
                        record.idempotency_key,
                        exc_info=True,
                    )
                if record.source_kind == "prepared":
                    raise KnowledgeIndexRetentionRequiredError(
                        "新版本已经进入永久集合，旧版本清理失败时必须保留新文档"
                    ) from exc
                raise KnowledgeIndexRecoveryRequiredError(
                    "永久知识库旧版本清理尚未完成"
                ) from exc

        try:
            return self._operation_service.transition(
                collection_ref=record.collection_ref,
                idempotency_key=record.idempotency_key,
                expected_statuses={
                    STATUS_EXTERNAL_SUCCEEDED,
                    STATUS_REPLACEMENT_CLEANUP_PENDING,
                },
                target_status=STATUS_COMMITTED,
                last_error="",
            )
        except Exception as exc:
            self._record_error_best_effort(record, exc)
            if record.source_kind == "prepared":
                raise KnowledgeIndexRetentionRequiredError(
                    "永久知识库本地记录已提交，协调完成失败后必须保留全局文档"
                ) from exc
            raise

    def _compensate_external_failure(
        self,
        collection: CollectionRef,
        record: KnowledgeIndexOperationRecord,
        cause: Exception,
    ) -> bool:
        """补偿本次尚未成功转交的外部变更，并保存确定或不确定结果。"""
        errors: list[str] = []
        if record.external_location:
            try:
                self._workspace_client.update_embeddings(
                    collection.ref,
                    deletes=(record.external_location,),
                    user_id=self._user_id,
                )
            except Exception as exc:
                errors.append(self._safe_error(exc, fallback="解除集合绑定失败"))

            if record.source_kind == "upload":
                try:
                    if parse_xlsx_sheet_location(record.external_location) is not None:
                        self._document_client.delete_document_artifact(
                            record.external_location,
                            user_id=self._user_id,
                        )
                    else:
                        self._document_client.delete_document(
                            record.external_location,
                            user_id=self._user_id,
                        )
                    # 全局删除会清除所有 Workspace 关联，因此即使前面的显式解绑失败，
                    # 文档清理仍已达到更强的最终状态，可以视为补偿成功。
                    errors.clear()
                except Exception as exc:
                    errors.append(self._safe_error(exc, fallback="删除全局文档失败"))
        else:
            errors.append("缺少外部文档位置，无法证明上传是否产生全局实体")

        target_status = (
            STATUS_COMPENSATION_FAILED if errors else STATUS_COMPENSATED
        )
        error_message = "；".join(errors) or self._safe_error(
            cause,
            fallback="外部知识库操作已补偿",
        )
        try:
            self._operation_service.transition(
                collection_ref=record.collection_ref,
                idempotency_key=record.idempotency_key,
                expected_statuses={STATUS_PENDING, STATUS_DOCUMENT_READY},
                target_status=target_status,
                last_error=error_message,
            )
        except Exception:
            # 补偿状态落库失败不能覆盖最初的 AnythingLLM 异常。调用方根据 False 进入
            # “必须保留文档”路径，运维人员仍可通过日志和外部引用执行人工对账。
            logger.critical(
                "外部资源补偿完成后，无法写入知识库协调状态: "
                "idempotency_key=%s target_status=%s",
                record.idempotency_key,
                target_status,
                exc_info=True,
            )
            return False
        logger.log(
            logging.ERROR if errors else logging.WARNING,
            "永久知识库外部资源失败补偿结束: idempotency_key=%s "
            "compensation_status=%s error_count=%d original_error_type=%s",
            record.idempotency_key,
            target_status,
            len(errors),
            type(cause).__name__,
        )
        return not errors

    def _handle_unknown_upload_failure(
        self,
        record: KnowledgeIndexOperationRecord,
        cause: Exception,
    ) -> None:
        """上传未返回可定位实体时阻断自动重传，避免制造不可追踪重复文档。"""
        error_message = self._safe_error(cause, fallback="文档上传结果不确定")
        try:
            self._operation_service.transition(
                collection_ref=record.collection_ref,
                idempotency_key=record.idempotency_key,
                expected_statuses={STATUS_UPLOADING},
                target_status=STATUS_COMPENSATION_FAILED,
                last_error=error_message,
            )
        except Exception:
            logger.critical(
                "文档上传失败后，无法写入知识库协调状态: idempotency_key=%s",
                record.idempotency_key,
                exc_info=True,
            )

    def _compensate_unrecorded_upload(
        self,
        record: KnowledgeIndexOperationRecord,
        document: AnythingLLMDocument,
        cause: Exception,
    ) -> None:
        """协调状态写入失败时，使用内存中的真实位置立即删除刚上传文档。"""
        cleanup_error = ""
        try:
            if parse_xlsx_sheet_location(document.location) is not None:
                self._document_client.delete_document_artifact(
                    document.location,
                    user_id=self._user_id,
                )
            else:
                self._document_client.delete_document(
                    document.location,
                    user_id=self._user_id,
                )
        except Exception as exc:
            cleanup_error = self._safe_error(exc, fallback="删除未登记全局文档失败")
        target_status = (
            STATUS_COMPENSATION_FAILED if cleanup_error else STATUS_COMPENSATED
        )
        try:
            self._operation_service.transition(
                collection_ref=record.collection_ref,
                idempotency_key=record.idempotency_key,
                expected_statuses={STATUS_UPLOADING},
                target_status=target_status,
                last_error=(
                    cleanup_error
                    or self._safe_error(cause, fallback="协调写入失败后已删除上传文档")
                ),
            )
        except Exception:
            logger.critical(
                "未登记上传文档的补偿完成后，无法更新协调状态: "
                "idempotency_key=%s cleanup_succeeded=%s",
                record.idempotency_key,
                not bool(cleanup_error),
                exc_info=True,
            )

    def _record_error_best_effort(
        self,
        record: KnowledgeIndexOperationRecord,
        error: Exception,
    ) -> None:
        """尽力写入安全错误摘要，且不使用记录失败覆盖原始业务异常。"""
        try:
            self._operation_service.record_external_error(
                collection_ref=record.collection_ref,
                idempotency_key=record.idempotency_key,
                error_message=self._safe_error(error, fallback="知识库操作失败"),
            )
        except Exception:
            logger.critical(
                "无法更新知识库协调记录的错误信息: idempotency_key=%s",
                record.idempotency_key,
                exc_info=True,
            )

    def _require_collection(self, collection: CollectionRef) -> CollectionRef:
        """拒绝未由当前任务级 Gateway 确保的伪造集合引用。"""
        if not isinstance(collection, CollectionRef):
            raise TypeError("collection 必须是 CollectionRef")
        known = self._known_collections.get(collection.ref)
        if known is None or known != collection:
            raise ValueError("collection 不是当前 Gateway 管理的集合引用")
        return known

    @staticmethod
    def _normalize_metadata(
        metadata: KnowledgeDocumentMetadata,
    ) -> dict[str, Any]:
        """把类型化文档元数据转换为协调表的规范快照。"""
        if not isinstance(metadata, KnowledgeDocumentMetadata):
            raise TypeError("metadata 必须是 KnowledgeDocumentMetadata")
        return {
            "file_name": metadata.file_name,
            "original_name": metadata.original_name,
            "ingested_file_name": metadata.ingested_file_name,
            "attributes": metadata.attributes_dict(),
        }

    @staticmethod
    def _sha256_file(path: Path) -> str:
        """流式计算最终上传文件摘要，避免大文件一次性载入内存。"""
        digest = hashlib.sha256()
        with path.open("rb") as file_object:
            for chunk in iter(lambda: file_object.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _indexed_result(
        record: KnowledgeIndexOperationRecord,
        *,
        created: bool,
    ) -> IndexedDocument:
        """把已提交协调记录转换为供应商无关文档结果。"""
        if record.status != STATUS_COMMITTED:
            raise KnowledgeIndexError("只有 committed 操作可以返回 IndexedDocument")
        return IndexedDocument(
            collection_ref=record.collection_ref,
            document_ref=record.document_ref,
            external_location=record.external_location,
            idempotency_key=record.idempotency_key,
            created=created,
            reused=not created,
        )

    @staticmethod
    def _required_text(value: str, *, name: str) -> str:
        """校验并返回非空文本参数。"""
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{name} 不能为空")
        return normalized

    @staticmethod
    def _safe_error(error: Exception, *, fallback: str) -> str:
        """生成不包含响应正文、密钥或完整 metadata 的有限长度错误摘要。"""
        message = " ".join(str(error or fallback).split()) or fallback
        message = re.sub(
            r"(?i)(authorization|api[_-]?key|bearer)\s*[:=]?\s*[^\s,;]+",
            r"\1=<redacted>",
            message,
        )
        return message[:500]
