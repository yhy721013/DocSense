#!/usr/bin/env python3
"""一次性发布全新的阶段 2 Task Control v2 数据库。

本命令只适用于旧数据库完整文件集已经由受控维护动作删除、历史控制面事实明确不再使用的
部署现场。普通应用启动不得调用本入口，也不得根据路径缺失自动推断 fresh。
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.modules.analysis.adapters.sqlite import (
    ANALYSIS_CONTROL_COMPONENT_NAME,
    ANALYSIS_CONTROL_COMPONENT_VERSION,
    load_analysis_control_manifest,
)
from app.modules.report.adapters.sqlite import (
    REPORT_CONTROL_COMPONENT_NAME,
    REPORT_CONTROL_COMPONENT_VERSION,
    load_report_control_manifest,
)
from app.modules.tasks.adapters.sqlite import (
    TaskControlBootstrapError,
    bootstrap_fresh_task_control_database,
)
from app.modules.weaponry.adapters.sqlite import (
    WEAPONRY_CONTROL_COMPONENT_NAME,
    WEAPONRY_CONTROL_COMPONENT_VERSION,
    load_weaponry_control_manifest,
)


logger = logging.getLogger("docsense.task_control_fresh_bootstrap")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "在旧文件集和目标文件集均不存在时，一次性原子发布完整 Task Control v2"
        ),
    )
    parser.add_argument(
        "--legacy-db-path",
        required=True,
        help="已经弃用且完整文件集必须不存在的旧 Task SQLite 主路径",
    )
    parser.add_argument(
        "--new-db-path",
        required=True,
        help="计划原子发布的 Task Control v2 SQLite 主路径",
    )
    parser.add_argument(
        "--confirm-abandon-legacy-task-facts",
        action="store_true",
        help=(
            "明确确认旧 Task/Callback/资源事实已经放弃；缺少该参数时命令失败关闭"
        ),
    )
    parser.add_argument(
        "--busy-timeout-ms",
        type=int,
        default=5_000,
        help="SQLite/Schema 锁等待预算，范围 1..60000，默认 5000",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """执行 fresh 发布；日志只包含错误分类和数据库身份短摘要。"""

    args = _parse_args(argv)
    known_components = {
        REPORT_CONTROL_COMPONENT_NAME: load_report_control_manifest(),
        WEAPONRY_CONTROL_COMPONENT_NAME: load_weaponry_control_manifest(),
        ANALYSIS_CONTROL_COMPONENT_NAME: load_analysis_control_manifest(),
    }
    required_components = {
        REPORT_CONTROL_COMPONENT_NAME: REPORT_CONTROL_COMPONENT_VERSION,
        WEAPONRY_CONTROL_COMPONENT_NAME: WEAPONRY_CONTROL_COMPONENT_VERSION,
        ANALYSIS_CONTROL_COMPONENT_NAME: ANALYSIS_CONTROL_COMPONENT_VERSION,
    }
    try:
        result = bootstrap_fresh_task_control_database(
            args.legacy_db_path,
            args.new_db_path,
            fresh_install_confirmed=args.confirm_abandon_legacy_task_facts,
            busy_timeout_ms=args.busy_timeout_ms,
            known_components=known_components,
            required_components=required_components,
        )
    except (TypeError, ValueError) as exc:
        logger.error(
            "Task Control fresh 初始化参数非法: error_type=%s",
            type(exc).__name__,
        )
        return 3
    except TaskControlBootstrapError as exc:
        logger.error(
            "Task Control fresh 初始化失败: error_code=%s",
            exc.code,
        )
        return 2

    logger.info(
        "Task Control fresh 初始化完成: created=%s "
        "db_instance_uuid_prefix=%s root_fingerprint_prefix=%s components=%d",
        result.created,
        result.identity.db_instance_uuid[:8],
        result.identity.root_fingerprint[:12],
        len(result.identity.registered_components),
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
