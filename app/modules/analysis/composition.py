"""文件分析生产/离线组合根。

这里是 Analysis 运行链唯一允许把 Application、Port 与 Adapter 装配在一起的位置。它不
导入 Flask 蓝图、不读取 Request、不创建线程，也不访问 AnythingLLM；调用方必须显式提供
所有 I/O 边界，并在容器生命周期阶段显式调用 Dispatcher.start()。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any

from app.modules.analysis.adapters.local_dispatcher import (
    LocalAnalysisDispatcherSnapshot,
    LocalAnalysisTaskDispatcher,
)
from app.modules.analysis.adapters.v2_runtime import AnalysisV2TaskDispatcher
from app.modules.analysis.adapters.resource_activity import (
    InMemoryAnalysisResourceActivityAdapter,
)
from app.modules.analysis.application import (
    FreezeExpiredAnalysisCallbackGuards,
    RecoverAnalysisCallbackSynchronously,
    RecoverAnalysisResources,
    RunAnalysisTask,
    SubmitAnalysisBatch,
)
from app.modules.analysis.domain.task_inputs import (
    AnalysisTaskInputV1,
    AnalysisTranslationProfile,
)
from app.modules.analysis.domain.execution_profile import AnalysisExecutionProfile
from app.modules.analysis.application.workflow_models import AnalysisTaskCompletion
from app.modules.analysis.ports import (
    AnalysisAuditPort,
    AnalysisBatchAdmissionPort,
    AnalysisBatchCommandPort,
    AnalysisCallbackPort,
    AnalysisCallbackRecoverySourcePort,
    AnalysisDispatchFailureBackoffPort,
    AnalysisKnowledgePort,
    AnalysisPoisonTaskCommandPort,
    AnalysisRagPortFactory,
    AnalysisResourceActivityPort,
    AnalysisResourcePort,
    AnalysisTaskWorkspacePort,
    AnalysisTranslationPort,
    FilePreparationPort,
)
from app.modules.tasks.ports import (
    GuardedProgressPublisherPort,
    ProcessSingletonGuardPort,
    TaskCommandPort,
    TaskExecutionPermitPort,
    TaskQueueInspectionPort,
    ProgressPublisherPort,
)
from app.modules.analysis.adapters.runtime_config import AnalysisInfrastructureConfig


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnalysisApplicationServices:
    """Analysis 单实例运行链的依赖集合，仅供容器和离线组合测试读取。"""

    submit: SubmitAnalysisBatch
    runner: RunAnalysisTask
    dispatcher: LocalAnalysisTaskDispatcher
    callbacks: AnalysisCallbackPort
    callback_recovery: RecoverAnalysisCallbackSynchronously
    resource_recovery: RecoverAnalysisResources
    resource_activity: AnalysisResourceActivityPort
    task_commands: TaskCommandPort[
        object,
        AnalysisTaskInputV1,
        AnalysisTaskCompletion,
    ]
    progress_publisher: GuardedProgressPublisherPort
    execution_limiter: TaskExecutionPermitPort
    config: AnalysisInfrastructureConfig


@dataclass(frozen=True)
class AnalysisV2ApplicationServices:
    """阶段 2-6 生产对象图；不暴露旧 TaskCommand/Runner Authority 入口。"""

    submit: SubmitAnalysisBatch
    dispatcher: AnalysisV2TaskDispatcher
    callbacks: AnalysisCallbackPort
    callback_recovery: RecoverAnalysisCallbackSynchronously
    resource_recovery: RecoverAnalysisResources
    progress_publisher: ProgressPublisherPort
    execution_limiter: TaskExecutionPermitPort
    execution_profile: AnalysisExecutionProfile
    translation_profile: AnalysisTranslationProfile
    config: AnalysisInfrastructureConfig

    def __post_init__(self) -> None:
        if not isinstance(self.submit, SubmitAnalysisBatch):
            raise TypeError("submit 必须是 SubmitAnalysisBatch")
        if not isinstance(self.dispatcher, AnalysisV2TaskDispatcher):
            raise TypeError("dispatcher 必须是 AnalysisV2TaskDispatcher")
        if not isinstance(self.callbacks, AnalysisCallbackPort):
            raise TypeError("callbacks 必须实现 AnalysisCallbackPort")
        if not isinstance(self.callback_recovery, RecoverAnalysisCallbackSynchronously):
            raise TypeError("callback_recovery 类型错误")
        if not isinstance(self.resource_recovery, RecoverAnalysisResources):
            raise TypeError("resource_recovery 类型错误")
        if not isinstance(self.progress_publisher, ProgressPublisherPort):
            raise TypeError("progress_publisher 必须实现 ProgressPublisherPort")
        if not isinstance(self.execution_limiter, TaskExecutionPermitPort):
            raise TypeError("execution_limiter 必须实现 TaskExecutionPermitPort")
        if not isinstance(self.execution_profile, AnalysisExecutionProfile):
            raise TypeError("execution_profile 必须是 AnalysisExecutionProfile")
        if not isinstance(self.translation_profile, AnalysisTranslationProfile):
            raise TypeError("translation_profile 必须是 TranslationProfile")
        if not isinstance(self.config, AnalysisInfrastructureConfig):
            raise TypeError("config 必须是 AnalysisInfrastructureConfig")
        if self.submit.dispatcher is not self.dispatcher:
            raise ValueError("Analysis v2 Submit 与 Dispatcher 必须共享实例")
        if not (
            self.callback_recovery.callbacks
            is self.dispatcher.callbacks
            is self.callbacks
        ):
            raise ValueError("Analysis Worker/check-task/维护必须共享 Callback Guard")
        if self.dispatcher.resources is not self.resource_recovery:
            raise ValueError("Analysis Worker 与维护必须共享资源恢复状态机")
        if self.dispatcher.execution_limiter is not self.execution_limiter:
            raise ValueError("Analysis Dispatcher 必须使用容器共享的重型任务 limiter")

    def start(self) -> None:
        self.dispatcher.start()

    def stop(self, *, timeout_seconds: float | None = None) -> bool:
        return self.dispatcher.stop(timeout_seconds=timeout_seconds)

    def close(self) -> None:
        self.dispatcher.close()

    def snapshot(self) -> LocalAnalysisDispatcherSnapshot:
        return self.dispatcher.snapshot()


def compose_analysis_v2_application_services(
    *,
    admission: AnalysisBatchAdmissionPort,
    dispatcher: AnalysisV2TaskDispatcher,
    callbacks: AnalysisCallbackPort,
    callback_recovery_source: AnalysisCallbackRecoverySourcePort,
    resource_recovery: RecoverAnalysisResources,
    progress_publisher: ProgressPublisherPort,
    execution_limiter: TaskExecutionPermitPort,
    execution_profile: AnalysisExecutionProfile,
    translation_profile: AnalysisTranslationProfile,
    config: AnalysisInfrastructureConfig,
    callback_url: str,
) -> AnalysisV2ApplicationServices:
    """组装已经完成 v2 基础设施构造的单一生产对象图，不启动后台线程。"""

    if not isinstance(admission, AnalysisBatchAdmissionPort):
        raise TypeError("admission 必须实现 AnalysisBatchAdmissionPort")
    if not isinstance(dispatcher, AnalysisV2TaskDispatcher):
        raise TypeError("dispatcher 必须是 AnalysisV2TaskDispatcher")
    if not isinstance(callbacks, AnalysisCallbackPort):
        raise TypeError("callbacks 必须实现 AnalysisCallbackPort")
    if not isinstance(callback_recovery_source, AnalysisCallbackRecoverySourcePort):
        raise TypeError("callback_recovery_source 必须实现 AnalysisCallbackRecoverySourcePort")
    if not isinstance(resource_recovery, RecoverAnalysisResources):
        raise TypeError("resource_recovery 必须是 RecoverAnalysisResources")
    if not isinstance(progress_publisher, ProgressPublisherPort):
        raise TypeError("progress_publisher 必须实现 ProgressPublisherPort")
    if not isinstance(execution_limiter, TaskExecutionPermitPort):
        raise TypeError("execution_limiter 必须实现 TaskExecutionPermitPort")
    if not isinstance(config, AnalysisInfrastructureConfig):
        raise TypeError("config 必须是 AnalysisInfrastructureConfig")
    if not isinstance(callback_url, str):
        raise TypeError("callback_url 必须是 str")
    callback_recovery = RecoverAnalysisCallbackSynchronously(
        source=callback_recovery_source,
        callbacks=callbacks,
        callback_url=callback_url,
    )
    services = AnalysisV2ApplicationServices(
        submit=SubmitAnalysisBatch(
            batch_commands=admission,
            dispatcher=dispatcher,
        ),
        dispatcher=dispatcher,
        callbacks=callbacks,
        callback_recovery=callback_recovery,
        resource_recovery=resource_recovery,
        progress_publisher=progress_publisher,
        execution_limiter=execution_limiter,
        execution_profile=execution_profile,
        translation_profile=translation_profile,
        config=config,
    )
    logger.info(
        "文件分析 v2 应用组合根已构造: runtime_mode=%s profile_prefix=%s "
        "callback_configured=%s background_started=false",
        config.runtime_mode,
        execution_profile.fingerprint[:12],
        bool(callback_url.strip()),
    )
    return services


def compose_analysis_application_services(
    *,
    task_commands: TaskCommandPort[
        object,
        AnalysisTaskInputV1,
        AnalysisTaskCompletion,
    ],
    progress_publisher: GuardedProgressPublisherPort,
    workspaces: AnalysisTaskWorkspacePort,
    files: FilePreparationPort,
    rag_factory: AnalysisRagPortFactory,
    knowledge: AnalysisKnowledgePort,
    audit: AnalysisAuditPort,
    translation: AnalysisTranslationPort,
    callbacks: AnalysisCallbackPort,
    callback_recovery_source: AnalysisCallbackRecoverySourcePort,
    resources: AnalysisResourcePort,
    execution_limiter: TaskExecutionPermitPort,
    process_guard: ProcessSingletonGuardPort,
    config: AnalysisInfrastructureConfig,
    callback_url: str,
    fatal_error_handler: Callable[[str], None] | None = None,
) -> AnalysisApplicationServices:
    """显式装配一条 Analysis 运行链，但绝不启动后台线程或真实外部调用。

    ``task_commands`` 必须同时承担批量受理、Worker 条件写、只读队列诊断、毒快照终态与
    事务级退避，保证这些动作面对的是同一份 execution 事实。生产容器与离线测试均从本
    函数进入，避免出现“测试链”和“生产链”各自维护一套隐藏装配规则。
    """

    if not isinstance(task_commands, TaskCommandPort):
        raise TypeError("task_commands 必须实现 TaskCommandPort")
    if not isinstance(task_commands, AnalysisBatchCommandPort):
        raise TypeError("task_commands 还必须实现 AnalysisBatchCommandPort")
    if not isinstance(task_commands, AnalysisPoisonTaskCommandPort):
        raise TypeError("task_commands 还必须实现 AnalysisPoisonTaskCommandPort")
    if not isinstance(task_commands, TaskQueueInspectionPort):
        raise TypeError("task_commands 还必须实现 TaskQueueInspectionPort")
    if not isinstance(task_commands, AnalysisDispatchFailureBackoffPort):
        raise TypeError(
            "task_commands 还必须实现 AnalysisDispatchFailureBackoffPort"
        )
    if not isinstance(progress_publisher, GuardedProgressPublisherPort):
        raise TypeError("progress_publisher 必须实现 GuardedProgressPublisherPort")
    if not isinstance(workspaces, AnalysisTaskWorkspacePort):
        raise TypeError("workspaces 必须实现 AnalysisTaskWorkspacePort")
    if not isinstance(files, FilePreparationPort):
        raise TypeError("files 必须实现 FilePreparationPort")
    if not isinstance(rag_factory, AnalysisRagPortFactory):
        raise TypeError("rag_factory 必须实现 AnalysisRagPortFactory")
    if not isinstance(knowledge, AnalysisKnowledgePort):
        raise TypeError("knowledge 必须实现 AnalysisKnowledgePort")
    if not isinstance(audit, AnalysisAuditPort):
        raise TypeError("audit 必须实现 AnalysisAuditPort")
    if not isinstance(translation, AnalysisTranslationPort):
        raise TypeError("translation 必须实现 AnalysisTranslationPort")
    if not isinstance(callbacks, AnalysisCallbackPort):
        raise TypeError("callbacks 必须实现 AnalysisCallbackPort")
    if not isinstance(
        callback_recovery_source,
        AnalysisCallbackRecoverySourcePort,
    ):
        raise TypeError(
            "callback_recovery_source 必须实现 AnalysisCallbackRecoverySourcePort"
        )
    if not isinstance(resources, AnalysisResourcePort):
        raise TypeError("resources 必须实现 AnalysisResourcePort")
    if not isinstance(execution_limiter, TaskExecutionPermitPort):
        raise TypeError("execution_limiter 必须实现 TaskExecutionPermitPort")
    if not isinstance(process_guard, ProcessSingletonGuardPort):
        raise TypeError("process_guard 必须实现 ProcessSingletonGuardPort")
    if not isinstance(config, AnalysisInfrastructureConfig):
        raise TypeError("config 必须是 AnalysisInfrastructureConfig")
    if not isinstance(callback_url, str):
        raise TypeError("callback_url 必须是 str")

    callback_recovery = RecoverAnalysisCallbackSynchronously(
        source=callback_recovery_source,
        callbacks=callbacks,
        callback_url=callback_url,
    )
    callback_guard_maintenance = FreezeExpiredAnalysisCallbackGuards(callbacks)
    # 资源恢复只补齐可证明幂等的审计写入；与 Dispatcher 的 accepted 退避共用同一组
    # 受限 base/max，避免把一个故障域配置成无限快速重试、另一个无限长时间沉默。
    resource_activity = InMemoryAnalysisResourceActivityAdapter()
    resource_recovery = RecoverAnalysisResources(
        store=resources,
        audit=audit,
        retry_base_seconds=config.dispatch_retry_base_seconds,
        retry_max_seconds=config.dispatch_retry_max_seconds,
        resource_activity=resource_activity,
    )
    runner = RunAnalysisTask(
        task_commands=task_commands,
        progress_publisher=progress_publisher,
        workspaces=workspaces,
        files=files,
        rag_factory=rag_factory,
        knowledge=knowledge,
        audit=audit,
        translation=translation,
        resources=resources,
        callbacks=callbacks,
        callback_url=callback_url,
        resource_close_running_grace_seconds=(
            config.resource_close_running_grace_seconds
        ),
        resource_activity=resource_activity,
    )
    dispatcher = LocalAnalysisTaskDispatcher(
        task_commands=task_commands,
        queue_inspector=task_commands,
        poison_commands=task_commands,
        dispatch_failure_backoff=task_commands,
        execute=runner.execute,
        resource_maintenance=resource_recovery,
        callback_guard_maintenance=callback_guard_maintenance,
        poison_callback_recovery=callback_recovery.execute,
        config=config,
        execution_limiter=execution_limiter,
        process_guard=process_guard,
        fatal_error_handler=fatal_error_handler,
    )
    submit = SubmitAnalysisBatch(
        batch_commands=task_commands,
        dispatcher=dispatcher,
    )
    services = AnalysisApplicationServices(
        submit=submit,
        runner=runner,
        dispatcher=dispatcher,
        callbacks=callbacks,
        callback_recovery=callback_recovery,
        resource_recovery=resource_recovery,
        resource_activity=resource_activity,
        task_commands=task_commands,
        progress_publisher=progress_publisher,
        execution_limiter=execution_limiter,
        config=config,
    )
    logger.info(
        "文件分析应用组合根已构造: runtime_mode=%s callback_configured=%s "
        "background_started=false",
        config.runtime_mode,
        bool(callback_url.strip()),
    )
    return services


__all__ = (
    "AnalysisApplicationServices",
    "AnalysisV2ApplicationServices",
    "compose_analysis_application_services",
    "compose_analysis_v2_application_services",
)
