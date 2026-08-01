"""Callback 历史只读端口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable



@dataclass(frozen=True)
class CallbackRecord:
    """可安全展示的 Callback 元数据，不包含目录绝对路径。"""

    record_id: str
    file_name: str
    modified_at: str
    size_bytes: int


@dataclass(frozen=True)
class CallbackRecordText:
    """一次文件读取结果；错误只表达类型，不把绝对路径暴露给 Application。"""

    text: str | None
    error_kind: str | None = None


@runtime_checkable
class CallbackHistoryReadPort(Protocol):
    """列举和读取 Callback 历史，不向上层暴露 ``pathlib.Path``。"""

    def list_records(self, *, limit: int) -> tuple[CallbackRecord, ...]: ...

    def find_record(self, record_id: str) -> CallbackRecord | None: ...

    def read_record(self, record_id: str) -> CallbackRecordText: ...
