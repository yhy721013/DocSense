"""武器谱来源文本翻译端口。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from .common import (
    WeaponryCallIdentity,
    WeaponryOperation,
    optional_text,
    required_text,
)


@dataclass(frozen=True)
class WeaponryTranslationRequest:
    """一次来源级翻译请求；文本缓存的生命周期不得超过当前 execution。"""

    call: WeaponryCallIdentity
    text: str
    target_language: str

    def __post_init__(self) -> None:
        if not isinstance(self.call, WeaponryCallIdentity):
            raise TypeError("call 必须是 WeaponryCallIdentity")
        if self.call.operation is not WeaponryOperation.TRANSLATION:
            raise ValueError("翻译只能使用 translation call")
        if not isinstance(self.text, str) or not self.text:
            raise ValueError("text 必须是非空 str")
        object.__setattr__(
            self,
            "target_language",
            required_text(self.target_language, name="target_language"),
        )


class WeaponryTranslationOutcome(str, Enum):
    """翻译成功或兼容失败。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class WeaponryTranslationResult:
    """翻译失败保持空文本，不升级为字段或任务失败。"""

    call: WeaponryCallIdentity
    text: str
    outcome: WeaponryTranslationOutcome
    error_code: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.call, WeaponryCallIdentity):
            raise TypeError("call 必须是 WeaponryCallIdentity")
        if self.call.operation is not WeaponryOperation.TRANSLATION:
            raise ValueError("翻译结果只能绑定 translation call")
        if not isinstance(self.text, str):
            raise TypeError("text 必须是 str")
        if not isinstance(self.outcome, WeaponryTranslationOutcome):
            raise TypeError("outcome 必须是 WeaponryTranslationOutcome")
        error_code = optional_text(self.error_code, name="error_code")
        if self.outcome is WeaponryTranslationOutcome.SUCCEEDED:
            if not self.text:
                raise ValueError("成功翻译必须返回非空文本")
            if error_code:
                raise ValueError("成功翻译不得携带 error_code")
        else:
            if self.text:
                raise ValueError("失败翻译必须返回空文本")
            if not error_code:
                raise ValueError("失败翻译必须携带 error_code")
        object.__setattr__(self, "error_code", error_code)


@runtime_checkable
class WeaponryTranslationPort(Protocol):
    """翻译外部能力；实现不得共享跨任务可变缓存。"""

    def translate(
        self,
        request: WeaponryTranslationRequest,
    ) -> WeaponryTranslationResult:
        ...


__all__ = [
    "WeaponryTranslationOutcome",
    "WeaponryTranslationPort",
    "WeaponryTranslationRequest",
    "WeaponryTranslationResult",
]
