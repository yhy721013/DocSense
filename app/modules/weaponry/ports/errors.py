"""武器谱端口的稳定失败分类。

异常只携带可持久化的内部错误码和不含业务正文的诊断消息。Application 可依据
``outcome`` 区分“明确失败”和“外部结果未知”，而不需要识别 requests/AnythingLLM 异常。
"""

from __future__ import annotations

from enum import Enum

from .common import required_text


class WeaponryExternalOutcome(str, Enum):
    """一次外部操作失败后能够确认的最精确结果。"""

    DEFINITELY_FAILED = "definitely_failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class WeaponryPortError(RuntimeError):
    """所有武器谱端口稳定异常的基类。"""

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = required_text(error_code, name="error_code")
        normalized_message = required_text(message, name="message")
        super().__init__(normalized_message)


class WeaponryExternalOperationError(WeaponryPortError):
    """供应商或基础设施调用失败，且明确标记发送结果是否未知。"""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        outcome: WeaponryExternalOutcome,
    ) -> None:
        if not isinstance(outcome, WeaponryExternalOutcome):
            raise TypeError("outcome 必须是 WeaponryExternalOutcome")
        self.outcome = outcome
        super().__init__(error_code, message)


class WeaponrySourceBoundaryError(WeaponryPortError):
    """供应商返回来源无法映射到当前任务/文档/Evidence 边界。"""


class WeaponryPortStateError(WeaponryPortError):
    """调用顺序、租约或资源状态不满足端口前置条件。"""


__all__ = [
    "WeaponryExternalOperationError",
    "WeaponryExternalOutcome",
    "WeaponryPortError",
    "WeaponryPortStateError",
    "WeaponrySourceBoundaryError",
]
