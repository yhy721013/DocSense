"""文件分析基础设施适配器。"""

from .callback_guard import SQLiteAnalysisCallbackAdapter
from .callback_recovery import SQLiteAnalysisCallbackRecoverySource
from .resource_activity import InMemoryAnalysisResourceActivityAdapter
from .legacy_audit import LegacyAnalysisAuditAdapter, LegacyAnalysisAuditAdapterError
from .legacy_files import (
    AnalysisFilePreparationError,
    LegacyAnalysisFilePreparationAdapter,
    LocalAnalysisTaskWorkspaceAdapter,
)
from .legacy_knowledge import LegacyAnalysisKnowledgeAdapter
from .legacy_rag import LegacyAnalysisRagAdapter, LegacyAnalysisRagAdapterFactory
from .local_dispatcher import (
    LocalAnalysisDispatcherSnapshot,
    LocalAnalysisTaskDispatcher,
)
from .resource_store import (
    AnalysisResourceStoreConcurrencyError,
    SQLiteAnalysisResourceStoreAdapter,
)
from .task_codec import (
    AnalysisTaskInputCodec,
    AnalysisTaskInputCodecError,
    AnalysisV5TaskCommandCodec,
)
from .task_commands import (
    AnalysisTaskCommandAdapterError,
    AnalysisTaskSnapshotCorruptedError,
    SQLiteAnalysisBatchCommandAdapter,
)
from .translation import (
    ArtifactAnalysisTranslationAdapter,
    AnalysisTranslationExecutionCoordinator,
    LegacyAnalysisTranslationService,
    SerializedAnalysisTranslationAdapter,
)
from .execution_profile_factory import build_analysis_execution_profile
from .runtime_config import (
    AnalysisClassificationConfig,
    AnalysisExecutionCapabilityConfig,
    AnalysisInfrastructureConfig,
    load_analysis_classification_config,
    load_analysis_execution_capability_config,
    load_analysis_infrastructure_config,
)
from .v2_batch_admission import (
    AnalysisV2BatchAdmissionError,
    SQLiteAnalysisV2BatchAdmissionAdapter,
)
from .v2_callback import TaskControlAnalysisCallbackAdapter
from .v2_callback_recovery import SQLiteAnalysisV2CallbackRecoverySource
from .v2_runtime import (
    AnalysisV2Maintenance,
    AnalysisV2MaintenanceSnapshot,
    AnalysisV2TaskDispatcher,
)

__all__ = (
    "AnalysisTaskInputCodec",
    "ArtifactAnalysisTranslationAdapter",
    "AnalysisTaskInputCodecError",
    "AnalysisV5TaskCommandCodec",
    "AnalysisV2BatchAdmissionError",
    "AnalysisResourceStoreConcurrencyError",
    "AnalysisTaskCommandAdapterError",
    "AnalysisTaskSnapshotCorruptedError",
    "AnalysisFilePreparationError",
    "AnalysisTranslationExecutionCoordinator",
    "LegacyAnalysisAuditAdapter",
    "LegacyAnalysisAuditAdapterError",
    "LegacyAnalysisFilePreparationAdapter",
    "LegacyAnalysisKnowledgeAdapter",
    "LegacyAnalysisRagAdapter",
    "LegacyAnalysisRagAdapterFactory",
    "LocalAnalysisDispatcherSnapshot",
    "LocalAnalysisTaskDispatcher",
    "InMemoryAnalysisResourceActivityAdapter",
    "LegacyAnalysisTranslationService",
    "LocalAnalysisTaskWorkspaceAdapter",
    "SerializedAnalysisTranslationAdapter",
    "SQLiteAnalysisBatchCommandAdapter",
    "SQLiteAnalysisV2BatchAdmissionAdapter",
    "TaskControlAnalysisCallbackAdapter",
    "SQLiteAnalysisV2CallbackRecoverySource",
    "AnalysisV2Maintenance",
    "AnalysisV2MaintenanceSnapshot",
    "AnalysisV2TaskDispatcher",
    "SQLiteAnalysisCallbackAdapter",
    "SQLiteAnalysisCallbackRecoverySource",
    "SQLiteAnalysisResourceStoreAdapter",
    "AnalysisClassificationConfig",
    "AnalysisExecutionCapabilityConfig",
    "AnalysisInfrastructureConfig",
    "build_analysis_execution_profile",
    "load_analysis_classification_config",
    "load_analysis_execution_capability_config",
    "load_analysis_infrastructure_config",
)
