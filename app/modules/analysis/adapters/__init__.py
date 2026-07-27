"""文件分析基础设施适配器。"""

from .callback_guard import SQLiteAnalysisCallbackAdapter
from .callback_recovery import SQLiteAnalysisCallbackRecoverySource
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
from .task_codec import AnalysisTaskInputCodec, AnalysisTaskInputCodecError
from .task_commands import (
    AnalysisTaskCommandAdapterError,
    AnalysisTaskSnapshotCorruptedError,
    SQLiteAnalysisBatchCommandAdapter,
)
from .translation import (
    AnalysisTranslationExecutionCoordinator,
    LegacyAnalysisTranslationService,
    SerializedAnalysisTranslationAdapter,
)

__all__ = (
    "AnalysisTaskInputCodec",
    "AnalysisTaskInputCodecError",
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
    "LegacyAnalysisTranslationService",
    "LocalAnalysisTaskWorkspaceAdapter",
    "SerializedAnalysisTranslationAdapter",
    "SQLiteAnalysisBatchCommandAdapter",
    "SQLiteAnalysisCallbackAdapter",
    "SQLiteAnalysisCallbackRecoverySource",
    "SQLiteAnalysisResourceStoreAdapter",
)
