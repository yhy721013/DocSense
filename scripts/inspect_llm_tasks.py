#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)

RUNTIME_DIR = Path(os.getenv("DOCSENSE_RUNTIME_DIR", str(ROOT / ".runtime"))).expanduser()
if not RUNTIME_DIR.is_absolute():
    raise RuntimeError("DOCSENSE_RUNTIME_DIR必须配置为绝对路径")
RUNTIME_DIR = RUNTIME_DIR.resolve()

DEFAULT_DB_PATH = Path(
    os.getenv("DOCSENSE_LLM_TASK_DB", str(RUNTIME_DIR / "llm_tasks.sqlite3"))
).expanduser().resolve()
DEFAULT_OUTPUT_DIR = RUNTIME_DIR / "sqlite"

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export llm_tasks.sqlite3 tables to a readable timestamped JSON file.",
    )
    parser.add_argument(
        "--db-path",
        default=os.getenv("DOCSENSE_LLM_TASK_DB", str(DEFAULT_DB_PATH)),
        help="SQLite 文件路径；默认读取 DOCSENSE_LLM_TASK_DB 或 DOCSENSE_RUNTIME_DIR/llm_tasks.sqlite3。",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="JSON 输出目录；默认 DOCSENSE_RUNTIME_DIR/sqlite，目录不存在时自动创建。",
    )
    return parser.parse_args()


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def parse_json_text(value: str) -> Any:
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def normalize_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {
            "_type": "bytes",
            "base64": base64.b64encode(value).decode("ascii"),
        }
    if isinstance(value, str):
        return parse_json_text(value)
    return value


def read_columns(conn: sqlite3.Connection, object_name: str) -> list[dict[str, Any]]:
    rows = conn.execute(f"PRAGMA table_info({quote_identifier(object_name)})").fetchall()
    return [
        {
            "name": row["name"],
            "type": row["type"],
            "notNull": bool(row["notnull"]),
            "defaultValue": row["dflt_value"],
            "primaryKeyPosition": row["pk"],
        }
        for row in rows
    ]


def order_clause(columns: list[dict[str, Any]]) -> str:
    primary_key_columns = [
        column
        for column in sorted(columns, key=lambda item: item["primaryKeyPosition"])
        if column["primaryKeyPosition"]
    ]
    if not primary_key_columns:
        return ""
    identifiers = ", ".join(quote_identifier(column["name"]) for column in primary_key_columns)
    return f" ORDER BY {identifiers}"


def read_rows(conn: sqlite3.Connection, object_name: str, columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    query = f"SELECT * FROM {quote_identifier(object_name)}{order_clause(columns)}"
    rows = conn.execute(query).fetchall()
    return [
        {key: normalize_value(row[key]) for key in row.keys()}
        for row in rows
    ]


def export_database(db_path: Path, output_dir: Path) -> Path:
    db_path = db_path.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()

    if not db_path.exists():
        raise FileNotFoundError(f"SQLite 文件不存在: {db_path}")
    if not db_path.is_file():
        raise IsADirectoryError(f"SQLite 路径不是文件: {db_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    output_path = output_dir / f"llm_tasks_{timestamp}.json"

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        objects = conn.execute(
            """
            SELECT name, type, sql
            FROM sqlite_master
            WHERE type IN ('table', 'view')
              AND name NOT LIKE 'sqlite_%'
            ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, name
            """
        ).fetchall()

        tables: list[dict[str, Any]] = []
        total_rows = 0
        for database_object in objects:
            name = database_object["name"]
            columns = read_columns(conn, name)
            rows = read_rows(conn, name, columns)
            row_count = len(rows)
            total_rows += row_count
            tables.append(
                {
                    "name": name,
                    "type": database_object["type"],
                    "schemaSql": database_object["sql"],
                    "columns": columns,
                    "rowCount": row_count,
                    "rows": rows,
                }
            )

        payload = {
            "metadata": {
                "exportedAt": datetime.now().astimezone().isoformat(),
                "databasePath": str(db_path),
                "outputPath": str(output_path),
                "sqliteVersion": sqlite3.sqlite_version,
                "objectCount": len(tables),
                "totalRows": total_rows,
            },
            "tables": tables,
        }
    finally:
        conn.close()

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s [%(name)s:%(lineno)d] %(message)s",
    )
    args = parse_args()
    try:
        output_path = export_database(Path(args.db_path), Path(args.output_dir))
    except Exception as exc:  # noqa: BLE001 - script entrypoint should return a concise CLI error.
        logger.error("导出失败: %s", exc)
        return 1

    logger.info("已导出 llm_tasks SQLite 内容到: %s", output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
