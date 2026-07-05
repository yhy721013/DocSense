"""交互审计写事务执行器、稳定异常和提交凭据。

本模块只负责审计写事务的并发与失败语义，不了解任务状态、RAG DTO 或具体审计表结构。
调用方通过 ``writer`` 回调描述一个完整事务；执行器保证仅对 SQLite 短暂锁竞争执行有限
重试，并且只有提交成功才返回结果。
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar


logger = logging.getLogger(__name__)

AUDIT_WRITE_MAX_ATTEMPTS = 5
"""交互审计 SQLite 写事务允许的总尝试次数硬上限。"""

_AUDIT_RETRY_BASE_DELAY_SECONDS = 0.05
"""SQLite 短暂锁冲突的首次退避时间；后续尝试按指数增长。"""

AUDIT_STATUS_SUCCEEDED = "succeeded"
"""只有完整审计事务提交后才能向调用方返回的成功门禁状态。"""

AUDIT_SCHEMA_VERSION = 2
"""带执行身份、完整 trace 明细和摘要校验的当前审计结构版本。"""

_T = TypeVar("_T")


class InteractionAuditError(RuntimeError):
    """交互审计无法完整提交时抛出的稳定应用异常。"""

    stage = "audit"


@dataclass(frozen=True)
class InteractionAuditResult:
    """一次完整交互审计事务的提交凭据。

    ``created`` 表示本次首次提交，``reused`` 表示相同执行和相同 trace 的幂等重放命中。
    两者必须且只能有一个为真。
    """

    interaction_id: int
    audit_status: str = AUDIT_STATUS_SUCCEEDED
    created: bool = True
    reused: bool = False

    def __post_init__(self) -> None:
        """防止无效记录 ID、伪造状态或矛盾幂等结果绕过门禁。"""
        if self.interaction_id < 1:
            raise ValueError("interaction_id 必须是正整数")
        if self.audit_status != AUDIT_STATUS_SUCCEEDED:
            raise ValueError("InteractionAuditResult 只能表示已提交的成功审计")
        if self.created == self.reused:
            raise ValueError(
                "InteractionAuditResult.created 与 reused 必须且只能有一个为 True"
            )


class SQLiteAuditExecutor(Generic[_T]):
    """使用独立连接执行带有限锁重试的 SQLite 审计事务。"""

    def __init__(
        self,
        connection_factory: Callable[[float], sqlite3.Connection],
    ) -> None:
        """保存连接工厂；每次尝试都必须返回一个新的连接。"""
        if not callable(connection_factory):
            raise TypeError("connection_factory 必须可调用")
        self._connection_factory = connection_factory

    @staticmethod
    def _is_retryable_lock_error(error: sqlite3.OperationalError) -> bool:
        """识别 SQLite BUSY/LOCKED 主错误码及其扩展错误码。"""
        error_code = getattr(error, "sqlite_errorcode", None)
        primary_error_code = (
            error_code & 0xFF if isinstance(error_code, int) else error_code
        )
        busy_code = getattr(sqlite3, "SQLITE_BUSY", 5)
        locked_code = getattr(sqlite3, "SQLITE_LOCKED", 6)
        if primary_error_code in {busy_code, locked_code}:
            return True
        normalized_message = str(error).casefold()
        return (
            "database is locked" in normalized_message
            or "database table is locked" in normalized_message
        )

    def run(
        self,
        *,
        operation: str,
        writer: Callable[[sqlite3.Connection], _T],
    ) -> _T:
        """执行一个完整审计写事务，仅在成功提交后返回 writer 结果。"""
        for attempt in range(1, AUDIT_WRITE_MAX_ATTEMPTS + 1):
            conn: sqlite3.Connection | None = None
            try:
                # 零等待连接把锁竞争交给显式策略处理，避免叠加 sqlite3 隐式等待后突破
                # 可证明的重试上限。
                conn = self._connection_factory(0.0)
                conn.execute("BEGIN IMMEDIATE")
                result = writer(conn)
                conn.commit()
                return result
            except sqlite3.OperationalError as exc:
                if conn is not None:
                    conn.rollback()
                retryable = self._is_retryable_lock_error(exc)
                if retryable and attempt < AUDIT_WRITE_MAX_ATTEMPTS:
                    delay = _AUDIT_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                    logger.warning(
                        "交互审计写入遇到 SQLite 锁竞争，将有限重试: operation=%s "
                        "attempt=%s max_attempts=%s delay_seconds=%.3f",
                        operation,
                        attempt,
                        AUDIT_WRITE_MAX_ATTEMPTS,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                logger.error(
                    "交互审计写入失败: operation=%s attempt=%s max_attempts=%s "
                    "retryable_lock=%s error_type=%s",
                    operation,
                    attempt,
                    AUDIT_WRITE_MAX_ATTEMPTS,
                    retryable,
                    type(exc).__name__,
                )
                message = (
                    "交互审计写入失败：SQLite 锁重试已耗尽"
                    if retryable
                    else "交互审计写入失败：SQLite 操作异常"
                )
                raise InteractionAuditError(message) from exc
            except sqlite3.Error as exc:
                if conn is not None:
                    conn.rollback()
                logger.error(
                    "交互审计写入失败且不可重试: operation=%s error_type=%s",
                    operation,
                    type(exc).__name__,
                )
                raise InteractionAuditError(
                    "交互审计写入失败：SQLite 持久化异常"
                ) from exc
            except Exception:
                if conn is not None:
                    conn.rollback()
                raise
            finally:
                if conn is not None:
                    conn.close()

        raise InteractionAuditError("交互审计写入失败：未获得确定结果")
