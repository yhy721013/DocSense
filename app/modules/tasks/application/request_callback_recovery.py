"""check-task 可靠回调恢复命令的应用编排。

本用例只读取任务事实并原子登记恢复命令，不执行 Callback HTTP，也不等待 RabbitMQ、
Worker 或甲方回调处理完成。只有 Command Port 的共享数据库事务成功提交后，本方法才
会返回结果；任何读取、校验或持久化异常都会向上传播，避免 Web 层误报 HTTP 200。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.modules.tasks.application.check_task_request import CheckTaskRequest
from app.modules.tasks.domain.models import TaskLookupItem, TaskSnapshot
from app.modules.tasks.ports.callback_recovery_commands import (
    CALLBACK_RECOVERY_TRIGGER_CHECK_TASK,
    CallbackRecoveryCommand,
    CallbackRecoveryCommandOutcome,
    CallbackRecoveryCommandPort,
    CallbackRecoveryCommandResult,
)
from app.modules.tasks.ports.task_read import TaskReadPort


logger = logging.getLogger(__name__)


def _trace_text(value: object, *, name: str) -> str:
    """校验内部追踪字段，防止把任意请求对象隐式写入日志或命令。"""

    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    return value.strip()


@dataclass(frozen=True)
class RequestCallbackRecoveryItemResult:
    """一个请求位置对应的读取和可靠登记结果。"""

    lookup: TaskLookupItem
    snapshot: TaskSnapshot | None
    command: CallbackRecoveryCommandResult | None

    def __post_init__(self) -> None:
        if not isinstance(self.lookup, TaskLookupItem):
            raise TypeError("lookup 必须是 TaskLookupItem")
        if self.snapshot is None:
            if self.command is not None:
                raise ValueError("缺失任务不得包含恢复命令结果")
            return
        if not isinstance(self.snapshot, TaskSnapshot):
            raise TypeError("snapshot 必须是 TaskSnapshot 或 None")
        if not isinstance(self.command, CallbackRecoveryCommandResult):
            raise TypeError("存在任务必须包含 CallbackRecoveryCommandResult")
        if self.snapshot.business_ref != self.lookup.business_ref:
            raise ValueError("任务快照与请求业务键不一致")
        if self.command.business_ref != self.lookup.business_ref:
            raise ValueError("命令结果与请求业务键不一致")
        if self.command.expected_task_id != self.snapshot.task_id:
            raise ValueError("命令结果与读取到的 expected TaskId 不一致")

    @property
    def found(self) -> bool:
        """表示该请求位置在初始 Task Read 中是否命中。"""

        return self.snapshot is not None


@dataclass(frozen=True)
class RequestCallbackRecoveryResult:
    """保持原始 ``params`` 顺序的框架无关可靠登记结果。"""

    ordered_items: tuple[RequestCallbackRecoveryItemResult, ...]

    def __post_init__(self) -> None:
        items = tuple(self.ordered_items)
        if not items:
            raise ValueError("ordered_items 不能为空")
        if any(
            not isinstance(item, RequestCallbackRecoveryItemResult)
            for item in items
        ):
            raise TypeError(
                "ordered_items 只能包含 RequestCallbackRecoveryItemResult"
            )
        if len({item.lookup.business_ref.business_type for item in items}) != 1:
            raise ValueError("同一结果批次只能包含一种 business_type")
        object.__setattr__(self, "ordered_items", items)

    @property
    def is_single(self) -> bool:
        return len(self.ordered_items) == 1

    @property
    def single_missing(self) -> bool:
        """供 Presenter 保持既有单项 404；批量缺失仍走空成功响应。"""

        return self.is_single and not self.ordered_items[0].found

    def count_outcome(self, outcome: CallbackRecoveryCommandOutcome) -> int:
        """统计一种内部登记结果，供日志和指标使用。"""

        if not isinstance(outcome, CallbackRecoveryCommandOutcome):
            raise TypeError("outcome 必须是 CallbackRecoveryCommandOutcome")
        return sum(
            1
            for item in self.ordered_items
            if item.command is not None and item.command.outcome is outcome
        )

    @property
    def missing_count(self) -> int:
        return sum(1 for item in self.ordered_items if not item.found)


class CallbackRecoveryTaskReadContractError(RuntimeError):
    """Task Read Adapter 违反批量长度、类型或业务键契约。"""


class CallbackRecoveryCommandContractError(RuntimeError):
    """Command Adapter 违反批量长度、顺序、类型或身份契约。"""


class RequestCallbackRecoveryService:
    """编排“有序读取 → 单次批量原子登记”，不触碰外部回调。"""

    def __init__(
        self,
        *,
        task_reader: TaskReadPort,
        command_port: CallbackRecoveryCommandPort,
    ) -> None:
        self._task_reader = task_reader
        self._command_port = command_port

    def request_recovery(
        self,
        request: CheckTaskRequest,
        *,
        trace_id: str = "",
        correlation_id: str = "",
    ) -> RequestCallbackRecoveryResult:
        """为已命中的任务原子创建或复用显式恢复命令。

        Task Read 用于保持当前单项/批量缺失语义；真正的 MySQL Adapter 仍必须在命令
        事务中复核最新 TaskId 与恢复资格。批量端口只调用一次，以便后续实现整批提交或
        整批回滚，不能在应用层逐项提交后再拼装一个看似成功的结果。
        """

        if not isinstance(request, CheckTaskRequest):
            raise TypeError("request 必须是 CheckTaskRequest")
        normalized_trace_id = _trace_text(trace_id, name="trace_id")
        normalized_correlation_id = _trace_text(
            correlation_id,
            name="correlation_id",
        )
        trace_label = normalized_trace_id or "-"
        correlation_label = normalized_correlation_id or "-"
        refs = tuple(item.business_ref for item in request.ordered_items)
        logger.info(
            "开始登记 check-task 可靠恢复命令: business_type=%s item_count=%s "
            "trace_id=%s correlation_id=%s",
            request.business_type,
            len(refs),
            trace_label,
            correlation_label,
        )

        try:
            raw_snapshots = self._task_reader.get_latest_many(refs)
        except Exception:
            logger.exception(
                "check-task 批量读取任务失败: item_count=%s trace_id=%s",
                len(refs),
                trace_label,
            )
            raise
        if not isinstance(raw_snapshots, tuple):
            raise CallbackRecoveryTaskReadContractError(
                "TaskReadPort.get_latest_many 必须返回 tuple"
            )
        snapshots = raw_snapshots
        if len(snapshots) != len(refs):
            raise CallbackRecoveryTaskReadContractError(
                "TaskReadPort.get_latest_many 返回长度与请求长度不一致"
            )

        commands: list[CallbackRecoveryCommand] = []
        for index, (lookup, snapshot) in enumerate(
            zip(request.ordered_items, snapshots)
        ):
            if snapshot is None:
                logger.info(
                    "check-task 任务未命中，不登记恢复命令: business_type=%s "
                    "business_key=%s index=%s trace_id=%s",
                    lookup.business_ref.business_type,
                    lookup.business_ref.business_key,
                    index,
                    trace_label,
                )
                continue
            if not isinstance(snapshot, TaskSnapshot):
                raise CallbackRecoveryTaskReadContractError(
                    "TaskReadPort.get_latest_many 只能返回 TaskSnapshot 或 None"
                )
            if snapshot.business_ref != lookup.business_ref:
                raise CallbackRecoveryTaskReadContractError(
                    "TaskReadPort.get_latest_many 返回了其他业务键的任务"
                )
            commands.append(
                CallbackRecoveryCommand(
                    expected_task_id=snapshot.task_id,
                    business_ref=snapshot.business_ref,
                    trigger=CALLBACK_RECOVERY_TRIGGER_CHECK_TASK,
                    trace_id=normalized_trace_id,
                    correlation_id=normalized_correlation_id,
                )
            )

        command_results = self._request_commands_atomically(
            tuple(commands),
            trace_label=trace_label,
        )

        ordered_results: list[RequestCallbackRecoveryItemResult] = []
        command_index = 0
        for index, (lookup, snapshot) in enumerate(
            zip(request.ordered_items, snapshots)
        ):
            if snapshot is None:
                ordered_results.append(
                    RequestCallbackRecoveryItemResult(
                        lookup=lookup,
                        snapshot=None,
                        command=None,
                    )
                )
                continue

            command_result = command_results[command_index]
            command_index += 1
            logger.info(
                "check-task 可靠恢复命令已登记或分类: business_type=%s "
                "business_key=%s task_id=%s index=%s outcome=%s trace_id=%s",
                lookup.business_ref.business_type,
                lookup.business_ref.business_key,
                snapshot.task_id,
                index,
                command_result.outcome.value,
                trace_label,
            )
            ordered_results.append(
                RequestCallbackRecoveryItemResult(
                    lookup=lookup,
                    snapshot=snapshot,
                    command=command_result,
                )
            )

        result = RequestCallbackRecoveryResult(tuple(ordered_results))
        logger.info(
            "check-task 可靠恢复命令批次完成: item_count=%s missing_count=%s "
            "created=%s already_active=%s not_needed=%s stale=%s trace_id=%s",
            len(result.ordered_items),
            result.missing_count,
            result.count_outcome(CallbackRecoveryCommandOutcome.CREATED),
            result.count_outcome(CallbackRecoveryCommandOutcome.ALREADY_ACTIVE),
            result.count_outcome(CallbackRecoveryCommandOutcome.NOT_NEEDED),
            result.count_outcome(CallbackRecoveryCommandOutcome.STALE),
            trace_label,
        )
        return result

    def _request_commands_atomically(
        self,
        commands: tuple[CallbackRecoveryCommand, ...],
        *,
        trace_label: str,
    ) -> tuple[CallbackRecoveryCommandResult, ...]:
        """调用一次批量端口并验证结果，空批次不创建伪事务。"""

        if not commands:
            return ()
        try:
            raw_results = self._command_port.request_many(commands)
        except Exception:
            # 不记录请求正文、回调正文或 URL；异常原样传播给未来 Web 错误边界。
            logger.exception(
                "check-task 可靠恢复命令事务失败: command_count=%s trace_id=%s",
                len(commands),
                trace_label,
            )
            raise
        if not isinstance(raw_results, tuple):
            raise CallbackRecoveryCommandContractError(
                "CallbackRecoveryCommandPort.request_many 必须返回 tuple"
            )
        results = raw_results
        if len(results) != len(commands):
            raise CallbackRecoveryCommandContractError(
                "CallbackRecoveryCommandPort.request_many 返回长度与命令长度不一致"
            )
        for command, result in zip(commands, results):
            if not isinstance(result, CallbackRecoveryCommandResult):
                raise CallbackRecoveryCommandContractError(
                    "CallbackRecoveryCommandPort 返回了非类型化结果"
                )
            if result.expected_task_id != command.expected_task_id:
                raise CallbackRecoveryCommandContractError(
                    "CallbackRecoveryCommandPort 未保持 expected TaskId 顺序"
                )
            if result.business_ref != command.business_ref:
                raise CallbackRecoveryCommandContractError(
                    "CallbackRecoveryCommandPort 未保持业务键顺序"
                )
        return results


__all__ = [
    "CallbackRecoveryCommandContractError",
    "CallbackRecoveryTaskReadContractError",
    "RequestCallbackRecoveryItemResult",
    "RequestCallbackRecoveryResult",
    "RequestCallbackRecoveryService",
]
