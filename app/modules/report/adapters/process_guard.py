"""报告模块对通用单实例文件锁的兼容薄适配器。"""

from __future__ import annotations

import logging
from pathlib import Path

from app.modules.tasks.adapters.process_guard import (
    FileProcessSingletonGuard as _GenericFileProcessSingletonGuard,
)


logger = logging.getLogger(__name__)


class FileProcessSingletonGuard(_GenericFileProcessSingletonGuard):
    """保留阶段 1C 的导入路径和报告日志命名空间。"""

    def __init__(self, path: str | Path) -> None:
        super().__init__(
            path,
            component_name="报告 Dispatcher",
            event_logger=logger,
        )


__all__ = ["FileProcessSingletonGuard"]
