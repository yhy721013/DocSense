# 通用工具目录说明

本目录保存跨业务模块可复用的工具与迁移期兼容封装。文件对话重构将新的供应商调用迁入 `integrations/anythingllm/`，因此本目录只保留兼容门面和调试辅助，不能继续增长新的文件对话编排代码。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `__init__.py` | 工具包标记。 |
| `anythingllm_client.py` | AnythingLLM 迁移期兼容门面，供尚未迁移的旧业务使用；新的文件对话不得直接依赖。 |
| `chat_debug_preview.py` | 根据本地权威文件对话数据生成调试预览，供 `blueprints/debug.py` 使用。 |
| `callback_client.py`、`callback_preview.py` | 既有回调调用与调试预览工具。 |
| `file_downloader.py` | 文件下载辅助。 |
| `mhtml_normalizer.py`、`ocr_preprocessor.py`、`word_extractor.py` | 文件预处理与内容提取辅助。 |
| `rag_pipeline.py` | 既有 RAG 流水线辅助。 |

## 文件对话相关流程

1. 生产文件对话通过 `ChatConversationFactory` 进入 `integrations/anythingllm/`，不经过兼容门面。
2. 调试蓝图调用 `chat_debug_preview.py`，从本地会话、消息、租约和任务状态生成可观察数据。
3. 兼容门面仅用于存量非文件对话调用，便于逐步迁移而不扩散耦合。

## 维护规则

- 不要向 `anythingllm_client.py` 新增文件对话工作区、线程、标题、删除或流式编排方法。
- 调试工具不可把内部 `run_id`、远端资源引用或异常堆栈泄露到公开 `/llm/chat*` 响应。
- 共享工具应保持无请求级可变状态，避免多线程/多实例下隐藏耦合。
