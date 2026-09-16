"""供应商无关、存储无关的共享文档处理领域对象。

这些对象只保存不可变事实和稳定身份，不包含 ``Path``、SQLite Row、Flask Request 或
任意供应商响应。后续把本地文件替换为 MinIO、把 SQLite 替换为 MySQL 时，Application
仍只依赖这里的值对象。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from app.modules.tasks.domain import TaskId


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


def _sha256_text(value: object, *, name: str) -> str:
    normalized = _required_text(value, name=name).lower()
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{name} 必须是 64 位小写十六进制 SHA-256")
    return normalized


def _canonical_json(value: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        raise TypeError("parameters 必须是 Mapping")
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError("parameters 必须是可稳定 JSON 序列化的数据") from exc
    if not isinstance(decoded, dict):
        raise ValueError("parameters 顶层必须是对象")
    return encoded


def _stable_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class DocumentRepresentation(str, Enum):
    """处理链中可被后续步骤消费的文档表示。"""

    ORIGINAL = "original"
    OOXML = "ooxml"
    PDF = "pdf"
    MARKDOWN = "markdown"
    TEXT = "text"
    HTML = "html"


class ArtifactKind(str, Enum):
    """Artifact 的生命周期角色，而非物理目录或 bucket。"""

    SOURCE = "source"
    NORMALIZED = "normalized"
    PREPARED = "prepared"
    RAG_PROJECTION = "rag_projection"
    TRANSLATION_BILINGUAL = "translation_bilingual"
    TRANSLATION_MONOLINGUAL = "translation_monolingual"
    QUARANTINE = "quarantine"


class ProcessingOutcome(str, Enum):
    """一次处理步骤的模块内终态或可观察状态。"""

    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True, slots=True)
class ProcessingProfile:
    """冻结处理器身份、实现指纹、目标表示和参数。"""

    processor_id: str
    processor_fingerprint: str
    target_representation: DocumentRepresentation
    parameters_json: str = "{}"
    profile_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "processor_id",
            _required_text(self.processor_id, name="processor_id"),
        )
        object.__setattr__(
            self,
            "processor_fingerprint",
            _required_text(
                self.processor_fingerprint,
                name="processor_fingerprint",
            ),
        )
        if not isinstance(self.target_representation, DocumentRepresentation):
            raise TypeError("target_representation 必须是 DocumentRepresentation")
        if not isinstance(self.parameters_json, str):
            raise TypeError("parameters_json 必须是 str")
        try:
            decoded = json.loads(self.parameters_json)
        except json.JSONDecodeError as exc:
            raise ValueError("parameters_json 必须是合法 JSON") from exc
        canonical = _canonical_json(decoded)
        object.__setattr__(self, "parameters_json", canonical)
        expected = _stable_digest(
            {
                "parameters": decoded,
                "processorFingerprint": self.processor_fingerprint,
                "processorId": self.processor_id,
                "targetRepresentation": self.target_representation.value,
            }
        )
        if self.profile_id and self.profile_id.lower() != expected:
            raise ValueError("profile_id 与冻结 profile 内容不一致")
        object.__setattr__(self, "profile_id", expected)

    @classmethod
    def create(
        cls,
        *,
        processor_id: str,
        processor_fingerprint: str,
        target_representation: DocumentRepresentation,
        parameters: Mapping[str, Any] | None = None,
    ) -> "ProcessingProfile":
        return cls(
            processor_id=processor_id,
            processor_fingerprint=processor_fingerprint,
            target_representation=target_representation,
            parameters_json=_canonical_json(parameters or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "parameters": json.loads(self.parameters_json),
            "processorFingerprint": self.processor_fingerprint,
            "processorId": self.processor_id,
            "profileId": self.profile_id,
            "targetRepresentation": self.target_representation.value,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProcessingProfile":
        expected_keys = {
            "parameters",
            "processorFingerprint",
            "processorId",
            "profileId",
            "targetRepresentation",
        }
        if not isinstance(value, Mapping) or set(value) != expected_keys:
            raise ValueError("ProcessingProfile 字段集合不合法")
        try:
            target = DocumentRepresentation(value["targetRepresentation"])
        except (TypeError, ValueError) as exc:
            raise ValueError("targetRepresentation 不合法") from exc
        return cls(
            processor_id=value["processorId"],
            processor_fingerprint=value["processorFingerprint"],
            target_representation=target,
            parameters_json=_canonical_json(value["parameters"]),
            profile_id=value["profileId"],
        )


@dataclass(frozen=True, slots=True)
class ArtifactMetadata:
    """Artifact 的可校验内容元数据。"""

    media_type: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "media_type",
            _required_text(self.media_type, name="media_type").lower(),
        )
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise ValueError("size_bytes 必须是非负整数")
        object.__setattr__(
            self,
            "sha256",
            _sha256_text(self.sha256, name="sha256"),
        )


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """Application 可持有的不可变 Artifact 引用；不暴露宿主路径。"""

    task_id: TaskId
    artifact_id: str
    step_key: str
    kind: ArtifactKind
    representation: DocumentRepresentation
    metadata: ArtifactMetadata
    ordinal: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        object.__setattr__(
            self,
            "artifact_id",
            _sha256_text(self.artifact_id, name="artifact_id"),
        )
        object.__setattr__(
            self,
            "step_key",
            _sha256_text(self.step_key, name="step_key"),
        )
        if not isinstance(self.kind, ArtifactKind):
            raise TypeError("kind 必须是 ArtifactKind")
        if not isinstance(self.representation, DocumentRepresentation):
            raise TypeError("representation 必须是 DocumentRepresentation")
        if not isinstance(self.metadata, ArtifactMetadata):
            raise TypeError("metadata 必须是 ArtifactMetadata")
        if (
            isinstance(self.ordinal, bool)
            or not isinstance(self.ordinal, int)
            or self.ordinal <= 0
        ):
            raise ValueError("ordinal 必须是正整数")


@dataclass(frozen=True, slots=True)
class DocumentProcessingRequest:
    """一次已规范化的文档处理请求。"""

    task_id: TaskId
    step_id: str
    source_artifact: ArtifactRef
    profile: ProcessingProfile
    trace_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        object.__setattr__(
            self,
            "step_id",
            _required_text(self.step_id, name="step_id"),
        )
        if not isinstance(self.source_artifact, ArtifactRef):
            raise TypeError("source_artifact 必须是 ArtifactRef")
        if self.source_artifact.task_id != self.task_id:
            raise ValueError("source_artifact 不属于当前 task")
        if not isinstance(self.profile, ProcessingProfile):
            raise TypeError("profile 必须是 ProcessingProfile")
        object.__setattr__(
            self,
            "trace_id",
            _required_text(self.trace_id, name="trace_id"),
        )

    @property
    def step_key(self) -> str:
        return derive_step_key(self)


@dataclass(frozen=True, slots=True)
class LineageEvent:
    """父 Artifact 到子 Artifact 的不可变处理谱系。"""

    event_id: str
    task_id: TaskId
    step_key: str
    parent_artifact_id: str
    child_artifact_id: str
    operation: str
    profile_id: str
    processor_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        for name in (
            "event_id",
            "step_key",
            "parent_artifact_id",
            "child_artifact_id",
            "profile_id",
        ):
            object.__setattr__(
                self,
                name,
                _sha256_text(getattr(self, name), name=name),
            )
        object.__setattr__(
            self,
            "operation",
            _required_text(self.operation, name="operation"),
        )
        object.__setattr__(
            self,
            "processor_fingerprint",
            _required_text(
                self.processor_fingerprint,
                name="processor_fingerprint",
            ),
        )

    @classmethod
    def create(
        cls,
        *,
        request: DocumentProcessingRequest,
        child: ArtifactRef,
    ) -> "LineageEvent":
        event_id = _stable_digest(
            {
                "childArtifactId": child.artifact_id,
                "operation": request.profile.processor_id,
                "parentArtifactId": request.source_artifact.artifact_id,
                "profileId": request.profile.profile_id,
                "stepKey": request.step_key,
            }
        )
        return cls(
            event_id=event_id,
            task_id=request.task_id,
            step_key=request.step_key,
            parent_artifact_id=request.source_artifact.artifact_id,
            child_artifact_id=child.artifact_id,
            operation=request.profile.processor_id,
            profile_id=request.profile.profile_id,
            processor_fingerprint=request.profile.processor_fingerprint,
        )


@dataclass(frozen=True, slots=True)
class DocumentProcessingResult:
    """处理用例结果；公开接口仍由业务模块自行映射。"""

    outcome: ProcessingOutcome
    step_key: str
    artifact: ArtifactRef | None = None
    lineage: LineageEvent | None = None
    warnings: tuple[str, ...] = ()
    error_code: str = ""
    reused: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ProcessingOutcome):
            raise TypeError("outcome 必须是 ProcessingOutcome")
        object.__setattr__(
            self,
            "step_key",
            _sha256_text(self.step_key, name="step_key"),
        )
        if self.artifact is not None and not isinstance(self.artifact, ArtifactRef):
            raise TypeError("artifact 必须是 ArtifactRef 或 None")
        if self.lineage is not None and not isinstance(self.lineage, LineageEvent):
            raise TypeError("lineage 必须是 LineageEvent 或 None")
        warnings = tuple(self.warnings)
        if any(not isinstance(item, str) for item in warnings):
            raise TypeError("warnings 只能包含 str")
        object.__setattr__(self, "warnings", warnings)
        object.__setattr__(self, "error_code", str(self.error_code).strip())
        if not isinstance(self.reused, bool):
            raise TypeError("reused 必须是 bool")
        if self.outcome is ProcessingOutcome.SUCCEEDED:
            if self.artifact is None or self.lineage is None or self.error_code:
                raise ValueError("成功结果必须包含 Artifact/Lineage 且不得包含 error_code")
        elif self.artifact is not None or self.lineage is not None:
            raise ValueError("非成功结果不得对外承诺 Artifact/Lineage")


def derive_step_key(request: DocumentProcessingRequest) -> str:
    """由任务、逻辑步骤、源内容和冻结 profile 推导稳定幂等键。"""

    return _stable_digest(
        {
            "profileId": request.profile.profile_id,
            "sourceArtifactId": request.source_artifact.artifact_id,
            "sourceSha256": request.source_artifact.metadata.sha256,
            "stepId": request.step_id,
            "taskId": request.task_id.value,
        }
    )


def derive_artifact_id(
    *,
    step_key: str,
    kind: ArtifactKind,
    representation: DocumentRepresentation,
    ordinal: int = 1,
) -> str:
    """推导不包含任务明文、路径或供应商信息的确定性 Artifact ID。"""

    if not isinstance(kind, ArtifactKind):
        raise TypeError("kind 必须是 ArtifactKind")
    if not isinstance(representation, DocumentRepresentation):
        raise TypeError("representation 必须是 DocumentRepresentation")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal <= 0:
        raise ValueError("ordinal 必须是正整数")
    return _stable_digest(
        {
            "kind": kind.value,
            "ordinal": ordinal,
            "representation": representation.value,
            "stepKey": _sha256_text(step_key, name="step_key"),
        }
    )


__all__ = [
    "ArtifactKind",
    "ArtifactMetadata",
    "ArtifactRef",
    "DocumentProcessingRequest",
    "DocumentProcessingResult",
    "DocumentRepresentation",
    "LineageEvent",
    "ProcessingOutcome",
    "ProcessingProfile",
    "derive_artifact_id",
    "derive_step_key",
]
