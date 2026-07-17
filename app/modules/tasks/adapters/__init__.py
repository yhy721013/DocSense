"""任务模块基础设施适配层。

阶段 1B-2 已加入 Progress 单实例兼容实现和任务只读适配器；check-task 的可靠命令
MySQL/Outbox Adapter 仍由阶段 3～4 实现并在阶段 6 作为后台兜底装配，不替换甲方规定的
请求内同步恢复。
"""

from .in_memory_progress import InMemoryProgressAdapter
from .latest_progress import LatestTaskProgressPublisherAdapter
from .legacy_task_read import LegacyTaskReadAdapter
from .execution_limiter import UploadTaskLimiter
from .legacy_task_commands import (
    EncodedTaskResult,
    EncodedTaskSubmission,
    LegacyTaskCommandAdapter,
    LegacyTaskCommandAdapterError,
    TaskCommandCodec,
)

__all__ = [
    "EncodedTaskResult",
    "EncodedTaskSubmission",
    "InMemoryProgressAdapter",
    "LegacyTaskCommandAdapter",
    "LegacyTaskCommandAdapterError",
    "LegacyTaskReadAdapter",
    "LatestTaskProgressPublisherAdapter",
    "TaskCommandCodec",
    "UploadTaskLimiter",
]
