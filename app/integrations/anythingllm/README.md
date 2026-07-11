# AnythingLLM 集成目录说明

本目录封装 AnythingLLM 的 HTTP 调用和响应兼容逻辑。文件对话仅通过 `chat_gateway.py` 和 `chat_factory.py` 接入本目录；其他文件提供可复用的原子客户端、传输和 RAG/知识索引能力。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `__init__.py` | 导出对外可用的网关、工厂和配置对象。 |
| `chat_factory.py` | 文件对话任务级工厂。每次上下文进入时创建独立传输和原子客户端，退出时负责关闭。 |
| `chat_gateway.py` | `ChatConversationPort` 的 AnythingLLM 实现：创建/复用工作区与线程、绑定文档、流式回复、读取快照、临时标题线程和幂等删除。 |
| `policies.py` | 重试、调用次数和工作区配置策略；文件对话工作区策略由 `chat_workspace_settings()` 集中提供。 |
| `transport.py` | HTTP 传输、认证、超时、重试及连接关闭的底层实现。 |
| `workspaces.py` | 工作区查询、创建、配置和删除的原子客户端。 |
| `threads.py` | 线程创建、消息流、历史读取和删除的原子客户端。 |
| `documents.py` | 工作区文档读取、定位和绑定的原子客户端。 |
| `models.py` | AnythingLLM 内部响应模型与字段归一化辅助结构。 |
| `errors.py` | 集成层稳定异常类型。 |
| `factory.py` | 既有 RAG/知识索引网关工厂，与文件对话工厂保持相同的生命周期原则。 |
| `rag_gateway.py` | 文档 RAG 的供应商适配器。 |
| `knowledge_gateway.py` | 知识索引操作的供应商适配器。 |

## 文件对话工作流

```text
ChatRunExecutor
  -> ChatConversationFactory.create()
  -> AnythingLLMChatConversationFactory 创建 Transport 与原子客户端
  -> AnythingLLMChatGateway
       -> workspaces / threads / documents
       -> 将供应商响应转换为 Chat Port DTO、领域事件或稳定异常
  -> 上下文退出，关闭 Transport
```

## 关键约束

- 网关不得返回 SSE 原始文本、AnythingLLM 原始字段或 `requests.Session` 给应用层。
- 工作区、线程和文档引用在上层视为不透明标识；本目录负责解释供应商字段差异。
- 临时标题线程和主会话资源的删除必须返回稳定结果，使上层可持久化租约与重试任务。
- 工厂与网关不得成为全局共享网络客户端；每个任务或流请求拥有独立生命周期。
