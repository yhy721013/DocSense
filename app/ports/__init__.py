"""应用层的供应商无关端口。

该包只导出业务 DTO、稳定异常和 Protocol。业务服务通过这些类型表达意图，具体第三方
系统的协议、认证、资源字段和错误对象只能出现在适配器层，测试替身也不进入生产包。
"""

from .knowledge_index import (
    CollectionRef,
    IndexedDocument,
    KnowledgeIndexFactory,
    KnowledgeIndexPort,
    OperationResult,
)
from .rag import (
    CleanupResult,
    DocumentRagFactory,
    DocumentRagPort,
    DocumentRagSession,
    MAX_RAG_QUERY_ATTEMPTS,
    PreparedDocumentRef,
    RagAttempt,
    RagExecutionTrace,
    RagLifecycleEvent,
    RagOperationError,
    RagResult,
    RagSource,
    validate_rag_query_max_attempts,
)

__all__ = [
    "CleanupResult",
    "CollectionRef",
    "DocumentRagFactory",
    "DocumentRagPort",
    "DocumentRagSession",
    "IndexedDocument",
    "KnowledgeIndexFactory",
    "KnowledgeIndexPort",
    "MAX_RAG_QUERY_ATTEMPTS",
    "OperationResult",
    "PreparedDocumentRef",
    "RagAttempt",
    "RagExecutionTrace",
    "RagLifecycleEvent",
    "RagOperationError",
    "RagResult",
    "RagSource",
    "validate_rag_query_max_attempts",
]
