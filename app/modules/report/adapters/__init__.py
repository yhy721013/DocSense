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
from .callback_recovery import SQLiteReportCallbackRecoverySource
from .interaction_audit import SQLiteReportInteractionAuditAdapter
from .legacy_files import LegacyReportFileAdapter
from .local_artifacts import LocalReportArtifactAdapter
from .local_dispatcher import (
    LocalReportDispatcherSnapshot,
    LocalReportTaskDispatcher,
)
from .process_guard import FileProcessSingletonGuard
from .resource_store import SQLiteReportResourceStoreAdapter
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
    "ReportTaskCommandCodec",
    "SQLiteReportCallbackAdapter",
    "SQLiteReportCallbackRecoverySource",
    "SQLiteReportInteractionAuditAdapter",
    "SQLiteReportResourceStoreAdapter",
]
