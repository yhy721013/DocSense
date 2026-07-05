"""供应商无关的知识库索引契约与结果模型。

本模块把“确保集合存在、保存文档、移除文档、按幂等键对账”定义为稳定的应用层能力。
调用方只能持有不透明引用，不得根据引用内容推导外部系统的存储目录、资源名称或请求
地址。具体索引系统的字段转换、网络调用和错误翻译由后续适配器负责。
"""

from __future__ import annotations

import hashlib
import json
from contextlib import AbstractContextManager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from .rag import PreparedDocumentRef


def build_document_idempotency_key(
    *,
    file_name: str,
    architecture_id: int,
    content_sha256: str,
) -> str:
    """构造版本化、供应商无关的永久文档默认幂等键。

    文件名区分业务对象，存储 architecture 区分永久集合，内容摘要区分同名文件的新版本。
    最终只暴露固定长度摘要，避免把可能包含敏感信息的完整文件名写入协调索引和日志。
    """
    normalized_file_name = str(file_name or "").strip()
    if not normalized_file_name:
        raise ValueError("file_name 不能为空")
    if (
        isinstance(architecture_id, bool)
        or not isinstance(architecture_id, int)
        or architecture_id < 1
    ):
        raise ValueError("architecture_id 必须是正整数")
    normalized_digest = str(content_sha256 or "").strip().casefold()
    if (
        len(normalized_digest) != 64
        or any(character not in "0123456789abcdef" for character in normalized_digest)
    ):
        raise ValueError("content_sha256 必须是 64 位十六进制摘要")
    canonical_identity = (
        f"{normalized_file_name}\0{architecture_id}\0{normalized_digest}"
    )
    identity_digest = hashlib.sha256(canonical_identity.encode("utf-8")).hexdigest()
    return f"document:v1:{identity_digest}"


class KnowledgeIndexError(RuntimeError):
    """永久知识库操作未能安全完成时抛出的供应商无关基础异常。"""


class KnowledgeIndexConflictError(KnowledgeIndexError):
    """相同幂等身份携带不同业务内容或发生非法状态转换。"""


class KnowledgeIndexRecoveryRequiredError(KnowledgeIndexError):
    """外部结果或补偿结果不确定，必须人工或专用恢复流程介入。"""


class KnowledgeIndexRetentionRequiredError(KnowledgeIndexRecoveryRequiredError):
    """永久索引已经接管文档，调用方不得再执行全局删除。

    该异常专门描述跨系统提交的部分成功：永久集合已经绑定文档，或本地权威记录已经提交，
    但后续协调状态写入没有全部完成。调用方仍应把业务任务标记为失败，不过清理 RAG 会话
    时必须使用 ``retain_document=True``，让后续对账继续复用同一全局文档。
    """

    retain_document_required = True


@dataclass(frozen=True)
class CollectionSpec:
    """永久知识集合的稳定业务身份和显示名称。

    ``architecture_id`` 是 DocSense 本地权威关系键，``name`` 只用于创建或展示外部
    Workspace。适配器必须先按 architecture ID 解析本地映射，禁止根据显示名称反向猜测
    业务身份，从而避免误接管同名 Workspace。
    """

    architecture_id: int
    name: str

    def __post_init__(self) -> None:
        """规范化集合身份，并拒绝布尔值伪装成整数。"""
        if (
            isinstance(self.architecture_id, bool)
            or not isinstance(self.architecture_id, int)
            or self.architecture_id < 1
        ):
            raise ValueError("CollectionSpec.architecture_id 必须是正整数")
        normalized_name = str(self.name or "").strip()
        if not normalized_name:
            raise ValueError("CollectionSpec.name 不能为空")
        object.__setattr__(self, "name", normalized_name)


@dataclass(frozen=True)
class KnowledgeDocumentMetadata:
    """永久索引文档的类型化本地权威元数据。

    文件身份字段与可扩展业务属性分离，避免调用方把 ``architecture_id``、``file_name``
    等控制字段混入任意 Mapping。``attributes`` 通过严格 JSON 往返生成深复制快照，既阻止
    调用方在提交过程中修改嵌套对象，也保证该对象一定可以持久化到 SQLite。
    """

    file_name: str
    original_name: str
    attributes: Mapping[str, Any]

    def __post_init__(self) -> None:
        """规范化文件名并冻结严格 JSON 业务属性。"""
        normalized_file_name = str(self.file_name or "").strip()
        if not normalized_file_name:
            raise ValueError("KnowledgeDocumentMetadata.file_name 不能为空")
        normalized_original_name = str(self.original_name or "").strip()
        if not normalized_original_name:
            normalized_original_name = normalized_file_name
        if not isinstance(self.attributes, Mapping):
            raise TypeError("KnowledgeDocumentMetadata.attributes 必须是 Mapping")
        try:
            serialized = json.dumps(
                dict(self.attributes),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            snapshot = json.loads(serialized)
        except (TypeError, ValueError) as exc:
            raise ValueError("KnowledgeDocumentMetadata.attributes 必须是严格 JSON 对象") from exc
        if not isinstance(snapshot, dict):
            raise TypeError("KnowledgeDocumentMetadata.attributes 必须序列化为 JSON 对象")
        object.__setattr__(self, "file_name", normalized_file_name)
        object.__setattr__(self, "original_name", normalized_original_name)
        object.__setattr__(self, "attributes", MappingProxyType(snapshot))


@dataclass(frozen=True)
class KnowledgeOperationContext:
    """一次长期知识库操作对应的供应商无关业务身份。

    ``execution_id`` 区分同一业务键的主动重跑和同一次执行重试；业务类型与业务键用于
    协调记录、审计关联和故障巡检。Gateway 不得从 metadata 或集合名称反向猜测这些字段。
    """

    execution_id: str
    business_type: str
    business_key: str

    def __post_init__(self) -> None:
        """拒绝无法建立协调记录的空业务身份。"""
        for field_name in ("execution_id", "business_type", "business_key"):
            normalized_value = str(getattr(self, field_name) or "").strip()
            if not normalized_value:
                raise ValueError(f"KnowledgeOperationContext.{field_name} 不能为空")
            object.__setattr__(self, field_name, normalized_value)


@dataclass(frozen=True)
class CollectionRef:
    """业务层可安全持有的知识集合引用。

    ``ref`` 是适配器生成的不透明稳定标识，``name`` 是业务请求的集合名称。调用方可以
    比较引用是否相同，但不能解析 ``ref`` 或把它拼接成外部请求参数。
    """

    ref: str
    name: str
    architecture_id: int

    def __post_init__(self) -> None:
        """拒绝无法用于关联索引操作的空引用或空名称。"""
        normalized_ref = str(self.ref or "").strip()
        normalized_name = str(self.name or "").strip()
        if not normalized_ref:
            raise ValueError("CollectionRef.ref 不能为空")
        if not normalized_name:
            raise ValueError("CollectionRef.name 不能为空")
        if (
            isinstance(self.architecture_id, bool)
            or not isinstance(self.architecture_id, int)
            or self.architecture_id < 1
        ):
            raise ValueError("CollectionRef.architecture_id 必须是正整数")
        object.__setattr__(self, "ref", normalized_ref)
        object.__setattr__(self, "name", normalized_name)


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
        if not isinstance(self.created, bool) or not isinstance(self.reused, bool):
            raise TypeError("IndexedDocument.created 与 reused 必须是 bool")
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

    def __post_init__(self) -> None:
        """保证成功、幂等命中和错误信息之间不存在矛盾状态。"""
        if not isinstance(self.success, bool):
            raise TypeError("OperationResult.success 必须是 bool")
        if not isinstance(self.already_applied, bool):
            raise TypeError("OperationResult.already_applied 必须是 bool")
        normalized_error = str(self.error_message or "").strip()
        if self.already_applied and not self.success:
            raise ValueError("already_applied=True 时 success 必须为 True")
        if self.success and normalized_error:
            raise ValueError("成功的 OperationResult 不得包含 error_message")
        if not self.success and not normalized_error:
            raise ValueError("失败的 OperationResult 必须包含 error_message")
        object.__setattr__(self, "error_message", normalized_error)


@runtime_checkable
class KnowledgeIndexPort(Protocol):
    """业务服务访问长期知识库索引的稳定端口。"""

    def ensure_collection(self, spec: CollectionSpec) -> CollectionRef:
        """按稳定业务身份确保集合存在，并返回可重复使用的引用。"""
        ...

    def store_document(
        self,
        collection: CollectionRef,
        file_path: str,
        metadata: KnowledgeDocumentMetadata,
        *,
        operation_context: KnowledgeOperationContext,
        idempotency_key: str,
    ) -> IndexedDocument:
        """按幂等键保存文档；重复请求必须复用首次写入的逻辑文档。"""
        ...

    def store_prepared_document(
        self,
        collection: CollectionRef,
        document: PreparedDocumentRef,
        metadata: KnowledgeDocumentMetadata,
        *,
        operation_context: KnowledgeOperationContext,
        idempotency_key: str,
    ) -> IndexedDocument:
        """把 RAG 已准备的同一文档登记到长期集合，不得再次上传文件。

        ``document`` 的两个引用均为不透明值。具体适配器负责验证该句柄是否属于自己管理
        的供应商，并完成集合绑定和本地业务元数据登记；业务层只负责传递和持久化返回
        结果。新链路不得调用未经验证的供应商文档元数据更新端点。
        """
        ...

    def detach_document(
        self,
        collection: CollectionRef,
        external_location: str,
        *,
        operation_context: KnowledgeOperationContext,
    ) -> OperationResult:
        """只解除目标集合与文档的绑定，不永久删除全局文档。

        全局删除属于未转交文档的补偿能力，由 ``DocumentRagSession.close`` 根据会话所有权
        执行；本端口不得仅凭外部位置删除可能被其他集合共享的全局实体。
        """
        ...

    def reconcile_document(
        self,
        collection: CollectionRef,
        *,
        operation_context: KnowledgeOperationContext,
        idempotency_key: str,
    ) -> Optional[IndexedDocument]:
        """按幂等键查询既有文档；不存在时返回 ``None``。"""
        ...


@runtime_checkable
class KnowledgeIndexFactory(Protocol):
    """为单个任务创建并托管长期知识库端口的应用层工厂契约。"""

    def create(self) -> AbstractContextManager[KnowledgeIndexPort]:
        """创建一次任务独占的知识库端口租约。"""
        ...
