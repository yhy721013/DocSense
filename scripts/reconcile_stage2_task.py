#!/usr/bin/env python3
"""阶段 2-7 Task Recovery 默认只读诊断与严格内部处置入口。

本脚本不执行 SQL、不访问供应商，也不提供“承担重复风险后强制重跑”。写模式先以完整
expected 身份领取 Recovery Case，再由业务 Policy 和 Store fencing 决定是否允许收敛。
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import sys
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.modules.analysis.adapters.sqlite import (  # noqa: E402
    ANALYSIS_CONTROL_COMPONENT_NAME,
    ANALYSIS_CONTROL_COMPONENT_VERSION,
    load_analysis_control_manifest,
)
from app.modules.analysis.adapters.sqlite.recovery_finalization import (  # noqa: E402
    SQLiteAnalysisRecoveryFinalizationPreflight,
)
from app.modules.analysis.adapters.sqlite.recovery_resume import (  # noqa: E402
    SQLiteAnalysisRecoveryResumePreflight,
)
from app.modules.analysis.application import AnalysisTaskRecoveryPolicy  # noqa: E402
from app.modules.report.adapters.sqlite import (  # noqa: E402
    REPORT_CONTROL_COMPONENT_NAME,
    REPORT_CONTROL_COMPONENT_VERSION,
    load_report_control_manifest,
)
from app.modules.report.adapters.sqlite.recovery_finalization import (  # noqa: E402
    SQLiteReportRecoveryFinalizationPreflight,
)
from app.modules.report.adapters.sqlite.recovery_resume import (  # noqa: E402
    SQLiteReportRecoveryResumePreflight,
)
from app.modules.report.application import ReportTaskRecoveryPolicy  # noqa: E402
from app.modules.tasks.adapters import (  # noqa: E402
    SecureTaskLeaseTokenFactory,
    SystemSafeClock,
)
from app.modules.tasks.adapters.recovery_finalization import (  # noqa: E402
    RoutedTaskRecoveryFinalizationPreflight,
)
from app.modules.tasks.adapters.recovery_resume import (  # noqa: E402
    RoutedTaskRecoveryResumePreflight,
)
from app.modules.tasks.adapters.sqlite import (  # noqa: E402
    SQLiteConnectionFactory,
    SQLiteTransactionManager,
    build_sqlite_task_control_uow_factories,
    validate_existing_task_control_database,
)
from app.modules.tasks.application import (  # noqa: E402
    RecoveryCoordinator,
    RecoveryOperatorAction,
    RecoveryOperatorService,
    StrictRecoveryDecisionCommand,
)
from app.modules.tasks.domain import TaskId, TaskState  # noqa: E402
from app.modules.weaponry.adapters.sqlite import (  # noqa: E402
    WEAPONRY_CONTROL_COMPONENT_NAME,
    WEAPONRY_CONTROL_COMPONENT_VERSION,
    load_weaponry_control_manifest,
)
from app.modules.weaponry.adapters.sqlite.recovery_finalization import (  # noqa: E402
    SQLiteWeaponryRecoveryFinalizationPreflight,
)
from app.modules.weaponry.adapters.sqlite.recovery_resume import (  # noqa: E402
    SQLiteWeaponryRecoveryResumePreflight,
)
from app.modules.weaponry.application import WeaponryTaskRecoveryPolicy  # noqa: E402


def _runtime_seconds(name: str, default: float, *, allow_zero: bool = False) -> float:
    """按 Task Runtime 同名配置读取有限秒数，禁止运维脚本悄悄采用宽松时钟。"""

    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是有限数字") from exc
    if (
        not math.isfinite(value)
        or value < 0
        or (value == 0 and not allow_zero)
    ):
        raise ValueError(f"{name} 必须是{'非负' if allow_zero else '正'}有限数字")
    return value


def _safe_clock() -> SystemSafeClock:
    """创建与生产 Task Runtime 使用同一 jitter 配置名的失败关闭时钟。"""

    return SystemSafeClock(
        max_jitter_seconds=_runtime_seconds(
            "DOCSENSE_TASK_MAX_CLOCK_JITTER_SECONDS",
            3.0,
            allow_zero=True,
        )
    )


def _default_database_path() -> Path:
    runtime_dir = Path(
        os.getenv("DOCSENSE_RUNTIME_DIR", str(ROOT / ".runtime"))
    ).expanduser()
    return Path(
        os.getenv(
            "DOCSENSE_TASK_CONTROL_DB_PATH",
            str(runtime_dir / "db" / "task-control-v2.sqlite3"),
        )
    ).expanduser()


def _manager(database_path: Path) -> SQLiteTransactionManager:
    manifests = {
        REPORT_CONTROL_COMPONENT_NAME: load_report_control_manifest(),
        WEAPONRY_CONTROL_COMPONENT_NAME: load_weaponry_control_manifest(),
        ANALYSIS_CONTROL_COMPONENT_NAME: load_analysis_control_manifest(),
    }
    bootstrap = validate_existing_task_control_database(
        database_path,
        known_components=manifests,
        required_components={
            REPORT_CONTROL_COMPONENT_NAME: REPORT_CONTROL_COMPONENT_VERSION,
            WEAPONRY_CONTROL_COMPONENT_NAME: WEAPONRY_CONTROL_COMPONENT_VERSION,
            ANALYSIS_CONTROL_COMPONENT_NAME: ANALYSIS_CONTROL_COMPONENT_VERSION,
        },
    )
    return SQLiteTransactionManager(SQLiteConnectionFactory(bootstrap))


def _preflight_builder(connection):  # type: ignore[no-untyped-def]
    return RoutedTaskRecoveryFinalizationPreflight(
        {
            "report": SQLiteReportRecoveryFinalizationPreflight(connection),
            "weaponry": SQLiteWeaponryRecoveryFinalizationPreflight(connection),
            "file": SQLiteAnalysisRecoveryFinalizationPreflight(connection),
        }
    )


def _resume_preflight_builder(connection):  # type: ignore[no-untyped-def]
    return RoutedTaskRecoveryResumePreflight(
        {
            "report": SQLiteReportRecoveryResumePreflight(connection),
            "weaponry": SQLiteWeaponryRecoveryResumePreflight(connection),
            "file": SQLiteAnalysisRecoveryResumePreflight(connection),
        }
    )


def _service(database_path: Path) -> RecoveryOperatorService:
    factories = build_sqlite_task_control_uow_factories(
        _manager(database_path),
        recovery_finalization_preflight_builder=_preflight_builder,
        recovery_resume_preflight_builder=_resume_preflight_builder,
    )
    coordinator = RecoveryCoordinator(
        clock=_safe_clock(),
        recovery_uow_factory=factories.recovery,
        lease_token_factory=SecureTaskLeaseTokenFactory(),
        policies={
            "report": ReportTaskRecoveryPolicy(),
            "weaponry": WeaponryTaskRecoveryPolicy(),
            "file": AnalysisTaskRecoveryPolicy(),
        },
        recovery_lease_seconds=_runtime_seconds(
            "DOCSENSE_TASK_RECOVERY_LEASE_DURATION_SECONDS",
            30.0,
        ),
    )
    return RecoveryOperatorService(
        recovery_uow_factory=factories.recovery,
        coordinator=coordinator,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="诊断或严格处置阶段 2-7 Recovery Case（默认 dry-run）"
    )
    parser.add_argument("--db-path", default=str(_default_database_path()))
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--write", action="store_true", help="显式启用写模式")
    parser.add_argument("--action", choices=[item.value for item in RecoveryOperatorAction])
    parser.add_argument("--generation", type=int)
    parser.add_argument("--expected-task-row-version", type=int)
    parser.add_argument("--source-attempt-no", type=int)
    parser.add_argument("--source-fencing-token", type=int)
    parser.add_argument("--expected-recovery-fencing-token", type=int)
    parser.add_argument("--operator")
    parser.add_argument("--reason-code")
    parser.add_argument("--evidence-digest")
    parser.add_argument("--step-key", default="")
    parser.add_argument("--source-step-attempt-no", type=int, default=0)
    parser.add_argument("--expected-step-row-version", type=int, default=0)
    parser.add_argument("--operation-id", default="")
    parser.add_argument("--observation-id", default="")
    parser.add_argument("--terminal-state", choices=["succeeded", "failed"])
    parser.add_argument("--checkpoint-code", default="")
    parser.add_argument("--checkpoint-digest", default="")
    parser.add_argument("--public-status", default="")
    parser.add_argument("--message", default="")
    parser.add_argument("--result-ref", default="")
    parser.add_argument("--next-observation-at", default="")
    return parser


def _required_write_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    required = (
        "action",
        "generation",
        "expected_task_row_version",
        "source_attempt_no",
        "source_fencing_token",
        "expected_recovery_fencing_token",
        "operator",
        "reason_code",
        "evidence_digest",
    )
    missing = [name for name in required if getattr(args, name) is None]
    if missing:
        parser.error("写模式缺少严格参数: " + ", ".join(missing))


def _write_json(payload: object) -> None:
    """向标准输出写入一行稳定 JSON。

    运维入口的 stdout 是便于管道和自动化程序消费的结果通道，因此不将业务
    结果混入日志格式；同时显式避免 print，保持与项目运行时静态门禁一致。
    """

    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    database_path = Path(args.db_path).expanduser().resolve()
    if not database_path.is_file():
        parser.error("db-path 必须指向已存在的 Task Control SQLite 文件")
    service = _service(database_path)
    task_id = TaskId(args.task_id)
    if not args.write:
        inspection = service.inspect(task_id, args.case_id)
        result = {
            "mode": "dry_run",
            "found": inspection is not None,
            "recoveryCase": asdict(inspection) if inspection is not None else None,
        }
        _write_json(result)
        return 0 if inspection is not None else 2

    _required_write_args(parser, args)
    clock = _safe_clock()
    command = StrictRecoveryDecisionCommand(
        task_id=task_id,
        case_id=args.case_id,
        generation=args.generation,
        expected_task_row_version=args.expected_task_row_version,
        source_attempt_no=args.source_attempt_no,
        source_fencing_token=args.source_fencing_token,
        expected_recovery_fencing_token=args.expected_recovery_fencing_token,
        operator=args.operator,
        reason_code=args.reason_code,
        evidence_digest=args.evidence_digest,
        action=RecoveryOperatorAction(args.action),
        decided_at=clock.now_utc(),
        next_observation_at=args.next_observation_at,
        retry_from_step_key=args.step_key,
        source_step_attempt_no=args.source_step_attempt_no,
        expected_step_row_version=args.expected_step_row_version,
        operation_id=args.operation_id,
        observation_id=args.observation_id,
        terminal_state=TaskState(args.terminal_state) if args.terminal_state else None,
        checkpoint_code=args.checkpoint_code,
        checkpoint_digest=args.checkpoint_digest,
        public_status=args.public_status,
        message=args.message,
        result_ref=args.result_ref,
    )
    outcome = service.execute(command)
    _write_json(
        {"mode": "write", "action": args.action, "outcome": outcome.value}
    )
    return 0 if outcome.value == "applied" else 3


if __name__ == "__main__":
    raise SystemExit(main())
