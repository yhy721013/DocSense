"""长期知识库端口的线程安全内存测试替身。"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, replace
from threading import RLock
from typing import Any, Iterator, Mapping, Optional

from app.ports import (
    CollectionRef,
    CollectionSpec,
    IndexedDocument,
    KnowledgeDocumentMetadata,
    KnowledgeOperationContext,
    OperationResult,
    PreparedDocumentRef,
)


@dataclass(frozen=True)
class _StoredDocument:
    """测试替身内部保存的文档状态，不作为端口公共结果暴露。

    元数据仅保存调用时的浅复制快照，用于隔离调用方后续修改。端口契约并未承诺读取
    元数据，因此公共 DTO 不包含这一测试内部字段。
    """

    result: IndexedDocument
    source_ref: str
    metadata: Mapping[str, Any]


class _FakeKnowledgeIndexState:
    """多个任务级 Fake Port 共享的永久知识库内存后端。"""

    def __init__(self) -> None:
        self.lock = RLock()
        self.collections_by_architecture: dict[int, CollectionRef] = {}
        self.collections_by_ref: dict[str, CollectionRef] = {}
        self.documents_by_key: dict[tuple[str, str], _StoredDocument] = {}
        self.keys_by_location: dict[tuple[str, str], tuple[str, str]] = {}
        self.collection_sequence = 0
        self.document_sequence = 0


class FakeKnowledgeIndexPort:
    """线程安全、无外部副作用的知识库端口测试实现。

    测试替身按“集合引用 + 幂等键”确定文档唯一性。查询和写入位于同一把可重入锁内，
    因此多个线程同时提交相同幂等键时，只有一个调用得到 ``created=True``，其他调用均
    得到指向同一逻辑文档的 ``reused=True`` 结果。
    """

    def __init__(self, state: _FakeKnowledgeIndexState | None = None) -> None:
        """绑定共享后端；直接构造时仍创建独立状态以兼容单元测试。"""
        self._state = state or _FakeKnowledgeIndexState()
        self._lock = self._state.lock
        self._collections_by_architecture = self._state.collections_by_architecture
        self._collections_by_ref = self._state.collections_by_ref
        self._documents_by_key = self._state.documents_by_key
        self._keys_by_location = self._state.keys_by_location

    def ensure_collection(self, spec: CollectionSpec) -> CollectionRef:
        """返回既有集合，或以原子方式创建一个新的稳定集合引用。"""
        if not isinstance(spec, CollectionSpec):
            raise TypeError("spec 必须是 CollectionSpec")
        normalized_name = spec.name
        with self._lock:
            existing = self._collections_by_architecture.get(spec.architecture_id)
            if existing is not None:
                if existing.name.casefold() != normalized_name.casefold():
                    raise ValueError("同一 architecture_id 不能对应不同集合名称")
                return existing

            self._state.collection_sequence += 1
            collection = CollectionRef(
                ref=f"collection:{self._state.collection_sequence}",
                name=normalized_name,
                architecture_id=spec.architecture_id,
            )
            self._collections_by_architecture[spec.architecture_id] = collection
            self._collections_by_ref[collection.ref] = collection
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
        """原子保存文档，重复幂等键返回同一文档的复用结果。"""
        normalized_path = self._required_text(file_path, name="file_path")
        normalized_key = self._required_text(idempotency_key, name="idempotency_key")
        metadata_snapshot = self._snapshot_metadata(metadata)
        self._require_operation_context(operation_context)

        with self._lock:
            known_collection = self._require_collection(collection)
            lookup_key = (known_collection.ref, normalized_key)
            existing = self._documents_by_key.get(lookup_key)
            if existing is not None:
                if dict(existing.metadata) != metadata_snapshot:
                    raise ValueError("相同幂等键不能携带不同 metadata")
                return replace(existing.result, created=False, reused=True)
            self._remove_superseded_document(
                collection_ref=known_collection.ref,
                lookup_key=lookup_key,
                metadata=metadata_snapshot,
            )

            self._state.document_sequence += 1
            result = IndexedDocument(
                collection_ref=known_collection.ref,
                document_ref=f"document:{self._state.document_sequence}",
                external_location=f"external:{self._state.document_sequence}",
                idempotency_key=normalized_key,
                created=True,
                reused=False,
            )
            self._documents_by_key[lookup_key] = _StoredDocument(
                result=result,
                source_ref=normalized_path,
                metadata=metadata_snapshot,
            )
            self._keys_by_location[
                (known_collection.ref, result.external_location)
            ] = lookup_key
            return result

    def store_prepared_document(
        self,
        collection: CollectionRef,
        document: PreparedDocumentRef,
        metadata: KnowledgeDocumentMetadata,
        *,
        operation_context: KnowledgeOperationContext,
        idempotency_key: str,
    ) -> IndexedDocument:
        """登记 RAG 已准备的文档，并原样保留其不透明身份和外部位置。"""
        normalized_key = self._required_text(idempotency_key, name="idempotency_key")
        document_ref = self._required_text(document.document_ref, name="document_ref")
        external_location = self._required_text(
            document.external_location,
            name="external_location",
        )
        metadata_snapshot = self._snapshot_metadata(metadata)
        self._require_operation_context(operation_context)

        with self._lock:
            known_collection = self._require_collection(collection)
            lookup_key = (known_collection.ref, normalized_key)
            existing = self._documents_by_key.get(lookup_key)
            if existing is not None:
                if dict(existing.metadata) != metadata_snapshot:
                    raise ValueError("相同幂等键不能携带不同 metadata")
                if (
                    existing.result.document_ref != document_ref
                    or existing.result.external_location != external_location
                ):
                    raise ValueError("相同幂等键不能指向不同的预备文档")
                return replace(existing.result, created=False, reused=True)
            self._remove_superseded_document(
                collection_ref=known_collection.ref,
                lookup_key=lookup_key,
                metadata=metadata_snapshot,
            )

            location_key = (known_collection.ref, external_location)
            location_owner = self._keys_by_location.get(location_key)
            if location_owner is not None and location_owner != lookup_key:
                raise ValueError("同一集合中的外部文档位置不能绑定多个幂等键")

            result = IndexedDocument(
                collection_ref=known_collection.ref,
                document_ref=document_ref,
                external_location=external_location,
                idempotency_key=normalized_key,
                created=True,
                reused=False,
            )
            self._documents_by_key[lookup_key] = _StoredDocument(
                result=result,
                source_ref=external_location,
                metadata=metadata_snapshot,
            )
            self._keys_by_location[location_key] = lookup_key
            return result

    def detach_document(
        self,
        collection: CollectionRef,
        external_location: str,
        *,
        operation_context: KnowledgeOperationContext,
    ) -> OperationResult:
        """原子解除集合绑定；目标已不存在时保持成功的幂等语义。"""
        self._require_operation_context(operation_context)
        normalized_location = self._required_text(
            external_location,
            name="external_location",
        )
        with self._lock:
            known_collection = self._require_collection(collection)
            location_key = (known_collection.ref, normalized_location)
            document_key = self._keys_by_location.pop(location_key, None)
            if document_key is None:
                return OperationResult(success=True, already_applied=True)

            self._documents_by_key.pop(document_key, None)
            return OperationResult(success=True, already_applied=False)

    def reconcile_document(
        self,
        collection: CollectionRef,
        *,
        operation_context: KnowledgeOperationContext,
        idempotency_key: str,
    ) -> Optional[IndexedDocument]:
        """查询当前仍存在的文档，并把命中明确标记为复用结果。"""
        normalized_key = self._required_text(idempotency_key, name="idempotency_key")
        self._require_operation_context(operation_context)
        with self._lock:
            known_collection = self._require_collection(collection)
            stored = self._documents_by_key.get((known_collection.ref, normalized_key))
            if stored is None:
                return None
            return replace(stored.result, created=False, reused=True)

    def _require_collection(self, collection: CollectionRef) -> CollectionRef:
        """确认集合引用由当前测试替身创建且内容没有被伪造。

        本方法必须在持有 ``_lock`` 时调用，使校验和后续文档操作处于同一原子临界区。
        """
        known = self._collections_by_ref.get(collection.ref)
        if known is None or known != collection:
            raise ValueError("collection 不是当前知识库端口管理的有效集合引用")
        return known

    def _remove_superseded_document(
        self,
        *,
        collection_ref: str,
        lookup_key: tuple[str, str],
        metadata: Mapping[str, Any],
    ) -> None:
        """模拟生产 Gateway 提交新版本后解绑同名旧版本的最终状态。"""
        file_name = str(metadata.get("file_name") or "")
        for existing_key, stored in tuple(self._documents_by_key.items()):
            if existing_key == lookup_key or existing_key[0] != collection_ref:
                continue
            if str(stored.metadata.get("file_name") or "") == file_name:
                self._documents_by_key.pop(existing_key, None)
                self._keys_by_location.pop(
                    (collection_ref, stored.result.external_location),
                    None,
                )

    @staticmethod
    def _require_operation_context(
        operation_context: KnowledgeOperationContext,
    ) -> None:
        """要求测试调用显式提供正式业务操作上下文。"""
        if not isinstance(operation_context, KnowledgeOperationContext):
            raise TypeError("operation_context 必须是 KnowledgeOperationContext")

    @staticmethod
    def _snapshot_metadata(metadata: KnowledgeDocumentMetadata) -> dict[str, Any]:
        """生成 JSON 兼容的深复制快照，模拟阶段 8 本地权威记录约束。

        浅复制无法隔离嵌套列表和字典，调用方在提交后修改原对象会悄悄改变 Fake 的幂等
        比较结果。通过严格 JSON 往返既能切断可变引用，也会拒绝 NaN、集合和自定义对象等
        无法写入真实协调表的值，使测试行为更接近生产持久化边界。
        """
        if not isinstance(metadata, KnowledgeDocumentMetadata):
            raise TypeError("metadata 必须是 KnowledgeDocumentMetadata")
        serialized = json.dumps(
            {
                "file_name": metadata.file_name,
                "original_name": metadata.original_name,
                # 与生产协调记录一致保存实际上传文件名，避免 MHTML/OCR 预处理后的
                # 来源映射在离线测试中被错误简化为业务哈希名。
                "ingested_file_name": metadata.ingested_file_name,
                "attributes": metadata.attributes_dict(),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        snapshot = json.loads(serialized)
        if not isinstance(snapshot, dict):
            raise TypeError("metadata 必须序列化为 JSON 对象")
        return snapshot

    @staticmethod
    def _required_text(value: str, *, name: str) -> str:
        """规范化并校验测试替身操作所需的非空文本参数。"""
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{name} 不能为空")
        return normalized


class FakeKnowledgeIndexFactory:
    """为每次任务创建独立内存知识库 Port 的可观察测试工厂。"""

    def __init__(self) -> None:
        """初始化已创建 Port 列表和活动租约计数。"""
        self._ports: list[FakeKnowledgeIndexPort] = []
        self._active_leases = 0
        self._state = _FakeKnowledgeIndexState()

    @property
    def ports(self) -> tuple[FakeKnowledgeIndexPort, ...]:
        """返回已经进入过任务作用域的 Port 快照。"""
        return tuple(self._ports)

    @property
    def active_leases(self) -> int:
        """返回当前尚未退出的永久知识库租约数量。"""
        return self._active_leases

    @contextmanager
    def create(self) -> Iterator[FakeKnowledgeIndexPort]:
        """创建任务独占 Port，并在异常路径准确归还活动租约。"""
        port = FakeKnowledgeIndexPort(self._state)
        self._ports.append(port)
        self._active_leases += 1
        try:
            yield port
        finally:
            self._active_leases -= 1
