#!/usr/bin/env python3
"""武器谱冻结与隔离的显式人工处置工具。

本工具只操作内部 SQLite 事实，不提供新的 HTTP 接口。所有会改变状态的命令都要求
操作者、原因和外部状态确认，并复用 Repository 的事务与追加式审计，避免直接执行
SQL 时遗漏 fencing、活跃 Worker 检查或首次处置证据。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
from typing import Sequence

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env", override=False)

from app.modules.tasks.domain import TaskId  # noqa: E402
from app.modules.weaponry.adapters import (  # noqa: E402
    SQLiteWeaponryResourceStoreAdapter,
)
from app.modules.weaponry.domain import (  # noqa: E402
    WeaponryDomainValidationError,
    normalize_architecture_id_value,
)
from app.services.llm_service.task_service import LLMTaskService  # noqa: E402


logger = logging.getLogger(__name__)


def _default_db_path() -> Path:
    runtime_dir = Path(
        os.getenv("DOCSENSE_RUNTIME_DIR", str(ROOT / ".runtime"))
    ).expanduser()
    return Path(
        os.getenv(
            "DOCSENSE_LLM_TASK_DB",
            str(runtime_dir / "llm_tasks.sqlite3"),
        )
    ).expanduser()


def _existing_database(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"任务数据库不存在或不是文件: {path}")
    return path


def _architecture_business_key(value: str) -> str:
    try:
        return str(normalize_architecture_id_value(value))
    except WeaponryDomainValidationError as exc:
        raise ValueError("architecture-id 必须是合法正整数") from exc


def release_callback_guard(args: argparse.Namespace) -> dict[str, object]:
    """人工解除 outcome_unknown 门禁，但保留旧 execution 的未知投递事实。"""

    database = _existing_database(args.db_path)
    business_key = _architecture_business_key(args.architecture_id)
    outcome = LLMTaskService(str(database)).release_callback_delivery_guard(
        business_type="weaponry",
        business_key=business_key,
        released_by=args.operator,
        release_reason=args.reason,
        worker_stopped_confirmed=args.worker_stopped_confirmed,
        released_at=datetime.now(timezone.utc).isoformat(),
    )
    return {
        "action": "release-callback",
        "architectureId": business_key,
        "outcome": outcome,
    }


def resolve_resources(args: argparse.Namespace) -> dict[str, object]:
    """在远端人工对账后解除资源隔离，由恢复循环继续清理或确认已清理。"""

    database = _existing_database(args.db_path)
    store = SQLiteWeaponryResourceStoreAdapter(str(database))
    record = store.resolve_quarantine(
        TaskId(args.task_id),
        action=args.resolution,
        resolved_by=args.operator,
        reason=args.reason,
        external_state_confirmed=args.external_state_confirmed,
    )
    return {
        "action": "resolve-resources",
        "taskId": record.task_id.value,
        "state": record.state.value,
        "version": record.version,
        "resolution": args.resolution,
    }


def inspect_resources(args: argparse.Namespace) -> dict[str, object]:
    """输出不含供应商引用和业务正文的资源状态及人工处置审计。"""

    database = _existing_database(args.db_path)
    store = SQLiteWeaponryResourceStoreAdapter(str(database))
    task_id = TaskId(args.task_id)
    record = store.get(task_id)
    if record is None:
        return {
            "action": "inspect-resources",
            "taskId": task_id.value,
            "found": False,
        }
    owned = tuple(
        item for item in record.resources if item.ownership.value == "owned"
    )
    states: dict[str, int] = {}
    for item in owned:
        states[item.state.value] = states.get(item.state.value, 0) + 1
    return {
        "action": "inspect-resources",
        "taskId": task_id.value,
        "found": True,
        "state": record.state.value,
        "version": record.version,
        "ownedResourceCount": len(owned),
        "ownedResourceStates": states,
        "lastErrorCode": record.last_error_code,
        "lastErrorMessage": record.last_error_message,
        "operatorAudits": list(store.list_operator_audits(task_id)),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="安全处置武器谱 callback unknown 与资源 quarantine。",
    )
    parser.add_argument(
        "--db-path",
        default=str(_default_db_path()),
        help="任务 SQLite 路径；默认读取 DOCSENSE_LLM_TASK_DB。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    callback = subparsers.add_parser(
        "release-callback",
        help="确认旧 Worker 已停止后解除 callback outcome_unknown 门禁。",
    )
    callback.add_argument("--architecture-id", required=True)
    callback.add_argument("--operator", required=True)
    callback.add_argument("--reason", required=True)
    callback.add_argument(
        "--worker-stopped-confirmed",
        action="store_true",
        help="显式确认旧 Worker 已停止或隔离；缺少此标志时命令拒绝执行。",
    )
    callback.set_defaults(handler=release_callback_guard)

    resources = subparsers.add_parser(
        "resolve-resources",
        help="人工对账后解除 quarantined 资源记录。",
    )
    resources.add_argument("--task-id", required=True)
    resources.add_argument(
        "--resolution",
        choices=("retry_cleanup", "confirmed_absent"),
        required=True,
    )
    resources.add_argument("--operator", required=True)
    resources.add_argument("--reason", required=True)
    resources.add_argument(
        "--external-state-confirmed",
        action="store_true",
        help="显式确认已核对供应商端状态；缺少此标志时命令拒绝执行。",
    )
    resources.set_defaults(handler=resolve_resources)

    inspect = subparsers.add_parser(
        "inspect-resources",
        help="读取脱敏资源状态与处置审计，不改变任何状态。",
    )
    inspect.add_argument("--task-id", required=True)
    inspect.set_defaults(handler=inspect_resources)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s [%(name)s:%(lineno)d] %(message)s",
    )
    args = build_parser().parse_args(argv)
    try:
        result = args.handler(args)
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        logger.error("武器谱人工处置失败: command=%s error=%s", args.command, exc)
        sys.stdout.write(
            json.dumps(
                {"command": args.command, "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        return 1
    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
