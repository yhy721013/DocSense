"""武器谱 ``architectureId`` 的框架无关入站规范化规则。

主提交接口、``/llm/check-task`` 与 ``/llm/progress`` 都使用同一个业务键。如果三个
入口各自调用 ``str`` 或 ``int``，``1``、``"1"`` 与 ``"0001"`` 就可能指向不同任务，
甚至让布尔值、浮点数或对象进入任务索引。本模块集中实现已经批准的公开契约，当前
Flask 与未来 FastAPI 适配层均只能复用这里的规则，不能自行放宽。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.modules.weaponry.domain import (
    MAX_ARCHITECTURE_ID,
    WeaponryDomainValidationError,
    normalize_architecture_id_value,
)


ARCHITECTURE_ID_ERROR = (
    f"architectureId必须为1到{MAX_ARCHITECTURE_ID}之间的正整数"
)


class ArchitectureIdValidationError(ValueError):
    """公开请求中的 ``architectureId`` 违反已冻结的整数契约。"""


@dataclass(frozen=True)
class NormalizedArchitectureId:
    """同一 ArchitectureId 的公开数值与内部唯一业务键。"""

    value: int
    business_key: str


def normalize_architecture_id(value: object) -> NormalizedArchitectureId:
    """规范化 JSON 整数或仅含 ASCII 数字的十进制整数字符串。

    字符串允许任意数量的前导零，但不允许空白、正负号、小数或指数形式。比较上限时
    先处理十进制文本长度，再调用 ``int``，避免超长输入触发 Python 整数转换保护并
    泄漏成 HTTP 500。``bool`` 虽然是 Python ``int`` 的子类，也必须显式拒绝。
    """

    try:
        normalized = normalize_architecture_id_value(value)
    except WeaponryDomainValidationError as exc:
        # 领域层只描述值非法；Web 边界统一映射为已经冻结的公开错误文本。
        raise ArchitectureIdValidationError(ARCHITECTURE_ID_ERROR) from exc
    return NormalizedArchitectureId(
        value=normalized,
        business_key=str(normalized),
    )


__all__ = [
    "ARCHITECTURE_ID_ERROR",
    "ArchitectureIdValidationError",
    "NormalizedArchitectureId",
    "normalize_architecture_id",
]
