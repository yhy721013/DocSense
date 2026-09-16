"""分类节点变更的基础设施适配器。

阶段 1E-2R 提供本地 SQLite 事实边界；阶段 1E-3 新增请求级 AnythingLLM Client Factory、
严格预算和 Knowledge Port Adapter；阶段 1E-6 的生产组合根通过注入的 Port Factory 为每次
Application 执行请求新的 Knowledge Port。公开路由不直接构造具体 Adapter 或 Client。
"""

from .anythingllm_clients import (
    AnythingLLMReassignmentClientFactory,
    ReassignmentAnythingLLMClientFactoryProtocol,
    ReassignmentAnythingLLMClients,
)
from .anythingllm_knowledge import (
    AnythingLLMReassignmentKnowledgeAdapter,
    AnythingLLMReassignmentKnowledgeAdapterFactory,
)
from .infrastructure_config import (
    ReassignmentDeadlineExceededError,
    ReassignmentExecutionDeadline,
    ReassignmentInfrastructureConfig,
    ReassignmentInfrastructureConfigurationError,
    load_reassignment_infrastructure_config,
)
from .sqlite_repository import (
    SQLiteReassignmentRepository,
    SQLiteReassignmentUnitOfWork,
)

__all__ = [
    "AnythingLLMReassignmentClientFactory",
    "AnythingLLMReassignmentKnowledgeAdapter",
    "AnythingLLMReassignmentKnowledgeAdapterFactory",
    "ReassignmentAnythingLLMClientFactoryProtocol",
    "ReassignmentAnythingLLMClients",
    "ReassignmentDeadlineExceededError",
    "ReassignmentExecutionDeadline",
    "ReassignmentInfrastructureConfig",
    "ReassignmentInfrastructureConfigurationError",
    "SQLiteReassignmentRepository",
    "SQLiteReassignmentUnitOfWork",
    "load_reassignment_infrastructure_config",
]
