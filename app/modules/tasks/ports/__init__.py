"""任务模块抽象端口层：读取、可靠命令、同步恢复原型和 Progress 边界。"""

from .callback_recovery import (
    CallbackRecoveryPort,
    CallbackRecoveryResult,
    DELIVERY_OUTCOME_UNKNOWN,
)
from .callback_recovery_commands import (
    CALLBACK_RECOVERY_COMMAND_SCHEMA_VERSION,
    CALLBACK_RECOVERY_TRIGGER_CHECK_TASK,
    CallbackRecoveryCommand,
    CallbackRecoveryCommandOutcome,
    CallbackRecoveryCommandPort,
    CallbackRecoveryCommandResult,
)
from .progress import (
    ProgressSnapshotPort,
    ProgressSubscriber,
    ProgressSubscription,
    ProgressSubscriptionPort,
)
from .task_read import TaskReadPort

__all__ = [
    "CALLBACK_RECOVERY_COMMAND_SCHEMA_VERSION",
    "CALLBACK_RECOVERY_TRIGGER_CHECK_TASK",
    "CallbackRecoveryCommand",
    "CallbackRecoveryCommandOutcome",
    "CallbackRecoveryCommandPort",
    "CallbackRecoveryCommandResult",
    "CallbackRecoveryPort",
    "CallbackRecoveryResult",
    "DELIVERY_OUTCOME_UNKNOWN",
    "ProgressSnapshotPort",
    "ProgressSubscriber",
    "ProgressSubscription",
    "ProgressSubscriptionPort",
    "TaskReadPort",
]
