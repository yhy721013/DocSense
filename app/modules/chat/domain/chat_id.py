"""文件对话公开 ``chatId`` 的类型边界与规范化规则。

持久化层当前以文本键保存 chatId；这是内部实现细节，不能因此放宽 HTTP
接口的 JSON 类型约束。所有进入公开接口的值都必须先通过本模块校验，再转换为
内部稳定的文本键；所有回显给调用方的值则转换回整数。
"""

from __future__ import annotations

import re
from typing import Any


_CANONICAL_POSITIVE_INTEGER_PATTERN = re.compile(r"^[1-9][0-9]*$")


def require_public_chat_id(value: Any) -> int:
    """校验 JSON 请求体中的 chatId 必须为 Python ``int`` 正整数。

    ``bool`` 是 ``int`` 的子类，若只使用 ``isinstance(value, int)`` 会把
    ``true`` 和 ``false`` 误当作 1 和 0，因此必须先显式排除布尔值。字符串、
    浮点数、零和负数也一律拒绝，避免不同调用方得到不一致的会话键。
    """

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("chatId必须为正整数")
    return value


def parse_query_chat_id(value: Any) -> int:
    """解析 URL Query 中的 chatId，并要求其为规范十进制正整数。

    Query 参数在 Web 框架中天然以字符串形式出现。这里不把任意可转换字符串
    视为合法值，而是只接受不含符号、小数点、空白和前导零的十进制表示，确保
    ``?chatId=1`` 与 JSON 中的 ``{"chatId": 1}`` 具有唯一、可预测的对应关系。
    """

    if not isinstance(value, str) or not _CANONICAL_POSITIVE_INTEGER_PATTERN.fullmatch(
        value
    ):
        raise ValueError("chatId必须为正整数")
    return int(value)


def chat_id_storage_key(chat_id: int) -> str:
    """将已校验的公开 chatId 转为当前持久化层使用的规范文本键。"""

    return str(require_public_chat_id(chat_id))


def chat_id_public_value(value: Any) -> int:
    """将内部文本键转换为公开接口需要回显的整数。

    若内部数据不符合新的规范，调用方不能再收到已废弃的字符串 chatId。调用方
    可据此得到明确失败信号，同时日志会保留具体失败位置，便于运维排查存量脏数据。
    """

    # 内部键也必须保持规范形式；不能通过去除空白悄然兼容旧字符串会话 ID。
    normalized = str(value or "")
    if not _CANONICAL_POSITIVE_INTEGER_PATTERN.fullmatch(normalized):
        raise ValueError("内部chatId不是规范正整数")
    return int(normalized)


__all__ = [
    "chat_id_public_value",
    "chat_id_storage_key",
    "parse_query_chat_id",
    "require_public_chat_id",
]
