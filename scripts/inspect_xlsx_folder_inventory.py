"""只读盘点 AnythingLLM XLSX Collector Folder 与本地永久知识引用。

本工具只调用 AnythingLLM 的 GET 接口，并以 SQLite ``mode=ro`` + ``query_only`` 读取
``documents.doc_path``。它没有 apply/delete 参数，不签发 Cleanup Token，也不输出 Folder 名、
Sheet 名、API Key、Base URL 或业务文件名。任何自动清理仍只能由上传失败链基于同一次可信响应
签发完整成员 Token 后执行；历史永久知识 Folder 在本工具中永远只报告、不删除。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Sequence

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env", override=False)

from app.integrations.anythingllm.documents import (  # noqa: E402
    AnythingLLMDocumentClient,
    XlsxFolderInventoryItem,
)
from app.integrations.anythingllm.models import (  # noqa: E402
    parse_xlsx_sheet_location,
)
from app.integrations.anythingllm.transport import (  # noqa: E402
    AnythingLLMTransport,
)
from app.services.core.config import load_anythingllm_config  # noqa: E402


logger = logging.getLogger("scripts.inspect_xlsx_folder_inventory")


class XlsxFolderInventoryError(RuntimeError):
    """表示只读库存所需的远端或本地事实不完整。"""


def load_committed_xlsx_locations(database_path: Path) -> tuple[str, ...]:
    """只读加载已提交永久知识中的 XLSX Sheet 位置，不初始化或迁移 Schema。"""

    path = database_path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"知识库数据库不存在或不是文件: {path}")
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro",
        uri=True,
        timeout=5.0,
    )
    try:
        connection.execute("PRAGMA query_only = ON")
        rows = connection.execute(
            "SELECT doc_path FROM documents ORDER BY doc_path ASC"
        ).fetchall()
    except sqlite3.Error as exc:
        raise XlsxFolderInventoryError(
            "无法只读加载永久知识文档位置"
        ) from exc
    finally:
        connection.close()

    locations = {
        parsed[0]
        for row in rows
        if (parsed := parse_xlsx_sheet_location(str(row[0] or ""))) is not None
    }
    return tuple(sorted(locations))


def _folder_hash(folder_name: str) -> str:
    """返回可跨次比对、但不暴露远端 Folder 名的稳定摘要。"""

    return hashlib.sha256(folder_name.encode("utf-8")).hexdigest()[:16]


def build_inventory_report(
    inventory: Sequence[XlsxFolderInventoryItem],
    committed_locations: Sequence[str],
) -> dict[str, Any]:
    """把远端库存与本地提交事实合并为无敏感名称的只读治理报告。"""

    remote_by_folder = {
        item.folder_name: frozenset(item.member_locations) for item in inventory
    }
    if len(remote_by_folder) != len(tuple(inventory)):
        raise XlsxFolderInventoryError("远端库存包含重复 Folder 身份")

    committed_by_folder: dict[str, set[str]] = {}
    for location in committed_locations:
        parsed = parse_xlsx_sheet_location(location)
        if parsed is None:
            raise XlsxFolderInventoryError("本地提交位置包含非法 XLSX Sheet 身份")
        committed_by_folder.setdefault(parsed[1], set()).add(parsed[0])

    rows: list[dict[str, Any]] = []
    counters = {
        "committedProtectedFolderCount": 0,
        "committedDriftedFolderCount": 0,
        "unreferencedFolderCount": 0,
    }
    for folder_name in sorted(remote_by_folder, key=str.casefold):
        remote_members = remote_by_folder[folder_name]
        committed_members = frozenset(committed_by_folder.get(folder_name, set()))
        if not committed_members:
            state = "unreferenced_requires_ownership_review"
            recommendation = "manual_ownership_review"
            counters["unreferencedFolderCount"] += 1
        elif committed_members == remote_members:
            state = "committed_protected"
            recommendation = "report_only_no_automatic_delete"
            counters["committedProtectedFolderCount"] += 1
        else:
            # 只要本地存在任一永久知识引用，即使成员已经漂移，也必须按保护对象处理。
            state = "committed_drifted_protected"
            recommendation = "report_only_no_automatic_delete"
            counters["committedDriftedFolderCount"] += 1
        rows.append(
            {
                "folderHash": _folder_hash(folder_name),
                "memberCount": len(remote_members),
                "committedMemberCount": len(committed_members),
                "state": state,
                "recommendation": recommendation,
            }
        )

    missing_remote_folders = set(committed_by_folder) - set(remote_by_folder)
    report = {
        "schemaVersion": 1,
        "operation": "read-only-xlsx-folder-inventory",
        "remoteMutation": False,
        "folderCount": len(rows),
        "memberCount": sum(item["memberCount"] for item in rows),
        **counters,
        "missingRemoteCommittedFolderCount": len(missing_remote_folders),
        "attentionRequired": bool(
            counters["committedDriftedFolderCount"]
            or counters["unreferencedFolderCount"]
            or missing_remote_folders
        ),
        "folders": rows,
    }
    return report


def build_parser() -> argparse.ArgumentParser:
    """创建只有只读参数的 CLI；刻意不提供 apply、cleanup 或 delete 入口。"""

    runtime_dir = Path(
        os.getenv("DOCSENSE_RUNTIME_DIR", str(ROOT / ".runtime"))
    ).expanduser()
    parser = argparse.ArgumentParser(
        description="只读盘点 AnythingLLM XLSX Folder 与永久知识引用",
    )
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument(
        "--knowledge-db-path",
        type=Path,
        default=Path(
            os.getenv(
                "DOCSENSE_KNOWLEDGE_BASE_DB",
                os.getenv(
                    "KNOWLEDGE_BASE_DB_PATH",
                    str(runtime_dir / "knowledge_base.sqlite3"),
                ),
            )
        ),
        help="只读知识库 SQLite；用于识别必须保护的已提交 XLSX Folder。",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.user_id < 1:
        raise ValueError("user-id 必须是正整数")

    committed_locations = load_committed_xlsx_locations(
        args.knowledge_db_path
    )
    config = load_anythingllm_config()
    with AnythingLLMTransport(
        base_url=config.base_url,
        api_key=config.api_key,
        timeout=config.timeout,
    ) as transport:
        inventory = AnythingLLMDocumentClient(
            transport
        ).list_xlsx_folder_inventory(user_id=args.user_id)

    report = build_inventory_report(inventory, committed_locations)
    sys.stdout.write(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
    logger.info(
        "XLSX Folder 只读库存治理汇总: folder_count=%d member_count=%d "
        "committed_protected_count=%d committed_drifted_count=%d "
        "unreferenced_count=%d missing_remote_committed_count=%d "
        "attention_required=%s remote_mutation=false",
        report["folderCount"],
        report["memberCount"],
        report["committedProtectedFolderCount"],
        report["committedDriftedFolderCount"],
        report["unreferencedFolderCount"],
        report["missingRemoteCommittedFolderCount"],
        report["attentionRequired"],
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        raise SystemExit(main())
    except Exception as exc:
        logger.error(
            "XLSX Folder 只读库存失败: error_type=%s",
            type(exc).__name__,
        )
        raise
