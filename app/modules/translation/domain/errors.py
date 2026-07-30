"""独立 Translation 模块的稳定内部错误。"""

from __future__ import annotations


class TranslationError(RuntimeError):
    """带稳定错误码的翻译异常；不会直接序列化为公开接口响应。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code).strip()
        if not self.code:
            raise ValueError("TranslationError.code 不能为空")


__all__ = ["TranslationError"]
