"""Report 已实现续跑解析器的事务内存在性预检。"""

from __future__ import annotations

import sqlite3

from app.modules.tasks.adapters.sqlite.recovery_resume import (
    SQLiteTaskRecoveryResumePreflight,
)


class SQLiteReportRecoveryResumePreflight(SQLiteTaskRecoveryResumePreflight):
    def __init__(self, connection: sqlite3.Connection) -> None:
        super().__init__(
            connection,
            table_name="report_step_continuation_snapshots",
            supports_step=lambda key: key == "artifact.scope.begin",
        )


__all__ = ["SQLiteReportRecoveryResumePreflight"]
