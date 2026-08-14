"""报告基础设施适配器。

模块导入只暴露构造类型，不创建数据库连接、HTTP Transport 或后台线程。阶段 1C-6 已由
应用组合根显式构造并启动本地 Dispatcher；单纯导入本包仍保持无副作用。
"""

from .anythingllm_rag import (
    AnythingLLMReportClientFactory,
    AnythingLLMReportRagAdapter,
    ReportAnythingLLMClients,
)
from .callback_guard import SQLiteReportCallbackAdapter
from .v2_callback import TaskControlReportCallbackAdapter
from .v2_callback_recovery import SQLiteReportV2CallbackRecoverySource
from .v2_dispatcher import ReportV2TaskDispatcher
from .v2_maintenance import ReportV2Maintenance, ReportV2MaintenanceSnapshot
from .callback_recovery import SQLiteReportCallbackRecoverySource
from .docx_template import extract_docx_template_text
from .execution_profile_factory import build_report_execution_profile
from .interaction_audit import (
    SQLiteReportInteractionAuditAdapter,
    build_report_v2_interaction_audit_adapter,
)
from .legacy_files import LegacyReportFileAdapter
from .local_artifacts import LocalReportArtifactAdapter
from .local_dispatcher import (
    LocalReportDispatcherSnapshot,
    LocalReportTaskDispatcher,
)
from .process_guard import FileProcessSingletonGuard
from .resource_store import SQLiteReportResourceStoreAdapter
from .runtime_config import (
    ReportRuntimeConfig,
    ReportRuntimeConfigurationError,
    ReportExecutionCapabilityConfig,
    load_report_execution_capability_config,
    load_report_runtime_config,
)
from .task_codec import ReportTaskCommandCodec

__all__ = [
    "AnythingLLMReportClientFactory",
    "AnythingLLMReportRagAdapter",
    "FileProcessSingletonGuard",
    "LegacyReportFileAdapter",
    "LocalReportArtifactAdapter",
    "LocalReportDispatcherSnapshot",
    "LocalReportTaskDispatcher",
    "ReportAnythingLLMClients",
    "ReportRuntimeConfig",
    "ReportRuntimeConfigurationError",
    "ReportExecutionCapabilityConfig",
    "ReportTaskCommandCodec",
    "SQLiteReportCallbackAdapter",
    "TaskControlReportCallbackAdapter",
    "SQLiteReportV2CallbackRecoverySource",
    "ReportV2TaskDispatcher",
    "ReportV2Maintenance",
    "ReportV2MaintenanceSnapshot",
    "SQLiteReportCallbackRecoverySource",
    "SQLiteReportInteractionAuditAdapter",
    "build_report_v2_interaction_audit_adapter",
    "SQLiteReportResourceStoreAdapter",
    "extract_docx_template_text",
    "build_report_execution_profile",
    "load_report_execution_capability_config",
    "load_report_runtime_config",
]
