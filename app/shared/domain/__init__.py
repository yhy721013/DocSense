"""跨业务模块共享、无 I/O 的稳定领域规则。"""

from .knowledge_workspace import (
    ARCHITECTURE_ID_MAX,
    ARCHITECTURE_ID_MIN,
    PERMANENT_ARCHITECTURE_WORKSPACE_PREFIX,
    permanent_architecture_workspace_name,
)

__all__ = [
    "ARCHITECTURE_ID_MAX",
    "ARCHITECTURE_ID_MIN",
    "PERMANENT_ARCHITECTURE_WORKSPACE_PREFIX",
    "permanent_architecture_workspace_name",
]
