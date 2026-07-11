"""以 SQLite 为后端的文件对话运行内部事件账本。"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from app.services.chat.domain.events import ChatStreamEvent
from app.services.chat.domain.models import ChatRunEvent
from app.services.chat.persistence.repositories import _Repository, _required_text, _utc_now_iso


_TERMINAL_EVENT_TYPES = frozenset({"aborted", "done", "error"})
logger = logging.getLogger(__name__)


@runtime_checkable
class ChatRunEventStore(Protocol):
    """对话应用服务使用的内部事件账本能力。"""

    def append(self, *, run_id: str, event: ChatStreamEvent) -> ChatRunEvent:
        """在展示事件前持久化该事件。"""
        ...

    def append_many(
        self,
        *,
        run_id: str,
        events: Sequence[ChatStreamEvent],
    ) -> tuple[ChatRunEvent, ...]:
        """在一个事务中持久化有序的非终态事件批次。"""
        ...

    def list_by_run(self, run_id: str) -> tuple[ChatRunEvent, ...]:
        """按内部序号读取事件，但不定义 HTTP 语义。"""
        ...


class ChatRunEventRepository(_Repository):
    """为一条内部标识运行持久化严格有序的流事件。"""

    def append(self, *, run_id: str, event: ChatStreamEvent) -> ChatRunEvent:
        return self.append_many(run_id=run_id, events=(event,))[0]

    def append_many(
        self,
        *,
        run_id: str,
        events: Sequence[ChatStreamEvent],
    ) -> tuple[ChatRunEvent, ...]:
        """在一次 SQLite 写事务中追加连续的事件批次。"""
        normalized_run_id = _required_text(run_id, name="run_id")
        normalized_events = tuple(events)
        if not normalized_events:
            return ()
        if any(not isinstance(event, ChatStreamEvent) for event in normalized_events):
            raise TypeError("events must contain ChatStreamEvent")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            records = self.append_many_in_transaction(
                connection=connection,
                run_id=normalized_run_id,
                events=normalized_events,
            )
        logger.debug(
            "文件对话事件批次已写入本地账本: run_id=%s event_count=%d terminal_event=%s",
            normalized_run_id,
            len(records),
            records[-1].event_type if records[-1].event_type in _TERMINAL_EVENT_TYPES else "",
        )
        return records

    @classmethod
    def append_in_transaction(
        cls,
        *,
        connection: sqlite3.Connection,
        run_id: str,
        event: ChatStreamEvent,
    ) -> ChatRunEvent:
        """使用调用方事务追加一个事件。

        SQLite 运行锁适配器使用本方法，使终态事件可与运行状态和本地权威消息原子提交。
        """
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be sqlite3.Connection")
        return cls.append_many_in_transaction(
            connection=connection,
            run_id=run_id,
            events=(event,),
        )[0]

    @classmethod
    def append_many_in_transaction(
        cls,
        *,
        connection: sqlite3.Connection,
        run_id: str,
        events: Sequence[ChatStreamEvent],
    ) -> tuple[ChatRunEvent, ...]:
        """使用调用方持有的事务追加有序事件组。"""
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be sqlite3.Connection")
        normalized_run_id = _required_text(run_id, name="run_id")
        normalized_events = tuple(events)
        if not normalized_events:
            return ()
        for event in normalized_events:
            cls._require_event(event)
        run = connection.execute(
            "SELECT run_id FROM chat_runs WHERE run_id = ?",
            (normalized_run_id,),
        ).fetchone()
        if run is None:
            raise ValueError("chat_run 不存在")
        terminal = connection.execute(
            """
            SELECT event_type FROM chat_run_events
            WHERE run_id = ? AND event_type IN ('done', 'error', 'aborted')
            LIMIT 1
            """,
            (normalized_run_id,),
        ).fetchone()
        if terminal is not None:
            logger.warning(
                "拒绝追加文件对话事件：运行已存在终态事件: run_id=%s existing_event_type=%s",
                normalized_run_id,
                terminal["event_type"],
            )
            raise ValueError("chat_run already has a terminal event")
        sequence_row = connection.execute(
            "SELECT COALESCE(MAX(event_seq), 0) AS event_seq FROM chat_run_events WHERE run_id = ?",
            (normalized_run_id,),
        ).fetchone()
        event_seq = int(sequence_row["event_seq"])
        records: list[ChatRunEvent] = []
        terminal_seen = False
        for event in normalized_events:
            if terminal_seen:
                logger.warning(
                    "拒绝追加文件对话事件批次：终态事件后仍包含后续事件: run_id=%s",
                    normalized_run_id,
                )
                raise ValueError("chat_run batch contains events after a terminal event")
            event_seq += 1
            payload = cls._serialize_data(event)
            created_at = _utc_now_iso()
            connection.execute(
                """
                INSERT INTO chat_run_events (
                    run_id, event_seq, event_type, data_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    normalized_run_id,
                    event_seq,
                    event.event_type,
                    payload,
                    created_at,
                ),
            )
            records.append(
                ChatRunEvent(
                    run_id=normalized_run_id,
                    event_seq=event_seq,
                    event_type=event.event_type,
                    data=dict(event.data),
                    created_at=created_at,
                )
            )
            terminal_seen = event.event_type in _TERMINAL_EVENT_TYPES
        logger.debug(
            "文件对话事件已加入当前事务，等待提交: run_id=%s event_count=%d",
            normalized_run_id,
            len(records),
        )
        return tuple(records)

    def list_by_run(self, run_id: str) -> tuple[ChatRunEvent, ...]:
        normalized_run_id = _required_text(run_id, name="run_id")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT run_id, event_seq, event_type, data_json, created_at
                FROM chat_run_events
                WHERE run_id = ?
                ORDER BY event_seq ASC
                """,
                (normalized_run_id,),
            ).fetchall()
        events = tuple(self._row(row) for row in rows)
        logger.debug(
            "已读取文件对话内部事件账本: run_id=%s event_count=%d",
            normalized_run_id,
            len(events),
        )
        return events

    @staticmethod
    def _require_event(event: ChatStreamEvent) -> None:
        if not isinstance(event, ChatStreamEvent):
            raise TypeError("event must be ChatStreamEvent")

    @staticmethod
    def _serialize_data(event: ChatStreamEvent) -> str:
        return json.dumps(
            dict(event.data),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _row(row: sqlite3.Row) -> ChatRunEvent:
        data = json.loads(row["data_json"] or "{}")
        if not isinstance(data, dict):
            raise ValueError("chat_run_event data_json 必须是 JSON 对象")
        return ChatRunEvent(
            run_id=row["run_id"],
            event_seq=int(row["event_seq"]),
            event_type=row["event_type"],
            data=data,
            created_at=row["created_at"],
        )


__all__ = [
    "ChatRunEventRepository",
    "ChatRunEventStore",
]
