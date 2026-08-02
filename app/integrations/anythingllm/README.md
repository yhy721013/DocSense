# AnythingLLM 集成目录说明

本目录封装 AnythingLLM 的共享原子客户端、传输和 RAG/知识索引能力。Chat 专属网关与工厂位于 `app/modules/chat/adapters/`，并复用这里的原子客户端。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `__init__.py` | 导出对外可用的网关、工厂和配置对象。 |
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

## Chat 集成工作流

```text
ChatRunExecutor
  -> app/modules/chat/adapters/anythingllm_factory.py
  -> 工厂创建本目录的 Transport 与原子客户端
  -> app/modules/chat/adapters/anythingllm_gateway.py
       -> workspaces / threads / documents
       -> 将供应商响应转换为 Chat Port DTO、领域事件或稳定异常
  -> 上下文退出，关闭 Transport
```

## 关键约束

- 网关不得返回 SSE 原始文本、AnythingLLM 原始字段或 `requests.Session` 给应用层。
- 工作区、线程和文档引用在上层视为不透明标识；本目录负责解释供应商字段差异。
- 临时标题线程和主会话资源的删除必须返回稳定结果，使上层可持久化租约与重试任务。
- 工厂与网关不得成为全局共享网络客户端；每个任务或流请求拥有独立生命周期。
