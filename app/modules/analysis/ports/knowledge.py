"""永久知识库写入的三态 Port，避免把网络未知结果误判为失败或成功。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from app.modules.analysis.domain.task_inputs import FrozenJsonObject

from .common import AnalysisExecutionRef
from .rag import AnalysisRagSessionRef


class AnalysisKnowledgeWriteOutcome(str, Enum):
    COMMITTED = "committed"
    NOT_APPLIED = "not_applied"
    OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True)
class AnalysisKnowledgeDocumentMetadata:
    """永久知识库转交所需的稳定业务元数据。"""

    file_name: str
    original_file_name: str
    attributes: FrozenJsonObject

    def __post_init__(self) -> None:
        for field_name in ("file_name", "original_file_name"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} 必须是非空 str")
            object.__setattr__(self, field_name, value.strip())
        if not isinstance(self.attributes, FrozenJsonObject):
            raise TypeError("attributes 必须是 FrozenJsonObject")


@dataclass(frozen=True)
class AnalysisKnowledgeWriteRequest:
    execution: AnalysisExecutionRef
    architecture_id: int
    idempotency_key: str
    document: AnalysisRagSessionRef
    metadata: AnalysisKnowledgeDocumentMetadata

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        if isinstance(self.architecture_id, bool) or not isinstance(self.architecture_id, int) or self.architecture_id < 1:
            raise ValueError("architecture_id 必须是正整数")
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key.strip():
            raise ValueError("idempotency_key 必须是非空 str")
        object.__setattr__(self, "idempotency_key", self.idempotency_key.strip())
        if not isinstance(self.document, AnalysisRagSessionRef):
            raise TypeError("document 必须是 AnalysisRagSessionRef")
        if self.document.execution != self.execution or not self.document.document_bound:
            raise ValueError("document 必须是当前 execution 的已绑定文档")
        if not isinstance(self.metadata, AnalysisKnowledgeDocumentMetadata):
            raise TypeError("metadata 必须是 AnalysisKnowledgeDocumentMetadata")


@dataclass(frozen=True)
class AnalysisKnowledgeWriteResult:
    execution: AnalysisExecutionRef
    idempotency_key: str
    outcome: AnalysisKnowledgeWriteOutcome
    external_ref: str = ""
    detail_code: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key.strip():
            raise ValueError("idempotency_key 必须是非空 str")
        object.__setattr__(self, "idempotency_key", self.idempotency_key.strip())
        if not isinstance(self.outcome, AnalysisKnowledgeWriteOutcome):
            raise TypeError("outcome 必须是 AnalysisKnowledgeWriteOutcome")
        if not isinstance(self.external_ref, str):
            raise TypeError("external_ref 必须是 str")
        if not isinstance(self.detail_code, str):
            raise TypeError("detail_code 必须是 str")
        object.__setattr__(self, "external_ref", self.external_ref.strip())
        object.__setattr__(self, "detail_code", self.detail_code.strip())
        if self.outcome is AnalysisKnowledgeWriteOutcome.COMMITTED and not self.external_ref:
            raise ValueError("committed 结果必须携带 external_ref")
        if self.outcome is AnalysisKnowledgeWriteOutcome.COMMITTED and self.detail_code:
            raise ValueError("committed 结果不得携带 detail_code")
        if self.outcome is not AnalysisKnowledgeWriteOutcome.COMMITTED and not self.detail_code:
            raise ValueError("未提交结果必须携带 detail_code")


@runtime_checkable
class AnalysisKnowledgePort(Protocol):
    """永久知识写入必须提供可判定的三态结果。"""

    def persist(
        self,
        request: AnalysisKnowledgeWriteRequest,
    ) -> AnalysisKnowledgeWriteResult:
        ...


__all__ = (
    "AnalysisKnowledgePort",
    "AnalysisKnowledgeDocumentMetadata",
    "AnalysisKnowledgeWriteOutcome",
    "AnalysisKnowledgeWriteRequest",
    "AnalysisKnowledgeWriteResult",
)
