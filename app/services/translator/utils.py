"""兼容旧 Translation 工具导入路径。"""

from app.modules.translation.adapters.hymt_support import (
    ProgressTracker,
    build_prompt,
    clean_output,
)

__all__ = ["ProgressTracker", "build_prompt", "clean_output"]
