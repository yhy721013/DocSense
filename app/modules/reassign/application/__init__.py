"""分类节点变更应用层边界。

阶段 1E-6 提供已接线的同步前向编排与显式恢复服务。恢复必须携带预期 fencing、操作者和
原因码，只处理精确 Operation；不启动后台扫描线程、不创建队列。组合根和 Flask 路由只引用本层
公开用例，公开 HTTP 参数、状态码与响应格式完全不在本模块职责内。
"""

from .recover_reassignment import (
    RecoverReassignmentCommand,
    RecoverReassignmentOperation,
    ReassignmentRecoveryResult,
    ReassignmentRecoveryResultCategory,
)
from .service import DocumentReassignmentService, ReassignmentExecutionSettings

__all__ = [
    "DocumentReassignmentService",
    "ReassignmentExecutionSettings",
    "RecoverReassignmentCommand",
    "RecoverReassignmentOperation",
    "ReassignmentRecoveryResult",
    "ReassignmentRecoveryResultCategory",
]
