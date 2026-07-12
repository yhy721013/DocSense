# 既有 LLM 服务目录说明

本目录保存文件分析、报告、翻译、装备和任务等既有 LLM 业务服务。文件对话重构完成后，`/llm/chat*` 的实现已从此目录迁出，唯一正确位置是 `app/services/chat/`。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `__init__.py` | 既有 LLM 服务包标记。 |
| `analysis_service.py` | 文档分析业务服务。 |
| `interaction_audit_service.py` | 交互审计服务。 |
| `knowledge_index_operation_service.py` | 知识索引操作服务。 |
| `rag_resource_lease_service.py` | 既有 RAG 资源租约服务。 |
| `report_service.py` | 报告生成服务。 |
| `task_service.py` | 任务编排服务。 |
| `translation_service.py` | 翻译业务服务。 |
| `weaponry_service.py` | 装备相关业务服务。 |

## 文件对话迁移说明

历史上的 `chat_service.py` 已删除。文件对话的流式执行、标题、历史、中断、删除、资源租约和清理任务分别由 `services/chat/application/`、`domain/`、`persistence/` 与 `locking/` 承担。

```text
禁止：llm_service -> 直接编排文件对话 AnythingLLM 调用
允许：路由 -> services/chat/application -> Chat Port -> 集成层
```

## 维护规则

- 不要重新创建 `chat_service.py`，也不要把 `/llm/chat*` 新需求写入本目录。
- 若既有 LLM 服务需要与文件对话协作，应依赖 `services/chat` 的稳定应用服务或端口，而不是复制会话状态机。
