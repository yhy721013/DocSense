"""任务状态检查与显式回调恢复的应用编排。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.modules.tasks.application.check_task_request import CheckTaskRequest
from app.modules.tasks.domain.models import TaskLookupItem, TaskSnapshot
from app.modules.tasks.ports.callback_recovery import (
    CallbackRecoveryPort,
    CallbackRecoveryResult,
)
from app.modules.tasks.ports.task_read import TaskReadPort


logger = logging.getLogger(__name__)


# 旧同步原型的公开内部名称继续保留，避免阶段 1A 测试和后续审查证据失效。
# 两个名称指向同一个不可变类型，不存在双份校验规则或 DTO 转换。
CheckTaskStatusRequest = CheckTaskRequest


@dataclass(frozen=True)
class TaskCheckItemResult:
    """一个请求位置的内部检查结果。"""

    lookup: TaskLookupItem
    initial_snapshot: TaskSnapshot | None
    current_snapshot: TaskSnapshot | None
    recovery: CallbackRecoveryResult | None

    def __post_init__(self) -> None:
        if not isinstance(self.lookup, TaskLookupItem):
            raise TypeError("lookup 必须是 TaskLookupItem")
        if self.initial_snapshot is None:
            if self.current_snapshot is not None or self.recovery is not None:
                raise ValueError("缺失任务不得包含当前快照或恢复结果")
            return
        if not isinstance(self.initial_snapshot, TaskSnapshot):
            raise TypeError("initial_snapshot 必须是 TaskSnapshot 或 None")
        if not isinstance(self.current_snapshot, TaskSnapshot):
            raise TypeError("存在任务必须包含 current_snapshot")
        if not isinstance(self.recovery, CallbackRecoveryResult):
            raise TypeError("存在任务必须包含 CallbackRecoveryResult")
        if self.initial_snapshot.task_id != self.current_snapshot.task_id:
            raise ValueError("回调恢复前后必须属于同一个 TaskId")

    @property
    def found(self) -> bool:
        """标识该请求位置是否命中任务。"""

        return self.initial_snapshot is not None


@dataclass(frozen=True)
class CheckTaskStatusResult:
    """保持原请求顺序的框架无关检查结果。"""

    ordered_items: tuple[TaskCheckItemResult, ...]

    def __post_init__(self) -> None:
        items = tuple(self.ordered_items)
        if not items:
            raise ValueError("ordered_items 不能为空")
        if any(not isinstance(item, TaskCheckItemResult) for item in items):
            raise TypeError("ordered_items 只能包含 TaskCheckItemResult")
        object.__setattr__(self, "ordered_items", items)

    @property
    def is_single(self) -> bool:
        return len(self.ordered_items) == 1

    @property
    def single_missing(self) -> bool:
        """供 Presenter 映射既有单项 404；批量缺失不触发该标记。"""

        return self.is_single and not self.ordered_items[0].found

    @property
    def replayed_count(self) -> int:
        """返回本次已确认成功补发的项数，仅供内部日志和审计。"""

        return sum(
            1
            for item in self.ordered_items
            if item.recovery is not None and item.recovery.replayed
        )


class TaskReadContractError(RuntimeError):
    """Task Read Adapter 返回值违反顺序、长度或业务键契约。"""


class CallbackRecoveryContractError(RuntimeError):
    """Callback Recovery Adapter 返回了无效内部结果。"""


class CallbackRecoveryConsistencyError(RuntimeError):
    """回调恢复返回值与按同一 TaskId 重读到的持久化状态不一致。"""


class TaskSnapshotUnavailableError(RuntimeError):
    """已命中的 TaskId 在回调恢复后无法再次读取。"""


class CheckTaskStatusService:
    """显式编排“读取 → 回调恢复 → 按同一 TaskId 重读”。"""

    def __init__(
        self,
        *,
        task_reader: TaskReadPort,
        callback_recovery: CallbackRecoveryPort,
    ) -> None:
        self._task_reader = task_reader
        self._callback_recovery = callback_recovery

    def check(
        self,
        request: CheckTaskStatusRequest,
        *,
        trace_id: str = "",
    ) -> CheckTaskStatusResult:
        """检查一组有序任务并保留单项/批量缺失语义。

        本方法不生成 HTTP Response。端口抛出的恢复异常会在记录上下文后原样传播，
        避免网络或持久化失败被悄悄转换成成功空响应。
        """

        if not isinstance(request, CheckTaskStatusRequest):
            raise TypeError("request 必须是 CheckTaskStatusRequest")
        trace_label = str(trace_id or "").strip() or "-"
        refs = tuple(item.business_ref for item in request.ordered_items)
        logger.info(
            "开始检查任务状态: business_type=%s item_count=%s trace_id=%s",
            request.business_type,
            len(refs),
            trace_label,
        )
        snapshots = tuple(self._task_reader.get_latest_many(refs))
        if len(snapshots) != len(refs):
            raise TaskReadContractError(
                "TaskReadPort.get_latest_many 返回长度与请求长度不一致"
            )

        checked_items: list[TaskCheckItemResult] = []
        for index, (lookup, snapshot) in enumerate(
            zip(request.ordered_items, snapshots)
        ):
            if snapshot is None:
                logger.info(
                    "任务状态检查未命中: business_type=%s business_key=%s "
                    "index=%s trace_id=%s",
                    lookup.business_ref.business_type,
                    lookup.business_ref.business_key,
                    index,
                    trace_label,
                )
                checked_items.append(
                    TaskCheckItemResult(
                        lookup=lookup,
                        initial_snapshot=None,
                        current_snapshot=None,
                        recovery=None,
                    )
                )
                continue

            self._validate_latest_snapshot(snapshot, lookup, index=index)
            logger.debug(
                "开始检查任务回调恢复: business_type=%s business_key=%s "
                "task_id=%s callback_before=%s trace_id=%s",
                lookup.business_ref.business_type,
                lookup.business_ref.business_key,
                snapshot.task_id,
                snapshot.callback_status,
                trace_label,
            )
            try:
                recovery = self._callback_recovery.recover_if_needed(
                    snapshot.task_id
                )
            except Exception:
                logger.exception(
                    "任务回调恢复失败: business_type=%s business_key=%s "
                    "task_id=%s trace_id=%s",
                    lookup.business_ref.business_type,
                    lookup.business_ref.business_key,
                    snapshot.task_id,
                    trace_label,
                )
                raise

            if not isinstance(recovery, CallbackRecoveryResult):
                raise CallbackRecoveryContractError(
                    "CallbackRecoveryPort.recover_if_needed 返回类型无效"
                )

            # 无条件按原 TaskId 重读。即使 Adapter 声称“未尝试且状态未变化”，也不能
            # 直接相信内存返回值，否则错误实现或并发写入仍可能造成“返回成功、库中失败”
            # 等误报。这里绝不再次按业务键读取，避免切换到同一业务键的较新执行。
            current_snapshot = self._task_reader.get_by_id(snapshot.task_id)
            if current_snapshot is None:
                raise TaskSnapshotUnavailableError(
                    f"回调恢复后无法读取 TaskId={snapshot.task_id}"
                )
            self._validate_reread_snapshot(
                current_snapshot,
                expected=snapshot,
            )

            self._validate_recovery_consistency(
                current_snapshot,
                recovery=recovery,
            )

            checked_items.append(
                TaskCheckItemResult(
                    lookup=lookup,
                    initial_snapshot=snapshot,
                    current_snapshot=current_snapshot,
                    recovery=recovery,
                )
            )
            logger.info(
                "任务状态检查完成: business_type=%s business_key=%s "
                "task_id=%s found=true callback_before=%s callback_after=%s "
                "attempted=%s replayed=%s trace_id=%s",
                lookup.business_ref.business_type,
                lookup.business_ref.business_key,
                snapshot.task_id,
                snapshot.callback_status,
                current_snapshot.callback_status,
                recovery.attempted,
                recovery.replayed,
                trace_label,
            )

        result = CheckTaskStatusResult(tuple(checked_items))
        logger.info(
            "任务状态检查批次完成: business_type=%s item_count=%s "
            "replayed_count=%s trace_id=%s",
            request.business_type,
            len(result.ordered_items),
            result.replayed_count,
            trace_label,
        )
        return result

    @staticmethod
    def _validate_latest_snapshot(
        snapshot: TaskSnapshot,
        lookup: TaskLookupItem,
        *,
        index: int,
    ) -> None:
        if not isinstance(snapshot, TaskSnapshot):
            raise TaskReadContractError(
                f"TaskReadPort 在 index={index} 返回了非 TaskSnapshot"
            )
        if snapshot.business_ref != lookup.business_ref:
            raise TaskReadContractError(
                f"TaskReadPort 在 index={index} 返回了错误业务键"
            )

    @staticmethod
    def _validate_reread_snapshot(
        snapshot: TaskSnapshot,
        *,
        expected: TaskSnapshot,
    ) -> None:
        if not isinstance(snapshot, TaskSnapshot):
            raise TaskReadContractError("TaskReadPort.get_by_id 返回了非 TaskSnapshot")
        if snapshot.task_id != expected.task_id:
            raise TaskReadContractError("TaskReadPort.get_by_id 返回了错误 TaskId")
        if snapshot.business_ref != expected.business_ref:
            raise TaskReadContractError("TaskReadPort.get_by_id 返回了错误业务键")

    @staticmethod
    def _validate_recovery_consistency(
        snapshot: TaskSnapshot,
        *,
        recovery: CallbackRecoveryResult,
    ) -> None:
        """拒绝把“内存结果成功、持久化仍失败”误报为恢复成功。

        Callback Recovery Adapter 必须在返回前完成状态持久化。应用服务只相信按原
        TaskId 读取到的事实；二者不一致时抛出显式内部一致性错误，由 Web Adapter
        映射为失败响应并保留审计日志，绝不继续统计 replayed_count。
        """

        if snapshot.callback_status == recovery.final_status:
            return
        logger.error(
            "回调恢复结果与持久化状态不一致: task_id=%s "
            "reported_status=%s persisted_status=%s attempted=%s replayed=%s",
            snapshot.task_id,
            recovery.final_status,
            snapshot.callback_status,
            recovery.attempted,
            recovery.replayed,
        )
        raise CallbackRecoveryConsistencyError(
            "Callback Recovery Adapter 返回状态与持久化状态不一致: "
            f"TaskId={snapshot.task_id} reported={recovery.final_status} "
            f"persisted={snapshot.callback_status}"
        )


__all__ = [
    "CallbackRecoveryConsistencyError",
    "CallbackRecoveryContractError",
    "CheckTaskStatusRequest",
    "CheckTaskStatusResult",
    "CheckTaskStatusService",
    "TaskCheckItemResult",
    "TaskReadContractError",
    "TaskSnapshotUnavailableError",
]
