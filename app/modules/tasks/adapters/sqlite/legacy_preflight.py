"""阶段 2 Task Control 切换前的旧 SQLite 只读预检实现。

本工具只输出表级状态计数和路径摘要，不输出 TaskId、业务键、URL、正文或任何租约秘密。
它不会创建、迁移、修复、归档或删除数据库，也不会导入会初始化旧 Store 的项目配置模块。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any


CONTRACT_PATH = Path(__file__).with_name("database_contract.json")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_AUXILIARY_SUFFIXES = ("-wal", "-shm", "-journal")

logger = logging.getLogger(__name__)


class PreflightInputError(ValueError):
    """表示路径、契约或离线快照参数不满足 fail-closed 前提。"""


def _quote_identifier(value: str) -> str:
    """仅允许契约内的简单 SQLite 标识符，避免把资产内容解释成任意 SQL。"""

    if not _IDENTIFIER_PATTERN.fullmatch(value):
        raise PreflightInputError(f"非法 SQLite 标识符: {value!r}")
    return f'"{value}"'


def _resolved_path(value: str | Path, *, must_exist: bool) -> Path:
    """统一解析绝对路径；旧库必须真实存在，新库仅做身份比较而不创建。"""

    path = Path(value).expanduser().resolve(strict=must_exist)
    if must_exist and not path.is_file():
        raise PreflightInputError("旧 Task SQLite 路径不存在或不是普通文件")
    return path


def _path_identity(path: Path) -> str:
    """按当前平台规则比较路径，Windows 下自动折叠大小写差异。"""

    return os.path.normcase(os.path.realpath(path))


def _path_fingerprint(path: Path) -> str:
    """日志只记录路径摘要，避免暴露部署目录和用户名。"""

    material = _path_identity(path).encode("utf-8")
    return hashlib.sha256(material).hexdigest().upper()


def _file_set_snapshot(database_path: Path) -> dict[str, dict[str, int | bool]]:
    """采集主库及侧车文件元数据，用于证明只读检查前后没有改写文件集。"""

    candidates = {"sqlite3": database_path}
    candidates.update(
        {suffix.removeprefix("-"): Path(f"{database_path}{suffix}") for suffix in _AUXILIARY_SUFFIXES}
    )
    snapshot: dict[str, dict[str, int | bool]] = {}
    for name, path in candidates.items():
        if not path.exists():
            snapshot[name] = {"exists": False, "size": 0, "mtimeNs": 0}
            continue
        stat = path.stat()
        snapshot[name] = {
            "exists": True,
            "size": stat.st_size,
            "mtimeNs": stat.st_mtime_ns,
        }
    return snapshot


def _connect_read_only(path: Path, *, immutable_offline_snapshot: bool) -> sqlite3.Connection:
    """以 SQLite URI 只读打开旧库，绝不退化为普通可写连接。

    WAL 模式数据库在只读目录中可能需要创建共享内存侧车。只有运维已确认所有写进程停止、
    且现场不存在 WAL/SHM 时，才允许使用 immutable 离线快照模式；该模式不会触碰源文件集。
    """

    query = "mode=ro"
    if immutable_offline_snapshot:
        query += "&immutable=1"
    connection = sqlite3.connect(
        f"{path.as_uri()}?{query}",
        uri=True,
        timeout=2.0,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    table_name = _quote_identifier(table)
    return {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def _count_unknown_values(
    connection: sqlite3.Connection,
    *,
    table: str,
    column: str,
    allowed: list[str],
) -> int:
    """未知枚举按不兼容旧 Schema 处理，不能被误判成“无活动任务”。"""

    placeholders = ", ".join("?" for _ in allowed)
    table_name = _quote_identifier(table)
    column_name = _quote_identifier(column)
    sql = (
        f"SELECT COUNT(*) FROM {table_name} "
        f"WHERE {column_name} IS NULL OR CAST({column_name} AS TEXT) NOT IN ({placeholders})"
    )
    return int(connection.execute(sql, allowed).fetchone()[0])


def _count_rule(connection: sqlite3.Connection, rule: dict[str, Any]) -> int:
    """把有限的结构化规则编译为参数化 COUNT，不接受资产中的原始 SQL。"""

    table_name = _quote_identifier(str(rule["table"]))
    column_name = _quote_identifier(str(rule["column"]))
    values = [str(value) for value in rule["values"]]
    placeholders = ", ".join("?" for _ in values)
    operator = str(rule["operator"])
    if operator == "in":
        predicates = [f"CAST({column_name} AS TEXT) IN ({placeholders})"]
    elif operator == "not_in":
        predicates = [
            f"({column_name} IS NULL OR CAST({column_name} AS TEXT) NOT IN ({placeholders}))"
        ]
    else:
        raise PreflightInputError(f"不支持的预检操作符: {operator!r}")
    parameters: list[str] = list(values)

    for filter_column, filter_values_raw in dict(rule.get("filters", {})).items():
        filter_name = _quote_identifier(str(filter_column))
        filter_values = [str(value) for value in filter_values_raw]
        filter_placeholders = ", ".join("?" for _ in filter_values)
        predicates.append(
            f"CAST({filter_name} AS TEXT) IN ({filter_placeholders})"
        )
        parameters.extend(filter_values)

    sql = f"SELECT COUNT(*) FROM {table_name} WHERE " + " AND ".join(predicates)
    return int(connection.execute(sql, parameters).fetchone()[0])


def inspect_old_database(
    old_database_path: str | Path,
    new_database_path: str | Path,
    *,
    immutable_offline_snapshot: bool = False,
    writers_stopped_confirmed: bool = False,
) -> dict[str, Any]:
    """执行旧库只读预检并返回不含行身份的结构化诊断。"""

    old_path = _resolved_path(old_database_path, must_exist=True)
    new_path = _resolved_path(new_database_path, must_exist=False)
    if _path_identity(old_path) == _path_identity(new_path):
        raise PreflightInputError("旧 Task DB 与 v2 Task Control DB 解析后路径相同")
    if immutable_offline_snapshot and not writers_stopped_confirmed:
        raise PreflightInputError("immutable 离线快照模式必须显式确认所有相关写进程已停止")

    before = _file_set_snapshot(old_path)
    if immutable_offline_snapshot and (
        before["wal"]["exists"] or before["shm"]["exists"]
    ):
        raise PreflightInputError("存在 WAL/SHM 时禁止使用 immutable 离线快照模式")

    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    preflight_contract = contract["oldDatabasePreflight"]
    schema_errors: list[dict[str, Any]] = []
    rule_results: list[dict[str, Any]] = []

    connection = _connect_read_only(
        old_path,
        immutable_offline_snapshot=immutable_offline_snapshot,
    )
    try:
        quick_check = str(connection.execute("PRAGMA quick_check(1)").fetchone()[0])
        if quick_check != "ok":
            schema_errors.append({"code": "quick_check_failed", "detail": quick_check})

        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        columns_by_table: dict[str, set[str]] = {}
        for domain in preflight_contract["stateDomains"]:
            table = str(domain["table"])
            column = str(domain["column"])
            if table not in tables:
                continue
            columns = columns_by_table.setdefault(
                table,
                _table_columns(connection, table),
            )
            if column not in columns:
                schema_errors.append(
                    {
                        "code": "required_state_column_missing",
                        "table": table,
                        "column": column,
                    }
                )
                continue
            unknown_count = _count_unknown_values(
                connection,
                table=table,
                column=column,
                allowed=[str(value) for value in domain["allowed"]],
            )
            if unknown_count:
                schema_errors.append(
                    {
                        "code": "unknown_state_value",
                        "table": table,
                        "column": column,
                        "count": unknown_count,
                    }
                )

        for rule in preflight_contract["blockerRules"]:
            table = str(rule["table"])
            column = str(rule["column"])
            if table not in tables:
                continue
            columns = columns_by_table.setdefault(
                table,
                _table_columns(connection, table),
            )
            required_columns = {column, *dict(rule.get("filters", {})).keys()}
            missing_columns = sorted(required_columns - columns)
            if missing_columns:
                schema_errors.append(
                    {
                        "code": "blocker_rule_column_missing",
                        "ruleId": str(rule["id"]),
                        "columns": missing_columns,
                    }
                )
                continue
            count = _count_rule(connection, rule)
            rule_results.append(
                {
                    "ruleId": str(rule["id"]),
                    "category": str(rule["category"]),
                    "count": count,
                }
            )
    finally:
        connection.close()

    after = _file_set_snapshot(old_path)
    if before != after:
        schema_errors.append({"code": "source_file_set_changed_during_preflight"})

    blocker_count = sum(item["count"] for item in rule_results)
    if schema_errors:
        status = "invalid_fail_closed"
    elif blocker_count:
        status = "blocked_facts_require_reconciliation"
    else:
        status = "safe_for_empty_v2_initialization"

    return {
        "contractAssetVersion": contract["assetVersion"],
        "status": status,
        "oldPathSha256": _path_fingerprint(old_path),
        "newPathSha256": _path_fingerprint(new_path),
        "newPathExists": new_path.exists(),
        "readMode": (
            "sqlite_uri_mode_ro_immutable_offline"
            if immutable_offline_snapshot
            else "sqlite_uri_mode_ro"
        ),
        "sourceFileSet": after,
        "blockerCount": blocker_count,
        "blockerRules": rule_results,
        "schemaErrors": schema_errors,
        "rowIdentityIncluded": False,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="只读检查旧 Task SQLite 是否仍有阶段 2 切换阻塞事实。",
    )
    parser.add_argument("--old-db-path", required=True, help="现有 LLM Task SQLite 路径。")
    parser.add_argument("--new-db-path", required=True, help="计划使用的 v2 Task Control 路径。")
    parser.add_argument(
        "--immutable-offline-snapshot",
        action="store_true",
        help="只用于无 WAL/SHM 且全部写进程已停止的只读目录。",
    )
    parser.add_argument(
        "--writers-stopped-confirmed",
        action="store_true",
        help="运维显式确认所有可能写旧库的进程均已停止。",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s [%(name)s:%(lineno)d] %(message)s",
    )
    args = _parse_args()
    try:
        result = inspect_old_database(
            args.old_db_path,
            args.new_db_path,
            immutable_offline_snapshot=args.immutable_offline_snapshot,
            writers_stopped_confirmed=args.writers_stopped_confirmed,
        )
    except (OSError, sqlite3.Error, json.JSONDecodeError, PreflightInputError) as exc:
        logger.error("阶段 2 旧库预检拒绝: error_type=%s reason=%s", type(exc).__name__, exc)
        return 3

    logger.info(
        "阶段 2 旧库预检完成: status=%s old_path_sha256=%s blocker_count=%d",
        result["status"],
        result["oldPathSha256"],
        result["blockerCount"],
    )
    # 预检结果是供人工和自动化消费的标准输出，不属于运行日志。这里显式写入 stdout，
    # 既保留原有 JSON 文本与末尾换行，也遵守项目脚本禁止直接调用 print 的长期门禁。
    sys.stdout.write(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    if result["status"] == "safe_for_empty_v2_initialization":
        return 0
    if result["status"] == "blocked_facts_require_reconciliation":
        return 2
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
