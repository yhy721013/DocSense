"""文件对话应用服务产出的领域事件。"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True)
class ChatStreamEvent:
    """单个文件对话流使用的供应商无关事件。"""

    event_type: str
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized_type = str(self.event_type or "").strip()
        if not normalized_type:
            raise ValueError("event_type cannot be empty")
        object.__setattr__(self, "event_type", normalized_type)
        object.__setattr__(self, "data", MappingProxyType(dict(self.data)))


__all__ = ["ChatStreamEvent"]
