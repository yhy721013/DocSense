"""从已校验的实际 Adapter 与部署能力构造 Analysis Input v5 Profile。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.modules.analysis.domain.execution_profile import (
    ANALYSIS_EXECUTION_PROFILE_SCHEMA_NAME,
    ANALYSIS_EXECUTION_PROFILE_SCHEMA_VERSION,
    AnalysisExecutionProfile,
)

from .runtime_config import AnalysisExecutionCapabilityConfig


RAG_PROVIDER_ID = "anythingllm-api-v1.15"
RAG_WORKSPACE_PROFILE_ID = "analysis-isolated-workspace-v1"
PROMPT_PROFILE_ID = "analysis-classification-extraction-prompts-v1"
KNOWLEDGE_PROVIDER_ID = "anythingllm-api-v1.15"
KNOWLEDGE_PROTOCOL_VERSION = "knowledge-index-v1"


@runtime_checkable
class AnalysisFileExecutionIdentity(Protocol):
    @property
    def source_transport_profile_id(self) -> str: ...

    @property
    def max_download_bytes(self) -> int: ...

    @property
    def rag_projection_profile_id(self) -> str: ...


def build_analysis_execution_profile(
    *,
    capabilities: AnalysisExecutionCapabilityConfig,
    files: AnalysisFileExecutionIdentity,
) -> AnalysisExecutionProfile:
    """冻结实际 Source/投影能力与部署声明，禁止用固定摘要替代运行对象。"""

    if not isinstance(capabilities, AnalysisExecutionCapabilityConfig):
        raise TypeError("capabilities 必须是 AnalysisExecutionCapabilityConfig")
    if not isinstance(files, AnalysisFileExecutionIdentity):
        raise TypeError("files 必须暴露 Analysis 文件执行身份")
    return AnalysisExecutionProfile(
        schema_name=ANALYSIS_EXECUTION_PROFILE_SCHEMA_NAME,
        schema_version=ANALYSIS_EXECUTION_PROFILE_SCHEMA_VERSION,
        source_transport_profile_id=files.source_transport_profile_id,
        max_download_bytes=files.max_download_bytes,
        rag_provider_id=RAG_PROVIDER_ID,
        rag_provider_fingerprint=capabilities.rag_provider_fingerprint,
        rag_workspace_profile_id=RAG_WORKSPACE_PROFILE_ID,
        rag_projection_profile_id=files.rag_projection_profile_id,
        rag_model_fingerprint=capabilities.rag_model_fingerprint,
        prompt_profile_id=PROMPT_PROFILE_ID,
        knowledge_provider_id=KNOWLEDGE_PROVIDER_ID,
        # 当前 RAG 与永久知识工厂共享同一个 AnythingLLMConfig；若组合根未来拆分，
        # 本复用必须失败关闭并通过 Profile 升版表达，而不是继续使用旧 fingerprint。
        knowledge_provider_fingerprint=capabilities.rag_provider_fingerprint,
        knowledge_protocol_version=KNOWLEDGE_PROTOCOL_VERSION,
    )


__all__ = [
    "KNOWLEDGE_PROTOCOL_VERSION",
    "KNOWLEDGE_PROVIDER_ID",
    "PROMPT_PROFILE_ID",
    "RAG_PROVIDER_ID",
    "RAG_WORKSPACE_PROFILE_ID",
    "build_analysis_execution_profile",
]
