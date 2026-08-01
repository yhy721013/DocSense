"""现有 ``LLMTaskService`` 到任务只读 Port 的兼容适配器。"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from app.modules.tasks.domain import TaskBusinessRef, TaskId, TaskSnapshot
from app.services.llm_service.task_service import LLMTaskService


logger = logging.getLogger(__name__)


class LegacyTaskReadAdapter:
    """把遗留字典快照转换为任务模块不可变类型。

    本类只做读取和边界清洗，不调用回调、不发布 Progress、不更新 SQLite。阶段 3
    引入正式 Repository 后，应用服务可直接替换该 Adapter，而无需修改 WebSocket
    路由或 Presenter。
    """

    def __init__(self, task_service: LLMTaskService) -> None:
        if not isinstance(task_service, LLMTaskService):
            raise TypeError("task_service 必须是 LLMTaskService")
        self._task_service = task_service

    def get_by_id(self, task_id: TaskId) -> TaskSnapshot | None:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        raw = self._task_service.get_task_by_execution_id(task_id.value)
        return self._to_snapshot(raw) if raw is not None else None

    def get_latest(self, business_ref: TaskBusinessRef) -> TaskSnapshot | None:
        if not isinstance(business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        raw = self._task_service.get_task(
            business_ref.business_type,
            business_ref.business_key,
        )
        return self._to_snapshot(raw) if raw is not None else None

    def get_latest_many(
        self,
        business_refs: tuple[TaskBusinessRef, ...],
    ) -> tuple[TaskSnapshot | None, ...]:
        refs = tuple(business_refs)
        if any(not isinstance(item, TaskBusinessRef) for item in refs):
            raise TypeError("business_refs 只能包含 TaskBusinessRef")
        # 遗留 Service 尚无保序且保留缺失位置的批量查询。兼容期逐项读取能够保持
        # Port 契约；阶段 3 的 MySQL Repository 应改为一次批量 SQL 后按输入重排。
        return tuple(self.get_latest(item) for item in refs)

    @staticmethod
    def _to_snapshot(raw: Mapping[str, object]) -> TaskSnapshot:
        """严格转换遗留行；公开状态暂不反向推导未来正式状态机。"""

        if not isinstance(raw, Mapping):
            raise TypeError("遗留任务快照必须是 Mapping")
        business_type = str(raw.get("business_type") or "").strip()
        business_key = str(raw.get("business_key") or "").strip()
        public_status = str(raw.get("status") or "").strip()
        execution_id = str(raw.get("execution_id") or "").strip()
        callback_status = str(raw.get("callback_status") or "").strip()
        created_at = str(raw.get("created_at") or "").strip()
        updated_at = str(raw.get("updated_at") or "").strip()

        # 兼容层只把旧状态保留为明确的内部标签，不在阶段 1B 提前发明跨业务统一
        # 状态机；阶段 2 会建立正式状态迁移和公开状态映射。
        execution_state = f"legacy_status:{public_status}"
        snapshot = TaskSnapshot(
            task_id=TaskId(execution_id),
            task_type=f"{business_type}_task",
            business_ref=TaskBusinessRef(business_type, business_key),
            execution_state=execution_state,
            public_status=public_status,
            progress=raw.get("progress"),
            message=str(raw.get("message") or ""),
            callback_status=callback_status,
            created_at=created_at,
            updated_at=updated_at,
            callback_attempts=raw.get("callback_attempts", 0),
        )
        logger.debug(
            "已转换遗留任务只读快照: business_type=%s callback_status=%s "
            "has_task_identity=%s",
            snapshot.business_ref.business_type,
            snapshot.callback_status,
            bool(snapshot.task_id.value),
        )
        return snapshot


__all__ = ["LegacyTaskReadAdapter"]
