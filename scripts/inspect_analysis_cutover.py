#!/usr/bin/env python3
"""阶段 1F-7A：Analysis 存量数据库切换/诊断用只读预检工具。

脚本只读取既有 SQLite 事实表，不初始化 Schema、不创建 HTTP Client、不访问
AnythingLLM，也不会启动 ``run.py``。它的唯一职责是把不能带入 1F 运行链的历史
残留以稳定、脱敏且有上限的 JSON 形式报告出来。当前“每次更新均由 clean.py 清库
重建”的发布流程无需把本工具作为日常启动门禁；保留存量库、备份恢复或清理结果
存疑时仍必须使用。

预检失败时必须人工处置对应任务、Callback Guard 或资源租约；本工具不提供自动
清理和重放入口，避免在外部副作用未知时扩大影响范围。
"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any, Final


logger = logging.getLogger(__name__)

# ``argparse`` 保留退出码 2 给参数错误。退出码 3 表示数据面存在切换阻塞项，便于
# Shell、CI 与未来部署编排在不解析日志文本的情况下可靠地停止切换。
_EXIT_PRECHECK_BLOCKED: Final[int] = 3
_MAX_LIMIT: Final[int] = 500
_DEFAULT_LIMIT: Final[int] = 50

# 本脚本显式校验所需表，而不是复用会执行建表逻辑的 Service。这样旧库、错误库或
# 未迁移库只会得到失败结果，不会因为一次诊断而产生任何 Schema 写入。
_REQUIRED_TABLES: Final[frozenset[str]] = frozenset(
    {
        "llm_tasks",
        "llm_task_executions",
        "callback_delivery_guards",
        "rag_resource_leases",
    }
)

_OPEN_RAG_LEASE_STATUSES: Final[tuple[str, ...]] = (
    "planned",
    "active",
    "audit_failed",
    "audited",
)


class AnalysisCutoverPreflightError(RuntimeError):
    """表示目标库不满足只读预检所需的最小事实表条件。"""


def _existing_database_path(value: str) -> Path:
    """校验目标为已存在的 SQLite 文件，拒绝隐式创建新数据库。"""

    candidate = Path(value).expanduser().resolve()
    if not candidate.exists():
        raise argparse.ArgumentTypeError(f"SQLite 文件不存在: {candidate}")
    if not candidate.is_file():
        raise argparse.ArgumentTypeError(f"SQLite 路径不是文件: {candidate}")
    return candidate


def _bounded_limit(value: str) -> int:
    """解析有限正整数，避免一次预检输出无边界的内部记录。"""

    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--limit 必须是整数") from exc
    if not 1 <= parsed <= _MAX_LIMIT:
        raise argparse.ArgumentTypeError(
            f"--limit 必须在 1 到 {_MAX_LIMIT} 之间"
        )
    return parsed


def _open_read_only_connection(database_path: Path) -> sqlite3.Connection:
    """以 SQLite URI 的只读模式连接，额外开启 query_only 防御误写。"""

    # ``Path.as_uri`` 会正确转义 Windows 路径中的空格和非 ASCII 字符；``mode=ro``
    # 使 SQLite 在文件不存在或无读取权限时直接失败，而不是创建一个空数据库。
    database_uri = f"{database_path.as_uri()}?mode=ro"
    connection = sqlite3.connect(
        database_uri,
        uri=True,
        timeout=5.0,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _require_schema(connection: sqlite3.Connection) -> None:
    """确认预检所依赖的事实表都已存在，缺失时 fail closed。"""

    rows = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        """
    ).fetchall()
    existing_tables = {str(row["name"]) for row in rows}
    missing_tables = sorted(_REQUIRED_TABLES.difference(existing_tables))
    if missing_tables:
        raise AnalysisCutoverPreflightError(
            "缺少 Analysis 切换预检所需事实表: " + ", ".join(missing_tables)
        )


def _count(
    connection: sqlite3.Connection,
    query: str,
    parameters: tuple[object, ...] = (),
) -> int:
    """执行只读计数查询，并将 SQLite 返回值规范为 Python 整数。"""

    row = connection.execute(query, parameters).fetchone()
    if row is None:  # pragma: no cover - COUNT 查询在 SQLite 中始终会返回一行。
        raise AnalysisCutoverPreflightError("预检计数查询未返回结果")
    return int(row[0])


def _legacy_active_tasks(
    connection: sqlite3.Connection,
    *,
    limit: int,
) -> tuple[int, list[dict[str, Any]]]:
    """读取尚未纳入 1F-4 批次执行模型的旧活跃 file 任务。

    历史 ``llm_tasks`` 是公开进度投影，新链路批量受理后的执行记录会具备完整
    ``batch_id`` 与 ``batch_sequence``。预检只阻断前者，避免把已受理的新批次误判为
    历史残留；任何没有对应新批次执行事实的 status 0/1 记录都保守地按旧任务处理。
    """

    predicate = """
        task.business_type = 'file'
        AND task.status IN ('0', '1')
        AND NOT EXISTS (
            SELECT 1
            FROM llm_task_executions AS execution
            WHERE execution.execution_id = task.execution_id
              AND execution.business_type = 'file'
              AND execution.batch_id IS NOT NULL
              AND execution.batch_sequence IS NOT NULL
        )
    """
    count = _count(
        connection,
        f"SELECT COUNT(*) FROM llm_tasks AS task WHERE {predicate}",
    )
    rows = connection.execute(
        f"""
        SELECT task.execution_id, task.status, task.updated_at
        FROM llm_tasks AS task
        WHERE {predicate}
        ORDER BY task.updated_at ASC, task.execution_id ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    # 仅导出人工定位所需的内部执行身份与状态，严禁导出 business_key、文件名、请求
    # 正文、回调地址或任何可能携带密钥的字段。
    return count, [
        {
            "executionId": str(row["execution_id"]),
            "status": str(row["status"]),
            "updatedAt": str(row["updated_at"]),
        }
        for row in rows
    ]


def _legacy_recoverable_callbacks(
    connection: sqlite3.Connection,
    *,
    limit: int,
) -> tuple[int, list[dict[str, Any]]]:
    """读取切换后无法由新 Guard 链识别的历史终态待恢复回调。"""

    predicate = """
        task.business_type = 'file'
        AND task.status IN ('2', '3')
        AND task.callback_status IN ('pending', 'failed')
        AND NOT EXISTS (
            SELECT 1
            FROM llm_task_executions AS execution
            WHERE execution.execution_id = task.execution_id
              AND execution.business_type = 'file'
              AND execution.batch_id IS NOT NULL
              AND execution.batch_sequence IS NOT NULL
        )
    """
    count = _count(
        connection,
        f"SELECT COUNT(*) FROM llm_tasks AS task WHERE {predicate}",
    )
    rows = connection.execute(
        f"""
        SELECT task.execution_id, task.status, task.callback_status, task.updated_at
        FROM llm_tasks AS task
        WHERE {predicate}
        ORDER BY task.updated_at ASC, task.execution_id ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return count, [
        {
            "executionId": str(row["execution_id"]),
            "status": str(row["status"]),
            "callbackStatus": str(row["callback_status"]),
            "updatedAt": str(row["updated_at"]),
        }
        for row in rows
    ]


def _new_execution_backlog(
    connection: sqlite3.Connection,
    *,
    execution_state: str,
    limit: int,
) -> tuple[int, list[dict[str, Any]]]:
    """读取具有完整批次身份的新 execution 积压，不导出业务键或输入快照。"""

    if execution_state not in {"accepted", "running"}:
        raise ValueError("execution_state 只能是 accepted 或 running")
    predicate = """
        business_type = 'file'
        AND batch_id IS NOT NULL
        AND batch_sequence IS NOT NULL
        AND execution_state = ?
    """
    count = _count(
        connection,
        f"SELECT COUNT(*) FROM llm_task_executions WHERE {predicate}",
        (execution_state,),
    )
    rows = connection.execute(
        f"""
        SELECT execution_id, execution_state, updated_at
        FROM llm_task_executions
        WHERE {predicate}
        ORDER BY updated_at ASC, execution_id ASC
        LIMIT ?
        """,
        (execution_state, limit),
    ).fetchall()
    return count, [
        {
            "executionId": str(row["execution_id"]),
            "executionState": str(row["execution_state"]),
            "updatedAt": str(row["updated_at"]),
        }
        for row in rows
    ]


def _blocking_callback_guards(
    connection: sqlite3.Connection,
    *,
    limit: int,
) -> tuple[int, list[dict[str, Any]]]:
    """读取处于 sending 或 outcome_unknown 的 file Callback Guard。"""

    predicate = """
        business_type = 'file'
        AND state IN ('sending', 'outcome_unknown')
    """
    count = _count(
        connection,
        f"SELECT COUNT(*) FROM callback_delivery_guards WHERE {predicate}",
    )
    rows = connection.execute(
        f"""
        SELECT owner_execution_id, state, lease_version, updated_at
        FROM callback_delivery_guards
        WHERE {predicate}
        ORDER BY updated_at ASC, owner_execution_id ASC, lease_version ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return count, [
        {
            "ownerExecutionId": str(row["owner_execution_id"] or ""),
            "state": str(row["state"]),
            "leaseVersion": int(row["lease_version"]),
            "updatedAt": str(row["updated_at"]),
        }
        for row in rows
    ]


def _open_legacy_rag_leases(
    connection: sqlite3.Connection,
    *,
    limit: int,
) -> tuple[int, list[dict[str, Any]]]:
    """读取未形成已关闭事实的 file RAG 资源租约。

    1F-5B 切换前不存在允许与旧 Worker 并行持有的 file 资源租约，因此所有 open 状态
    都按旧残留阻断。对 ``audit_failed``/``audited`` 同样阻断，防止把“已审计但尚未关闭”
    错当成无需清理。
    """

    placeholders = ", ".join("?" for _ in _OPEN_RAG_LEASE_STATUSES)
    predicate = f"""
        business_type = 'file'
        AND status IN ({placeholders})
    """
    count = _count(
        connection,
        "SELECT COUNT(*) FROM rag_resource_leases WHERE " + predicate,
        _OPEN_RAG_LEASE_STATUSES,
    )
    rows = connection.execute(
        f"""
        SELECT execution_id, status, updated_at
        FROM rag_resource_leases
        WHERE {predicate}
        ORDER BY updated_at ASC, execution_id ASC
        LIMIT ?
        """,
        (*_OPEN_RAG_LEASE_STATUSES, limit),
    ).fetchall()
    return count, [
        {
            "executionId": str(row["execution_id"]),
            "status": str(row["status"]),
            "updatedAt": str(row["updated_at"]),
        }
        for row in rows
    ]


def inspect_analysis_cutover(
    *,
    database_path: Path,
    limit: int = _DEFAULT_LIMIT,
) -> dict[str, Any]:
    """构造可供部署门禁消费的只读预检结果。"""

    if not 1 <= limit <= _MAX_LIMIT:
        raise ValueError(f"limit 必须在 1 到 {_MAX_LIMIT} 之间")

    # sqlite3.Connection 自带的上下文管理器只提交/回滚事务，并不会关闭句柄。预检
    # 可能在部署进程内作为库函数调用，因此必须显式 closing，避免 Windows 下阻塞
    # 数据库备份、重命名或版本替换。
    with closing(_open_read_only_connection(database_path)) as connection:
        _require_schema(connection)
        legacy_task_count, legacy_tasks = _legacy_active_tasks(connection, limit=limit)
        legacy_callback_count, legacy_callbacks = _legacy_recoverable_callbacks(
            connection,
            limit=limit,
        )
        accepted_count, accepted_executions = _new_execution_backlog(
            connection,
            execution_state="accepted",
            limit=limit,
        )
        running_count, running_executions = _new_execution_backlog(
            connection,
            execution_state="running",
            limit=limit,
        )
        callback_guard_count, callback_guards = _blocking_callback_guards(
            connection,
            limit=limit,
        )
        open_lease_count, open_leases = _open_legacy_rag_leases(
            connection,
            limit=limit,
        )

    counts = {
        "legacyActiveTasks": legacy_task_count,
        "legacyRecoverableCallbacks": legacy_callback_count,
        "newRunningExecutions": running_count,
        "callbackGuardBlockers": callback_guard_count,
        "openLegacyRagLeases": open_lease_count,
    }
    observations = {
        # accepted 尚未执行外部副作用，可在明确的新 Dispatcher owner 启动后继续消费；
        # 因此单独观测而不自动阻断。running 可能已经执行外部动作且 1F 不自动接管，
        # 必须作为硬阻断项。
        "newAcceptedExecutions": accepted_count,
    }
    ready = not any(counts.values())
    logger.info(
        "Analysis 切换只读预检完成: ready=%s legacy_active_tasks=%s "
        "legacy_recoverable_callbacks=%s new_accepted_executions=%s "
        "new_running_executions=%s callback_guard_blockers=%s "
        "open_legacy_rag_leases=%s",
        ready,
        legacy_task_count,
        legacy_callback_count,
        accepted_count,
        running_count,
        callback_guard_count,
        open_lease_count,
    )
    return {
        "mode": "dry_run",
        "ready": ready,
        "counts": counts,
        "observations": observations,
        "legacyActiveTasks": legacy_tasks,
        "legacyRecoverableCallbacks": legacy_callbacks,
        "newAcceptedExecutions": accepted_executions,
        "newRunningExecutions": running_executions,
        "callbackGuards": callback_guards,
        "openLegacyRagLeases": open_leases,
        "truncated": {
            "legacyActiveTasks": legacy_task_count > limit,
            "legacyRecoverableCallbacks": legacy_callback_count > limit,
            "newAcceptedExecutions": accepted_count > limit,
            "newRunningExecutions": running_count > limit,
            "callbackGuards": callback_guard_count > limit,
            "openLegacyRagLeases": open_lease_count > limit,
        },
    }


def _parser() -> argparse.ArgumentParser:
    """创建只有 dry-run 的 CLI，避免诊断脚本被误用为批量清理器。"""

    parser = argparse.ArgumentParser(
        description="只读检查 Analysis 1F-5B 路由切换前的历史阻塞项"
    )
    parser.add_argument(
        "--database",
        required=True,
        type=_existing_database_path,
        help="已存在的 SQLite 文件；脚本不会建库或初始化 Schema",
    )
    parser.add_argument(
        "--limit",
        type=_bounded_limit,
        default=_DEFAULT_LIMIT,
        help=f"每类输出的最大条数，范围 1 到 {_MAX_LIMIT}，默认 {_DEFAULT_LIMIT}",
    )
    return parser


def _write_json(payload: dict[str, Any]) -> None:
    """向 stdout 输出唯一机器可读结果，日志始终写入 stderr。"""

    json.dump(payload, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    """执行预检并用稳定退出码表达可切换性。"""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    args = _parser().parse_args(argv)
    try:
        payload = inspect_analysis_cutover(
            database_path=args.database,
            limit=args.limit,
        )
    except (AnalysisCutoverPreflightError, sqlite3.Error, OSError, ValueError) as exc:
        # 不记录数据库路径、业务键、文件名和请求正文，避免运维日志成为敏感信息出口。
        logger.error(
            "Analysis 切换只读预检失败: error_type=%s",
            type(exc).__name__,
        )
        return 1

    _write_json(payload)
    return 0 if payload["ready"] else _EXIT_PRECHECK_BLOCKED


if __name__ == "__main__":  # pragma: no cover - 允许离线命令行直接执行。
    raise SystemExit(main())
