"""AnythingLLM 集成层共享的重试与调用次数策略。

本模块只保存多个 AnythingLLM 适配器共同遵守的执行策略，不收纳 URL、密钥或通用业务
常量。硬上限用于保护外部服务和任务成本，默认值用于生产装配；两者即使当前数值相同，
也必须保持独立命名，避免调整默认行为时意外放宽安全边界。
"""

from __future__ import annotations


MAX_UPLOAD_RETRIES = 3
"""单次全局文档上传在首次请求之后允许的额外重试次数硬上限。"""

DEFAULT_UPLOAD_RETRIES = 3
"""单次全局文档上传默认额外重试次数；总请求次数还包含首次请求。"""

DEFAULT_UPLOAD_RETRY_BASE_DELAY_SECONDS = 3.0
"""Document Processor 暂时不可用时，指数退避的默认基础秒数。"""

MAX_EMBEDDING_ATTEMPTS = 3
"""单次文档绑定操作允许的总调用次数硬上限，包含首次调用。"""

DEFAULT_EMBEDDING_ATTEMPTS = 2
"""单次文档绑定操作默认总调用次数，包含首次调用。"""


def validate_upload_max_retries(value: int) -> int:
    """校验并返回全局文档上传的额外重试次数。

    显式拒绝 ``bool`` 和浮点数。Python 中 ``bool`` 是 ``int`` 的子类，如果只执行区间
    比较，``True`` 会被错误解释为一次重试，并把配置错误带入生产任务。
    """
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_UPLOAD_RETRIES
    ):
        raise ValueError(
            f"upload_max_retries 必须是 0 到 {MAX_UPLOAD_RETRIES} 之间的整数"
        )
    return value


def validate_upload_retry_base_delay(value: float) -> float:
    """校验并返回上传指数退避的非负基础秒数。"""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value < 0
    ):
        raise ValueError("upload_retry_base_delay 必须是非负数")
    return float(value)


def validate_embedding_max_attempts(value: int) -> int:
    """校验并返回文档绑定操作包含首次调用在内的最大调用次数。"""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_EMBEDDING_ATTEMPTS
    ):
        raise ValueError(
            "embedding_max_attempts 必须是 1 到 "
            f"{MAX_EMBEDDING_ATTEMPTS} 之间的整数"
        )
    return value
