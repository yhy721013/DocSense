"""Aggregate persistence store for file chat."""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from app.services.chat.persistence.repositories import (
    ChatDocumentRepository,
    ChatMessageRepository,
    ChatRunRepository,
    ChatRunInputRepository,
    ChatSessionRepository,
    ensure_chat_schema,
)
from app.services.chat.persistence.resource_lease_service import (
    ChatResourceLeaseService,
)


logger = logging.getLogger(__name__)


@runtime_checkable
class ChatPersistenceStore(Protocol):
    """Persistence capabilities required by chat application services."""

    db_path: str
    sessions: ChatSessionRepository
    documents: ChatDocumentRepository
    runs: ChatRunRepository
    run_inputs: ChatRunInputRepository
    messages: ChatMessageRepository
    resource_leases: ChatResourceLeaseService


class ChatStore:
    """Aggregate access point for chat persistence repositories."""

    def __init__(self, db_path: str) -> None:
        ensure_chat_schema(db_path)
        self.db_path = db_path
        # ChatStore 是应用层唯一聚合入口；各仓储共享同一个 db_path，
        # 但每个操作独立获取连接，避免把 SQLite 连接对象跨线程复用。
        self.sessions = ChatSessionRepository(db_path, initialize=False)
        self.documents = ChatDocumentRepository(db_path, initialize=False)
        self.runs = ChatRunRepository(db_path, initialize=False)
        self.run_inputs = ChatRunInputRepository(db_path, initialize=False)
        self.messages = ChatMessageRepository(db_path, initialize=False)
        self.resource_leases = ChatResourceLeaseService(
            db_path,
            initialize=False,
        )
        logger.info("文件对话持久化仓储已初始化: db_path=%s", db_path)


__all__ = ["ChatPersistenceStore", "ChatStore"]
