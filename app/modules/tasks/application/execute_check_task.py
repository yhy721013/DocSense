"""公开 check-task 同步语义的应用入口。"""

from __future__ import annotations

from dataclasses import dataclass
import logging

from .check_task_request import CheckTaskRequest
from .check_status import CheckTaskStatusResult, CheckTaskStatusService


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecuteCheckTaskCommand:
    """完整预校验并按规范业务键去重后的同步检查命令。"""

    request: CheckTaskRequest
    requested_count: int
    duplicate_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.request, CheckTaskRequest):
            raise TypeError("request 必须是 CheckTaskRequest")
        if (
            isinstance(self.requested_count, bool)
            or not isinstance(self.requested_count, int)
            or self.requested_count < len(self.request.ordered_items)
        ):
            raise ValueError("requested_count 不能小于规范化后的唯一项数量")
        if (
            isinstance(self.duplicate_count, bool)
            or not isinstance(self.duplicate_count, int)
            or self.duplicate_count < 0
            or self.duplicate_count
            != self.requested_count - len(self.request.ordered_items)
        ):
            raise ValueError("duplicate_count 与请求数量不一致")


@dataclass(frozen=True)
class ExecuteCheckTaskResult:
    """同步检查结果，同时保留原始批量语义所需的计数。"""

    status: CheckTaskStatusResult
    requested_count: int
    duplicate_count: int

    @property
    def single_missing(self) -> bool:
        """只有原始请求确为单项时，缺失才映射既有 HTTP 404。"""

        return self.requested_count == 1 and self.status.single_missing

    @property
    def missing_count(self) -> int:
        return sum(1 for item in self.status.ordered_items if not item.found)

    @property
    def unique_count(self) -> int:
        return len(self.status.ordered_items)

    @property
    def replayed_count(self) -> int:
        return self.status.replayed_count


class ExecuteCheckTask:
    """保持“整批预校验 → 同步恢复 → 恢复后重读”的公开边界。"""

    def __init__(self, status_service: CheckTaskStatusService) -> None:
        if not isinstance(status_service, CheckTaskStatusService):
            raise TypeError("status_service 必须是 CheckTaskStatusService")
        self._status_service = status_service

    def execute(
        self,
        command: ExecuteCheckTaskCommand,
        *,
        trace_id: str,
    ) -> ExecuteCheckTaskResult:
        if not isinstance(command, ExecuteCheckTaskCommand):
            raise TypeError("command 必须是 ExecuteCheckTaskCommand")
        status = self._status_service.check(command.request, trace_id=trace_id)
        result = ExecuteCheckTaskResult(
            status=status,
            requested_count=command.requested_count,
            duplicate_count=command.duplicate_count,
        )
        logger.info(
            "任务检查与必要回调恢复已完成: business_type=%s "
            "requested_count=%d unique_count=%d missing_count=%d "
            "duplicate_item_count=%d callback_replayed_count=%d "
            "has_request_trace=%s",
            command.request.business_type,
            result.requested_count,
            result.unique_count,
            result.missing_count,
            result.duplicate_count,
            result.replayed_count,
            bool(trace_id),
        )
        return result


__all__ = [
    "ExecuteCheckTask",
    "ExecuteCheckTaskCommand",
    "ExecuteCheckTaskResult",
]
