"""本地 Debug 查询模块。

该模块只承载内部调试页所需的只读查询，不定义或改变任何公开业务接口。
"""

from .composition import DebugApplicationServices, compose_debug_application_services

__all__ = ["DebugApplicationServices", "compose_debug_application_services"]
