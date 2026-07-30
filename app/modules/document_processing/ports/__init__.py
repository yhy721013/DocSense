"""共享文档处理端口及 Legacy Office 兼容导出。"""

from .legacy_office import LegacyOfficePreparer
from .processing import (
    ArtifactContent,
    ArtifactCatalogPort,
    ArtifactPublication,
    ArtifactStorePort,
    DocumentProcessorPort,
    ProcessingAcquireDecision,
    ProcessingAcquireResult,
    ProcessingRecordPort,
    ProcessingRecoveryPort,
    ProcessingRecordSnapshot,
    ProcessingRecordState,
    ProcessorOutput,
    ResourcePort,
)

__all__ = [
    "ArtifactContent",
    "ArtifactCatalogPort",
    "ArtifactPublication",
    "ArtifactStorePort",
    "DocumentProcessorPort",
    "LegacyOfficePreparer",
    "ProcessingAcquireDecision",
    "ProcessingAcquireResult",
    "ProcessingRecordPort",
    "ProcessingRecoveryPort",
    "ProcessingRecordSnapshot",
    "ProcessingRecordState",
    "ProcessorOutput",
    "ResourcePort",
]
