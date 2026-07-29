"""共享文档处理领域对象及 Legacy Office 兼容导出。"""

from .errors import (
    ArtifactConflictError,
    ArtifactError,
    ArtifactIntegrityError,
    DocumentProcessingError,
    ProcessingRecordConflictError,
    ProcessingRecordError,
)
from .legacy_office import (
    LEGACY_OFFICE_SAFE_ERROR_MESSAGE,
    LegacyOfficeConfig,
    LegacyOfficeConversionError,
    LegacyOfficePreparationResult,
    _CleanupLease,
)
from .models import (
    ArtifactKind,
    ArtifactMetadata,
    ArtifactRef,
    DocumentProcessingRequest,
    DocumentProcessingResult,
    DocumentRepresentation,
    LineageEvent,
    ProcessingOutcome,
    ProcessingProfile,
    derive_artifact_id,
    derive_step_key,
)
from .mhtml import extract_mhtml_text, is_mhtml_content

__all__ = [
    "ArtifactConflictError",
    "ArtifactError",
    "ArtifactIntegrityError",
    "ArtifactKind",
    "ArtifactMetadata",
    "ArtifactRef",
    "DocumentProcessingError",
    "DocumentProcessingRequest",
    "DocumentProcessingResult",
    "DocumentRepresentation",
    "LEGACY_OFFICE_SAFE_ERROR_MESSAGE",
    "LegacyOfficeConfig",
    "LegacyOfficeConversionError",
    "LegacyOfficePreparationResult",
    "LineageEvent",
    "ProcessingOutcome",
    "ProcessingProfile",
    "ProcessingRecordConflictError",
    "ProcessingRecordError",
    "derive_artifact_id",
    "derive_step_key",
    "extract_mhtml_text",
    "is_mhtml_content",
]
