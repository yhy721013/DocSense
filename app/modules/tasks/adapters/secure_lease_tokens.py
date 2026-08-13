"""Task lease token 的高熵本地生成适配器。"""

from __future__ import annotations

import secrets


class SecureTaskLeaseTokenFactory:
    """每次 claim 生成独立 token；调用方和日志均不得持久展示该值。"""

    def __init__(self, *, entropy_bytes: int = 32) -> None:
        if type(entropy_bytes) is not int or entropy_bytes < 32:
            raise ValueError("entropy_bytes 必须是至少 32 的整数")
        self._entropy_bytes = entropy_bytes

    def new_token(self) -> str:
        return secrets.token_urlsafe(self._entropy_bytes)


__all__ = ["SecureTaskLeaseTokenFactory"]
