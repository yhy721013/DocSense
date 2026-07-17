# 既有 LLM 服务目录说明

本目录保存文件分析、报告、翻译、装备和任务等既有 LLM 业务服务。文件对话重构完成后，`/llm/chat*` 的实现已从此目录迁出，唯一正确位置是 `app/services/chat/`。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `__init__.py` | 既有 LLM 服务包标记。 |
| `analysis_service.py` | 文档分析业务服务。 |
| `interaction_audit_service.py` | 交互审计服务；阶段 1C-4 将内部审计 Schema 升级为 v3，无损保存 RAG `trace_id` 与每次 attempt 的 `call_id`。 |
| `knowledge_index_operation_service.py` | 知识索引操作服务。 |
| `rag_resource_lease_service.py` | 既有 RAG 资源租约服务。 |
| `report_service.py` | 报告生成遗留兼容实现；当前公开路由和生产组合根均不再调用，仅为黄金样例、旧测试与安全回滚观察保留。 |
| `task_service.py` | 既有任务投影/审计/回调服务；阶段 1C-3 增量提供 SQLite execution、原子受理/领取、expected TaskId 条件写及 Guard fencing；阶段 1C-4 以幂等补列兼容审计 Schema v3；阶段 1C-5 新增 Guard 人工解除追加审计、任务资源恢复记录、终态权威 Artifact 所有权及可恢复清理扫描。报告模块已通过 tasks Adapter 使用这些单实例兼容能力，其他业务仍在后续波次迁移。 |
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
- `task_service.py` 的阶段 1C-3 方法是单实例 SQLite 过渡实现，不得描述为 MySQL、Outbox、
  RabbitMQ、跨实例锁或可靠队列；业务模块应通过 tasks Port/Adapter 使用，不能继续把业务
  DTO 和外部 I/O 塞入该服务。
- 不得从生产路由、组合根或新业务代码重新导入 `report_service.run_report_task`。遗留文件
  只有在运行路径、测试与配置三类引用都清零后，才能按阶段 1G 的静态证据流程删除。
