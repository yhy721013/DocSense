"""供应商无关的知识库索引契约与结果模型。

本模块把“确保集合存在、保存文档、移除文档、按幂等键对账”定义为稳定的应用层能力。
调用方只能持有不透明引用，不得根据引用内容推导外部系统的存储目录、资源名称或请求
地址。具体索引系统的字段转换、网络调用和错误翻译由后续适配器负责。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class CollectionRef:
    """业务层可安全持有的知识集合引用。

    ``ref`` 是适配器生成的不透明稳定标识，``name`` 是业务请求的集合名称。调用方可以
    比较引用是否相同，但不能解析 ``ref`` 或把它拼接成外部请求参数。
    """

    ref: str
    name: str

    def __post_init__(self) -> None:
        """拒绝无法用于关联索引操作的空引用或空名称。"""
        if not str(self.ref or "").strip():
            raise ValueError("CollectionRef.ref 不能为空")
        if not str(self.name or "").strip():
            raise ValueError("CollectionRef.name 不能为空")


@dataclass(frozen=True)
class IndexedDocument:
    """知识库成功保存或复用后的稳定文档结果。

    ``document_ref`` 标识逻辑文档，``external_location`` 是供后续删除或审计使用的不透明
    外部位置。``created`` 与 ``reused`` 必须且只能有一个为真，从而让业务层明确区分首次
    写入和幂等复用，不需要根据外部响应自行猜测。
    """

    collection_ref: str
    document_ref: str
    external_location: str
    idempotency_key: str
    created: bool
    reused: bool

    def __post_init__(self) -> None:
        """校验文档身份字段以及创建、复用状态的互斥关系。"""
        required_fields = {
            "collection_ref": self.collection_ref,
            "document_ref": self.document_ref,
            "external_location": self.external_location,
            "idempotency_key": self.idempotency_key,
        }
        for field_name, field_value in required_fields.items():
            if not str(field_value or "").strip():
                raise ValueError(f"IndexedDocument.{field_name} 不能为空")
        if self.created == self.reused:
            raise ValueError("IndexedDocument.created 与 reused 必须且只能有一个为 True")


@dataclass(frozen=True)
class OperationResult:
    """无需返回实体的知识库操作结果。

    ``already_applied=True`` 表示目标状态在调用前已经成立。例如重复删除同一文档时，
    适配器仍可返回成功，但明确说明本次没有执行新的状态变更。
    """

    success: bool
    already_applied: bool = False
    error_message: str = ""


@runtime_checkable
class KnowledgeIndexPort(Protocol):
    """业务服务访问长期知识库索引的稳定端口。"""

    def ensure_collection(self, name: str) -> CollectionRef:
        """确保指定业务集合存在，并返回可重复使用的稳定引用。"""
        ...

    def store_document(
        self,
        collection: CollectionRef,
        file_path: str,
        metadata: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> IndexedDocument:
        """按幂等键保存文档；重复请求必须复用首次写入的逻辑文档。"""
        ...

    def remove_document(
        self,
        collection: CollectionRef,
        external_location: str,
    ) -> OperationResult:
        """按不透明外部位置幂等移除文档。"""
        ...

    def reconcile_document(
        self,
        collection: CollectionRef,
        *,
        idempotency_key: str,
    ) -> Optional[IndexedDocument]:
        """按幂等键查询既有文档；不存在时返回 ``None``。"""
        ...
