"""check-task 可靠回调恢复命令端口。

本端口只表达“在共享数据库事务中登记或复用恢复命令”，绝不执行 Callback HTTP、
发布 RabbitMQ 消息或伪造队列成功。阶段 3～4 的 MySQL/Outbox Adapter 必须实现这里
冻结的批量原子语义；阶段 1B-1 只提供类型、协议和 Fake 契约。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from app.modules.tasks.domain.models import TaskBusinessRef, TaskId


CALLBACK_RECOVERY_COMMAND_SCHEMA_VERSION = 1
CALLBACK_RECOVERY_TRIGGER_CHECK_TASK = "check_task"


def _optional_text(value: object, *, name: str) -> str:
    """规范化可空内部追踪字段，拒绝隐式字符串化任意对象。"""

    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    return value.strip()


def _required_text(value: object, *, name: str) -> str:
    """规范化非空内部标识。"""

    normalized = _optional_text(value, name=name)
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


class CallbackRecoveryCommandOutcome(str, Enum):
    """可靠恢复命令在事务提交后的内部登记结果。"""

    CREATED = "created"
    ALREADY_ACTIVE = "already_active"
    NOT_NEEDED = "not_needed"
    STALE = "stale"


_ACTIVE_COMMAND_OUTCOMES = frozenset(
    {
        CallbackRecoveryCommandOutcome.CREATED,
        CallbackRecoveryCommandOutcome.ALREADY_ACTIVE,
    }
)


@dataclass(frozen=True)
class CallbackRecoveryCommand:
    """提交给可靠命令端口的一项最小内部命令。

    ``expected_task_id`` 是 Web 请求读取到的具体执行代次。MySQL Adapter 必须在同一
    事务中重新核对该业务键的最新 TaskId 和恢复资格；若已经出现新代次，应返回
    ``stale``，不能为旧任务创建 Outbox。恢复请求 ID 由持久化 Adapter 原子生成或复用，
    因此不由 Web/Application 层提前伪造。
    """

    expected_task_id: TaskId
    business_ref: TaskBusinessRef
    trigger: str = CALLBACK_RECOVERY_TRIGGER_CHECK_TASK
    schema_version: int = CALLBACK_RECOVERY_COMMAND_SCHEMA_VERSION
    trace_id: str = ""
    correlation_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.expected_task_id, TaskId):
            raise TypeError("expected_task_id 必须是 TaskId")
        if not isinstance(self.business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        trigger = _required_text(self.trigger, name="trigger")
        if trigger != CALLBACK_RECOVERY_TRIGGER_CHECK_TASK:
            raise ValueError("trigger 只允许 check_task")
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != CALLBACK_RECOVERY_COMMAND_SCHEMA_VERSION
        ):
            raise ValueError(
                "schema_version 必须等于当前 Callback Recovery Command 版本"
            )
        object.__setattr__(self, "trigger", trigger)
        object.__setattr__(
            self,
            "trace_id",
            _optional_text(self.trace_id, name="trace_id"),
        )
        object.__setattr__(
            self,
            "correlation_id",
            _optional_text(self.correlation_id, name="correlation_id"),
        )


@dataclass(frozen=True)
class CallbackRecoveryCommandResult:
    """一项命令在数据库事务提交后的结果。

    ``created`` 和 ``already_active`` 必须携带同一类内部 recovery request ID，供阶段
    4～6 的 Outbox/Worker 继续处理；``not_needed`` 与 ``stale`` 没有可投递请求，禁止
    携带该 ID。此对象只在内部流转，Presenter 不得将任何字段输出到 check-task 响应。
    """

    expected_task_id: TaskId
    business_ref: TaskBusinessRef
    outcome: CallbackRecoveryCommandOutcome
    recovery_request_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.expected_task_id, TaskId):
            raise TypeError("expected_task_id 必须是 TaskId")
        if not isinstance(self.business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        if not isinstance(self.outcome, CallbackRecoveryCommandOutcome):
            raise TypeError("outcome 必须是 CallbackRecoveryCommandOutcome")
        request_id = _optional_text(
            self.recovery_request_id,
            name="recovery_request_id",
        )
        if self.outcome in _ACTIVE_COMMAND_OUTCOMES and not request_id:
            raise ValueError("活动命令结果必须包含 recovery_request_id")
        if self.outcome not in _ACTIVE_COMMAND_OUTCOMES and request_id:
            raise ValueError("非活动命令结果不得包含 recovery_request_id")
        object.__setattr__(self, "recovery_request_id", request_id)

    @property
    def active(self) -> bool:
        """表示结果对应一个已创建或已复用的活动恢复请求。"""

        return self.outcome in _ACTIVE_COMMAND_OUTCOMES


@runtime_checkable
class CallbackRecoveryCommandPort(Protocol):
    """原子登记一批 check-task 恢复命令的能力边界。

    实现必须在一个数据库事务内处理整批命令，并返回与输入等长、同序的结果。事务
    提交失败必须抛出异常且不能保留部分命令；同一 TaskId 在 pending/queued/running
    状态至多一个活动恢复请求，重复调用必须复用其 ID。RabbitMQ 发布不在本方法内执行，
    由与恢复请求同事务写入的 Outbox 在后续阶段异步完成。
    """

    def request_many(
        self,
        commands: tuple[CallbackRecoveryCommand, ...],
    ) -> tuple[CallbackRecoveryCommandResult, ...]:
        """原子创建或复用命令；持久化失败必须向调用方传播。"""
        ...


__all__ = [
    "CALLBACK_RECOVERY_COMMAND_SCHEMA_VERSION",
    "CALLBACK_RECOVERY_TRIGGER_CHECK_TASK",
    "CallbackRecoveryCommand",
    "CallbackRecoveryCommandOutcome",
    "CallbackRecoveryCommandPort",
    "CallbackRecoveryCommandResult",
]
