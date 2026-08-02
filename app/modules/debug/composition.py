"""Debug Application、Port 与 Adapter 的框架无关组合根。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.modules.debug.adapters import (
    FileCallbackHistoryReadAdapter,
    LocalChatDebugSnapshotReadAdapter,
)
from app.modules.debug.application import LoadCallbackPreview, LoadChatDebugBootstrap
from app.modules.chat.adapters.sqlite.store import ChatPersistenceStore
from app.services.core.database import DatabaseService


@dataclass(frozen=True)
class DebugApplicationServices:
    """路由和兼容 Facade 可调用的 Debug 查询外观。"""

    callback_preview: LoadCallbackPreview
    chat_bootstrap: LoadChatDebugBootstrap


def compose_debug_application_services(
    *,
    chat_store: ChatPersistenceStore,
    kb_service: DatabaseService,
    callback_history_dir: Path | None = None,
) -> DebugApplicationServices:
    """装配纯只读 Debug 查询；不读取 Flask context、不联网、不启动线程。"""

    return DebugApplicationServices(
        callback_preview=LoadCallbackPreview(
            FileCallbackHistoryReadAdapter(callback_history_dir)
        ),
        chat_bootstrap=LoadChatDebugBootstrap(
            LocalChatDebugSnapshotReadAdapter(
                chat_store=chat_store,
                kb_service=kb_service,
            )
        ),
    )
