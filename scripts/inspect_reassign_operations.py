#!/usr/bin/env python3
"""分类节点变更 Operation 的只读诊断与受控人工恢复入口。

默认行为只读取已存在的 SQLite 事实表，不初始化 Schema、不创建 HTTP Client、不访问
AnythingLLM，也不会启动 ``run.py``。需要实际恢复时，操作者必须显式给出精确 Operation
ID、当前 fencing token、操作者、原因码和本实例 lease owner；这样脚本不能被误用为无边界
批量重放器。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    # 以 ``python scripts/inspect_reassign_operations.py`` 执行时，Python 默认仅把 scripts
    # 目录放入 sys.path。显式加入项目根可保证导入的始终是当前工作区代码，而非同名全局包。
    sys.path.insert(0, str(ROOT))

from app.modules.reassign.adapters import (  # noqa: E402 - ROOT 必须先加入 sys.path。
    AnythingLLMReassignmentClientFactory,
    AnythingLLMReassignmentKnowledgeAdapterFactory,
    SQLiteReassignmentRepository,
    load_reassignment_infrastructure_config,
)
from app.modules.reassign.application import (  # noqa: E402
    RecoverReassignmentCommand,
    RecoverReassignmentOperation,
    ReassignmentExecutionSettings,
    ReassignmentRecoveryResultCategory,
)
from app.modules.reassign.composition import (  # noqa: E402
    compose_reassign_application_services,
)


logger = logging.getLogger(__name__)

# 退出码 2 由 argparse 保留给参数错误。恢复工具为三类未收口结果使用稳定的非零退出码，
# 让 Shell、CI 和未来运维编排不必解析日志文字即可判断是否真正完成恢复。
_EXIT_OPERATION_NOT_FOUND = 3
_EXIT_TAKEOVER_REJECTED = 4
_EXIT_RECOVERY_REQUIRED = 5
_COMPLETED_RECOVERY_CATEGORIES = frozenset(
    {
        ReassignmentRecoveryResultCategory.RECOVERED_SUCCEEDED,
        ReassignmentRecoveryResultCategory.RECOVERED_FAILED_NO_SIDE_EFFECT,
        ReassignmentRecoveryResultCategory.COMPENSATED,
    }
)


def _positive_int(value: str) -> int:
    """解析 CLI 正整数，拒绝布尔式、零和负数等不安全的 fencing 输入。"""

    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是正整数") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def _positive_seconds(value: str) -> float:
    """解析有限正秒数，避免把无限 lease 或 NaN 交给恢复状态机。"""

    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是正有限秒数") from exc
    if parsed <= 0.0 or parsed == float("inf") or parsed != parsed:
        raise argparse.ArgumentTypeError("必须是正有限秒数")
    return parsed


def _existing_database_path(value: str) -> Path:
    """校验目标是已经存在的 SQLite 文件，诊断入口绝不创建新数据库。"""

    candidate = Path(value).expanduser().resolve()
    if not candidate.exists():
        raise argparse.ArgumentTypeError(f"SQLite 文件不存在: {candidate}")
    if not candidate.is_file():
        raise argparse.ArgumentTypeError(f"SQLite 路径不是文件: {candidate}")
    return candidate


def _record_summary(record: object) -> dict[str, Any]:
    """生成不含文件名、路径、token 与供应商正文的最小诊断摘要。"""

    # 此函数只接收 Port 返回的强类型记录；以显式属性读取而非 ``asdict``，避免未来新增敏感
    # 字段后被脚本意外导出。
    operation = record.operation
    return {
        "operationId": operation.operation_id,
        "status": operation.status.value,
        "currentStep": (
            operation.current_step.value if operation.current_step is not None else None
        ),
        "leaseExpiresAt": operation.lease_expires_at,
        "fencingToken": operation.fencing_token,
        "requiresRemoteMembershipChange": operation.document.requires_remote_membership_change,
        "sourceWorkspaceRecorded": record.source_workspace_slug is not None,
        "targetWorkspaceRecorded": record.target_workspace_slug is not None,
        "targetWorkspaceOwnership": (
            record.target_workspace_ownership.value
            if record.target_workspace_ownership is not None
            else None
        ),
        "recoveryRequiredFencingToken": record.recovery_required_fencing_token,
        "updatedAt": record.updated_at,
    }


def _parser() -> argparse.ArgumentParser:
    """创建显式双模式 CLI；默认始终是只读诊断。"""

    parser = argparse.ArgumentParser(
        description=(
            "只读列出已过期的分类节点变更 Operation；使用 --apply 才会执行单条受控恢复。"
        )
    )
    parser.add_argument(
        "--database",
        required=True,
        type=_existing_database_path,
        help="既有 knowledge_base SQLite 文件路径；脚本不会创建数据库或初始化 Schema。",
    )
    parser.add_argument(
        "--limit",
        type=_positive_int,
        default=50,
        help="只读扫描上限（1-500，默认 50）。",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="执行单条人工恢复；省略时始终 dry-run。",
    )
    parser.add_argument("--operation-id", help="--apply 时必填的精确内部 Operation ID。")
    parser.add_argument(
        "--expected-fencing-token",
        type=_positive_int,
        help="--apply 时必填；必须来自刚刚完成的只读诊断。",
    )
    parser.add_argument("--actor", help="--apply 时必填的人工操作者标识，仅摘要写入审计。")
    parser.add_argument("--reason-code", help="--apply 时必填的受控恢复原因码。")
    parser.add_argument(
        "--lease-owner",
        help="--apply 时必填的当前实例唯一 owner；禁止使用共享或空 owner。",
    )
    parser.add_argument(
        "--lease-duration-seconds",
        type=_positive_seconds,
        help=(
            "--apply 时必填；必须覆盖 DOCSENSE_REASSIGN_TOTAL_TIMEOUT_SECONDS "
            "与安全余量。"
        ),
    )
    parser.add_argument(
        "--lease-safety-margin-seconds",
        type=_positive_seconds,
        default=5.0,
        help="恢复 lease 的额外安全余量（默认 5 秒）。",
    )
    return parser


def _validate_apply_arguments(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> None:
    """阻止遗漏审计主体或 CAS 条件的写模式。"""

    if not args.apply:
        return
    required_names = (
        "operation_id",
        "expected_fencing_token",
        "actor",
        "reason_code",
        "lease_owner",
        "lease_duration_seconds",
    )
    missing = [
        f"--{name.replace('_', '-')}"
        for name in required_names
        if getattr(args, name) in {None, ""}
    ]
    if missing:
        parser.error("--apply 必须同时提供: " + ", ".join(missing))
    if args.limit > 500:
        parser.error("--limit 必须在 1 到 500 之间")


def _build_recovery_service(
    *,
    repository: SQLiteReassignmentRepository,
    lease_owner: str,
    lease_duration_seconds: float,
    lease_safety_margin_seconds: float,
) -> RecoverReassignmentOperation:
    """仅在显式写模式构造请求级 AnythingLLM Factory 与恢复服务。"""

    # 保持 dry-run 不读取现有 AnythingLLM 环境配置；写模式才加载其受控配置，避免普通审计
    # 因无关环境变量缺失而失败，也避免给人“已准备发起网络请求”的错误印象。
    from app.services.core.config import load_anythingllm_config

    infrastructure_config = load_reassignment_infrastructure_config()
    anythingllm_config = load_anythingllm_config()
    if not anythingllm_config.base_url or not anythingllm_config.api_key:
        raise ValueError(
            "--apply 需要配置 ANYTHINGLLM_BASE_URL 与 ANYTHINGLLM_API_KEY"
        )
    client_factory = AnythingLLMReassignmentClientFactory(anythingllm_config)
    knowledge_factory = AnythingLLMReassignmentKnowledgeAdapterFactory(
        client_factory,
        infrastructure_config,
    )
    settings = ReassignmentExecutionSettings(
        lease_owner=lease_owner,
        lease_duration_seconds=lease_duration_seconds,
        remote_total_timeout_seconds=infrastructure_config.total_timeout_seconds,
        lease_safety_margin_seconds=lease_safety_margin_seconds,
    )
    # 诊断脚本只从组合根取得 Recovery Application 外观，不直接调用 Repository 的终态
    # 收口方法。这样人工恢复与未来队列 Worker 共享同一依赖方向和 fencing 边界。
    return compose_reassign_application_services(
        repository=repository,
        knowledge_factory=knowledge_factory,
        settings=settings,
        infrastructure_config=infrastructure_config,
    ).recovery


def _scan(repository: SQLiteReassignmentRepository, *, limit: int) -> dict[str, Any]:
    """执行只读、有界扫描并返回可安全打印的 JSON 负载。"""

    if limit > 500:
        raise ValueError("limit 必须在 1 到 500 之间")
    with repository.unit_of_work(read_only=True) as unit_of_work:
        records = unit_of_work.list_recoverable_operations(limit=limit)
    logger.info("分类节点变更只读诊断完成: expired_operation_count=%s", len(records))
    return {
        "mode": "dry_run",
        "count": len(records),
        "operations": [_record_summary(record) for record in records],
    }


def _write_json(payload: dict[str, Any]) -> None:
    """向标准输出写入唯一机器可读结果，不把运维数据混入日志流。"""

    # 诊断脚本的成功结果是给人工或自动化消费的 JSON，不属于运行日志；使用显式 stdout
    # 写入可避免 ``print`` 的隐式格式化，也符合项目运行时代码统一通过 logging 记录日志的门禁。
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _recovery_exit_code(category: ReassignmentRecoveryResultCategory) -> int:
    """把内部恢复结果映射为稳定进程退出码。

    “命令完成执行”不等于“Operation 已恢复”。只有三个终态分类返回 0；不存在、接管失败
    和仍待恢复分别使用独立非零值，避免自动化工具把保护现场错误地当成成功处理。
    """

    if not isinstance(category, ReassignmentRecoveryResultCategory):
        raise TypeError("category 必须是 ReassignmentRecoveryResultCategory")
    if category in _COMPLETED_RECOVERY_CATEGORIES:
        return 0
    if category is ReassignmentRecoveryResultCategory.OPERATION_NOT_FOUND:
        return _EXIT_OPERATION_NOT_FOUND
    if category is ReassignmentRecoveryResultCategory.TAKEOVER_REJECTED:
        return _EXIT_TAKEOVER_REJECTED
    return _EXIT_RECOVERY_REQUIRED


def main(argv: list[str] | None = None) -> int:
    """执行 CLI；业务异常以非零退出码返回，避免运维误把失败当作恢复成功。"""

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s [%(name)s] %(message)s",
    )
    parser = _parser()
    args = parser.parse_args(argv)
    _validate_apply_arguments(parser, args)

    # ``initialize_schema=False`` 是诊断安全边界：即使数据库尚未迁移到 1E，默认扫描也
    # 只能报出表不存在，不得通过 DDL 改变现场。实际恢复同样要求表已由受控部署初始化。
    repository = SQLiteReassignmentRepository(
        args.database,
        initialize_schema=False,
    )
    try:
        if not args.apply:
            _write_json(_scan(repository, limit=args.limit))
            return 0

        service = _build_recovery_service(
            repository=repository,
            lease_owner=args.lease_owner,
            lease_duration_seconds=args.lease_duration_seconds,
            lease_safety_margin_seconds=args.lease_safety_margin_seconds,
        )
        result = service.recover(
            RecoverReassignmentCommand(
                operation_id=args.operation_id,
                expected_fencing_token=args.expected_fencing_token,
                actor=args.actor,
                reason_code=args.reason_code,
            )
        )
        _write_json(
            {
                "mode": "apply",
                "operationId": result.operation_id,
                "result": result.category.value,
            }
        )
        return _recovery_exit_code(result.category)
    except Exception as exc:  # noqa: BLE001 - CLI 必须给出简洁失败边界。
        logger.error("分类节点变更诊断或恢复失败: error_type=%s", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
