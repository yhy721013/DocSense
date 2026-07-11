"""供应商与存储产品无关的文件对话运行租约契约。

这里定义的是应用层对“谁有权继续执行某个 run”的表达，而不是某一种
数据库锁、消息队列或分布式锁的实现。当前 SQLite 适配器只能提供单应用
实例内的运行权校验，因此不会伪造 lease token 或 fencing token；未来共享
持久化/worker 适配器必须在本模块的契约下提供真实的条件领取、续租和围栏。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.services.chat.domain.events import ChatStreamEvent
from app.services.chat.domain.models import ChatRun


def _required_text(value: str, *, name: str) -> str:
    """规范化并校验租约内部使用的必填标识，避免空值进入协调逻辑。"""
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} cannot be empty")
    return normalized


@dataclass(frozen=True)
class ChatRunLeaseCapabilities:
    """一个运行协调适配器真实具备的租约能力。

    能力对象用于启动装配和测试门禁，而不是业务分支的替代品。特别是，
    ``supports_single_instance`` 与 ``supports_shared_instances`` 分别表达
    适配器已验证的部署能力；未来更强的适配器无需因为否定式字段而被拒绝。
    """

    supports_single_instance: bool
    supports_shared_instances: bool
    supports_conditional_claim: bool
    supports_lease_renewal: bool
    supports_fencing: bool
    requires_execution_lease_for_mutations: bool


SINGLE_INSTANCE_CHAT_RUN_LEASE_CAPABILITIES = ChatRunLeaseCapabilities(
    supports_single_instance=True,
    supports_shared_instances=False,
    supports_conditional_claim=True,
    supports_lease_renewal=True,
    supports_fencing=False,
    requires_execution_lease_for_mutations=False,
)


@dataclass(frozen=True)
class ChatRunLease:
    """仅在服务端内部流转的一次 run 执行所有权证明。

    ``lease_token`` 和 ``fencing_token`` 预留给未来共享持久化与 worker。
    当前单实例实现会同时留空两者，并且通过能力对象明确声明其不具备跨实例
    围栏保障。二者必须同时存在，防止调用方误把半个租约当成可用的分布式锁。
    """

    run_id: str
    chat_id: str
    owner_instance_id: str
    lease_token: str = ""
    fencing_token: int | None = None
    expires_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _required_text(self.run_id, name="run_id"))
        object.__setattr__(self, "chat_id", _required_text(self.chat_id, name="chat_id"))
        object.__setattr__(
            self,
            "owner_instance_id",
            _required_text(self.owner_instance_id, name="owner_instance_id"),
        )
        token = str(self.lease_token or "").strip()
        expires_at = str(self.expires_at or "").strip()
        fencing_token = self.fencing_token
        if isinstance(fencing_token, bool):
            raise TypeError("fencing_token must be int or None")
        if fencing_token is not None and (
            not isinstance(fencing_token, int) or fencing_token < 1
        ):
            raise ValueError("fencing_token must be a positive integer or None")
        if bool(token) != (fencing_token is not None):
            raise ValueError(
                "lease_token and fencing_token must either both be present or both be absent"
            )
        if expires_at and not token:
            raise ValueError("expires_at requires a lease_token")
        object.__setattr__(self, "lease_token", token)
        object.__setattr__(self, "expires_at", expires_at)

    @property
    def has_fencing(self) -> bool:
        """返回该租约是否可用于跨实例的 token/fencing 条件写入。"""
        return bool(self.lease_token and self.fencing_token is not None)


class ChatRunLeaseLostError(RuntimeError):
    """执行者提交心跳或终态时已不再拥有对应 run 的运行权。"""

    def __init__(self, *, run_id: str, reason: str) -> None:
        self.run_id = _required_text(run_id, name="run_id")
        self.reason = _required_text(reason, name="reason")
        super().__init__(f"chat run execution lease is no longer valid: {self.reason}")


@runtime_checkable
class ChatRunCoordinator(Protocol):
    """协调 run 生命周期及内部执行租约的产品无关边界。

    未来 worker 只能经由携带 ``ChatRunLease`` 的续租和终态提交接口更新
    run；具体实现必须以 lease/fencing token 作为条件更新的一部分。当前
    SQLite 单实例实现保留相同签名，但其能力对象会明确标记无 fencing。
    """

    @property
    def lease_capabilities(self) -> ChatRunLeaseCapabilities:
        """返回此协调器实际可提供的租约能力。"""
        ...

    def try_acquire_chat_run(
        self,
        *,
        chat_id: str,
        run_id: str | None = None,
        user_message: str | None = None,
        user_files: tuple[tuple[str, str], ...] = (),
        input_documents: tuple[tuple[str, str, str, str], ...] = (),
    ) -> ChatRun:
        """原子受理一个 chat run，并保持同一会话的活跃 run 互斥。"""
        ...

    def begin_chat_deletion(self, *, chat_id: str) -> None:
        """原子切换会话到删除中的准入状态。"""
        ...

    def issue_execution_lease(self, *, run_id: str) -> ChatRunLease:
        """为已受理的 run 生成内部执行所有权证明。"""
        ...

    def validate_execution_lease(self, *, lease: ChatRunLease) -> ChatRun:
        """校验执行者尚可继续推进该 run。"""
        ...

    def heartbeat_execution_lease(self, *, lease: ChatRunLease) -> ChatRun:
        """使用执行租约续期；未来实现必须执行 token/fencing 条件更新。"""
        ...

    def complete_run_with_execution_lease(
        self,
        *,
        lease: ChatRunLease,
        user_message_id: str,
        assistant_message_id: str,
        assistant_content: str,
        terminal_event: ChatStreamEvent | None = None,
    ) -> ChatRun:
        """使用执行租约原子提交成功终态及本地消息。"""
        ...

    def fail_run_with_execution_lease(
        self,
        *,
        lease: ChatRunLease,
        user_message_id: str,
        error_message: str,
        terminal_event: ChatStreamEvent | None = None,
    ) -> ChatRun:
        """使用执行租约原子提交失败终态及本地 user 消息。"""
        ...

    def abort_run_with_execution_lease(
        self,
        *,
        lease: ChatRunLease,
        user_message_id: str,
        terminal_event: ChatStreamEvent | None = None,
    ) -> ChatRun:
        """使用执行租约原子提交中断终态及本地 user 消息。"""
        ...

    def complete_run(self, run_id: str) -> ChatRun:
        """收敛未进入执行器的成功 run，供兼容恢复路径使用。"""
        ...

    def fail_run(self, run_id: str, *, error_message: str) -> ChatRun:
        """收敛未进入执行器的失败 run，供受理失败路径使用。"""
        ...

    def discard_unstarted_run(
        self,
        *,
        run_id: str,
        error_message: str,
    ) -> ChatRun:
        """收敛从未领取执行权的 accepted run，并丢弃 pending 用户消息。"""
        ...

    def abort_run(self, run_id: str) -> ChatRun:
        """收敛未进入执行器的中断 run。"""
        ...

    def request_abort(self, run_id: str) -> ChatRun:
        """持久化取消请求；通知只用于降低延迟，不能替代此事实来源。"""
        ...

    def expire_stale_runs_for_chat(self, *, chat_id: str) -> tuple[ChatRun, ...]:
        """释放超时的单实例 run，避免旧执行者永久占用会话。"""
        ...

    def heartbeat_run(self, run_id: str) -> ChatRun:
        """兼容的无租约心跳入口，仅限当前单实例适配器。"""
        ...

    def complete_run_with_messages(
        self,
        *,
        run_id: str,
        user_message_id: str,
        assistant_message_id: str,
        assistant_content: str,
        terminal_event: ChatStreamEvent | None = None,
    ) -> ChatRun:
        """兼容的无租约成功提交入口，仅限当前单实例适配器。"""
        ...

    def fail_run_with_user(
        self,
        *,
        run_id: str,
        user_message_id: str,
        error_message: str,
        terminal_event: ChatStreamEvent | None = None,
    ) -> ChatRun:
        """兼容的无租约失败提交入口，仅限当前单实例适配器。"""
        ...

    def abort_run_with_user(
        self,
        *,
        run_id: str,
        user_message_id: str,
        terminal_event: ChatStreamEvent | None = None,
    ) -> ChatRun:
        """兼容的无租约中断提交入口，仅限当前单实例适配器。"""
        ...


__all__ = [
    "ChatRunCoordinator",
    "ChatRunLease",
    "ChatRunLeaseCapabilities",
    "ChatRunLeaseLostError",
    "SINGLE_INSTANCE_CHAT_RUN_LEASE_CAPABILITIES",
]
