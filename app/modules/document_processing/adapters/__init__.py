"""共享文档处理基础设施适配器。"""

from .capacity import (
    FIFOCapacityAdapter,
    ResourceLimitedDocumentProcessorAdapter,
)
from .content import BytesArtifactContent, FileArtifactContent
from .local_artifacts import LocalArtifactStoreAdapter
from .local_pipeline import (
    LocalDocumentPreparationAdapter,
    LocalDocumentPreparationError,
    LocalDocumentPreparationRequest,
    LocalPreparedArtifact,
    ScannedPDFEngine,
)
from .mineru import (
    MINERU_PROCESSOR_FINGERPRINT,
    MINERU_PROCESSOR_ID,
    MinerUConverter,
    MinerUDocumentProcessorAdapter,
    MinerUOperationObserver,
    build_mineru_profile,
    mineru_endpoint_fingerprint,
)
from .passthrough import (
    PASSTHROUGH_PROCESSOR_FINGERPRINT,
    PASSTHROUGH_PROCESSOR_ID,
    ValidatedPassthroughDocumentProcessorAdapter,
    build_passthrough_profile,
)
from .sqlite_operations import SQLiteMinerUOperationObserver
from .sqlite_records import SQLiteProcessingRecordAdapter

__all__ = [
    "BytesArtifactContent",
    "FIFOCapacityAdapter",
    "FileArtifactContent",
    "LocalArtifactStoreAdapter",
    "LocalDocumentPreparationAdapter",
    "LocalDocumentPreparationError",
    "LocalDocumentPreparationRequest",
    "LocalPreparedArtifact",
    "MINERU_PROCESSOR_FINGERPRINT",
    "MINERU_PROCESSOR_ID",
    "MinerUConverter",
    "MinerUDocumentProcessorAdapter",
    "MinerUOperationObserver",
    "PASSTHROUGH_PROCESSOR_FINGERPRINT",
    "PASSTHROUGH_PROCESSOR_ID",
    "ResourceLimitedDocumentProcessorAdapter",
    "SQLiteMinerUOperationObserver",
    "SQLiteProcessingRecordAdapter",
    "ScannedPDFEngine",
    "ValidatedPassthroughDocumentProcessorAdapter",
    "build_mineru_profile",
    "mineru_endpoint_fingerprint",
    "build_passthrough_profile",
]
