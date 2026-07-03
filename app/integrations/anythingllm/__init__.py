"""AnythingLLM 集成基础设施的公共入口。

当前阶段仅导出任务级 HTTP 传输对象、通用 SSE 事件和稳定异常类型。工作区、文档、
线程等供应商接口语义将在原子客户端中实现，不应进入传输层。
"""

from app.integrations.anythingllm.errors import (
    AnythingLLMConnectionError,
    AnythingLLMHTTPError,
    AnythingLLMProtocolError,
    AnythingLLMTimeoutError,
    AnythingLLMTransportClosedError,
    AnythingLLMTransportError,
)
from app.integrations.anythingllm.documents import AnythingLLMDocumentClient
from app.integrations.anythingllm.models import (
    AnythingLLMAnswer,
    AnythingLLMDocument,
    AnythingLLMSource,
    AnythingLLMThread,
    AnythingLLMWorkspace,
)
from app.integrations.anythingllm.threads import AnythingLLMThreadClient
from app.integrations.anythingllm.transport import AnythingLLMTransport, SSEEvent
from app.integrations.anythingllm.workspaces import AnythingLLMWorkspaceClient

__all__ = [
    "AnythingLLMAnswer",
    "AnythingLLMConnectionError",
    "AnythingLLMDocument",
    "AnythingLLMDocumentClient",
    "AnythingLLMHTTPError",
    "AnythingLLMProtocolError",
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
]
