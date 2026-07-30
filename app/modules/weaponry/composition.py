"""武器谱 1D-5/1D-6 组合根与实例链一致性门禁。"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Callable

from app.modules.tasks.ports import (
    ProcessSingletonGuardPort,
    ProgressPublisherPort,
    TaskCommandPort,
    TaskExecutionPermitPort,
    TaskQueueInspectionPort,
)
from app.modules.weaponry.adapters.infrastructure_config import (
    WeaponryInfrastructureConfig,
    WeaponryRuntimeCapabilities,
    WeaponryRuntimePolicies,
    build_weaponry_runtime_policies,
    validate_weaponry_runtime_capabilities,
)
from app.modules.weaponry.adapters.local_dispatcher import (
    LocalWeaponryDispatcherSnapshot,
    LocalWeaponryTaskDispatcher,
)
from app.modules.weaponry.adapters.production_gate import (
    WeaponryProductionGateSnapshot,
    evaluate_weaponry_production_gate,
)
from app.modules.weaponry.application import (
    FreezeExpiredWeaponryCallbackGuards,
    RecoverWeaponryCallbackSynchronously,
    RunWeaponryTask,
    SubmitWeaponryTask,
    WeaponryFieldExecutor,
    WeaponryResourceRecoveryService,
)
from app.modules.weaponry.domain import (
    WeaponryInputSnapshot,
    WeaponryResult,
    WeaponrySubmission,
)
from app.modules.weaponry.ports import (
    AuxiliaryGuidancePort,
    EvidenceExtractionPort,
    TargetEvidenceRetrievalPort,
    WeaponryBoundedMaintenancePort,
    WeaponryCallbackPort,
    WeaponryCallbackRecoverySourcePort,
    WeaponryDocumentScopePort,
    WeaponryExternalResourceCleanupPort,
    WeaponryInteractionAuditPort,
    WeaponryResourceStorePort,
    WeaponryTranslationPort,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WeaponryApplicationServices:
    """一次完整武器谱运行装配；构造本身不会启动后台线程。"""

    submit: SubmitWeaponryTask
    runner: RunWeaponryTask
    dispatcher: LocalWeaponryTaskDispatcher
    callbacks: WeaponryCallbackPort
    callback_recovery: RecoverWeaponryCallbackSynchronously
    resource_recovery: WeaponryResourceRecoveryService
    document_scope: WeaponryDocumentScopePort
    task_commands: TaskCommandPort[
        WeaponrySubmission,
        WeaponryInputSnapshot,
        WeaponryResult,
    ]
    progress_publisher: ProgressPublisherPort
    execution_limiter: TaskExecutionPermitPort
    policies: WeaponryRuntimePolicies
    config: WeaponryInfrastructureConfig

    def __post_init__(self) -> None:
        """拒绝双 Repository、双 Callback 或绕过共享 limiter 的错误装配。"""

        if not isinstance(self.submit, SubmitWeaponryTask):
            raise TypeError("submit 必须是 SubmitWeaponryTask")
        if not isinstance(self.runner, RunWeaponryTask):
            raise TypeError("runner 必须是 RunWeaponryTask")
        if not isinstance(self.dispatcher, LocalWeaponryTaskDispatcher):
            raise TypeError("dispatcher 必须是 LocalWeaponryTaskDispatcher")
        if not isinstance(self.callbacks, WeaponryCallbackPort):
            raise TypeError("callbacks 必须实现 WeaponryCallbackPort")
        if not isinstance(
            self.callback_recovery,
            RecoverWeaponryCallbackSynchronously,
        ):
            raise TypeError(
                "callback_recovery 必须是 RecoverWeaponryCallbackSynchronously"
            )
        if not isinstance(self.resource_recovery, WeaponryResourceRecoveryService):
            raise TypeError("resource_recovery 必须是 WeaponryResourceRecoveryService")
        if not isinstance(self.document_scope, WeaponryDocumentScopePort):
            raise TypeError("document_scope 必须实现 WeaponryDocumentScopePort")
        if not isinstance(self.task_commands, TaskCommandPort):
            raise TypeError("task_commands 必须实现 TaskCommandPort")
        if not isinstance(self.progress_publisher, ProgressPublisherPort):
            raise TypeError("progress_publisher 必须实现 ProgressPublisherPort")
        if not isinstance(self.execution_limiter, TaskExecutionPermitPort):
            raise TypeError("execution_limiter 必须实现 TaskExecutionPermitPort")
        if not isinstance(self.policies, WeaponryRuntimePolicies):
            raise TypeError("policies 必须是 WeaponryRuntimePolicies")
        if not isinstance(self.config, WeaponryInfrastructureConfig):
            raise TypeError("config 必须是 WeaponryInfrastructureConfig")

        if self.submit.dispatcher is not self.dispatcher:
            raise ValueError("Weaponry Submit 与容器必须共享同一 Dispatcher")
        if self.dispatcher.runner is not self.runner:
            raise ValueError("Weaponry Dispatcher 与容器必须共享同一 Runner")
        if not (
            self.submit.task_commands
            is self.runner.task_commands
            is self.dispatcher.task_commands
            is self.task_commands
        ):
            raise ValueError("Weaponry Submit/Run/Dispatcher 必须共享同一 TaskCommand")
        if not (
            self.submit.progress_publisher
            is self.runner.progress_publisher
            is self.progress_publisher
        ):
            raise ValueError("Weaponry Submit/Run 必须共享同一 Progress Publisher")
        if self.runner.callbacks is not self.callbacks:
            raise ValueError("Weaponry Runner 与容器必须共享同一 Callback Adapter")
        if self.callback_recovery.callbacks is not self.callbacks:
            raise ValueError("Weaponry 正常执行与 check-task 必须共享同一 Callback Guard")
        if self.resource_recovery.store is not self.runner.resources:
            raise ValueError("Weaponry Runner 与资源恢复必须共享同一 Resource Store")
        if self.dispatcher.resource_maintenance is not self.resource_recovery:
            raise ValueError("Weaponry Dispatcher 必须运行容器共享的资源恢复用例")
        callback_maintenance = self.dispatcher.callback_guard_maintenance
        if (
            not isinstance(callback_maintenance, FreezeExpiredWeaponryCallbackGuards)
            or callback_maintenance.callbacks is not self.callbacks
        ):
            raise ValueError("Weaponry Dispatcher 必须维护容器共享的 Callback Guard")
        if self.runner.retrieval is not self.runner.field_executor.retrieval:
            raise ValueError("Weaponry Scope 与字段搜索必须共享同一 Retrieval Adapter")
        if self.dispatcher.execution_limiter is not self.execution_limiter:
            raise ValueError("Weaponry Dispatcher 必须使用容器共享的重型任务 limiter")
        if not self.dispatcher.has_process_guard:
            raise ValueError("Weaponry 本地 Dispatcher 必须装配跨进程单实例锁")

        selection = self.policies.evidence_selection
        execution = self.policies.execution
        auxiliary = self.policies.auxiliary_guidance
        if (
            selection.provider_fingerprint != self.config.provider_fingerprint
            or selection.embedding_fingerprint != self.config.embedding_fingerprint
            or selection.document_processing_fingerprint
            != self.config.document_processing_fingerprint
            or selection.query_version != self.config.query_version
            or selection.score_protocol != self.config.score_protocol
            or selection.reference_filter_strategy
            != self.config.reference_filter_strategy
            or execution.extraction_model_fingerprint
            != self.config.extraction_model_fingerprint
            or execution.extraction_context_strategy
            != self.config.extraction_context_strategy
        ):
            raise ValueError("Weaponry 运行策略与基础设施配置不一致")
        expected_auxiliary_enabled = auxiliary.policy_id != "none"
        if expected_auxiliary_enabled != self.config.terms_rule_context_enabled:
            raise ValueError("Weaponry 术语策略与基础设施开关不一致")

    def start(self) -> None:
        self.dispatcher.start()

    def stop(self, *, timeout_seconds: float | None = None) -> bool:
        return self.dispatcher.stop(timeout_seconds=timeout_seconds)

    def close(self) -> None:
        self.dispatcher.close()

    def snapshot(self) -> LocalWeaponryDispatcherSnapshot:
        return self.dispatcher.snapshot()

    def production_gate_snapshot(self) -> WeaponryProductionGateSnapshot:
        """读取机器可判定的真实供应商能力门禁，不触发网络或副作用。"""

        return evaluate_weaponry_production_gate(
            attestation_path=self.config.production_attestation_path,
            profile_id=self.policies.evidence_selection.profile_id,
            fingerprints={
                "provider": self.config.provider_fingerprint,
                "embedding": self.config.embedding_fingerprint,
                "documentProcessing": (
                    self.config.document_processing_fingerprint
                ),
                "extractionModel": self.config.extraction_model_fingerprint,
            },
        )


def compose_weaponry_application_services(
    *,
    task_commands: TaskCommandPort[
        WeaponrySubmission,
        WeaponryInputSnapshot,
        WeaponryResult,
    ],
    progress_publisher: ProgressPublisherPort,
    retrieval: TargetEvidenceRetrievalPort,
    extraction: EvidenceExtractionPort,
    guidance: AuxiliaryGuidancePort,
    translation: WeaponryTranslationPort,
    audit: WeaponryInteractionAuditPort,
    callbacks: WeaponryCallbackPort,
    callback_recovery_source: WeaponryCallbackRecoverySourcePort,
    resources: WeaponryResourceStorePort,
    resource_cleaner: WeaponryExternalResourceCleanupPort,
    document_scope: WeaponryDocumentScopePort,
    execution_limiter: TaskExecutionPermitPort,
    process_guard: ProcessSingletonGuardPort,
    config: WeaponryInfrastructureConfig,
    capabilities: WeaponryRuntimeCapabilities,
    creation_intent_recovery: WeaponryBoundedMaintenancePort | None = None,
    startup_gate: Callable[[], None] | None = None,
) -> WeaponryApplicationServices:
    """显式注入全部 I/O 边界并构造单一实例链，不启动线程或访问外部服务。

    离线测试向本函数传入严格 Fake；生产 1D-6 传入真实 Callback Guard、资源清理和恢复源。
    生产代码不会导入 ``tests.fakes``，也不存在构造失败后自动改用另一套策略的分支。
    """

    if not isinstance(task_commands, TaskQueueInspectionPort):
        raise TypeError("task_commands 还必须实现 TaskQueueInspectionPort")
    if not isinstance(execution_limiter, TaskExecutionPermitPort):
        raise TypeError("execution_limiter 必须实现 TaskExecutionPermitPort")
    if not isinstance(process_guard, ProcessSingletonGuardPort):
        raise TypeError("process_guard 必须实现 ProcessSingletonGuardPort")
    validate_weaponry_runtime_capabilities(config, capabilities)
    policies = build_weaponry_runtime_policies(config)

    callback_recovery = RecoverWeaponryCallbackSynchronously(
        source=callback_recovery_source,
        callbacks=callbacks,
    )
    callback_guard_maintenance = FreezeExpiredWeaponryCallbackGuards(callbacks)
    resource_recovery = WeaponryResourceRecoveryService(
        store=resources,
        cleaner=resource_cleaner,
        audit=audit,
        task_commands=task_commands,
        creation_intent_recovery=creation_intent_recovery,
    )

    field_executor = WeaponryFieldExecutor(
        retrieval=retrieval,
        extraction=extraction,
        guidance=guidance,
        translation=translation,
        audit=audit,
    )
    runner = RunWeaponryTask(
        task_commands=task_commands,
        progress_publisher=progress_publisher,
        retrieval=retrieval,
        field_executor=field_executor,
        callbacks=callbacks,
        resources=resources,
    )
    dispatcher = LocalWeaponryTaskDispatcher(
        task_commands=task_commands,
        queue_inspector=task_commands,
        runner=runner,
        resource_maintenance=resource_recovery,
        callback_guard_maintenance=callback_guard_maintenance,
        config=config,
        execution_limiter=execution_limiter,
        process_guard=process_guard,
        startup_gate=startup_gate,
    )
    submit = SubmitWeaponryTask(
        task_commands=task_commands,
        progress_publisher=progress_publisher,
        dispatcher=dispatcher,
    )
    services = WeaponryApplicationServices(
        submit=submit,
        runner=runner,
        dispatcher=dispatcher,
        callbacks=callbacks,
        callback_recovery=callback_recovery,
        resource_recovery=resource_recovery,
        document_scope=document_scope,
        task_commands=task_commands,
        progress_publisher=progress_publisher,
        execution_limiter=execution_limiter,
        policies=policies,
        config=config,
    )
    logger.info(
        "武器谱应用组合根已构造: runtime_mode=%s profile_id=%s "
        "terms_enabled=%s background_started=false",
        config.runtime_mode,
        policies.evidence_selection.profile_id,
        config.terms_rule_context_enabled,
    )
    return services


__all__ = [
    "WeaponryApplicationServices",
    "compose_weaponry_application_services",
]
