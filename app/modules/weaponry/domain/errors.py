"""武器谱领域层的稳定错误。

异常只描述供应商无关的业务不变量，不携带 Flask 状态码、数据库错误或 AnythingLLM
响应。Web Adapter 在后续波次中负责把这些错误映射为已经冻结的 HTTP 契约。
"""


class WeaponryDomainValidationError(ValueError):
    """武器谱领域对象或纯规则违反冻结不变量。"""


class WeaponryRetrievalValidationError(WeaponryDomainValidationError):
    """内部检索对象或 relevance profile 违反冻结不变量。"""


class DeprecatedWeaponryModeError(WeaponryDomainValidationError):
    """部署仍显式选择已废弃的模式 1。"""


__all__ = [
    "DeprecatedWeaponryModeError",
    "WeaponryDomainValidationError",
    "WeaponryRetrievalValidationError",
]
