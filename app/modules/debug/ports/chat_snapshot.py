"""Chat Debug 本地快照只读端口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable



@dataclass(frozen=True)
class ChatDebugSession:
    """Adapter 已完成 JavaScript 安全 chatId 投影的会话快照。"""

    chat_id: int
    file_names: tuple[str, ...]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ChatAvailableFile:
    """知识库文件的最小只读投影。"""

    file_name: str
    architecture_id: int


@dataclass(frozen=True)
class ChatDebugSnapshot:
    """Adapter 已完成过滤与安全投影的只读快照。"""

    sessions: tuple[ChatDebugSession, ...]
    available_files: tuple[ChatAvailableFile, ...]
    active_scope_member_count: int
    workspace_binding_count: int


@runtime_checkable
class ChatDebugSnapshotReadPort(Protocol):
    """读取 Debug 快照；不暴露具体 SQLite Repository。"""

    def read_snapshot(self) -> ChatDebugSnapshot: ...
