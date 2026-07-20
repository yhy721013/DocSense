"""AnythingLLM 创建意图的有界崩溃恢复与现场隔离。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import logging

from app.integrations.anythingllm.models import AnythingLLMWorkspace

from app.modules.weaponry.ports import (
    QuarantineWeaponryCreationIntent,
    QuarantineWeaponryResources,
    RegisterWeaponryResource,
    ResolveWeaponryCreationIntent,
    WeaponryCreationIntent,
    WeaponryCreationIntentKind,
    WeaponryCreationIntentStorePort,
    WeaponryResourceKind,
    WeaponryResourceOwnership,
    WeaponryResourceRecordState,
    WeaponryResourceStorePort,
    WeaponryTrackedResource,
)

from .anythingllm_clients import WeaponryAnythingLLMClientFactoryProtocol


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WeaponryCreationIntentRecoveryResult:
    """一次有界扫描的内部诊断，不进入公开任务结果。"""

    scanned_count: int
    reconciled_count: int
    quarantined_count: int
    deferred_count: int


class AnythingLLMWeaponryCreationIntentRecoveryAdapter:
    """恢复未完成 create 检查点；只查回和隔离，永不重发创建或自动删除。

    ``pending`` 可能来自进程在供应商提交 create 后、本地登记前崩溃。workspace 能按
    确定性名称唯一查回；Thread 当前没有唯一查询 API，只能连同父 workspace 现场隔离。
    任何查回结果都会先登记/隔离资源事实，再终结创建意图，避免再次崩溃后丢失现场。
    """

    def __init__(
        self,
        client_factory: WeaponryAnythingLLMClientFactoryProtocol,
        intents: WeaponryCreationIntentStorePort,
        resources: WeaponryResourceStorePort,
        *,
        user_id: int | None = 1,
    ) -> None:
        if not isinstance(client_factory, WeaponryAnythingLLMClientFactoryProtocol):
            raise TypeError("client_factory 必须实现 Weaponry AnythingLLM 工厂契约")
        if not isinstance(intents, WeaponryCreationIntentStorePort):
            raise TypeError("intents 必须实现 WeaponryCreationIntentStorePort")
        if not isinstance(resources, WeaponryResourceStorePort):
            raise TypeError("resources 必须实现 WeaponryResourceStorePort")
        self._client_factory = client_factory
        self._intents = intents
        self._resources = resources
        self._user_id = user_id

    def run_once(self, *, limit: int) -> WeaponryCreationIntentRecoveryResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        pending = self._intents.list_pending(limit=limit)
        reconciled = 0
        quarantined = 0
        deferred = 0

        # workspace 清单属于远端只读快照。同一轮最多会处理 ``limit`` 条创建意图，
        # 因此只拉取一次并按精确名称建索引，避免 50 条积压触发 50 次相同 HTTP 请求。
        # Thread 意图不依赖该快照；即使 AnythingLLM 暂时不可用，仍可按既定保守口径
        # 隔离 Thread 现场，而不会被 workspace 查询故障连带阻塞。
        workspaces_by_name: Mapping[str, tuple[AnythingLLMWorkspace, ...]] | None = {}
        if any(
            item.kind is not WeaponryCreationIntentKind.SOURCE_THREAD
            for item in pending
        ):
            try:
                workspaces_by_name = self._load_workspaces_by_name()
            except Exception:
                workspaces_by_name = None
                logger.exception(
                    "武器谱创建意图 workspace 清单读取失败，本轮仅暂缓 workspace 意图: "
                    "pending_count=%d",
                    len(pending),
                )
        for intent in pending:
            if (
                intent.kind is not WeaponryCreationIntentKind.SOURCE_THREAD
                and workspaces_by_name is None
            ):
                deferred += 1
                continue
            try:
                outcome = self._recover_one(intent, workspaces_by_name or {})
            except Exception:
                # 网络不可用或本地 CAS 竞争时保留 pending，下一轮只会再次查询，绝不
                # 重发 create。异常按单条隔离，不能阻断本批其他任务的现场收敛。
                deferred += 1
                logger.exception(
                    "武器谱创建意图恢复暂缓: task_id=%s intent_id=%s kind=%s",
                    intent.task_id.value,
                    intent.intent_id,
                    intent.kind.value,
                )
                continue
            if outcome == "reconciled":
                reconciled += 1
            else:
                quarantined += 1
        result = WeaponryCreationIntentRecoveryResult(
            scanned_count=len(pending),
            reconciled_count=reconciled,
            quarantined_count=quarantined,
            deferred_count=deferred,
        )
        if pending:
            logger.info(
                "武器谱创建意图有界恢复完成: scanned=%d reconciled=%d "
                "quarantined=%d deferred=%d",
                result.scanned_count,
                result.reconciled_count,
                result.quarantined_count,
                result.deferred_count,
            )
        return result

    def _recover_one(
        self,
        intent: WeaponryCreationIntent,
        workspaces_by_name: Mapping[str, tuple[AnythingLLMWorkspace, ...]],
    ) -> str:
        if intent.kind is WeaponryCreationIntentKind.SOURCE_THREAD:
            self._quarantine_scene(
                intent,
                error_code="source_thread_unique_lookup_unavailable",
                reason="Thread 创建结果无法唯一查回，父 workspace 现场已隔离",
            )
            return "quarantined"

        matches = workspaces_by_name.get(intent.expected_name, ())
        if len(matches) != 1:
            self._quarantine_scene(
                intent,
                error_code="workspace_creation_reconciliation_ambiguous",
                reason=(
                    "workspace 创建结果查回数量不是 1，禁止自动重放或删除"
                ),
            )
            return "quarantined"

        workspace = matches[0]
        current = self._required_resource_record(intent)
        tracked = self._tracked_workspace(intent, workspace.slug)
        current = self._resources.register(
            RegisterWeaponryResource(
                intent.task_id,
                tracked,
                current.version,
            )
        )
        if current.state is not WeaponryResourceRecordState.QUARANTINED:
            current = self._resources.quarantine(
                QuarantineWeaponryResources(
                    intent.task_id,
                    current.version,
                    "weaponry_creation_crash_window_reconciled",
                    "已查回崩溃窗口创建的 workspace，保留现场等待人工对账",
                )
            )
        self._intents.resolve(
            ResolveWeaponryCreationIntent(
                intent.task_id,
                intent.intent_id,
                intent.version,
                workspace.slug,
            )
        )
        logger.critical(
            "武器谱崩溃窗口 workspace 已唯一查回并隔离: task_id=%s "
            "intent_id=%s kind=%s",
            intent.task_id.value,
            intent.intent_id,
            intent.kind.value,
        )
        return "reconciled"

    def _load_workspaces_by_name(
        self,
    ) -> Mapping[str, tuple[AnythingLLMWorkspace, ...]]:
        """读取一次远端快照并保留同名项，供精确唯一性判断使用。"""

        with self._client_factory.create() as clients:
            workspaces: Sequence[AnythingLLMWorkspace] = (
                clients.workspaces.list_workspaces(user_id=self._user_id)
            )
        grouped: dict[str, list[AnythingLLMWorkspace]] = {}
        for workspace in workspaces:
            grouped.setdefault(workspace.name, []).append(workspace)
        return {name: tuple(items) for name, items in grouped.items()}

    def _quarantine_scene(
        self,
        intent: WeaponryCreationIntent,
        *,
        error_code: str,
        reason: str,
    ) -> None:
        current = self._required_resource_record(intent)
        if current.state is not WeaponryResourceRecordState.QUARANTINED:
            self._resources.quarantine(
                QuarantineWeaponryResources(
                    intent.task_id,
                    current.version,
                    error_code,
                    reason,
                )
            )
        self._intents.quarantine(
            QuarantineWeaponryCreationIntent(
                intent.task_id,
                intent.intent_id,
                intent.version,
                error_code,
            )
        )
        logger.critical(
            "武器谱未决创建意图已隔离: task_id=%s intent_id=%s error_code=%s",
            intent.task_id.value,
            intent.intent_id,
            error_code,
        )

    def _required_resource_record(self, intent: WeaponryCreationIntent):
        record = self._resources.get(intent.task_id)
        if record is None:
            raise RuntimeError("创建意图找不到任务资源记录")
        return record

    @staticmethod
    def _tracked_workspace(
        intent: WeaponryCreationIntent,
        workspace_slug: str,
    ) -> WeaponryTrackedResource:
        if intent.kind is WeaponryCreationIntentKind.RETRIEVAL_WORKSPACE:
            return WeaponryTrackedResource(
                resource_id=f"retrieval-scope:{workspace_slug}",
                kind=WeaponryResourceKind.RETRIEVAL_SCOPE,
                external_ref=workspace_slug,
                ownership=WeaponryResourceOwnership.OWNED,
                idempotency_key=(
                    f"weaponry:{intent.task_id.value}:retrieval-scope"
                ),
            )
        attempt_key = intent.intent_id.removesuffix(":extraction-workspace")
        return WeaponryTrackedResource(
            resource_id=f"extraction-context:{workspace_slug}",
            kind=WeaponryResourceKind.EXTRACTION_CONTEXT,
            external_ref=workspace_slug,
            ownership=WeaponryResourceOwnership.OWNED,
            idempotency_key=f"{attempt_key}:extraction-context",
            document_key=intent.document_key,
            call_id=intent.call_id,
        )


__all__ = [
    "AnythingLLMWeaponryCreationIntentRecoveryAdapter",
    "WeaponryCreationIntentRecoveryResult",
]
