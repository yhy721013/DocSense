"""武器谱严格 Fake 共用的线程安全调用轨迹。"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock


@dataclass(frozen=True)
class WeaponryInvocation:
    """不含 Prompt、正文、URL 或 Token 的测试调用摘要。"""

    operation: str
    task_id: str = ""
    call_id: str = ""


class WeaponryInvocationRecorder:
    """在并发测试中保持确定顺序的内存轨迹记录器。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._events: list[WeaponryInvocation] = []

    def record(self, operation: str, *, task_id: str = "", call_id: str = "") -> None:
        if not isinstance(operation, str) or not operation.strip():
            raise ValueError("operation 必须是非空 str")
        if not isinstance(task_id, str) or not isinstance(call_id, str):
            raise TypeError("task_id/call_id 必须是 str")
        with self._lock:
            self._events.append(
                WeaponryInvocation(
                    operation=operation.strip(),
                    task_id=task_id.strip(),
                    call_id=call_id.strip(),
                )
            )

    @property
    def events(self) -> tuple[WeaponryInvocation, ...]:
        with self._lock:
            return tuple(self._events)

    def clear(self) -> None:
        """清空当前装配的轨迹，供测试分隔 Submit 与 Run 两个观察窗口。"""

        with self._lock:
            self._events.clear()

    def contains(
        self,
        operation: str,
        *,
        task_id: str = "",
        call_id: str = "",
    ) -> bool:
        """判断指定无敏感摘要是否已经成功记录。"""

        with self._lock:
            return any(
                event.operation == operation
                and (not task_id or event.task_id == task_id)
                and (not call_id or event.call_id == call_id)
                for event in self._events
            )


__all__ = ["WeaponryInvocation", "WeaponryInvocationRecorder"]
