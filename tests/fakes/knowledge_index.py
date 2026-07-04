"""长期知识库端口的线程安全内存测试替身。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from threading import RLock
from typing import Any, Mapping, Optional

from app.ports import (
    CollectionRef,
    IndexedDocument,
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


class FakeKnowledgeIndexPort:
    """线程安全、无外部副作用的知识库端口测试实现。

    测试替身按“集合引用 + 幂等键”确定文档唯一性。查询和写入位于同一把可重入锁内，
    因此多个线程同时提交相同幂等键时，只有一个调用得到 ``created=True``，其他调用均
    得到指向同一逻辑文档的 ``reused=True`` 结果。
    """

    def __init__(self) -> None:
        """初始化空集合、空文档索引以及单调递增的内存标识序列。"""
        self._lock = RLock()
        self._collections_by_name: dict[str, CollectionRef] = {}
        self._collections_by_ref: dict[str, CollectionRef] = {}
        self._documents_by_key: dict[tuple[str, str], _StoredDocument] = {}
        self._keys_by_location: dict[tuple[str, str], tuple[str, str]] = {}
        self._collection_sequence = 0
        self._document_sequence = 0

    def ensure_collection(self, name: str) -> CollectionRef:
        """返回既有集合，或以原子方式创建一个新的稳定集合引用。"""
        normalized_name = self._required_text(name, name="name")
        with self._lock:
            existing = self._collections_by_name.get(normalized_name)
            if existing is not None:
                return existing

            self._collection_sequence += 1
            collection = CollectionRef(
                ref=f"collection:{self._collection_sequence}",
                name=normalized_name,
            )
            self._collections_by_name[normalized_name] = collection
            self._collections_by_ref[collection.ref] = collection
            return collection

    def store_document(
        self,
        collection: CollectionRef,
        file_path: str,
        metadata: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> IndexedDocument:
        """原子保存文档，重复幂等键返回同一文档的复用结果。"""
        normalized_path = self._required_text(file_path, name="file_path")
        normalized_key = self._required_text(idempotency_key, name="idempotency_key")
        metadata_snapshot = dict(metadata)

        with self._lock:
            known_collection = self._require_collection(collection)
            lookup_key = (known_collection.ref, normalized_key)
            existing = self._documents_by_key.get(lookup_key)
            if existing is not None:
                return replace(existing.result, created=False, reused=True)

            self._document_sequence += 1
            result = IndexedDocument(
                collection_ref=known_collection.ref,
                document_ref=f"document:{self._document_sequence}",
                external_location=f"external:{self._document_sequence}",
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
        metadata: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> IndexedDocument:
        """登记 RAG 已准备的文档，并原样保留其不透明身份和外部位置。"""
        normalized_key = self._required_text(idempotency_key, name="idempotency_key")
        document_ref = self._required_text(document.document_ref, name="document_ref")
        external_location = self._required_text(
            document.external_location,
            name="external_location",
        )
        metadata_snapshot = dict(metadata)

        with self._lock:
            known_collection = self._require_collection(collection)
            lookup_key = (known_collection.ref, normalized_key)
            existing = self._documents_by_key.get(lookup_key)
            if existing is not None:
                return replace(existing.result, created=False, reused=True)

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
            self._keys_by_location[(known_collection.ref, external_location)] = lookup_key
            return result

    def remove_document(
        self,
        collection: CollectionRef,
        external_location: str,
    ) -> OperationResult:
        """原子移除目标文档；目标已不存在时保持成功的幂等语义。"""
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
        idempotency_key: str,
    ) -> Optional[IndexedDocument]:
        """查询当前仍存在的文档，并把命中明确标记为复用结果。"""
        normalized_key = self._required_text(idempotency_key, name="idempotency_key")
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

    @staticmethod
    def _required_text(value: str, *, name: str) -> str:
        """规范化并校验测试替身操作所需的非空文本参数。"""
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{name} 不能为空")
        return normalized
