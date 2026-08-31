"""AnythingLLM 集成基础设施的公共入口。

公共入口导出任务级 HTTP 传输、原子 Client、稳定 DTO、异常和文档 RAG Gateway。
传输层保持供应商业务语义无关，跨原子接口的状态机只允许存在于 Gateway。
"""

from app.integrations.anythingllm.errors import (
    AnythingLLMConnectionError,
    AnythingLLMHTTPError,
    AnythingLLMProtocolError,
    AnythingLLMTimeoutError,
    AnythingLLMTransportClosedError,
    AnythingLLMTransportError,
)
from app.integrations.anythingllm.documents import (
    AnythingLLMDocumentClient,
    XlsxFolderInventoryItem,
)
from app.integrations.anythingllm.factory import (
    AnythingLLMGatewayFactory,
    AnythingLLMKnowledgeIndexFactory,
)
from app.integrations.anythingllm.knowledge_gateway import (
    AnythingLLMKnowledgeGateway,
)
from app.integrations.anythingllm.models import (
    AnythingLLMAnswer,
    AnythingLLMDocument,
    AnythingLLMSource,
    AnythingLLMThread,
    AnythingLLMWorkspace,
)
from app.integrations.anythingllm.rag_gateway import AnythingLLMRagGateway
from app.integrations.anythingllm.threads import AnythingLLMThreadClient
from app.integrations.anythingllm.transport import AnythingLLMTransport, SSEEvent
from app.integrations.anythingllm.workspaces import AnythingLLMWorkspaceClient

__all__ = [
    "AnythingLLMAnswer",
    "AnythingLLMConnectionError",
    "AnythingLLMDocument",
    "AnythingLLMDocumentClient",
    "AnythingLLMHTTPError",
    "AnythingLLMGatewayFactory",
    "AnythingLLMKnowledgeGateway",
    "AnythingLLMKnowledgeIndexFactory",
    "AnythingLLMProtocolError",
    "AnythingLLMRagGateway",
    "AnythingLLMSource",
    "AnythingLLMThread",
    "AnythingLLMThreadClient",
    "AnythingLLMTimeoutError",
    "AnythingLLMTransport",
    "AnythingLLMTransportClosedError",
    "AnythingLLMTransportError",
    "AnythingLLMWorkspace",
    "AnythingLLMWorkspaceClient",
    "SSEEvent",
    "XlsxFolderInventoryItem",
]
