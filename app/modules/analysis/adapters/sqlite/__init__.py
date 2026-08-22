"""Analysis Task Control v2 的 SQLite 适配器。"""

from .schema import (
    ANALYSIS_CONTROL_COMPONENT_NAME,
    ANALYSIS_CONTROL_COMPONENT_VERSION,
    bootstrap_analysis_task_control_database,
    load_analysis_control_manifest,
)
from .result_snapshot_store import SQLiteAnalysisResultSnapshotStore
from .step_continuation_store import SQLiteAnalysisStepContinuationStore
from .unit_of_work import (
    SQLiteAnalysisExecutionUnitOfWork,
    SQLiteAnalysisExecutionUnitOfWorkFactory,
)
from .resource_store import (
    AnalysisResourceStoreConcurrencyError,
    SQLiteAnalysisV2ResourceStoreAdapter,
)
from .audit_persistence import (
    SQLiteAnalysisAuditPersistence,
    build_analysis_v2_audit_adapter,
)

__all__ = [
    "ANALYSIS_CONTROL_COMPONENT_NAME",
    "ANALYSIS_CONTROL_COMPONENT_VERSION",
    "bootstrap_analysis_task_control_database",
    "load_analysis_control_manifest",
    "SQLiteAnalysisResultSnapshotStore",
    "SQLiteAnalysisStepContinuationStore",
    "SQLiteAnalysisExecutionUnitOfWork",
    "SQLiteAnalysisExecutionUnitOfWorkFactory",
    "AnalysisResourceStoreConcurrencyError",
    "SQLiteAnalysisV2ResourceStoreAdapter",
    "SQLiteAnalysisAuditPersistence",
    "build_analysis_v2_audit_adapter",
]
