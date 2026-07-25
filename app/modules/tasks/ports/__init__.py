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
    GuardedProgressPublisherPort,
    ProgressPublication,
    ProgressPublisherPort,
    ProgressSnapshotPort,
    ProgressSubscriber,
    ProgressSubscription,
    ProgressSubscriptionPort,
)
from .task_commands import (
    ExpectedProgressUpdate,
    ExpectedTaskCompletion,
    TaskClaimOutcome,
    TaskClaimResult,
    TaskCommandPort,
    TaskSubmissionCommand,
    TaskSubmissionOutcome,
    TaskSubmissionResult,
)
from .task_read import TaskReadPort
from .task_queue import TaskQueueInspectionPort, TaskQueueSnapshot
from .runtime import ProcessSingletonGuardPort, TaskExecutionPermitPort

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
    "ExpectedProgressUpdate",
    "ExpectedTaskCompletion",
    "GuardedProgressPublisherPort",
    "ProgressPublication",
    "ProgressPublisherPort",
    "ProgressSnapshotPort",
    "ProgressSubscriber",
    "ProgressSubscription",
    "ProgressSubscriptionPort",
    "ProcessSingletonGuardPort",
    "TaskReadPort",
    "TaskClaimOutcome",
    "TaskClaimResult",
    "TaskCommandPort",
    "TaskQueueInspectionPort",
    "TaskQueueSnapshot",
    "TaskExecutionPermitPort",
    "TaskSubmissionCommand",
    "TaskSubmissionOutcome",
    "TaskSubmissionResult",
]
