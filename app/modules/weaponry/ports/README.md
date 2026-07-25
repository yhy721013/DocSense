# Weaponry Ports

本层定义 Document Scope、Retrieval、Extraction、Auxiliary Guidance、Translation、Audit、
Callback、Resource 与 Dispatcher 抽象。阶段 1D-2 已实现只读 Document Scope；阶段 1D-3A
已完成其余供应商无关 Protocol、不可变 I/O DTO 和严格 Fake；阶段 1D-3B 已在 `../adapters/`
实现 Retrieval、Extraction、Auxiliary、Translation、Audit 和 Resource Store；阶段 1D-5 已实现
Dispatcher 的本地薄适配和离线组合；阶段 1D-6 已实现真实 Callback Guard/Recovery Source、
外部资源单项清理与有界恢复，并绑定公开路由。所有实现仍通过本层 Protocol 注入。

| 文件 | 职责与关键不变量 |
| --- | --- |
| `common.py` | 稳定 `call_id`/`attempt_no`、字段/来源调用范围和幂等操作结果。 |
| `retrieval.py` | execution 独占检索范围；只接受 `RetrievalQuery`，只返回 `EvidenceCandidate`。 |
| `extraction.py` | 只接受同一文档的 `SelectedEvidence`；Prompt Evidence/rows 必须逐项同序一致。 |
| `auxiliary_guidance.py` | 可删除的通用辅助语境边界；`none` 策略只能返回零 I/O 空结果。 |
| `translation.py` | 来源级翻译；失败稳定收敛为空文本，不升级为任务失败。 |
| `audit.py` | 每次外部调用先 reserve、后 complete；reserve 明确返回 reserved/pending/completed，只有首次 reserved 允许外部 I/O；正文只保存摘要、长度和计数。 |
| `callbacks.py` | latest-wins、Guard lease/fencing、明确失败与 outcome unknown、显式恢复。 |
| `resources.py` | owned/shared 资源、CAS、清理 lease/fencing、幂等清理、unknown 与 quarantine。 |
| `dispatcher.py` | 持久任务提交后的常量空间唤醒、只按 TaskId 的 Runner、有界维护任务和显式 start/stop/close 生命周期。 |
| `errors.py` | 明确失败、结果未知、来源越界和端口状态错误的稳定分类。 |

Port 只能使用武器谱领域 DTO、同层抽象和通用 Task 控制面 DTO，不得泄露 AnythingLLM、
SQLAlchemy、Flask、requests 或其他供应商结构。Candidate 不能直接进入 Extraction；
`EvidenceExtractionRequest` 的构造门禁保证 Adapter 不能暗中截断、替换或二次召回公开 `rows`。

`WeaponryResourceStorePort` 的清理租约包含随机 token 和单调 fencing token。shared 来源映射
始终保持 active，任何任务级 cleanup 都必须拒绝它；结果未知的 owned 清理也不能盲目重试，
必须先对账或隔离。该端口只描述事实，不在数据库事务中执行网络或文件副作用。

历史 Audit attempt 的 `pending`/`completed` 都不是新的调用许可。当前摘要审计不足以重建完整
模型回答时，Application 必须停止并隔离；后续恢复只能通过确定性供应商事实查回，不能盲目重放。
