"""Report Input v2 的 Canonical Execution Profile。

Profile 只保存受理时已经确定的执行能力身份与策略摘要，不保存 API Key、完整 URL、
Prompt 正文、模型响应或宿主路径。相同语义必须产生逐字节一致的 Canonical JSON 和
SHA-256；Worker 在第一笔外部 I/O 前用完整对象或 fingerprint 比较运行能力。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Mapping

from .errors import ReportDomainValidationError


REPORT_EXECUTION_PROFILE_SCHEMA_NAME = "docsense.report.execution-profile"
REPORT_EXECUTION_PROFILE_SCHEMA_VERSION = 1
REPORT_EMPTY_RESULT_POLICY = "success_with_empty_html_v1"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_PROFILE_FIELDS = frozenset(
    {
        "schema_name",
        "schema_version",
        "source_transport_profile_id",
        "max_download_bytes",
        "document_processing_profile_id",
        "document_processing_fingerprint",
        "template_extractor_profile_id",
        "rag_provider_id",
        "rag_provider_fingerprint",
        "rag_model_fingerprint",
        "rag_workspace_settings_fingerprint",
        "rag_upload_policy_fingerprint",
        "prompt_profile_id",
        "sanitizer_profile_id",
        "renderer_profile_id",
        "empty_result_policy",
    }
)


def _required_text(value: object, *, name: str, maximum: int = 128) -> str:
    if not isinstance(value, str):
        raise ReportDomainValidationError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ReportDomainValidationError(f"{name} 不能为空")
    if len(normalized) > maximum:
        raise ReportDomainValidationError(f"{name} 最多 {maximum} 个字符")
    return normalized


def _sha256(value: object, *, name: str) -> str:
    normalized = _required_text(value, name=name, maximum=64).lower()
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise ReportDomainValidationError(f"{name} 必须是 SHA-256 小写十六进制摘要")
    return normalized


@dataclass(frozen=True, slots=True)
class ReportExecutionProfile:
    """受理时冻结的 Report 执行决策快照。"""

    schema_name: str
    schema_version: int
    source_transport_profile_id: str
    max_download_bytes: int
    document_processing_profile_id: str
    document_processing_fingerprint: str
    template_extractor_profile_id: str
    rag_provider_id: str
    rag_provider_fingerprint: str
    rag_model_fingerprint: str
    rag_workspace_settings_fingerprint: str
    rag_upload_policy_fingerprint: str
    prompt_profile_id: str
    sanitizer_profile_id: str
    renderer_profile_id: str
    empty_result_policy: str

    def __post_init__(self) -> None:
        if self.schema_name != REPORT_EXECUTION_PROFILE_SCHEMA_NAME:
            raise ReportDomainValidationError("Report execution profile schema_name 不受支持")
        if self.schema_version != REPORT_EXECUTION_PROFILE_SCHEMA_VERSION:
            raise ReportDomainValidationError("Report execution profile schema_version 不受支持")
        for name in (
            "source_transport_profile_id",
            "document_processing_profile_id",
            "template_extractor_profile_id",
            "rag_provider_id",
            "prompt_profile_id",
            "sanitizer_profile_id",
            "renderer_profile_id",
        ):
            object.__setattr__(self, name, _required_text(getattr(self, name), name=name))
        for name in (
            "document_processing_fingerprint",
            "rag_provider_fingerprint",
            "rag_model_fingerprint",
            "rag_workspace_settings_fingerprint",
            "rag_upload_policy_fingerprint",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name=name))
        if (
            isinstance(self.max_download_bytes, bool)
            or not isinstance(self.max_download_bytes, int)
            or not 1 <= self.max_download_bytes <= 10 * 1024**4
        ):
            raise ReportDomainValidationError("max_download_bytes 必须是 1 字节到 10 TiB 的整数")
        if self.empty_result_policy != REPORT_EMPTY_RESULT_POLICY:
            raise ReportDomainValidationError("Report empty_result_policy 不受支持")

    def to_dict(self) -> dict[str, object]:
        """返回字段完整、顺序稳定但不依赖顺序计算身份的新字典。"""

        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "source_transport_profile_id": self.source_transport_profile_id,
            "max_download_bytes": self.max_download_bytes,
            "document_processing_profile_id": self.document_processing_profile_id,
            "document_processing_fingerprint": self.document_processing_fingerprint,
            "template_extractor_profile_id": self.template_extractor_profile_id,
            "rag_provider_id": self.rag_provider_id,
            "rag_provider_fingerprint": self.rag_provider_fingerprint,
            "rag_model_fingerprint": self.rag_model_fingerprint,
            "rag_workspace_settings_fingerprint": self.rag_workspace_settings_fingerprint,
            "rag_upload_policy_fingerprint": self.rag_upload_policy_fingerprint,
            "prompt_profile_id": self.prompt_profile_id,
            "sanitizer_profile_id": self.sanitizer_profile_id,
            "renderer_profile_id": self.renderer_profile_id,
            "empty_result_policy": self.empty_result_policy,
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
    def from_dict(cls, value: Mapping[str, object]) -> "ReportExecutionProfile":
        if not isinstance(value, Mapping):
            raise ReportDomainValidationError("execution_profile 必须是 Mapping")
        if frozenset(value.keys()) != _PROFILE_FIELDS:
            raise ReportDomainValidationError("execution_profile 字段集合不完整或包含未知字段")
        return cls(**{name: value[name] for name in _PROFILE_FIELDS})  # type: ignore[arg-type]


__all__ = [
    "REPORT_EMPTY_RESULT_POLICY",
    "REPORT_EXECUTION_PROFILE_SCHEMA_NAME",
    "REPORT_EXECUTION_PROFILE_SCHEMA_VERSION",
    "ReportExecutionProfile",
]
