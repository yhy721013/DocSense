"""任务模块基础设施适配层。

阶段 1B-2 已加入 Progress 单实例兼容实现和任务只读适配器；check-task 的可靠命令
MySQL/Outbox Adapter 仍由阶段 3～4 实现并在阶段 6 作为后台兜底装配，不替换甲方规定的
请求内同步恢复。
"""

from .in_memory_progress import InMemoryProgressAdapter
from .latest_progress import LatestTaskProgressPublisherAdapter
from .legacy_task_read import LegacyTaskReadAdapter
from .synchronous_callback_recovery import SynchronousCallbackRecoveryRouterAdapter
from .execution_limiter import UploadTaskLimiter
from .process_guard import FileProcessSingletonGuard
from .local_persistent_dispatcher import (
    LocalPersistentDispatcherSettings,
    LocalPersistentDispatcherSnapshot,
    LocalPersistentMaintenanceSnapshot,
    LocalPersistentMaintenanceTask,
    LocalPersistentTaskDispatcher,
)
from .legacy_task_commands import (
    EncodedTaskResult,
    EncodedTaskSubmission,
    LegacyTaskCommandAdapter,
    LegacyTaskCommandAdapterError,
    TaskCommandCodec,
)
from app.modules.tasks.http_deadlines import required_http_lease_seconds

__all__ = [
    "EncodedTaskResult",
    "EncodedTaskSubmission",
    "FileProcessSingletonGuard",
    "InMemoryProgressAdapter",
    "LocalPersistentDispatcherSettings",
    "LocalPersistentDispatcherSnapshot",
    "LocalPersistentMaintenanceSnapshot",
    "LocalPersistentMaintenanceTask",
    "LocalPersistentTaskDispatcher",
    "LegacyTaskCommandAdapter",
    "LegacyTaskCommandAdapterError",
    "LegacyTaskReadAdapter",
    "SynchronousCallbackRecoveryRouterAdapter",
    "LatestTaskProgressPublisherAdapter",
    "TaskCommandCodec",
    "UploadTaskLimiter",
    "required_http_lease_seconds",
]
