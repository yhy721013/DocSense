"""目标文档 Evidence Candidate 检索端口。

该端口只接受精炼的 :class:`RetrievalQuery`，并且只能返回尚未通过领域选择门禁的
``EvidenceCandidate``。Extraction Prompt、Selected Evidence 和供应商 workspace DTO 在类型层
均无法传入此端口，从而避免旧链路把“怎么回答”的指令混入“检索什么”的 Query。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.modules.tasks.domain import TaskId
from app.modules.weaponry.domain import (
    EVIDENCE_SCORE_MODE_RANK,
    EVIDENCE_SCORE_MODE_SCORE,
    EvidenceCandidate,
    EvidenceSelectionPolicy,
    RetrievalQuery,
    WeaponryDocumentScope,
)

from .common import (
    IdempotentOperationResult,
    WeaponryCallIdentity,
    WeaponryOperation,
    positive_int,
    required_text,
    text_tuple,
)


@dataclass(frozen=True)
class OpenTargetEvidenceScope:
    """为一次 execution 打开独占检索范围的命令。"""

    task_id: TaskId
    document_scope: WeaponryDocumentScope
    policy: EvidenceSelectionPolicy

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.document_scope, WeaponryDocumentScope):
            raise TypeError("document_scope 必须是 WeaponryDocumentScope")
        if not self.document_scope.documents:
            raise ValueError("空文档范围不得打开目标检索资源")
        if not isinstance(self.policy, EvidenceSelectionPolicy):
            raise TypeError("policy 必须是 EvidenceSelectionPolicy")


@dataclass(frozen=True)
class TargetEvidenceScope:
    """Adapter 已建立的 execution 独占检索范围。

    ``scope_ref`` 是本模块生成或 Adapter 映射的不透明引用，Application 不得从中解析真实
    workspace 名称。指纹字段用于在检索前证明实际 Provider 与任务快照一致。
    """

    task_id: TaskId
    scope_ref: str
    allowed_document_keys: tuple[str, ...]
    selection_profile_id: str
    provider_fingerprint: str
    embedding_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        object.__setattr__(self, "scope_ref", required_text(self.scope_ref, name="scope_ref"))
        object.__setattr__(
            self,
            "allowed_document_keys",
            text_tuple(
                self.allowed_document_keys,
                name="allowed_document_keys",
                allow_empty=False,
            ),
        )
        for name in (
            "selection_profile_id",
            "provider_fingerprint",
            "embedding_fingerprint",
        ):
            object.__setattr__(
                self,
                name,
                required_text(getattr(self, name), name=name),
            )


@dataclass(frozen=True)
class SearchTargetEvidence:
    """在已打开范围内执行一次字段级 Candidate 召回。"""

    scope: TargetEvidenceScope
    call: WeaponryCallIdentity
    query: RetrievalQuery
    allowed_document_keys: tuple[str, ...]
    candidate_top_n: int

    def __post_init__(self) -> None:
        if not isinstance(self.scope, TargetEvidenceScope):
            raise TypeError("scope 必须是 TargetEvidenceScope")
        if not isinstance(self.call, WeaponryCallIdentity):
            raise TypeError("call 必须是 WeaponryCallIdentity")
        if self.call.operation is not WeaponryOperation.TARGET_RETRIEVAL:
            raise ValueError("目标检索只能使用 target_retrieval call")
        if self.call.task_id != self.scope.task_id:
            raise ValueError("call 与 scope 不属于同一 task_id")
        if not isinstance(self.query, RetrievalQuery):
            raise TypeError("query 必须是 RetrievalQuery，禁止传入 ExtractionPrompt")
        allowed = text_tuple(
            self.allowed_document_keys,
            name="allowed_document_keys",
            allow_empty=False,
        )
        scope_keys = set(self.scope.allowed_document_keys)
        if any(document_key not in scope_keys for document_key in allowed):
            raise ValueError("allowed_document_keys 超出已冻结检索范围")
        object.__setattr__(self, "allowed_document_keys", allowed)
        positive_int(self.candidate_top_n, name="candidate_top_n")


@dataclass(frozen=True)
class TargetEvidenceSearchResult:
    """一次检索调用返回的 Candidate 与实际运行指纹。"""

    scope_ref: str
    call: WeaponryCallIdentity
    candidates: tuple[EvidenceCandidate, ...]
    score_mode: str
    provider_fingerprint: str
    embedding_fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope_ref", required_text(self.scope_ref, name="scope_ref"))
        if not isinstance(self.call, WeaponryCallIdentity):
            raise TypeError("call 必须是 WeaponryCallIdentity")
        if self.call.operation is not WeaponryOperation.TARGET_RETRIEVAL:
            raise ValueError("检索结果只能绑定 target_retrieval call")
        if not isinstance(self.candidates, (tuple, list)) or any(
            not isinstance(item, EvidenceCandidate) for item in self.candidates
        ):
            raise TypeError("candidates 只能包含 EvidenceCandidate")
        object.__setattr__(self, "candidates", tuple(self.candidates))
        if self.score_mode not in {
            EVIDENCE_SCORE_MODE_SCORE,
            EVIDENCE_SCORE_MODE_RANK,
        }:
            raise ValueError("score_mode 只能是 score 或 rank")
        for name in ("provider_fingerprint", "embedding_fingerprint"):
            object.__setattr__(
                self,
                name,
                required_text(getattr(self, name), name=name),
            )


@runtime_checkable
class TargetEvidenceRetrievalPort(Protocol):
    """为每个 execution 建立独占范围并返回未选择 Candidate。"""

    def open_scope(self, command: OpenTargetEvidenceScope) -> TargetEvidenceScope:
        ...

    def search_target(
        self,
        command: SearchTargetEvidence,
    ) -> TargetEvidenceSearchResult:
        ...

    def close_scope(self, scope: TargetEvidenceScope) -> IdempotentOperationResult:
        """幂等关闭范围；真实资源事实由 Resource Port 持久化。"""
        ...


__all__ = [
    "OpenTargetEvidenceScope",
    "SearchTargetEvidence",
    "TargetEvidenceRetrievalPort",
    "TargetEvidenceScope",
    "TargetEvidenceSearchResult",
]
