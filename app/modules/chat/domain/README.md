# 文件对话领域模型目录说明

本目录定义文件对话的稳定业务语言。它不依赖 Flask、AnythingLLM、SQLite 或网络库，因此可被当前单实例适配器和未来共享持久化/队列适配器共同使用。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `__init__.py` | 导出领域模型、状态常量和资源标识辅助函数。 |
| `models.py` | 会话、运行、运行输入、文档绑定、消息、资源租约、清理任务及其状态集合。 |
| `document_candidates.py` | 受理前冻结的供应商无关文档候选。 |
| `document_scope.py` | Requested/Active/Effective Scope 的不可变 DTO、严格内部 Schema 和纯状态转换。 |
| `events.py` | 供应商无关的 `ChatStreamEvent`，作为应用层到展示层的内部事件载体。 |
| `resource_ids.py` | 生成稳定租约标识，并以自描述 JSON 封装工作区归属的远端资源引用。 |

## 领域关系

```text
ChatSession
  ├─ ChatRun -> ChatRunInput -> ChatRunInputFile
  ├─ ChatMessage -> ChatMessageFile
  ├─ ChatDocumentBinding（历史版本）与当前版本投影
  ├─ ChatResourceLease（工作区、线程、文档绑定）
  └─ ChatCleanupJob（对租约/会话资源的补偿任务）
```

## 状态流转原则

- 运行从受理到执行，再收敛为成功、失败或中断；内部 `run_id` 不属于外部协议。
- 消息先以待处理状态写入，只有在运行终态中按既定规则提交或丢弃。
- 资源租约先记录计划状态，再写入远端身份并激活；清理失败必须保留可审计、可重试的状态。
- 清理任务与资源租约分离：租约描述资源身份，任务描述补偿何时与如何重试。

## 维护规则

- 不得将供应商专有字段、HTTP 参数或 SSE 文本加入领域模型。
- 新状态必须同时明确状态集合、合法迁移、持久化约束和离线测试；不能只在某个服务中临时判断字符串。
