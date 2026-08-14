"""从已校验运行能力构造 Report Input v2 Execution Profile。"""

from __future__ import annotations

import hashlib
import json
from typing import Protocol, runtime_checkable

from app.integrations.anythingllm.policies import (
    DEFAULT_EMBEDDING_ATTEMPTS,
    DEFAULT_UPLOAD_RETRIES,
    DEFAULT_UPLOAD_RETRY_BASE_DELAY_SECONDS,
    DOCUMENT_RAG_WORKSPACE_POLICY_VERSION,
    document_rag_workspace_settings,
)
from app.modules.report.domain import (
    REPORT_EMPTY_RESULT_POLICY,
    REPORT_EXECUTION_PROFILE_SCHEMA_NAME,
    REPORT_EXECUTION_PROFILE_SCHEMA_VERSION,
    ReportExecutionProfile,
)

from .runtime_config import ReportExecutionCapabilityConfig, ReportRuntimeConfig


SOURCE_TRANSPORT_PROFILE_ID = "http-source-atomic-download-v1"
TEMPLATE_EXTRACTOR_PROFILE_ID = "report-docx-xml-text-v1"
RAG_PROVIDER_ID = "anythingllm-api-v1.15"
PROMPT_PROFILE_ID = "report-generation-prompt-v1"
SANITIZER_PROFILE_ID = "report-artifact-source-sanitizer-v1"
RENDERER_PROFILE_ID = "report-html-renderer-v1"


@runtime_checkable
class _DocumentPreparationIdentity(Protocol):
    @property
    def execution_profile_id(self) -> str: ...

    @property
    def execution_profile_fingerprint(self) -> str: ...


def _fingerprint(value: object) -> str:
    material = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def build_report_execution_profile(
    *,
    runtime_config: ReportRuntimeConfig,
    capabilities: ReportExecutionCapabilityConfig,
    document_preparer: _DocumentPreparationIdentity,
) -> ReportExecutionProfile:
    """构造可写入 Input v2 的完整能力快照。

    生产组合根必须把实际安装的同一 ``document_preparer`` 传入，禁止用固定常量代替
    运行对象。当前生产 RAG 参数沿用既有默认值；未来开放配置时必须把实际值传入本工厂。
    """

    if not isinstance(runtime_config, ReportRuntimeConfig):
        raise TypeError("runtime_config 必须是 ReportRuntimeConfig")
    if not isinstance(capabilities, ReportExecutionCapabilityConfig):
        raise TypeError("capabilities 必须是 ReportExecutionCapabilityConfig")
    if not isinstance(document_preparer, _DocumentPreparationIdentity):
        raise TypeError("document_preparer 必须暴露冻结执行身份")

    workspace_fingerprint = _fingerprint(
        {
            "policyVersion": DOCUMENT_RAG_WORKSPACE_POLICY_VERSION,
            "settings": document_rag_workspace_settings(),
        }
    )
    upload_fingerprint = _fingerprint(
        {
            "profile": "anythingllm-report-upload-v1",
            "uploadMaxRetries": DEFAULT_UPLOAD_RETRIES,
            "uploadRetryBaseDelaySeconds": DEFAULT_UPLOAD_RETRY_BASE_DELAY_SECONDS,
            "embeddingMaxAttempts": DEFAULT_EMBEDDING_ATTEMPTS,
            "orderedUpload": True,
        }
    )
    return ReportExecutionProfile(
        schema_name=REPORT_EXECUTION_PROFILE_SCHEMA_NAME,
        schema_version=REPORT_EXECUTION_PROFILE_SCHEMA_VERSION,
        source_transport_profile_id=SOURCE_TRANSPORT_PROFILE_ID,
        max_download_bytes=runtime_config.max_download_bytes,
        document_processing_profile_id=document_preparer.execution_profile_id,
        document_processing_fingerprint=(
            document_preparer.execution_profile_fingerprint
        ),
        template_extractor_profile_id=TEMPLATE_EXTRACTOR_PROFILE_ID,
        rag_provider_id=RAG_PROVIDER_ID,
        rag_provider_fingerprint=capabilities.rag_provider_fingerprint,
        rag_model_fingerprint=capabilities.rag_model_fingerprint,
        rag_workspace_settings_fingerprint=workspace_fingerprint,
        rag_upload_policy_fingerprint=upload_fingerprint,
        prompt_profile_id=PROMPT_PROFILE_ID,
        sanitizer_profile_id=SANITIZER_PROFILE_ID,
        renderer_profile_id=RENDERER_PROFILE_ID,
        empty_result_policy=REPORT_EMPTY_RESULT_POLICY,
    )


__all__ = [
    "PROMPT_PROFILE_ID",
    "RAG_PROVIDER_ID",
    "RENDERER_PROFILE_ID",
    "SANITIZER_PROFILE_ID",
    "SOURCE_TRANSPORT_PROFILE_ID",
    "TEMPLATE_EXTRACTOR_PROFILE_ID",
    "build_report_execution_profile",
]
