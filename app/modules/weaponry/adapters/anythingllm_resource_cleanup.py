"""AnythingLLM 武器谱临时资源的单项幂等删除适配器。"""

from __future__ import annotations

import logging

from app.integrations.anythingllm import (
    AnythingLLMHTTPError,
    AnythingLLMTransportClosedError,
    AnythingLLMTransportError,
)
from app.modules.weaponry.ports import (
    CleanupWeaponryExternalResource,
    WeaponryExternalResourceCleanupResult,
    WeaponryResourceCleanupOutcome,
    WeaponryResourceKind,
)

from .anythingllm_clients import WeaponryAnythingLLMClientFactoryProtocol


logger = logging.getLogger(__name__)

_WORKSPACE_RESOURCE_KINDS = frozenset(
    {
        WeaponryResourceKind.RETRIEVAL_SCOPE,
        WeaponryResourceKind.EXTRACTION_CONTEXT,
        WeaponryResourceKind.EVIDENCE_CONTEXT,
    }
)
_WORKSPACE_CHILD_RESOURCE_KINDS = frozenset(
    {
        WeaponryResourceKind.DOCUMENT_BINDING,
        WeaponryResourceKind.EMBEDDING,
    }
)


class AnythingLLMWeaponryResourceCleanupAdapter:
    """按资源类型执行一次删除，不持有 Store、lease 或跨任务网络 Session。

    Workspace 的 DELETE 会连同绑定文档和 embedding 一起收敛，因此子资源只提交本地
    ``succeeded``。404 证明目标已不存在，也按幂等成功处理；超时、断连等不能证明远端
    是否已经删除的异常返回 ``outcome_unknown``，由 Application 立即隔离。
    """

    def __init__(
        self,
        client_factory: WeaponryAnythingLLMClientFactoryProtocol,
        *,
        user_id: int | None = 1,
    ) -> None:
        if not isinstance(client_factory, WeaponryAnythingLLMClientFactoryProtocol):
            raise TypeError("client_factory 必须实现武器谱 AnythingLLM Client 工厂")
        if user_id is not None and (
            isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1
        ):
            raise ValueError("user_id 必须是正整数或 None")
        self._client_factory = client_factory
        self._user_id = user_id

    def cleanup(
        self,
        command: CleanupWeaponryExternalResource,
    ) -> WeaponryExternalResourceCleanupResult:
        if not isinstance(command, CleanupWeaponryExternalResource):
            raise TypeError("command 必须是 CleanupWeaponryExternalResource")
        resource = command.resource
        if resource.kind in _WORKSPACE_CHILD_RESOURCE_KINDS:
            logger.info(
                "武器谱 Workspace 子资源随父资源收敛: task_id=%s resource_id=%s kind=%s",
                command.task_id.value,
                resource.resource_id,
                resource.kind.value,
            )
            return WeaponryExternalResourceCleanupResult(
                WeaponryResourceCleanupOutcome.SUCCEEDED
            )
        if resource.kind not in _WORKSPACE_RESOURCE_KINDS and (
            resource.kind is not WeaponryResourceKind.SOURCE_CONVERSATION
        ):
            # 当前生产 Adapter 从不创建这些 owned 类型。遇到未知事实不能进入永久重试，
            # 使用 outcome-unknown 让上层隔离并保留人工审计现场。
            logger.error(
                "武器谱资源类型没有安全清理实现，准备隔离: task_id=%s resource_id=%s "
                "kind=%s",
                command.task_id.value,
                resource.resource_id,
                resource.kind.value,
            )
            return WeaponryExternalResourceCleanupResult(
                WeaponryResourceCleanupOutcome.OUTCOME_UNKNOWN,
                "weaponry_resource_kind_not_supported",
                resource.kind.value,
            )

        mutation_started = False
        try:
            with self._client_factory.create() as clients:
                if resource.kind is WeaponryResourceKind.SOURCE_CONVERSATION:
                    workspace_slug, thread_slug = self._conversation_ref(
                        resource.external_ref
                    )
                    mutation_started = True
                    clients.threads.delete_thread(
                        workspace_slug,
                        thread_slug,
                        user_id=self._user_id,
                    )
                else:
                    mutation_started = True
                    clients.workspaces.delete_workspace(
                        resource.external_ref,
                        user_id=self._user_id,
                    )
        except AnythingLLMHTTPError as exc:
            if exc.status_code == 404:
                logger.info(
                    "武器谱外部资源已不存在，按幂等删除成功处理: task_id=%s "
                    "resource_id=%s kind=%s",
                    command.task_id.value,
                    resource.resource_id,
                    resource.kind.value,
                )
                return WeaponryExternalResourceCleanupResult(
                    WeaponryResourceCleanupOutcome.SUCCEEDED
                )
            return self._failed_result(exc, mutation_started=mutation_started)
        except AnythingLLMTransportError as exc:
            return self._failed_result(exc, mutation_started=mutation_started)
        except ValueError as exc:
            # 外部引用无法解析属于持久事实损坏，重试不会改善，必须隔离。
            logger.error(
                "武器谱外部资源引用无效，准备隔离: task_id=%s resource_id=%s",
                command.task_id.value,
                resource.resource_id,
            )
            return WeaponryExternalResourceCleanupResult(
                WeaponryResourceCleanupOutcome.OUTCOME_UNKNOWN,
                "weaponry_resource_external_ref_invalid",
                type(exc).__name__,
            )
        except Exception as exc:
            # 未知 Adapter/Client 异常不能被武断归为“未发送”。若删除调用已经开始，保守
            # 冻结；尚未开始时允许持久冷却后重试初始化。
            logger.exception(
                "武器谱外部资源清理发生未分类异常: task_id=%s resource_id=%s "
                "mutation_started=%s",
                command.task_id.value,
                resource.resource_id,
                mutation_started,
            )
            return WeaponryExternalResourceCleanupResult(
                (
                    WeaponryResourceCleanupOutcome.OUTCOME_UNKNOWN
                    if mutation_started
                    else WeaponryResourceCleanupOutcome.FAILED
                ),
                "weaponry_resource_cleanup_exception",
                type(exc).__name__,
            )

        logger.info(
            "武器谱外部资源清理完成: task_id=%s resource_id=%s kind=%s",
            command.task_id.value,
            resource.resource_id,
            resource.kind.value,
        )
        return WeaponryExternalResourceCleanupResult(
            WeaponryResourceCleanupOutcome.SUCCEEDED
        )

    @staticmethod
    def _conversation_ref(value: str) -> tuple[str, str]:
        parts = value.split("\x1f")
        if len(parts) != 2 or any(not item.strip() for item in parts):
            raise ValueError("source_conversation external_ref 无效")
        return parts[0].strip(), parts[1].strip()

    @staticmethod
    def _failed_result(
        error: AnythingLLMTransportError,
        *,
        mutation_started: bool,
    ) -> WeaponryExternalResourceCleanupResult:
        # 非 2xx HTTP 响应是明确拒绝，可冷却后重试；关闭后的 Transport 在发请求前即可
        # 判定失败。其余传输异常在 DELETE 已开始后无法证明远端是否执行。
        outcome_unknown = mutation_started and not isinstance(
            error,
            (AnythingLLMHTTPError, AnythingLLMTransportClosedError),
        )
        outcome = (
            WeaponryResourceCleanupOutcome.OUTCOME_UNKNOWN
            if outcome_unknown
            else WeaponryResourceCleanupOutcome.FAILED
        )
        status_detail = (
            f"http_status={error.status_code}"
            if error.status_code is not None
            else type(error).__name__
        )
        error_code = (
            "weaponry_resource_cleanup_outcome_unknown"
            if outcome_unknown
            else "weaponry_resource_cleanup_rejected"
        )
        logger.warning(
            "武器谱外部资源清理未成功: error_code=%s external_error_code=%s "
            "outcome=%s detail=%s",
            error_code,
            error.code,
            outcome.value,
            status_detail,
        )
        return WeaponryExternalResourceCleanupResult(
            outcome,
            error_code,
            status_detail,
        )


__all__ = ["AnythingLLMWeaponryResourceCleanupAdapter"]
