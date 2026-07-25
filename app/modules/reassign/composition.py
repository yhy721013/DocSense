"""分类节点变更模块的显式应用组合根。

组合根只连接已经构造好的 Port、配置和 Application Service；它不读取环境变量、不创建
Flask Response，也不启动线程。生产 Container 是唯一负责创建 SQLite Repository 与请求级
AnythingLLM Factory 的位置，未来 Dispatcher/Worker 只能持有本文件返回的 Application 外观。
"""

from __future__ import annotations

from dataclasses import dataclass
import logging

from .adapters import ReassignmentInfrastructureConfig
from .application import (
    DocumentReassignmentService,
    RecoverReassignmentOperation,
    ReassignmentExecutionSettings,
)
from .ports import ReassignmentKnowledgePortFactory, ReassignmentRepositoryPort


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReassignmentPortBundle:
    """同步执行与恢复服务共享的 Repository 和请求级 Knowledge Factory。"""

    repository: ReassignmentRepositoryPort
    knowledge_factory: ReassignmentKnowledgePortFactory

    def __post_init__(self) -> None:
        if not isinstance(self.repository, ReassignmentRepositoryPort):
            raise TypeError("repository 必须实现 ReassignmentRepositoryPort")
        if not isinstance(self.knowledge_factory, ReassignmentKnowledgePortFactory):
            raise TypeError(
                "knowledge_factory 必须实现 ReassignmentKnowledgePortFactory"
            )


@dataclass(frozen=True)
class ReassignApplicationServices:
    """分类节点变更唯一的生产 Application 外观。

    Container、未来 Dispatcher 和 Worker 只能调用 ``document_reassignment`` 或 ``recovery``，
    不能绕过这两个用例直接写 Repository 终态。Port Bundle、基础设施配置和执行设置只在
    ``compose_reassign_application_services()`` 内部使用，不通过该外观泄露给调用方。
    """

    document_reassignment: DocumentReassignmentService
    recovery: RecoverReassignmentOperation

    def __post_init__(self) -> None:
        if not isinstance(self.document_reassignment, DocumentReassignmentService):
            raise TypeError("document_reassignment 必须是 DocumentReassignmentService")
        if not isinstance(self.recovery, RecoverReassignmentOperation):
            raise TypeError("recovery 必须是 RecoverReassignmentOperation")


def compose_reassign_application_services(
    *,
    repository: ReassignmentRepositoryPort,
    knowledge_factory: ReassignmentKnowledgePortFactory,
    settings: ReassignmentExecutionSettings,
    infrastructure_config: ReassignmentInfrastructureConfig,
) -> ReassignApplicationServices:
    """以一份 Port 与设置装配同步执行和显式恢复两个 Application 用例。

    函数本身不发起数据库或网络 I/O。调用方必须先创建无状态 Factory；每次同步请求仍由
    Knowledge Factory 生成独立 deadline/Transport，从而避免跨请求、跨线程共享可变网络状态。
    """

    ports = ReassignmentPortBundle(
        repository=repository,
        knowledge_factory=knowledge_factory,
    )
    if not isinstance(settings, ReassignmentExecutionSettings):
        raise TypeError("settings 必须是 ReassignmentExecutionSettings")
    if not isinstance(infrastructure_config, ReassignmentInfrastructureConfig):
        raise TypeError(
            "infrastructure_config 必须是 ReassignmentInfrastructureConfig"
        )
    if (
        settings.remote_total_timeout_seconds
        != infrastructure_config.total_timeout_seconds
    ):
        raise ValueError(
            "settings.remote_total_timeout_seconds 必须等于基础设施总预算"
        )
    if settings.lease_safety_margin_seconds <= 0.0:
        raise ValueError("分类节点变更 lease 安全余量必须为正数")

    services = ReassignApplicationServices(
        document_reassignment=DocumentReassignmentService(
            ports.repository,
            ports.knowledge_factory,
            settings,
        ),
        recovery=RecoverReassignmentOperation(
            ports.repository,
            ports.knowledge_factory,
            settings,
        ),
    )
    logger.info(
        "分类节点变更应用组合根已构造: runtime_mode=%s total_timeout_seconds=%.3f "
        "lease_duration_seconds=%.3f background_started=false",
        infrastructure_config.runtime_mode,
        infrastructure_config.total_timeout_seconds,
        settings.lease_duration_seconds,
    )
    return services


__all__ = [
    "ReassignApplicationServices",
    "ReassignmentPortBundle",
    "compose_reassign_application_services",
]
