"""恢复 Application 的兼容导出模块。

阶段 1E-7 已把原有巨型恢复 Facade 迁移到 :mod:`recovery_facade`，并由 Observer、Checkpoint
Reconciler、Compensator、Finalizer 四个独立协作器实际承载算法。本模块保留既有 Python 导入路径，
避免组合根、诊断脚本或内部调用方因文件级重构发生行为或接口漂移。
"""

from .recovery_facade import (
    RecoverReassignmentCommand,
    RecoverReassignmentOperation,
    ReassignmentRecoveryResult,
    ReassignmentRecoveryResultCategory,
)


__all__ = [
    "RecoverReassignmentCommand",
    "RecoverReassignmentOperation",
    "ReassignmentRecoveryResult",
    "ReassignmentRecoveryResultCategory",
]
