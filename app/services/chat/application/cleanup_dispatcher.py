"""文件对话持久化清理任务的调度边界。

任务会在调用此边界前完成持久化。因此调度器不会接收捕获的回调或请求级对象；
未来外部工作进程可以仅凭 ``job_id`` 重新加载同一任务。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.services.chat.domain.models import ChatCleanupJob


@dataclass(frozen=True)
class ChatCleanupDispatchCapabilities:
    """向组合根暴露的正向投递能力声明。"""

    supports_single_instance: bool
    supports_external_workers: bool
    reliable_delivery: bool
    supports_delayed_retry: bool
    supports_synchronous_completion: bool


INLINE_CHAT_CLEANUP_DISPATCH_CAPABILITIES = ChatCleanupDispatchCapabilities(
    supports_single_instance=True,
    supports_external_workers=False,
    reliable_delivery=False,
    supports_delayed_retry=False,
    supports_synchronous_completion=True,
)


@runtime_checkable
class ChatCleanupDispatcher(Protocol):
    """通知调度器：一条已持久化的清理任务已可执行。"""

    @property
    def capabilities(self) -> ChatCleanupDispatchCapabilities:
        """返回该适配器可验证的投递能力。"""
        ...

    def dispatch(self, *, job: ChatCleanupJob) -> ChatCleanupJob:
        """调度持久化任务并返回其当前持久化状态。"""
        ...


class InlineChatCleanupDispatcher:
    """当前同步模式下的通知适配器。

    适配器持有组合根在装配时选定的应用级执行器。``dispatch`` 只向执行器
    传递持久化任务 ID，因此它既不是伪造的内存队列，也不是请求专属回调注册表。
    现有删除接口需要同步完成，因为其响应必须如实说明远端清理是否已经完成。
    """

    capabilities = INLINE_CHAT_CLEANUP_DISPATCH_CAPABILITIES

    def __init__(
        self,
        *,
        execute: Callable[..., ChatCleanupJob],
    ) -> None:
        if not callable(execute):
            raise TypeError("execute must be callable")
        self._execute = execute

    def dispatch(self, *, job: ChatCleanupJob) -> ChatCleanupJob:
        if not isinstance(job, ChatCleanupJob):
            raise TypeError("job must be ChatCleanupJob")
        result = self._execute(job_id=job.job_id)
        if not isinstance(result, ChatCleanupJob):
            raise TypeError("cleanup executor must return ChatCleanupJob")
        return result


__all__ = [
    "ChatCleanupDispatchCapabilities",
    "ChatCleanupDispatcher",
    "INLINE_CHAT_CLEANUP_DISPATCH_CAPABILITIES",
    "InlineChatCleanupDispatcher",
]
