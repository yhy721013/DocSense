"""Analysis Input v5 的 Canonical Execution Profile。

Profile 只冻结受理时已经确定的执行能力身份与容量上限，不保存 API Key、完整 URL、
Prompt 正文、模型响应或宿主路径。Worker 必须在第一笔外部 I/O 前完整比较该 Profile，
不得用当前环境替历史任务补值。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Mapping

from .errors import AnalysisContractError


ANALYSIS_EXECUTION_PROFILE_SCHEMA_NAME = "docsense.analysis.execution-profile"
ANALYSIS_EXECUTION_PROFILE_SCHEMA_VERSION = 1
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PROFILE_FIELDS = frozenset(
    {
        "schema_name",
        "schema_version",
        "source_transport_profile_id",
        "max_download_bytes",
        "rag_provider_id",
        "rag_provider_fingerprint",
        "rag_workspace_profile_id",
        "rag_projection_profile_id",
        "rag_model_fingerprint",
        "prompt_profile_id",
        "knowledge_provider_id",
        "knowledge_provider_fingerprint",
        "knowledge_protocol_version",
    }
)


def _required_text(value: object, *, name: str, maximum: int = 128) -> str:
    if not isinstance(value, str):
        raise AnalysisContractError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise AnalysisContractError(f"{name} 不能为空")
    if len(normalized) > maximum:
        raise AnalysisContractError(f"{name} 最多 {maximum} 个字符")
    return normalized


def _sha256(value: object, *, name: str) -> str:
    normalized = _required_text(value, name=name, maximum=64).lower()
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise AnalysisContractError(f"{name} 必须是 SHA-256 小写十六进制摘要")
    return normalized


@dataclass(frozen=True, slots=True)
class AnalysisExecutionProfile:
    """受理时冻结的 Analysis 执行能力快照。"""

    schema_name: str
    schema_version: int
    source_transport_profile_id: str
    max_download_bytes: int
    rag_provider_id: str
    rag_provider_fingerprint: str
    rag_workspace_profile_id: str
    rag_projection_profile_id: str
    rag_model_fingerprint: str
    prompt_profile_id: str
    knowledge_provider_id: str
    knowledge_provider_fingerprint: str
    knowledge_protocol_version: str

    def __post_init__(self) -> None:
        if self.schema_name != ANALYSIS_EXECUTION_PROFILE_SCHEMA_NAME:
            raise AnalysisContractError("Analysis execution profile schema_name 不受支持")
        if self.schema_version != ANALYSIS_EXECUTION_PROFILE_SCHEMA_VERSION:
            raise AnalysisContractError("Analysis execution profile schema_version 不受支持")
        for name in (
            "source_transport_profile_id",
            "rag_provider_id",
            "rag_workspace_profile_id",
            "prompt_profile_id",
            "knowledge_provider_id",
            "knowledge_protocol_version",
        ):
            object.__setattr__(self, name, _required_text(getattr(self, name), name=name))
        for name in (
            "rag_provider_fingerprint",
            "rag_projection_profile_id",
            "rag_model_fingerprint",
            "knowledge_provider_fingerprint",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name=name))
        if (
            isinstance(self.max_download_bytes, bool)
            or not isinstance(self.max_download_bytes, int)
            or not 1 <= self.max_download_bytes <= 10 * 1024**4
        ):
            raise AnalysisContractError("max_download_bytes 必须是 1 字节到 10 TiB 的整数")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "source_transport_profile_id": self.source_transport_profile_id,
            "max_download_bytes": self.max_download_bytes,
            "rag_provider_id": self.rag_provider_id,
            "rag_provider_fingerprint": self.rag_provider_fingerprint,
            "rag_workspace_profile_id": self.rag_workspace_profile_id,
            "rag_projection_profile_id": self.rag_projection_profile_id,
            "rag_model_fingerprint": self.rag_model_fingerprint,
            "prompt_profile_id": self.prompt_profile_id,
            "knowledge_provider_id": self.knowledge_provider_id,
            "knowledge_provider_fingerprint": self.knowledge_provider_fingerprint,
            "knowledge_protocol_version": self.knowledge_protocol_version,
        }

    @property
    def canonical_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "AnalysisExecutionProfile":
        if not isinstance(value, Mapping):
            raise AnalysisContractError("execution_profile 必须是 Mapping")
        if frozenset(value.keys()) != _PROFILE_FIELDS:
            raise AnalysisContractError("execution_profile 字段集合不完整或包含未知字段")
        return cls(**{name: value[name] for name in _PROFILE_FIELDS})  # type: ignore[arg-type]


__all__ = [
    "ANALYSIS_EXECUTION_PROFILE_SCHEMA_NAME",
    "ANALYSIS_EXECUTION_PROFILE_SCHEMA_VERSION",
    "AnalysisExecutionProfile",
]
