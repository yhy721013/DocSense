"""Task Control SQLite 显式事务管理器。

写事务固定使用 ``BEGIN IMMEDIATE``。Transaction Manager 只管理连接、提交、回滚、关闭与
busy 分类，不知道 Task、Callback 或 Recovery 业务语义，也不会重放调用方逻辑。
"""

from __future__ import annotations

import logging
import sqlite3
import threading

from .connection import SQLiteConnectionFactory, SQLiteConnectionFactoryError


logger = logging.getLogger(__name__)
_TRANSACTION_CONTEXT = threading.local()


class SQLiteTransactionError(RuntimeError):
    """显式事务生命周期或底层 SQLite 操作失败。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


class SQLiteBusyError(SQLiteTransactionError):
    """BEGIN/COMMIT 遇到 busy/locked；上层可分类，但本层绝不隐藏重试。"""


def _active_database_keys() -> set[str]:
    active = getattr(_TRANSACTION_CONTEXT, "active_database_keys", None)
    if active is None:
        active = set()
        _TRANSACTION_CONTEXT.active_database_keys = active
    return active


def _sqlite_error_code(exc: sqlite3.Error, *, default: str) -> str:
    message = str(exc).lower()
    if "busy" in message or "locked" in message:
        return "sqlite_busy"
    return default


class SQLiteTransaction:
    """单次、单线程、不可重入的短事务。"""

    def __init__(
        self,
        connection_factory: SQLiteConnectionFactory,
        *,
        read_only: bool,
    ) -> None:
        self._factory = connection_factory
        self._read_only = read_only
        self._connection: sqlite3.Connection | None = None
        self._owner_thread_id: int | None = None
        self._entered_once = False
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def connection(self) -> sqlite3.Connection:
        """仅向同包 UoW/Store 装配暴露活动连接，不得传到 Application 层。"""

        self._require_active_owner()
        assert self._connection is not None
        return self._connection

    def __enter__(self) -> "SQLiteTransaction":
        if self._entered_once:
            raise SQLiteTransactionError(
                "transaction_reentry_forbidden",
                "SQLite Transaction 只能进入一次",
            )
        self._entered_once = True
        database_key = self._factory.database_identity_key
        active_keys = _active_database_keys()
        if database_key in active_keys:
            raise SQLiteTransactionError(
                "nested_transaction_forbidden",
                "同一线程禁止嵌套 Task Control UnitOfWork",
            )
        active_keys.add(database_key)
        connection: sqlite3.Connection | None = None
        try:
            connection = self._factory.open(read_only=self._read_only)
            connection.execute("BEGIN" if self._read_only else "BEGIN IMMEDIATE")
        except SQLiteConnectionFactoryError as exc:
            active_keys.discard(database_key)
            if exc.code == "connection_sqlite_busy":
                raise SQLiteBusyError(
                    "sqlite_busy",
                    "SQLite 身份复核期间数据库被独占锁占用",
                ) from exc
            raise
        except sqlite3.Error as exc:
            if connection is not None:
                connection.close()
            active_keys.discard(database_key)
            code = _sqlite_error_code(exc, default="transaction_begin_failed")
            logger.error(
                "Task Control SQLite 事务开启失败: code=%s read_only=%s "
                "db_instance_uuid_prefix=%s",
                code,
                self._read_only,
                database_key[:8],
            )
            error_type = SQLiteBusyError if code == "sqlite_busy" else SQLiteTransactionError
            raise error_type(
                code,
                f"SQLite 事务开启失败: error_type={type(exc).__name__}",
            ) from exc
        except BaseException:
            if connection is not None:
                connection.close()
            active_keys.discard(database_key)
            raise
        self._connection = connection
        self._owner_thread_id = threading.get_ident()
        self._active = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> bool:
        # 无论正常还是异常退出，只要调用方尚未显式 commit/rollback，就默认回滚。
        if self._active:
            self.rollback()
        return False

    def commit(self) -> None:
        self._require_active_owner()
        assert self._connection is not None
        try:
            self._connection.commit()
        except BaseException as exc:
            try:
                self._connection.rollback()
            finally:
                self._finish()
            if not isinstance(exc, sqlite3.Error):
                logger.error(
                    "Task Control SQLite 事务提交被中断并已回滚: error_type=%s "
                    "db_instance_uuid_prefix=%s",
                    type(exc).__name__,
                    self._factory.database_identity_key[:8],
                )
                raise
            code = _sqlite_error_code(exc, default="transaction_commit_failed")
            logger.error(
                "Task Control SQLite 事务提交失败: code=%s error_type=%s "
                "db_instance_uuid_prefix=%s",
                code,
                type(exc).__name__,
                self._factory.database_identity_key[:8],
            )
            error_type = SQLiteBusyError if code == "sqlite_busy" else SQLiteTransactionError
            raise error_type(
                code,
                f"SQLite 事务提交失败: error_type={type(exc).__name__}",
            ) from exc
        self._finish()

    def rollback(self) -> None:
        self._require_active_owner()
        assert self._connection is not None
        try:
            self._connection.rollback()
        except sqlite3.Error as exc:
            raise SQLiteTransactionError(
                "transaction_rollback_failed",
                f"SQLite 事务回滚失败: error_type={type(exc).__name__}",
            ) from exc
        finally:
            self._finish()

    def _require_active_owner(self) -> None:
        if not self._active or self._connection is None:
            raise SQLiteTransactionError(
                "transaction_not_active",
                "SQLite Transaction 未处于活动状态",
            )
        if self._owner_thread_id != threading.get_ident():
            raise SQLiteTransactionError(
                "transaction_thread_mismatch",
                "SQLite Transaction 不得跨线程使用",
            )

    def _finish(self) -> None:
        connection = self._connection
        self._connection = None
        self._active = False
        self._owner_thread_id = None
        _active_database_keys().discard(self._factory.database_identity_key)
        if connection is not None:
            connection.close()


class SQLiteTransactionManager:
    """为每个窄 UoW 创建新的显式 SQLite Transaction。"""

    def __init__(self, connection_factory: SQLiteConnectionFactory) -> None:
        if not isinstance(connection_factory, SQLiteConnectionFactory):
            raise TypeError("connection_factory 必须是 SQLiteConnectionFactory")
        self._connection_factory = connection_factory

    def begin(self, *, read_only: bool = False) -> SQLiteTransaction:
        if not isinstance(read_only, bool):
            raise TypeError("read_only 必须是 bool")
        return SQLiteTransaction(
            self._connection_factory,
            read_only=read_only,
        )


__all__ = [
    "SQLiteBusyError",
    "SQLiteTransaction",
    "SQLiteTransactionError",
    "SQLiteTransactionManager",
]
