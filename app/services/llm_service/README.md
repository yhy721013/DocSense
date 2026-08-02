# 共享兼容持久化目录说明

本目录只保留阶段 1 尚在使用的共享 SQLite 事实服务：任务/执行/回调审计、知识索引操作和 RAG
资源租约。Analysis、Report、Weaponry、Translation 的业务执行实现已经分别归入 `app/modules/`；
Chat 业务的唯一正确位置是 `app/modules/chat/`。本目录不得重新成为跨业务编排层。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `__init__.py` | 既有 LLM 服务包标记。 |
| `analysis_service.py` | 已于阶段 1G-5A1 删除；文档分析生产实现统一位于 `app/modules/analysis/`。历史更新记录中的旧路径名称继续保留。 |
| `interaction_audit_service.py` | 交互审计服务；阶段 1C-4 将内部审计 Schema 升级为 v3，无损保存 RAG `trace_id` 与每次 attempt 的 `call_id`。 |
| `knowledge_index_operation_service.py` | 知识索引操作服务。 |
| `rag_resource_lease_service.py` | 既有 RAG 资源租约服务。 |
| `report_service.py` | 已于阶段 1G-5A3 删除；报告生产实现统一位于 `app/modules/report/`。历史更新记录中的旧路径名称继续保留。 |
| `task_service.py` | 既有任务投影/审计/回调服务；阶段 1C-3 增量提供 SQLite execution、原子受理/领取、expected TaskId 条件写及 Guard fencing；阶段 1C-4 以幂等补列兼容审计 Schema v3；阶段 1C-5 新增 Guard 人工解除追加审计、任务资源恢复记录、终态权威 Artifact 所有权及可恢复清理扫描。2026-07-29 又新增 `callback_delivery_attempt_events` 追加审计，将 file/report/weaponry 的初次发送、显式 check-task 授权、完成、过期冻结和不一致冻结与 Guard CAS 放在同一 SQLite 事务内；该能力仍仅是单实例兼容实现。 |
| `translation_service.py` | 已于阶段 1G-5A5 删除；翻译生产实现统一位于 `app/modules/translation/`。历史更新记录中的旧路径名称继续保留。 |
| `weaponry_service.py` | 已于阶段 1G-5A4 删除；武器谱生产实现统一位于 `app/modules/weaponry/`。历史更新记录中的旧路径名称继续保留。 |

## 文件对话迁移说明

历史上的 `chat_service.py` 和 `app/services/chat/` 已删除。文件对话与知识谱系对话的流式执行、
标题、历史、中断、删除、资源租约和清理任务统一由 `app/modules/chat/` 的 Application、Domain、
Ports 与 Adapters 承担。

```text
禁止：llm_service -> 直接编排文件对话 AnythingLLM 调用
允许：路由 -> modules/chat/application -> modules/chat/ports <- modules/chat/adapters
```

## 维护规则

- 不要重新创建 `chat_service.py`，也不要把 `/llm/chat*` 新需求写入本目录。
- 若既有 LLM 服务需要与 Chat 协作，应依赖 `app/modules/chat` 的稳定应用服务或端口，而不是复制会话状态机。
- `task_service.py` 的阶段 1C-3 方法是单实例 SQLite 过渡实现，不得描述为 MySQL、Outbox、
  RabbitMQ、跨实例锁或可靠队列；业务模块应通过 tasks Port/Adapter 使用，不能继续把业务
  DTO 和外部 I/O 塞入该服务。
- 不得重新创建已经删除的旧报告或武器谱 Worker；阶段 1G 的永久静态门禁负责阻止旧路径回流。
