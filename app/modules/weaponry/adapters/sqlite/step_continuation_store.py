"""Weaponry 组件自有 Step 续跑快照 Store。"""

from __future__ import annotations

import sqlite3

from app.modules.tasks.adapters.sqlite.step_continuation_store import (
    SQLiteTaskStepContinuationStore,
)


class SQLiteWeaponryStepContinuationStore(SQLiteTaskStepContinuationStore):
    def __init__(self, connection: sqlite3.Connection) -> None:
        super().__init__(
            connection,
            table_name="weaponry_step_continuation_snapshots",
        )


__all__ = ["SQLiteWeaponryStepContinuationStore"]
