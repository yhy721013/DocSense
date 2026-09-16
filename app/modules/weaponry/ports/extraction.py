"""只基于 Selected Evidence 的无历史抽取端口。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from app.modules.weaponry.domain import (
    EXTRACTION_CONTEXT_EVIDENCE_ONLY_V1,
    EXTRACTION_CONTEXT_PROVIDED_EVIDENCE_V1,
    AuxiliaryGuidance,
    ExtractionPrompt,
    SelectedEvidence,
    WeaponryDocumentSnapshot,
    WeaponryFieldSpecification,
)

from .common import (
    WeaponryCallIdentity,
    WeaponryOperation,
    non_negative_int,
    required_text,
    sha256_digest,
    text_tuple,
)


_CONTEXT_STRATEGIES = frozenset(
    {
        EXTRACTION_CONTEXT_PROVIDED_EVIDENCE_V1,
        EXTRACTION_CONTEXT_EVIDENCE_ONLY_V1,
    }
)


@dataclass(frozen=True)
class EvidenceExtractionRequest:
    """一次来源级抽取的完整、供应商无关输入。

    构造时即证明 Prompt 中的 ``evidence_ids``/``rows`` 与 Selected Evidence 完整文本逐项
    同序一致。Adapter 不得重新检索、截断或替换这组 Evidence。
    """

    call: WeaponryCallIdentity
    document: WeaponryDocumentSnapshot
    field: WeaponryFieldSpecification
    evidence: tuple[SelectedEvidence, ...]
    prompt: ExtractionPrompt
    guidance: tuple[AuxiliaryGuidance, ...]
    context_strategy: str
    model_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.call, WeaponryCallIdentity):
            raise TypeError("call 必须是 WeaponryCallIdentity")
        if self.call.operation is not WeaponryOperation.EVIDENCE_EXTRACTION:
            raise ValueError("抽取只能使用 evidence_extraction call")
        if not isinstance(self.document, WeaponryDocumentSnapshot):
            raise TypeError("document 必须是 WeaponryDocumentSnapshot")
        if self.call.document_sequence != self.document.sequence_no:
            raise ValueError("call 的 document_sequence 与目标文档不一致")
        if not isinstance(self.field, WeaponryFieldSpecification):
            raise TypeError("field 必须是 WeaponryFieldSpecification")
        if not isinstance(self.evidence, (tuple, list)) or not self.evidence or any(
            not isinstance(item, SelectedEvidence) for item in self.evidence
        ):
            raise TypeError("evidence 必须包含 SelectedEvidence")
        evidence = tuple(self.evidence)
        if any(item.document_key != self.document.document_key for item in evidence):
            raise ValueError("一次抽取只能包含当前 document_key 的 Evidence")
        evidence_ids = tuple(item.candidate_id for item in evidence)
        evidence_rows = tuple(item.text for item in evidence)
        if not isinstance(self.prompt, ExtractionPrompt):
            raise TypeError("prompt 必须是 ExtractionPrompt，禁止传入 RetrievalQuery")
        if self.prompt.document_key != self.document.document_key:
            raise ValueError("Prompt document_key 与目标文档不一致")
        if self.prompt.field_type != self.field.field_type:
            raise ValueError("Prompt field_type 与字段定义不一致")
        if self.prompt.evidence_ids != evidence_ids or self.prompt.rows != evidence_rows:
            raise ValueError("Prompt Evidence/rows 与 Selected Evidence 不一致")
        if not isinstance(self.guidance, (tuple, list)) or any(
            not isinstance(item, AuxiliaryGuidance) for item in self.guidance
        ):
            raise TypeError("guidance 只能包含 AuxiliaryGuidance")
        guidance = tuple(self.guidance)
        guidance_ids = tuple(item.guidance_id for item in guidance)
        if len(set(guidance_ids)) != len(guidance_ids):
            raise ValueError("guidance_id 不能重复")
        if self.context_strategy not in _CONTEXT_STRATEGIES:
            raise ValueError("context_strategy 不受支持")
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "guidance", guidance)
        object.__setattr__(
            self,
            "model_fingerprint",
            required_text(self.model_fingerprint, name="model_fingerprint"),
        )


class ExtractionValidationOutcome(str, Enum):
    """Adapter 已完成的上下文归属校验结果。"""

    MATCHED = "matched"
    EMPTY_ANSWER = "empty_answer"


@dataclass(frozen=True)
class ExtractionSourceTrace:
    """已映射到目标 Evidence 的供应商无关来源轨迹。"""

    source_ref: str
    document_key: str
    evidence_id: str
    source_marker_digest: str

    def __post_init__(self) -> None:
        for name in ("source_ref", "document_key", "evidence_id"):
            object.__setattr__(
                self,
                name,
                required_text(getattr(self, name), name=name),
            )
        object.__setattr__(
            self,
            "source_marker_digest",
            sha256_digest(
                self.source_marker_digest,
                name="source_marker_digest",
            ),
        )


@dataclass(frozen=True)
class ExtractionAnswer:
    """已完成 Evidence 边界校验的抽取回答。

    无法映射、缺失、混合或额外来源不应构造本对象，而应由 Adapter 抛出
    ``WeaponrySourceBoundaryError``。正文只保留清洗结果；原始回答仅保存摘要和字符数。
    """

    call: WeaponryCallIdentity
    text: str
    raw_response_digest: str
    raw_response_chars: int
    evidence_ids: tuple[str, ...]
    sources: tuple[ExtractionSourceTrace, ...]
    validation_outcome: ExtractionValidationOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.call, WeaponryCallIdentity):
            raise TypeError("call 必须是 WeaponryCallIdentity")
        if self.call.operation is not WeaponryOperation.EVIDENCE_EXTRACTION:
            raise ValueError("抽取回答只能绑定 evidence_extraction call")
        if not isinstance(self.text, str):
            raise TypeError("text 必须是 str")
        object.__setattr__(
            self,
            "raw_response_digest",
            sha256_digest(
                self.raw_response_digest,
                name="raw_response_digest",
            ),
        )
        non_negative_int(self.raw_response_chars, name="raw_response_chars")
        object.__setattr__(
            self,
            "evidence_ids",
            text_tuple(self.evidence_ids, name="evidence_ids", allow_empty=False),
        )
        if not isinstance(self.sources, (tuple, list)) or any(
            not isinstance(item, ExtractionSourceTrace) for item in self.sources
        ):
            raise TypeError("sources 只能包含 ExtractionSourceTrace")
        sources = tuple(self.sources)
        source_refs = tuple(item.source_ref for item in sources)
        if len(set(source_refs)) != len(source_refs):
            raise ValueError("sources 不能包含重复 source_ref")
        evidence_id_set = set(self.evidence_ids)
        if any(item.evidence_id not in evidence_id_set for item in sources):
            raise ValueError("sources 只能引用当前回答 evidence_ids")
        object.__setattr__(self, "sources", sources)
        if not isinstance(self.validation_outcome, ExtractionValidationOutcome):
            raise TypeError("validation_outcome 必须是 ExtractionValidationOutcome")
        if self.text and self.validation_outcome is not ExtractionValidationOutcome.MATCHED:
            raise ValueError("非空回答必须通过 matched 来源校验")
        if not self.text and self.validation_outcome is not ExtractionValidationOutcome.EMPTY_ANSWER:
            raise ValueError("空回答必须使用 empty_answer 校验结果")


@runtime_checkable
class EvidenceExtractionPort(Protocol):
    """每次调用必须使用无历史、只包含 Request Evidence 的推理上下文。"""

    def extract(self, request: EvidenceExtractionRequest) -> ExtractionAnswer:
        ...


__all__ = [
    "EvidenceExtractionPort",
    "EvidenceExtractionRequest",
    "ExtractionAnswer",
    "ExtractionSourceTrace",
    "ExtractionValidationOutcome",
]
