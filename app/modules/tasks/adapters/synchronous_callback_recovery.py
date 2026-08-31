"""三类现有同步 Callback 恢复用例到 Tasks Port 的组合适配器。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import logging

from app.modules.tasks.domain import (
    CALLBACK_OUTCOME_UNKNOWN,
    TaskBusinessRef,
    TaskId,
    TaskSnapshot,
)
from app.modules.tasks.ports import (
    CallbackRecoveryResult,
    DELIVERY_OUTCOME_UNKNOWN,
    TaskReadPort,
)


logger = logging.getLogger(__name__)


class SynchronousCallbackRecoveryRouterAdapter:
    """按持久 TaskId 路由到业务恢复用例，并从数据库事实生成统一结果。

    路由函数只接收规范化 ``TaskBusinessRef`` 和请求 trace；tasks 模块不导入
    Analysis、Report 或 Weaponry，避免控制面反向依赖具体业务实现。
    """

    def __init__(
        self,
        *,
        task_reader: TaskReadPort,
        routes: Mapping[str, Callable[[TaskId, TaskBusinessRef, str], bool]],
    ) -> None:
        if not isinstance(task_reader, TaskReadPort):
            raise TypeError("task_reader 必须实现 TaskReadPort")
        normalized_routes = dict(routes)
        if not normalized_routes or any(
            not isinstance(key, str) or not key.strip() or not callable(value)
            for key, value in normalized_routes.items()
        ):
            raise ValueError("routes 必须是非空业务类型到恢复函数的映射")
        self._task_reader = task_reader
        self._routes = normalized_routes

    def recover_if_needed(self, task_id: TaskId) -> CallbackRecoveryResult:
        """兼容基础 Port；无请求上下文的内部调用使用空 trace。"""

        return self.recover_if_needed_with_context(task_id, trace_id="")

    def recover_if_needed_with_context(
        self,
        task_id: TaskId,
        *,
        trace_id: str,
    ) -> CallbackRecoveryResult:
        """兼容只传 TaskId 的调用；生产查询优先传入已经读取的快照。"""

        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        before = self._task_reader.get_by_id(task_id)
        if before is None:
            raise RuntimeError("同步回调恢复前任务不存在")
        return self.recover_snapshot_with_context(before, trace_id=trace_id)

    def recover_snapshot_with_context(
        self,
        snapshot: TaskSnapshot,
        *,
        trace_id: str,
    ) -> CallbackRecoveryResult:
        """恢复指定快照，并返回同一 TaskId 的最新持久化事实。

        ``expected TaskId`` 会继续传到具体业务恢复器，防止初次读取后同一业务键又
        受理了新 execution 时，把新任务的回调结果错误归到旧任务。投递是否真正发生
        依据持久化 attempt 增量判断，而不是把“严格 2xx”错误等同于“尝试过”。
        """

        if not isinstance(snapshot, TaskSnapshot):
            raise TypeError("snapshot 必须是 TaskSnapshot")
        if not isinstance(trace_id, str):
            raise TypeError("trace_id 必须是 str")
        before = snapshot
        route = self._routes.get(before.business_ref.business_type)
        if route is None:
            raise RuntimeError("未装配该业务类型的同步回调恢复链")

        replayed = route(before.task_id, before.business_ref, trace_id)
        if not isinstance(replayed, bool):
            raise TypeError("业务同步回调恢复用例必须返回 bool")
        after = self._task_reader.get_by_id(before.task_id)
        if (
            after is None
            or after.task_id != before.task_id
            or after.business_ref != before.business_ref
        ):
            raise RuntimeError("同步回调恢复后无法读取同一任务")
        if after.callback_attempts < before.callback_attempts:
            raise RuntimeError("同步回调恢复后 callback_attempts 发生倒退")
        attempted = after.callback_attempts > before.callback_attempts
        if replayed and not attempted:
            raise RuntimeError("同步回调声称成功但持久化 attempt 未增加")
        delivery_outcome = (
            DELIVERY_OUTCOME_UNKNOWN
            if attempted and after.callback_status == CALLBACK_OUTCOME_UNKNOWN
            else ""
        )
        logger.info(
            "同步回调恢复路由完成: business_type=%s attempted=%s "
            "replayed=%s callback_status=%s has_request_trace=%s",
            before.business_ref.business_type,
            attempted,
            replayed,
            after.callback_status,
            bool(trace_id.strip()),
        )
        return CallbackRecoveryResult(
            attempted=attempted,
            replayed=replayed,
            final_status=after.callback_status,
            delivery_outcome=delivery_outcome,
            current_snapshot=after,
        )


__all__ = ["SynchronousCallbackRecoveryRouterAdapter"]
