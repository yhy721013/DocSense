# 阶段 1F-6 资源与 Callback 闭环执行记录

> - 执行日期：2026-07-27
> - 对应设计：[阶段 1F 文件分析高内聚收口文件级实施设计](../重构记录/260726-阶段1F文件分析高内聚收口文件级实施设计.md)
> - 阶段范围：仅为新的、带批次身份的 file execution 建立资源事实与 Callback 闭环；不切换生产执行链。

## 1. 结论

阶段 1F-6 已完成内部资源事实、清理收口、统一 Callback Guard 和同步恢复用例的实现及离线验证。
实现沿用“先持久化意图、后执行外部副作用、再持久化结果”的顺序；任何外部结果未知、审计身份不完整、
所有权不明确或 CAS 事实写入失败均保留现场并隔离，不能自动重放 RAG close/delete 或 Callback HTTP。

本阶段未改动公开接口：没有修改 `docs/接口文档/`、`/llm/analysis`、file 类型
`/llm/check-task`、请求参数、响应体、Callback 载荷、Progress、HTTP 状态码或 Header。生产路由、
Container、Dispatcher 的接线仍明确留给后续 1F-7A / 1F-5A / 1F-5B。

## 2. 实现内容

### 2.1 新 execution 的资源事实

`LLMTaskService` 新增追加式 `analysis_resource_records` 表和短 SQLite 事务读写方法；Adapter 只接受
`batch_id`、`batch_sequence` 均存在的新 file execution，并在读取时联表核验完整
`AnalysisExecutionRef`。历史 `rag_resource_leases` 不具备完整所有权和清理结果，因此本阶段只保留其
只读诊断边界，不迁移、不自动删除。

资源记录的每次推进都使用 `state + version` CAS。载荷持久化任务目录、本地文件位置、Context /
Conversation / Document 等不透明引用、召回/交互审计凭据、知识库四分类结果、close 意图、运行中标记、
三态 close 结果及审计确认；同状态 CAS 也允许在引用分批出现时立即补充事实，版本仍会递增。

```text
tracking
  ├─ 审计未确认 / 事实未知 ───────────────→ quarantined
  └─ 已审计且允许 close → cleanup_pending → (写 running 事实) → cleaned
                                                       └─ unknown / CAS 失败 → quarantined
```

`RecoverAnalysisResources` 只针对已持久化且到期的记录执行可证明幂等的
`append_lifecycle_events`；它不会重新执行模型、RAG 查询、永久知识写入、文件下载、RAG close 或删除。
审计追加暂时失败才进行有限退避，超过上限即隔离。

### 2.2 Application 关闭顺序

`RunAnalysisTask` 保持原有构造兼容性，新增可选的 `resources`、`callbacks` 和 `callback_url` 内部依赖。
未注入时保留 1F-3 的离线编排行为；注入后每个 execution 仍只有局部资源状态，不在 Application 实例
上缓存 Session、资源版本或 callback 租约。

新顺序为：RAG Factory 前登记资源 → Context/Conversation 引用出现后即时 CAS → 召回与交互审计
事实 → 知识库结果 → 任务终态条件写 → Callback Guard → close 意图/running → RAG close 三态结果 →
审计追加确认。终态一旦提交，Callback 或 close 的后续故障不得反向改写既有任务状态。

### 2.3 Callback Guard 与同步恢复

新增 Analysis Callback Port、SQLite Adapter 和恢复源，复用既有全局
`callback_delivery_guards` 的 latest owner、租约和 fencing 条件：

- 获取发送权、发送前二次校验、完成 Guard 均在短 SQLite 事务中完成；HTTP 永远在事务外；
- HTTP 只接受严格 2xx；响应读取超时、连接中断等无法证明接收端状态的结果冻结为
  `outcome_unknown`，不自动重发；
- 空 callback URL 仍获取 Guard 并收敛为 `skipped`，避免业务键永久停在可发送状态；
- 同步恢复只读取 latest 且已终态的新 file execution；遇到已有 owner 时有限等待，随后最多重读一次，
  并以首次读取的 `callback_attempts` 快照做原子授权，不能把同一并发波次的明确失败滚动成多次
  HTTP，也不能重跑模型、RAG 或知识库；
- 过期发送租约可由未来维护任务有界冻结，但本阶段未启动后台线程或 Dispatcher。

### 2.4 全面复核后的闭环加固

- Callback 恢复查询改为单次窄投影，只解码最终回调结果，不读取或反序列化请求正文与 execution
  输入；无关大快照或损坏输入不再阻断合法终态回调。
- Resource Port 与 SQLite Service 同时执行显式迁移矩阵，`cleaned/quarantined` 成为不可逆终态。
- 扫描遇到非法 JSON 或结构毒化记录时，使用 `execution + state + version` 控制面 CAS 隔离，原始
  payload 保持不变并记录长度/hash；预算耗尽后的隔离也不再重复解析已知坏 payload。
- 每条资源恢复增加最后一道异常边界；单条坏记录只能形成隔离或 pending 诊断，不能终止未来
  Dispatcher 的整个维护循环。
- 已返回的 RAG close outcome 与 lifecycle events 会在 fallback 中完整重建后再进入
  `audit_pending`，避免首次结果事实 CAS 失败时丢失已知外部证据。

## 3. 变更边界

| 层次 | 主要文件 | 职责 |
| --- | --- | --- |
| 持久化控制面 | `app/services/llm_service/task_service.py` | 资源表、CAS、恢复延期、到期扫描及批次身份联表读取。 |
| Port | `app/modules/analysis/ports/resources.py`、`callbacks.py` | 强类型资源/Callback DTO、状态机与运行时 Protocol。 |
| Adapter | `app/modules/analysis/adapters/resource_store.py`、`callback_guard.py`、`callback_recovery.py` | SQLite 资源事实、Guard/HTTP、latest 终态恢复候选。 |
| Application | `recover_resources.py`、`recover_callback.py`、`run_analysis.py` 及协作器 | 生命周期收口、有限恢复、同步回调恢复和可选依赖编排。 |
| 离线测试 | `test_analysis_resource_recovery.py`、`test_analysis_callback_guard.py`、`test_analysis_application.py` | CAS/隔离/退避、50 并发至多一次 HTTP、正常调用顺序及兼容性。 |

所有新增日志均只输出内部任务标识、阶段和稳定错误类别；不写入 Prompt、原始文档、回调正文或供应商响应。

## 4. 验证证据

未启动 `run.py`，未连接真实模型、AnythingLLM、OCR、知识库或 Callback 服务。验证全部使用项目
`venv\Scripts\python.exe`、临时 SQLite、严格 Fake 和替身 HTTP Transport：

1. `venv\Scripts\python.exe -B -m py_compile ...`：1F-6 相关 Application、Adapter、Port 与 TaskService
   编译通过。
2. `venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_analysis*.py" -q`：283 项通过，
   0 failure / 0 error。
3. `venv\Scripts\python.exe -B -m unittest tests.test_task_service tests.test_analysis_batch tests.test_analysis_resource_recovery tests.test_analysis_callback_guard tests.test_analysis_application tests.test_analysis_ports tests.test_analysis_production_adapters tests.test_architecture_boundaries -q`：132 项通过，0 failure / 0 error。
4. 资源状态更新可读性收口后，`venv\Scripts\python.exe -B -m unittest tests.test_analysis_resource_recovery tests.test_analysis_callback_guard tests.test_analysis_application tests.test_architecture_boundaries -q`：46 项通过，0 failure / 0 error。
5. `git diff --check`：通过。

测试输出中的审计、RAG、知识库、Callback 和 SQLite 锁异常均来自显式故障注入断言，不是连接外部服务的
运行错误。

## 5. 未完成项与后续门禁

- 1F-6 组件尚未接入生产 Worker、Container、Dispatcher、`/llm/analysis` 或 file 类型
  `/llm/check-task`；在 1F-5B 之前不得与遗留执行器并行处理同一任务。
- 当前 SQLite 路径仍是 `single_instance` 能力边界。资源 CAS、Callback fencing 和有限恢复为后续
  可靠队列/多实例一致性提供事实基础，但不能据此声明已具备多实例、高并发生产能力。
- 1F-7A 已完成切换前的 50 任务隔离与只读预检；全面复核后又将历史终态待恢复回调和新
  `running` execution 纳入硬阻断，并保留新 `accepted` execution 的显式观测。任何未知外部副作用
  必须继续 fail closed。
