"""Task Control SQLite 短连接工厂。

Factory 只能由成功的 Bootstrap 结果构造。每次调用创建独立、线程绑定的连接并复核数据库
身份；不缓存 Connection，不建表，不修复 Schema，也不执行业务重试。
"""

from __future__ import annotations

import logging
from pathlib import Path
import sqlite3

from .bootstrap import TaskControlBootstrapResult
from .schema import TaskControlSchemaError, validate_task_control_connection_identity


logger = logging.getLogger(__name__)


class SQLiteConnectionFactoryError(RuntimeError):
    """表示短连接创建或身份复核失败。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


class SQLiteConnectionFactory:
    """从已验证数据库身份创建短生命周期 SQLite Connection。"""

    def __init__(
        self,
        bootstrap_result: TaskControlBootstrapResult,
        *,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if not isinstance(bootstrap_result, TaskControlBootstrapResult):
            raise TypeError("bootstrap_result 必须是 TaskControlBootstrapResult")
        if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int):
            raise TypeError("busy_timeout_ms 必须是整数")
        if busy_timeout_ms < 1 or busy_timeout_ms > 60_000:
            raise ValueError("busy_timeout_ms 必须位于 1..60000")
        path = Path(bootstrap_result.database_path).resolve(strict=True)
        if not path.is_file():
            raise SQLiteConnectionFactoryError(
                "connection_database_missing",
                "Bootstrap 成功后的数据库主文件不存在",
            )
        self._path = path
        self._expected_identity = bootstrap_result.identity
        self._busy_timeout_ms = busy_timeout_ms

    @property
    def database_path(self) -> Path:
        """仅供基础设施装配/诊断读取；业务日志不得直接输出该路径。"""

        return self._path

    @property
    def database_identity_key(self) -> str:
        """同进程嵌套事务门禁使用的稳定、非秘密身份。"""

        return self._expected_identity.db_instance_uuid

    @property
    def busy_timeout_ms(self) -> int:
        return self._busy_timeout_ms

    def open(self, *, read_only: bool = False) -> sqlite3.Connection:
        """创建并验证一个独立连接；失败时保证关闭半初始化连接。"""

        if not isinstance(read_only, bool):
            raise TypeError("read_only 必须是 bool")
        mode = "ro" if read_only else "rw"
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"{self._path.as_uri()}?mode={mode}",
                uri=True,
                timeout=self._busy_timeout_ms / 1000,
                isolation_level=None,
                # 明确使用 Python 默认的同线程门禁，禁止 Connection 在 Worker 间共享。
                check_same_thread=True,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA recursive_triggers = OFF")
            if read_only:
                connection.execute("PRAGMA query_only = ON")
            validate_task_control_connection_identity(
                connection,
                self._expected_identity,
            )
        except TaskControlSchemaError as exc:
            if connection is not None:
                connection.close()
            logger.error(
                "Task Control 短连接身份复核失败: code=%s db_instance_uuid_prefix=%s",
                exc.code,
                self._expected_identity.db_instance_uuid[:8],
            )
            raise SQLiteConnectionFactoryError(exc.code, str(exc)) from exc
        except sqlite3.OperationalError as exc:
            if connection is not None:
                connection.close()
            message = str(exc).lower()
            code = (
                "connection_sqlite_busy"
                if "busy" in message or "locked" in message
                else "connection_sqlite_operational_error"
            )
            logger.error(
                "Task Control 短连接创建失败: code=%s error_type=%s "
                "db_instance_uuid_prefix=%s",
                code,
                type(exc).__name__,
                self._expected_identity.db_instance_uuid[:8],
            )
            raise SQLiteConnectionFactoryError(
                code,
                f"SQLite 短连接创建失败: error_type={type(exc).__name__}",
            ) from exc
        except sqlite3.Error as exc:
            if connection is not None:
                connection.close()
            raise SQLiteConnectionFactoryError(
                "connection_sqlite_error",
                f"SQLite 短连接创建失败: error_type={type(exc).__name__}",
            ) from exc
        except BaseException:
            # 取消/中断也不得遗留一个身份尚未验证完成的连接。
            if connection is not None:
                connection.close()
            raise
        logger.debug(
            "Task Control 短连接身份复核通过: read_only=%s db_instance_uuid_prefix=%s",
            read_only,
            self._expected_identity.db_instance_uuid[:8],
        )
        return connection


__all__ = ["SQLiteConnectionFactory", "SQLiteConnectionFactoryError"]
