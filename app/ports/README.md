# 业务端口目录说明

本目录定义业务层依赖的稳定能力边界。端口只表达“需要完成什么”，不暴露 AnythingLLM 工作区字段、HTTP 请求结构、SSE 文本或具体数据库实现。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `__init__.py` | 统一导出端口、数据传输对象和稳定异常，供应用服务与适配器使用。 |
| `chat.py` | 文件对话端口。定义会话引用、文档引用、消息快照、操作结果、对话端口、任务级工厂及异常语义。 |
| `rag.py` | 既有文档 RAG 能力端口。 |
| `knowledge_index.py` | 既有知识索引能力端口。 |

## 文件对话调用方向

```text
application 服务
  -> ChatConversationFactory
  -> ChatConversationPort
  -> AnythingLLMChatGateway（基础设施适配器）
```

应用服务只应传递 `ChatSessionRefs`、`ChatDocumentRef` 等供应商无关对象；适配器负责将其映射为具体工作区、线程与文档调用。

## 维护规则

- 不要在 `chat.py` 中新增 HTTP 请求字段、SSE 事件文本、AnythingLLM 专有字段或测试替身。
- 新能力优先通过新的端口方法和稳定 DTO 表达；是否影响 `/llm/chat*` 契约必须先确认，且不得随意增删前后端接口参数。
- 端口接口应能被离线替身实现，以保障应用服务测试不依赖远端系统。
