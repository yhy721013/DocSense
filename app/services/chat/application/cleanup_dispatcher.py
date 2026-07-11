"""内部资源清理的可替换调度边界。

当前实现同步执行清理回调，不能被描述为可靠队列。将来部署可靠调度系统后，
只需要在容器中替换本模块的 Dispatcher；删除服务、Blueprint 和 HTTP 契约均
不应感知队列客户端或投递协议。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar, runtime_checkable


_Result = TypeVar("_Result")


def _required_text(value: str, *, name: str) -> str:
    """规范化清理任务的内部标识，拒绝空任务进入调度器。"""
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} cannot be empty")
    return normalized


@dataclass(frozen=True)
class ChatCleanupDispatchCapabilities:
    """清理调度器的真实能力声明，用于避免误把同步实现当作可靠队列。"""

    single_instance_only: bool
    reliable_delivery: bool
    supports_delayed_retry: bool
    supports_external_workers: bool


INLINE_CHAT_CLEANUP_DISPATCH_CAPABILITIES = ChatCleanupDispatchCapabilities(
    single_instance_only=True,
    reliable_delivery=False,
    supports_delayed_retry=False,
    supports_external_workers=False,
)


@dataclass(frozen=True)
class ChatCleanupTask:
    """一次内部资源补偿或删除重试的稳定任务描述。"""

    chat_id: str
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "chat_id", _required_text(self.chat_id, name="chat_id"))
        object.__setattr__(self, "reason", _required_text(self.reason, name="reason"))


@runtime_checkable
class ChatCleanupDispatcher(Protocol):
    """将资源清理工作交给可替换执行边界的协议。

    ``execute`` 由当前同步实现直接调用。未来可靠适配器可将同一 ``task``
    持久化并交给 worker；在冻结的删除接口仍需同步返回结果前，它必须提供
    等价的等待/协调机制，而不能改变 API 的响应语义。
    """

    @property
    def capabilities(self) -> ChatCleanupDispatchCapabilities:
        """返回此调度器可验证的投递和执行能力。"""
        ...

    def dispatch(
        self,
        *,
        task: ChatCleanupTask,
        execute: Callable[[], _Result],
    ) -> _Result:
        """安排并执行一个清理任务，返回清理函数的原始结果。"""
        ...


class InlineChatCleanupDispatcher:
    """仅用于单实例的同步清理调度器，不提供可靠投递或后台重试。"""

    capabilities = INLINE_CHAT_CLEANUP_DISPATCH_CAPABILITIES

    def dispatch(
        self,
        *,
        task: ChatCleanupTask,
        execute: Callable[[], _Result],
    ) -> _Result:
        if not isinstance(task, ChatCleanupTask):
            raise TypeError("task must be ChatCleanupTask")
        if not callable(execute):
            raise TypeError("execute must be callable")
        return execute()


__all__ = [
    "ChatCleanupDispatchCapabilities",
    "ChatCleanupDispatcher",
    "ChatCleanupTask",
    "INLINE_CHAT_CLEANUP_DISPATCH_CAPABILITIES",
    "InlineChatCleanupDispatcher",
]
