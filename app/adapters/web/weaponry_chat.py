"""知识谱系对话五接口的框架无关严格入站解析。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from app.modules.chat.domain.identity import MAX_JAVASCRIPT_SAFE_INTEGER, WeaponryChatIdentity

BODY_ERROR = "请求体必须为JSON对象"
UNKNOWN_FIELD_ERROR = "请求包含未知字段"
QUERY_DUPLICATE_ERROR = "Query参数不能重复"
BUSINESS_TYPE_ERROR = "businessType必须为weaponryChat"
PARAMS_ERROR = "params不能为空"
USER_ID_ERROR = f"userId必须为1到{MAX_JAVASCRIPT_SAFE_INTEGER}之间的正整数"
ARCHITECTURE_ID_EMPTY_ERROR = "architectureId不能为空"
ARCHITECTURE_ID_ERROR = f"architectureId必须为1到{MAX_JAVASCRIPT_SAFE_INTEGER}之间的正整数"
MESSAGE_ERROR = "message不能为空"

_ASCII_DECIMAL = re.compile(r"^[0-9]+$", flags=re.ASCII)
_TOP_LEVEL_FIELDS = frozenset({"businessType", "params"})
_IDENTITY_FIELDS = frozenset({"userId", "architectureId"})
_SEND_FIELDS = _IDENTITY_FIELDS | {"message"}


class WeaponryChatRequestValidationError(ValueError):
    """知识谱系对话请求不满足已冻结公开合同时抛出。"""


@dataclass(frozen=True)
class WeaponryChatRequest:
    identity: WeaponryChatIdentity
    message: str | None = None


def _safe_positive_int(value: Any, *, error: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > MAX_JAVASCRIPT_SAFE_INTEGER
    ):
        raise WeaponryChatRequestValidationError(error)
    return value


def _decimal_text(value: str, *, empty_error: str, error: str) -> int:
    if not isinstance(value, str) or value == "":
        raise WeaponryChatRequestValidationError(empty_error)
    if not _ASCII_DECIMAL.fullmatch(value):
        raise WeaponryChatRequestValidationError(error)
    return _safe_positive_int(int(value, 10), error=error)


def _post_architecture_id(value: Any) -> int:
    if value is None:
        raise WeaponryChatRequestValidationError(ARCHITECTURE_ID_EMPTY_ERROR)
    if isinstance(value, str):
        return _decimal_text(
            value,
            empty_error=ARCHITECTURE_ID_EMPTY_ERROR,
            error=ARCHITECTURE_ID_ERROR,
        )
    return _safe_positive_int(value, error=ARCHITECTURE_ID_ERROR)


def parse_weaponry_chat_post(payload: Any, *, require_message: bool) -> WeaponryChatRequest:
    """解析发送或管理 POST；未知字段在读取业务值前统一拒绝。"""
    if not isinstance(payload, Mapping):
        raise WeaponryChatRequestValidationError(BODY_ERROR)
    if set(payload) - _TOP_LEVEL_FIELDS:
        raise WeaponryChatRequestValidationError(UNKNOWN_FIELD_ERROR)
    if payload.get("businessType") != "weaponryChat":
        raise WeaponryChatRequestValidationError(BUSINESS_TYPE_ERROR)
    params = payload.get("params")
    if not isinstance(params, Mapping):
        raise WeaponryChatRequestValidationError(PARAMS_ERROR)
    if set(params) - (_SEND_FIELDS if require_message else _IDENTITY_FIELDS):
        raise WeaponryChatRequestValidationError(UNKNOWN_FIELD_ERROR)
    user_id = _safe_positive_int(params.get("userId"), error=USER_ID_ERROR)
    architecture_id = _post_architecture_id(params.get("architectureId"))
    message: str | None = None
    if require_message:
        raw_message = params.get("message")
        if not isinstance(raw_message, str) or not raw_message.strip():
            raise WeaponryChatRequestValidationError(MESSAGE_ERROR)
        message = raw_message.strip()
    return WeaponryChatRequest(WeaponryChatIdentity(user_id, architecture_id), message)


def parse_weaponry_chat_history_query(
    query_items: Sequence[tuple[str, str]],
) -> WeaponryChatIdentity:
    """解析保留重复项的 Query 序列；两个 ID 均允许 ASCII 前导零。"""
    items = tuple(query_items)
    if any(name not in _IDENTITY_FIELDS for name, _ in items):
        raise WeaponryChatRequestValidationError(UNKNOWN_FIELD_ERROR)
    values: dict[str, str] = {}
    for name, value in items:
        if name in values:
            raise WeaponryChatRequestValidationError(QUERY_DUPLICATE_ERROR)
        values[name] = value
    user_id = _decimal_text(
        values.get("userId", ""),
        empty_error=USER_ID_ERROR,
        error=USER_ID_ERROR,
    )
    architecture_id = _decimal_text(
        values.get("architectureId", ""),
        empty_error=ARCHITECTURE_ID_EMPTY_ERROR,
        error=ARCHITECTURE_ID_ERROR,
    )
    return WeaponryChatIdentity(user_id, architecture_id)


__all__ = [
    "WeaponryChatRequest",
    "WeaponryChatRequestValidationError",
    "parse_weaponry_chat_history_query",
    "parse_weaponry_chat_post",
]
