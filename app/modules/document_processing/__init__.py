"""共享文档处理模块的稳定公开导出。

Legacy Office 的原有导入路径在 1H-2 完成调用方迁移前继续兼容；新调用方应优先依赖
``domain``、``ports`` 和 ``application`` 子包中的窄边界。
"""

from .application import PrepareDocument, ReconcileProcessingRecord
from .domain import (
    ArtifactKind,
    ArtifactMetadata,
    ArtifactRef,
    DocumentProcessingRequest,
    DocumentProcessingResult,
    DocumentRepresentation,
    LEGACY_OFFICE_SAFE_ERROR_MESSAGE,
    LegacyOfficeConfig,
    LegacyOfficeConversionError,
    LegacyOfficePreparationResult,
    LineageEvent,
    ProcessingOutcome,
    ProcessingProfile,
)
from .libreoffice import (
    LibreOfficeLegacyOfficePreparer,
    discover_libreoffice_executable,
    is_legacy_office_path,
)
from .ports import (
    ArtifactCatalogPort,
    ArtifactContent,
    ArtifactPublication,
    ArtifactStorePort,
    DocumentProcessorPort,
    LegacyOfficePreparer,
    ProcessingRecordPort,
    ProcessingRecoveryPort,
    ProcessorOutput,
)

__all__ = [
    "ArtifactContent",
    "ArtifactCatalogPort",
    "ArtifactKind",
    "ArtifactMetadata",
    "ArtifactPublication",
    "ArtifactRef",
    "ArtifactStorePort",
    "DocumentProcessingRequest",
    "DocumentProcessingResult",
    "DocumentProcessorPort",
    "DocumentRepresentation",
    "LEGACY_OFFICE_SAFE_ERROR_MESSAGE",
    "LegacyOfficeConfig",
    "LegacyOfficeConversionError",
    "LegacyOfficePreparationResult",
    "LegacyOfficePreparer",
    "LineageEvent",
    "LibreOfficeLegacyOfficePreparer",
    "PrepareDocument",
    "ProcessingOutcome",
    "ProcessingProfile",
    "ProcessingRecordPort",
    "ProcessingRecoveryPort",
    "ProcessorOutput",
    "ReconcileProcessingRecord",
    "discover_libreoffice_executable",
    "is_legacy_office_path",
]
