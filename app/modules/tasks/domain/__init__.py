"""任务模块领域层：不可变任务身份、业务引用和进度快照。"""

from .models import (
    CALLBACK_FAILED,
    CALLBACK_PENDING,
    CALLBACK_SKIPPED,
    CALLBACK_STATUSES,
    CALLBACK_SUCCESS,
    ProgressKey,
    ProgressSnapshot,
    ProgressSubscriptionRequest,
    TaskBusinessRef,
    TaskId,
    TaskLookupItem,
    TaskSnapshot,
)

__all__ = [
    "CALLBACK_FAILED",
    "CALLBACK_PENDING",
    "CALLBACK_SKIPPED",
    "CALLBACK_STATUSES",
    "CALLBACK_SUCCESS",
    "ProgressKey",
    "ProgressSnapshot",
    "ProgressSubscriptionRequest",
    "TaskBusinessRef",
    "TaskId",
    "TaskLookupItem",
    "TaskSnapshot",
]
