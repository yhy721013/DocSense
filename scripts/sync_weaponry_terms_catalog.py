"""预检或显式同步 Weaponry 本地术语卡到版本化 AnythingLLM workspace。

``--dry-run`` 只读本地目录、既有 SQLite 状态和 AnythingLLM；``--apply`` 调用与生产
启动门禁完全相同的协调器实现。工具不会启动 Flask、Dispatcher 或 ``run.py``。
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env", override=False)

from app.modules.weaponry.adapters import (  # noqa: E402
    AnythingLLMTermsCatalogCoordinator,
    AnythingLLMWeaponryClientFactory,
    SQLiteTermsCatalogStateStore,
    TermsCatalogWorkspaceResolver,
    build_terms_catalog_manifest,
    load_weaponry_infrastructure_config,
)
from app.services.core.config import (  # noqa: E402
    load_anythingllm_config,
    load_llm_integration_config,
)


logger = logging.getLogger("scripts.sync_weaponry_terms_catalog")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="预检或同步 Weaponry 版本化术语目录",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="只显示计划，不创建 workspace、不上传、不绑定",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="执行与生产启动门禁相同的幂等同步",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = load_weaponry_infrastructure_config()
    if not config.terms_rule_context_enabled:
        raise RuntimeError(
            "WEAPONRY_TERMS_RULE_CONTEXT_ENABLED=false，术语同步严格保持零 I/O"
        )
    manifest = build_terms_catalog_manifest(config.terms_dir or "")
    anythingllm_config = load_anythingllm_config()
    llm_config = load_llm_integration_config()
    factory = AnythingLLMWeaponryClientFactory(anythingllm_config)
    resolver = TermsCatalogWorkspaceResolver(
        factory,
        workspace_base_name=config.terms_workspace_name or "",
    )
    if args.dry_run:
        # dry-run 不取得进程所有权，也不初始化任何表；它只读取已有 SQLite 事实和
        # AnythingLLM 目录现状，可在服务运行时用于无写入预检。
        coordinator = AnythingLLMTermsCatalogCoordinator(
            factory,
            manifest=manifest,
            workspace_base_name=config.terms_workspace_name or "",
            state_store=SQLiteTermsCatalogStateStore(
                llm_config.task_db_path,
                read_only=True,
            ),
            resolver=resolver,
        )
        plan = coordinator.inspect()
        sys.stdout.write(
            json.dumps(
                {
                    "mode": "dry-run",
                    "fingerprint": plan.fingerprint,
                    "workspaceName": plan.workspace_name,
                    "workspaceSlug": plan.workspace_slug,
                    "workspaceExists": plan.workspace_exists,
                    "expectedCardCount": plan.expected_card_count,
                    "missingCardIds": list(plan.missing_card_ids),
                    "unexpectedDocumentTitles": list(
                        plan.unexpected_document_titles
                    ),
                    "blockedOutcomeUnknown": plan.blocked_outcome_unknown,
                    "writeRequired": plan.write_required,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        return 2 if (
            plan.blocked_outcome_unknown
            or plan.unexpected_document_titles
        ) else 0

    # 显式 apply 与生产 Dispatcher 使用完全相同的单实例锁地址，防止运维同步和
    # 服务启动同时成为远端写入所有者。可写状态表也必须在取锁后初始化。
    from app.modules.tasks.adapters import FileProcessSingletonGuard
    from app.services.core.settings import RUNTIME_DIR

    process_guard = FileProcessSingletonGuard(
        RUNTIME_DIR / "locks" / "weaponry-dispatcher.lock",
        component_name="武器谱术语目录同步",
        event_logger=logger,
    )
    if not process_guard.acquire():
        raise RuntimeError(
            "Weaponry 单实例锁已被服务或其他同步进程占用，拒绝执行 --apply"
        )
    try:
        coordinator = AnythingLLMTermsCatalogCoordinator(
            factory,
            manifest=manifest,
            workspace_base_name=config.terms_workspace_name or "",
            state_store=SQLiteTermsCatalogStateStore(llm_config.task_db_path),
            resolver=resolver,
        )
        descriptor = coordinator.prepare()
    finally:
        process_guard.release()

    sys.stdout.write(
        json.dumps(
            {
                "mode": "apply",
                "fingerprint": descriptor.fingerprint,
                "workspaceName": descriptor.workspace_name,
                "workspaceSlug": descriptor.workspace_slug,
                "cardCount": descriptor.card_count,
                "status": "ready",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
