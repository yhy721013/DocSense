"""SQLite-backed internal event ledger for file-chat runs."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from app.services.chat.domain.events import ChatStreamEvent
from app.services.chat.domain.models import ChatRunEvent
from app.services.chat.persistence.repositories import _Repository, _required_text, _utc_now_iso


_TERMINAL_EVENT_TYPES = frozenset({"aborted", "done", "error"})


@runtime_checkable
class ChatRunEventStore(Protocol):
    """Internal event-ledger capability used by chat application services."""

    def append(self, *, run_id: str, event: ChatStreamEvent) -> ChatRunEvent:
        """Persist one event before its presentation."""
        ...

    def append_many(
        self,
        *,
        run_id: str,
        events: Sequence[ChatStreamEvent],
    ) -> tuple[ChatRunEvent, ...]:
        """Persist an ordered non-terminal event batch in one transaction."""
        ...

    def list_by_run(self, run_id: str) -> tuple[ChatRunEvent, ...]:
        """Read events in internal sequence order without defining HTTP semantics."""
        ...


class ChatRunEventRepository(_Repository):
    """Persist strictly ordered stream events for one internally identified run."""

    def append(self, *, run_id: str, event: ChatStreamEvent) -> ChatRunEvent:
        return self.append_many(run_id=run_id, events=(event,))[0]

    def append_many(
        self,
        *,
        run_id: str,
        events: Sequence[ChatStreamEvent],
    ) -> tuple[ChatRunEvent, ...]:
        """Append a contiguous event batch under one SQLite write transaction."""
        normalized_run_id = _required_text(run_id, name="run_id")
        normalized_events = tuple(events)
        if not normalized_events:
            return ()
        if any(not isinstance(event, ChatStreamEvent) for event in normalized_events):
            raise TypeError("events must contain ChatStreamEvent")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self.append_many_in_transaction(
                connection=connection,
                run_id=normalized_run_id,
                events=normalized_events,
            )

    @classmethod
    def append_in_transaction(
        cls,
        *,
        connection: sqlite3.Connection,
        run_id: str,
        event: ChatStreamEvent,
    ) -> ChatRunEvent:
        """Append an event using the caller's transaction.

        This is used by the SQLite run-lock adapter so a terminal event commits
        atomically with the run state and locally authoritative messages.
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
        """Append an ordered event group using a caller-owned transaction."""
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
        return tuple(self._row(row) for row in rows)

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
