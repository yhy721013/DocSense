"""Debug 查询的框架无关、不可变结果模型。"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from app.modules.debug.ports.callback_history import CallbackRecord
from app.modules.debug.ports.chat_snapshot import ChatAvailableFile, ChatDebugSession


def _freeze_json_value(value: Any) -> Any:
    """递归冻结已解析 JSON，避免并发 Debug 请求共享可变嵌套对象。

    ``json.loads`` 只会产生 Mapping、list 与 JSON 标量。这里仍对未知类型
    fail-closed，防止未来 Adapter 悄悄把可变业务对象塞进 Application 结果。
    Presenter 会在输出边界把 MappingProxy/tuple 还原为 dict/list，因此公开响应
    的字段、数组类型和值均保持不变。
    """

    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("Debug JSON 对象键必须是 str")
            frozen[key] = _freeze_json_value(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Debug JSON 包含不支持的值类型: {type(value).__name__}")


@dataclass(frozen=True)
class CallbackPreviewResult:
    """Callback Debug 用例结果；Presenter 再投影为冻结的驼峰 JSON。"""

    ok: bool
    message: str
    payload: Mapping[str, Any] | None
    records: tuple[CallbackRecord, ...]
    selected_record: CallbackRecord | None

    def __post_init__(self) -> None:
        if self.payload is not None:
            if not isinstance(self.payload, Mapping):
                raise TypeError("payload 必须是 Mapping 或 None")
            object.__setattr__(self, "payload", _freeze_json_value(self.payload))
        records = tuple(self.records)
        if any(not isinstance(item, CallbackRecord) for item in records):
            raise TypeError("records 必须只包含 CallbackRecord")
        if self.selected_record is not None and not isinstance(
            self.selected_record, CallbackRecord
        ):
            raise TypeError("selected_record 必须是 CallbackRecord 或 None")
        object.__setattr__(self, "records", records)


@dataclass(frozen=True)
class ChatBootstrapResult:
    """Chat Debug 初始化结果及仅用于脱敏观测的聚合计数。"""

    ok: bool
    message: str
    sessions: tuple[ChatDebugSession, ...]
    available_files: tuple[ChatAvailableFile, ...]
    active_scope_member_count: int = 0
    workspace_binding_count: int = 0
