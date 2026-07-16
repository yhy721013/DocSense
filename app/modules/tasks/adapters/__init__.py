"""任务模块基础设施适配层。

阶段 1B-2 已加入 Progress 单实例兼容实现和任务只读适配器；check-task 的可靠命令
MySQL/Outbox Adapter 仍由阶段 3～4 实现并在阶段 6 装配。
"""

from .in_memory_progress import InMemoryProgressAdapter
from .legacy_task_read import LegacyTaskReadAdapter

__all__ = ["InMemoryProgressAdapter", "LegacyTaskReadAdapter"]
