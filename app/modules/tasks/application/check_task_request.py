"""check-task 用例共享的框架无关请求对象。

Web Adapter 必须先完成 JSON、``businessType``、``params`` 元素类型和业务键校验，
再把稳定的 ``TaskLookupItem`` 交给本模块。这里不接收 Flask ``request`` 或任意字典，
从而让历史同步检查原型与未来可靠恢复命令用例复用同一条输入契约。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.modules.tasks.domain.models import TaskLookupItem


@dataclass(frozen=True)
class CheckTaskRequest:
    """已经由 Web Adapter 完整校验的一次有序 check-task 请求。"""

    ordered_items: tuple[TaskLookupItem, ...]

    def __post_init__(self) -> None:
        items = tuple(self.ordered_items)
        if not items:
            raise ValueError("ordered_items 不能为空")
        if any(not isinstance(item, TaskLookupItem) for item in items):
            raise TypeError("ordered_items 只能包含 TaskLookupItem")
        business_types = {item.business_ref.business_type for item in items}
        if len(business_types) != 1:
            raise ValueError("同一次 check-task 请求只能包含一种 business_type")
        object.__setattr__(self, "ordered_items", items)

    @property
    def business_type(self) -> str:
        """返回本批请求唯一的业务类型。"""

        return self.ordered_items[0].business_ref.business_type


__all__ = ["CheckTaskRequest"]
