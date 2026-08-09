# 通用工具目录说明

本目录只保存不拥有业务状态的少量跨模块工具。阶段 1G 已删除旧 AnythingLLM 聚合 Client、
Debug Preview、RAG Pipeline 及 MHTML/OCR 文件处理 Facade；文件对话、调试查询和文档处理分别由
`app/modules/chat`、`app/modules/debug` 和 `app/modules/document_processing` 拥有。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `__init__.py` | 工具包标记。 |
| `callback_client.py` | 既有 HTTP Callback 发送辅助；具体 Guard、重试授权和业务载荷仍由各业务模块拥有。 |
| `file_downloader.py` | 文件下载辅助。 |
| `word_extractor.py` | DOCX 文本提取辅助，不拥有任务、Artifact 或翻译生命周期。 |

## 所有权边界

1. AnythingLLM 协议调用统一进入 `app/integrations/anythingllm`，业务编排留在各业务模块。
2. `/debug/*` 通过 `app/modules/debug` 的 Query/Port/Adapter 读取只读快照。
3. 格式转换、OCR、MHTML、MinerU 和 Artifact 谱系统一归 `app/modules/document_processing`。

## 维护规则

- 不得重新创建旧 AnythingLLM、Debug Preview、RAG 或文件处理 Facade。
- 调试工具不可把内部 `run_id`、远端资源引用或异常堆栈泄露到公开 `/llm/chat*` 响应。
- 共享工具应保持无请求级可变状态，避免多线程/多实例下隐藏耦合。
