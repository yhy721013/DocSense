"""迁移波次期间按业务类型路由新旧 Task 只读事实。"""

from __future__ import annotations

from app.modules.tasks.domain import TaskBusinessRef, TaskId, TaskSnapshot
from app.modules.tasks.ports import TaskReadPort


class RoutedTaskReadAdapter:
    """按组合根显式声明的业务集合路由 v2；尚未迁移的业务继续读取旧库。

    按 TaskId 无法预先知道业务类型，因此先查 v2，再回退旧库。按业务键读取时禁止在
    Adapter 内维护隐式默认集合：每个业务完成一次切换后，组合根必须同步更新显式集合，
    否则构造阶段就会暴露遗漏，避免公开 check-task/Progress 静默读到遗留数据库。
    """

    def __init__(
        self,
        *,
        v2_reader: TaskReadPort,
        legacy_reader: TaskReadPort,
        v2_business_types: frozenset[str],
    ) -> None:
        if not isinstance(v2_reader, TaskReadPort):
            raise TypeError("v2_reader 必须实现 TaskReadPort")
        if not isinstance(legacy_reader, TaskReadPort):
            raise TypeError("legacy_reader 必须实现 TaskReadPort")
        normalized = frozenset(item.strip() for item in v2_business_types)
        if not normalized or "" in normalized:
            raise ValueError("v2_business_types 必须包含非空业务类型")
        self._v2 = v2_reader
        self._legacy = legacy_reader
        self._v2_business_types = normalized

    def get_by_id(self, task_id: TaskId) -> TaskSnapshot | None:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        snapshot = self._v2.get_by_id(task_id)
        return snapshot if snapshot is not None else self._legacy.get_by_id(task_id)

    def get_latest(self, business_ref: TaskBusinessRef) -> TaskSnapshot | None:
        if not isinstance(business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        reader = (
            self._v2
            if business_ref.business_type in self._v2_business_types
            else self._legacy
        )
        return reader.get_latest(business_ref)

    def get_latest_many(
        self,
        business_refs: tuple[TaskBusinessRef, ...],
    ) -> tuple[TaskSnapshot | None, ...]:
        refs = tuple(business_refs)
        if any(not isinstance(item, TaskBusinessRef) for item in refs):
            raise TypeError("business_refs 只能包含 TaskBusinessRef")
        # check-task 的单个请求只含一种业务类型；逐项路由仍明确保序，并兼容内部混合调用。
        return tuple(self.get_latest(item) for item in refs)


__all__ = ["RoutedTaskReadAdapter"]
