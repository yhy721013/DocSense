# 阶段 1F-3R Application 与任务级 I/O 审查修复执行记录

> - 执行日期：2026-07-26
> - 对应设计：[阶段 1F 文件分析高内聚收口文件级实施设计](../重构记录/260726-阶段1F文件分析高内聚收口文件级实施设计.md)
> - 实施范围：修复 1F-3 未接线 Application、RAG/Audit Port 与遗留 Adapter 的审计、关联、三态幂等和恢复查回缺口
> - 公开契约结论：未修改 `docs/接口文档/`、生产 Blueprint、路由、请求/响应字段、状态码、Header、Progress 或 callback 格式；未增删任何前后端接口参数

## 1. 修复内容

### 1.1 打开阶段部分资源失败仍可审计

- `AnalysisInteractionAuditRecord` 的失败语义允许 `session=None`，用于表达 Context 已创建但
  Conversation 未创建成功、因而无法合法构造完整 `AnalysisRagSessionRef` 的场景。成功交互仍强制
  完整 Session，不能把失败兼容放宽到成功门禁。
- Application 只要取得有序 lifecycle，就会尝试原子持久化失败交互，不再以完整 Session 是否存在
  作为失败审计门槛。
- SQLite Audit Adapter 从成功的 `context_create`/`conversation_create` lifecycle 中提取已确认的部分
  外部引用。Context 回滚失败时，`workspace_slug` 和失败 cleanup 状态均可查回，供 1F-6 建立资源
  记录；没有外部引用时仍保持空值，不伪造 Session。

### 1.2 RAG attempt 与阶段 Conversation 精确关联

- 每次 `execute` 在调用原生 Gateway 前记录累计 attempt 水位。失败映射只读取本次新增 attempt；阶段
  Conversation 在模型请求前失败时，当前错误的 response/sources 保持为空，不能串用上一阶段结果。
- 阶段切换后的活动 Conversation 从最新成功的 `conversation_create` lifecycle 获取。共享 Gateway
  `trace.conversation_ref` 继续保持主 Conversation 的既有合同，不为 Analysis 私自改变共享语义。
- 未分类异常会补充与 `outcome_unknown=True` 一致的 lifecycle event，再构造稳定
  `AnalysisRagExecutionError`；不再由 DTO 一致性 `ValueError` 掩盖原始异常和生命周期证据。

### 1.3 close 三态幂等和证据校验

- Adapter 缓存第一次 `close_session` 的完整结果。第一次为 `OUTCOME_UNKNOWN` 时，后续进程内重复调用
  返回同一 unknown 事实和 sequence，不重新发送 DELETE，也不升级为 `KNOWN_NOT_APPLIED`。
- `AnalysisRagSource.score` 在 Port 边界拒绝 NaN/Infinity，避免严格 JSON 审计把供应商数据错误误判为
  SQLite 故障。
- 失败响应的 sources 与成功响应采用相同文档归属规则；Session 已绑定文档时，来源必须全部属于当前
  `document_ref`。交互审计 DTO 再执行一次同样的纵深校验。

### 1.4 stale 门禁和审计查回性能

- 首次 expected TaskId Progress 条件写前移到 `workspace.create` 之前。首次门禁已判 stale 的 execution
  不再创建空任务目录；严格 Fake 顺序测试同步冻结新顺序。
- `LLMTaskService` 增加内部精确交互查回：使用已有唯一审计幂等键索引，并联合 execution、业务类型和
  业务键复核归属。Analysis Audit Adapter 不再加载同名文件的全部历史 Prompt、响应和 sources 后在
  Python 中过滤，恢复查询成本不再随历史记录无界增长。

## 2. 测试补强

新增或修正的离线用例覆盖：

1. 首次 stale 在工作目录和文件准备前停止。
2. Context-only 打开失败通过 Application 和真实临时 SQLite 审计落库并可精确查回。
3. 阶段 Conversation 创建失败不串用上一轮响应。
4. 阶段切换后的 SessionRef 指向最新真实 Conversation。
5. 未分类异常产生一致的 outcome-unknown lifecycle。
6. 首次 close unknown 后重复调用保持同一 unknown 结果。
7. 非有限 score 与跨文档失败来源在 Port 边界被拒绝。

## 3. 验证结果

| 检查项 | 结果 |
| --- | --- |
| 受影响 Application、生产 Adapter、Port 与 TaskService | 79 项通过 |
| `test_analysis*.py` 定向发现 | 252 项通过 |
| 含日志断言的 23 个关联模块复验 | 508 项通过 |
| 安全全仓动态发现 | 发现 1,693 项；执行前打印并核对排除 13 项，实际执行 1,680 项，0 failure / 0 error / 0 skipped |
| 架构边界及核心 RAG/Application/Adapter 组合 | 50 项通过 |
| `run.py` 与真实外部服务 | 未启动、未连接 |

安全全仓排除项保持既定 13 项：7 项 `LocalScriptTests` 可能启动本地 Shell、文件服务或
`run.py`；5 项 `LLMTestAssetsTests` 依赖个人联调夹具；1 项 Analysis Security Migration 用例断言
Windows 无法稳定表达的 POSIX `0640` 权限位。排除名称在运行前逐项打印，排除集合没有因失败扩大。

## 4. 接线与后续边界

- 当前生产 `/llm/analysis` 仍由遗留 `analysis_service.py` 执行；本轮没有 Container、Dispatcher、
  Worker 或路由接线，不存在新旧双执行。
- 本轮让“部分资源失败事实”可被审计查回，但没有提前实现 1F-6 的 `analysis_resource_records`、CAS
  恢复、Callback Guard 或后台清理。1F-6 必须从已审计 lifecycle 建立可恢复资源事实，不能只读日志
  猜测资源归属。
- 当前运行模式继续是显式 `single_instance`。本轮离线 SQLite 与线程测试不代表可靠任务队列、
  多实例 fencing、分布式数据库一致性或生产容量已经完成。
- `RunAnalysisTask` 中遗留算法编排仍较大。为避免在故障语义修正中同时搬移数百行黄金算法，本轮没有
  进行机械拆分；后续只能在黄金与故障矩阵保护下按模型工作流、失败收敛和知识转交职责渐进拆分，
  不得重新并入 Web、SQLite 或供应商客户端依赖。该工作已正式排入 1F-3R 之后、1F-4 之前的
  [1F-3S Application 等价拆分](../重构记录/260726-阶段1F-3S文件分析Application等价拆分实施计划.md)。
