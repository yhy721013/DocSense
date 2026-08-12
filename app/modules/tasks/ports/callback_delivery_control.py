"""Admission 使用的 Callback Guard 冲突查询边界。

本文件只冻结 Task Control 所需的本地权威事实；它不发送 HTTP，也不读取本地 Callback
历史诊断文件。完整 Delivery 状态迁移将在对应持久化步骤实现前单独接受契约测试。
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol, runtime_checkable

from app.modules.tasks.domain import TaskBusinessRef


class CallbackAdmissionConflict(str, Enum):
    """新 Task 受理时需要阻断的 Callback 状态。"""

    NONE = "none"
    SENDING = "sending"
    OUTCOME_UNKNOWN = "outcome_unknown"


@runtime_checkable
class CallbackAdmissionConflictPort(Protocol):
    """在 Admission UoW 的同一一致性视图中读取冲突。"""

    def get_admission_conflict(
        self,
        business_ref: TaskBusinessRef,
    ) -> CallbackAdmissionConflict:
        ...


__all__ = ["CallbackAdmissionConflict", "CallbackAdmissionConflictPort"]
