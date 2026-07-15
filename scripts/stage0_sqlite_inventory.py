#!/usr/bin/env python3
"""以 SQLite 只读连接生成阶段 0 数据资产清单。

用途是为后续 MySQL 空库建模提供表、列、索引、行数和状态分布事实，不承担数据迁移。
连接使用 ``mode=ro``，并额外设置 ``PRAGMA query_only=ON``；脚本不执行建表、修复、
VACUUM、迁移或任何 DML。输出不包含业务正文，只包含 Schema 元数据和聚合计数。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any, Sequence


LOGGER = logging.getLogger("docsense.stage0_sqlite_inventory")
STATUS_COLUMN_NAMES = frozenset(
    {
        "status",
        "callback_status",
        "state",
        "run_state",
        "cleanup_state",
        "delivery_outcome",
    }
)


def _quote_identifier(identifier: str) -> str:
    """安全引用来自 sqlite_master 的标识符，避免特殊字符破坏只读查询。"""

    return '"' + identifier.replace('"', '""') + '"'


def _pragma_scalar(connection: sqlite3.Connection, name: str) -> Any:
    row = connection.execute(f"PRAGMA {name}").fetchone()
    return row[0] if row else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _status_distributions(
    connection: sqlite3.Connection,
    *,
    table_name: str,
    columns: list[dict[str, Any]],
    max_values: int,
) -> dict[str, list[dict[str, Any]]]:
    """只对状态类列做 GROUP BY，避免读取或输出任何正文数据。"""

    result: dict[str, list[dict[str, Any]]] = {}
    quoted_table = _quote_identifier(table_name)
    for column in columns:
        column_name = str(column["name"])
        if column_name.lower() not in STATUS_COLUMN_NAMES:
            continue
        quoted_column = _quote_identifier(column_name)
        rows = connection.execute(
            f"SELECT {quoted_column}, COUNT(*) AS item_count "
            f"FROM {quoted_table} GROUP BY {quoted_column} "
            "ORDER BY item_count DESC LIMIT ?",
            (max_values,),
        ).fetchall()
        result[column_name] = [
            {"value": row[0], "count": int(row[1])} for row in rows
        ]
    return result


def _inspect_table(
    connection: sqlite3.Connection,
    table_name: str,
    *,
    include_row_counts: bool,
    max_status_values: int,
) -> dict[str, Any]:
    quoted_table = _quote_identifier(table_name)
    column_rows = connection.execute(f"PRAGMA table_xinfo({quoted_table})").fetchall()
    columns = [
        {
            "position": int(row[0]),
            "name": row[1],
            "declaredType": row[2],
            "notNull": bool(row[3]),
            "default": row[4],
            "primaryKeyPosition": int(row[5]),
            "hidden": int(row[6]),
        }
        for row in column_rows
    ]
    index_rows = connection.execute(f"PRAGMA index_list({quoted_table})").fetchall()
    indexes = [
        {
            "name": row[1],
            "unique": bool(row[2]),
            "origin": row[3],
            "partial": bool(row[4]),
        }
        for row in index_rows
    ]
    foreign_key_rows = connection.execute(
        f"PRAGMA foreign_key_list({quoted_table})"
    ).fetchall()
    foreign_keys = [
        {
            "id": int(row[0]),
            "sequence": int(row[1]),
            "targetTable": row[2],
            "fromColumn": row[3],
            "toColumn": row[4],
            "onUpdate": row[5],
            "onDelete": row[6],
        }
        for row in foreign_key_rows
    ]

    table: dict[str, Any] = {
        "name": table_name,
        "columns": columns,
        "indexes": indexes,
        "foreignKeys": foreign_keys,
        "statusDistributions": _status_distributions(
            connection,
            table_name=table_name,
            columns=columns,
            max_values=max_status_values,
        ),
    }
    if include_row_counts:
        table["rowCount"] = int(
            connection.execute(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()[0]
        )
    return table


def inspect_database(
    database_path: Path,
    *,
    include_row_counts: bool = True,
    include_hash: bool = False,
    integrity_check: bool = False,
    max_status_values: int = 50,
) -> dict[str, Any]:
    """盘点单个数据库；路径不存在或不是普通文件时立即失败。"""

    resolved = database_path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"不是普通数据库文件: {resolved}")
    stat_before = resolved.stat()
    # Path.as_uri 已正确处理 Windows 盘符和空格；追加 mode=ro 从连接层禁止写入。
    connection = sqlite3.connect(
        f"{resolved.as_uri()}?mode=ro",
        uri=True,
        timeout=5,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        object_rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        object_counts: dict[str, int] = {}
        objects: list[dict[str, Any]] = []
        for row in object_rows:
            object_type = str(row["type"])
            object_counts[object_type] = object_counts.get(object_type, 0) + 1
            objects.append(
                {
                    "type": object_type,
                    "name": row["name"],
                    "table": row["tbl_name"],
                    # 只输出规范化 DDL，便于发现约束；DDL 不含业务记录。
                    "sql": row["sql"],
                }
            )
        table_names = [
            str(row["name"]) for row in object_rows if row["type"] == "table"
        ]
        result: dict[str, Any] = {
            "path": str(resolved),
            "fileBytes": int(stat_before.st_size),
            "sqliteVersion": sqlite3.sqlite_version,
            "queryOnly": bool(_pragma_scalar(connection, "query_only")),
            "userVersion": int(_pragma_scalar(connection, "user_version") or 0),
            "applicationId": int(_pragma_scalar(connection, "application_id") or 0),
            "pageSize": int(_pragma_scalar(connection, "page_size") or 0),
            "pageCount": int(_pragma_scalar(connection, "page_count") or 0),
            "freeListCount": int(_pragma_scalar(connection, "freelist_count") or 0),
            "journalMode": _pragma_scalar(connection, "journal_mode"),
            "objectCounts": object_counts,
            "objects": objects,
            "tables": [
                _inspect_table(
                    connection,
                    name,
                    include_row_counts=include_row_counts,
                    max_status_values=max_status_values,
                )
                for name in table_names
            ],
        }
        if include_hash:
            result["sha256"] = _sha256(resolved)
        if integrity_check:
            result["integrityCheck"] = [
                row[0] for row in connection.execute("PRAGMA integrity_check").fetchall()
            ]
    finally:
        connection.close()

    stat_after = resolved.stat()
    result["fileUnchangedDuringInspection"] = (
        stat_before.st_size == stat_after.st_size
        and stat_before.st_mtime_ns == stat_after.st_mtime_ns
    )
    if not result["fileUnchangedDuringInspection"]:
        LOGGER.warning(
            "盘点期间数据库文件发生外部变化: database=%s",
            resolved.name,
        )
    return result


def _to_summary(inventory: dict[str, Any]) -> dict[str, Any]:
    """生成适合阶段记录引用的紧凑摘要，仍保留状态聚合和列名。"""

    return {
        key: inventory[key]
        for key in (
            "path",
            "fileBytes",
            "sha256",
            "sqliteVersion",
            "queryOnly",
            "userVersion",
            "applicationId",
            "pageSize",
            "pageCount",
            "freeListCount",
            "journalMode",
            "objectCounts",
            "fileUnchangedDuringInspection",
            "integrityCheck",
        )
        if key in inventory
    } | {
        "tables": [
            {
                "name": table["name"],
                "rowCount": table.get("rowCount"),
                "columns": [column["name"] for column in table["columns"]],
                "indexCount": len(table["indexes"]),
                "foreignKeyCount": len(table["foreignKeys"]),
                "statusDistributions": table["statusDistributions"],
            }
            for table in inventory["tables"]
        ]
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DocSense 阶段 0 SQLite 只读盘点")
    parser.add_argument("databases", nargs="+", type=Path)
    parser.add_argument("--skip-row-counts", action="store_true")
    parser.add_argument("--hash", action="store_true", dest="include_hash")
    parser.add_argument("--integrity-check", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--max-status-values", type=int, default=50)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = build_parser().parse_args(argv)
    if not 1 <= args.max_status_values <= 1000:
        print("--max-status-values 必须位于 1..1000", file=sys.stderr)
        return 2
    inventories: list[dict[str, Any]] = []
    try:
        for path in args.databases:
            LOGGER.info("开始只读盘点 SQLite: database=%s", path.name)
            inventory = inspect_database(
                path,
                include_row_counts=not args.skip_row_counts,
                include_hash=args.include_hash,
                integrity_check=args.integrity_check,
                max_status_values=args.max_status_values,
            )
            inventories.append(_to_summary(inventory) if args.summary else inventory)
            LOGGER.info(
                "SQLite 盘点完成: database=%s tables=%d bytes=%d unchanged=%s",
                path.name,
                len(inventory["tables"]),
                inventory["fileBytes"],
                inventory["fileUnchangedDuringInspection"],
            )
    except (OSError, sqlite3.Error, ValueError) as exc:
        LOGGER.error("SQLite 盘点失败: error_type=%s", type(exc).__name__)
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({"databases": inventories}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
