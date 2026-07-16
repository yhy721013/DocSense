"""任务模块抽象端口层：读取、回调恢复和 Progress 协作边界。"""

from .callback_recovery import (
    CallbackRecoveryPort,
    CallbackRecoveryResult,
    DELIVERY_OUTCOME_UNKNOWN,
)
from .progress import (
    ProgressSnapshotPort,
    ProgressSubscriber,
    ProgressSubscription,
    ProgressSubscriptionPort,
)
from .task_read import TaskReadPort

__all__ = [
    "CallbackRecoveryPort",
    "CallbackRecoveryResult",
    "DELIVERY_OUTCOME_UNKNOWN",
    "ProgressSnapshotPort",
    "ProgressSubscriber",
    "ProgressSubscription",
    "ProgressSubscriptionPort",
    "TaskReadPort",
]
