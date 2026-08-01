"""任务控制面使用的框架无关领域对象。

本模块只保存已经由入站 Adapter 校验、规范化后的内部值。任何 Flask request、
SQLite Row、任意字典或供应商响应都必须先在边界处转换，不能直接进入这些对象。
不可变 DTO 既能阻止后台任务运行期间输入漂移，也便于后续把实现替换为 MySQL、
RabbitMQ 或 Redis，而不改变应用服务签名。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from math import isfinite
from typing import Generic, TypeVar


CALLBACK_PENDING = "pending"
CALLBACK_SENDING = "sending"
CALLBACK_SUCCESS = "success"
CALLBACK_FAILED = "failed"
CALLBACK_SKIPPED = "skipped"
CALLBACK_OUTCOME_UNKNOWN = "outcome_unknown"
CALLBACK_STATUSES = frozenset(
    {
        CALLBACK_PENDING,
        CALLBACK_SENDING,
        CALLBACK_SUCCESS,
        CALLBACK_FAILED,
        CALLBACK_SKIPPED,
        CALLBACK_OUTCOME_UNKNOWN,
    }
)

_PROGRESS_QUANT = Decimal("0.0001")
TTaskInput = TypeVar("TTaskInput")


def _required_text(value: object, *, name: str) -> str:
    """校验内部标识类文本；不把数字等错误类型静默转换为字符串。"""

    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


def _optional_text(value: object, *, name: str) -> str:
    """校验允许为空的文本，同时拒绝任意对象的隐式字符串化。"""

    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    return value


def _normalized_progress(value: object, *, name: str) -> float:
    """把合法进度统一为 ``[0, 1]`` 内四位小数的稳定比例。

    Adapter 可以容忍并清洗遗留脏值，但进入领域快照后的值必须真实有效。这里拒绝
    越界或非有限值，避免错误数据在 Presenter 中再次被静默掩盖。
    """

    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise TypeError(f"{name} 必须是数字")
    numeric = float(value)
    if not isfinite(numeric):
        raise ValueError(f"{name} 必须是有限数字")
    if numeric < 0.0 or numeric > 1.0:
        raise ValueError(f"{name} 必须位于 0 到 1 之间")
    try:
        normalized = Decimal(str(value)).quantize(
            _PROGRESS_QUANT,
            rounding=ROUND_HALF_UP,
        )
    except InvalidOperation as exc:
        raise ValueError(f"{name} 无法规范化") from exc
    return float(normalized)


@dataclass(frozen=True)
class TaskId:
    """一次具体任务执行的不可变内部身份。

    兼容阶段由现有 ``execution_id`` 填充。该值只能用于内部条件读取、更新、日志和
    审计，不能进入现有 HTTP、WebSocket、SSE 或 Callback 响应。
    """

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _required_text(self.value, name="TaskId.value"))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class TaskBusinessRef:
    """公开业务键在内部的规范化引用。"""

    business_type: str
    business_key: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "business_type",
            _required_text(self.business_type, name="business_type"),
        )
        object.__setattr__(
            self,
            "business_key",
            _required_text(self.business_key, name="business_key"),
        )


@dataclass(frozen=True)
class TaskSnapshot:
    """应用服务读取到的一次任务执行快照。

    ``public_status`` 只保存兼容存储中的公开状态原值，不能反向驱动
    ``execution_state``。阶段 2 引入完整状态机后，公开映射仍应由 Presenter 完成。
    """

    task_id: TaskId
    task_type: str
    business_ref: TaskBusinessRef
    execution_state: str
    public_status: str
    progress: float
    message: str
    callback_status: str
    created_at: str
    updated_at: str
    callback_attempts: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        object.__setattr__(
            self,
            "task_type",
            _required_text(self.task_type, name="task_type"),
        )
        object.__setattr__(
            self,
            "execution_state",
            _required_text(self.execution_state, name="execution_state"),
        )
        object.__setattr__(
            self,
            "public_status",
            _required_text(self.public_status, name="public_status"),
        )
        object.__setattr__(
            self,
            "progress",
            _normalized_progress(self.progress, name="progress"),
        )
        object.__setattr__(
            self,
            "message",
            _optional_text(self.message, name="message"),
        )
        callback_status = _required_text(
            self.callback_status,
            name="callback_status",
        )
        if callback_status not in CALLBACK_STATUSES:
            raise ValueError("callback_status 不是受支持的内部状态")
        object.__setattr__(self, "callback_status", callback_status)
        object.__setattr__(
            self,
            "created_at",
            _required_text(self.created_at, name="created_at"),
        )
        object.__setattr__(
            self,
            "updated_at",
            _required_text(self.updated_at, name="updated_at"),
        )
        if (
            isinstance(self.callback_attempts, bool)
            or not isinstance(self.callback_attempts, int)
            or self.callback_attempts < 0
        ):
            raise ValueError("callback_attempts 必须是非负整数")


@dataclass(frozen=True)
class TaskExecutionSnapshot(Generic[TTaskInput]):
    """携带不可变输入的一次任务执行事实。

    ``TaskSnapshot`` 面向既有查询和 Progress 回退，只描述公开投影；本类型则面向
    Worker/Application，保证执行器能够仅凭 ``TaskId`` 恢复受理时输入。泛型输入由
    各业务模块定义，tasks 模块不导入 report/analysis 等业务类型，从而保持控制面
    对业务实现的单向依赖。
    """

    task_id: TaskId
    task_type: str
    business_ref: TaskBusinessRef
    execution_state: str
    public_status: str
    progress: float
    message: str
    input_snapshot: TTaskInput
    accepted_at: str
    trace_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        object.__setattr__(
            self,
            "task_type",
            _required_text(self.task_type, name="task_type"),
        )
        object.__setattr__(
            self,
            "execution_state",
            _required_text(self.execution_state, name="execution_state"),
        )
        object.__setattr__(
            self,
            "public_status",
            _required_text(self.public_status, name="public_status"),
        )
        object.__setattr__(
            self,
            "progress",
            _normalized_progress(self.progress, name="progress"),
        )
        object.__setattr__(
            self,
            "message",
            _optional_text(self.message, name="message"),
        )
        if self.input_snapshot is None:
            raise ValueError("input_snapshot 不能为空")
        object.__setattr__(
            self,
            "accepted_at",
            _required_text(self.accepted_at, name="accepted_at"),
        )
        object.__setattr__(
            self,
            "trace_id",
            _required_text(self.trace_id, name="trace_id"),
        )


@dataclass(frozen=True)
class TaskLookupItem:
    """一项已解析的 check-task 查询。

    ``business_ref`` 用于内部查询；``response_key`` 与 ``response_value`` 保留当前
    协议原本的键名和值类型，供波次 1B Presenter 映射错误项。成功响应改为空后，
    这些公开值仍用于日志定位和单项/批量缺失判断，但不会泄露内部 TaskId。
    """

    business_ref: TaskBusinessRef
    response_key: str
    response_value: str | int

    def __post_init__(self) -> None:
        if not isinstance(self.business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        object.__setattr__(
            self,
            "response_key",
            _required_text(self.response_key, name="response_key"),
        )
        value = self.response_value
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise TypeError("response_value 只能是 str 或 int")
        if isinstance(value, str):
            value = _required_text(value, name="response_value")
        object.__setattr__(self, "response_value", value)


@dataclass(frozen=True)
class ProgressKey:
    """Progress 查询和订阅使用的规范化内部键。"""

    business_type: str
    business_key: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "business_type",
            _required_text(self.business_type, name="business_type"),
        )
        object.__setattr__(
            self,
            "business_key",
            _required_text(self.business_key, name="business_key"),
        )

    @property
    def business_ref(self) -> TaskBusinessRef:
        """转换为 Task Read Port 可识别的同一业务引用。"""

        return TaskBusinessRef(self.business_type, self.business_key)


@dataclass(frozen=True)
class ProgressSnapshot:
    """一次带内部执行身份和顺序信息的进度快照。"""

    key: ProgressKey
    task_id: TaskId
    progress: float
    message: str
    internal_state: str
    sequence_no: int
    updated_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, ProgressKey):
            raise TypeError("key 必须是 ProgressKey")
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        object.__setattr__(
            self,
            "progress",
            _normalized_progress(self.progress, name="progress"),
        )
        object.__setattr__(
            self,
            "message",
            _optional_text(self.message, name="message"),
        )
        object.__setattr__(
            self,
            "internal_state",
            _required_text(self.internal_state, name="internal_state"),
        )
        if (
            isinstance(self.sequence_no, bool)
            or not isinstance(self.sequence_no, int)
            or self.sequence_no < 0
        ):
            raise ValueError("sequence_no 必须是非负整数")
        object.__setattr__(
            self,
            "updated_at",
            _required_text(self.updated_at, name="updated_at"),
        )


@dataclass(frozen=True)
class ProgressSubscriptionRequest:
    """已由 Web Adapter 完整校验的一次无 action 订阅请求。"""

    ordered_keys: tuple[ProgressKey, ...]

    def __post_init__(self) -> None:
        keys = tuple(self.ordered_keys)
        if not keys:
            raise ValueError("ordered_keys 不能为空")
        if any(not isinstance(key, ProgressKey) for key in keys):
            raise TypeError("ordered_keys 只能包含 ProgressKey")
        if len({key.business_type for key in keys}) != 1:
            raise ValueError("同一次 Progress 请求只能包含一种 business_type")
        object.__setattr__(self, "ordered_keys", keys)

    @property
    def business_type(self) -> str:
        return self.ordered_keys[0].business_type


__all__ = [
    "CALLBACK_FAILED",
    "CALLBACK_PENDING",
    "CALLBACK_SENDING",
    "CALLBACK_OUTCOME_UNKNOWN",
    "CALLBACK_SKIPPED",
    "CALLBACK_STATUSES",
    "CALLBACK_SUCCESS",
    "ProgressKey",
    "ProgressSnapshot",
    "ProgressSubscriptionRequest",
    "TaskBusinessRef",
    "TaskExecutionSnapshot",
    "TaskId",
    "TaskLookupItem",
    "TaskSnapshot",
]
