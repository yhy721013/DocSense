"""武器谱 ArchitectureId 的供应商无关规范化规则。

ArchitectureId 同时参与公开请求校验、任务业务键、持久化快照、进度订阅和回调身份。
这些入口如果分别实现 ``int``/``str`` 转换，带前导零的合法字符串就可能指向不同任务。
本模块只负责纯值规范化，不包含 HTTP 错误文本或框架类型，便于 Flask、未来 FastAPI、
任务 Codec 和遗留 Worker 共同复用。
"""

from __future__ import annotations

import re

from .errors import WeaponryDomainValidationError
from .models import MAX_ARCHITECTURE_ID


_ASCII_DECIMAL_PATTERN = re.compile(r"[0-9]+")


def normalize_architecture_id_value(value: object) -> int:
    """返回 ArchitectureId 的规范正整数值。

    公开兼容格式是 JSON 整数，或只含 ASCII 十进制数字的字符串。字符串允许前导零，
    但不允许空白、符号、小数和指数形式。先按字符串长度和字典序比较 64 位上界，再
    调用 ``int``，避免恶意超长文本触发 Python 的整数转换位数保护并泄漏为 500。
    """

    if isinstance(value, bool):
        raise WeaponryDomainValidationError("architecture_id 格式或范围非法")

    if isinstance(value, int):
        normalized = value
    elif isinstance(value, str) and _ASCII_DECIMAL_PATTERN.fullmatch(value):
        canonical = value.lstrip("0") or "0"
        maximum = str(MAX_ARCHITECTURE_ID)
        if len(canonical) > len(maximum) or (
            len(canonical) == len(maximum) and canonical > maximum
        ):
            raise WeaponryDomainValidationError("architecture_id 格式或范围非法")
        normalized = int(canonical)
    else:
        raise WeaponryDomainValidationError("architecture_id 格式或范围非法")

    if normalized < 1 or normalized > MAX_ARCHITECTURE_ID:
        raise WeaponryDomainValidationError("architecture_id 格式或范围非法")
    return normalized


__all__ = ["normalize_architecture_id_value"]
