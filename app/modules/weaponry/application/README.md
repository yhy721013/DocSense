# Weaponry Application

本层只负责编排武器谱用例，并且只能依赖本模块 `domain/`、`ports/`、`application/`
以及通用任务控制面抽象。阶段 1D-4～1D-6 已完成下列组件：

- `SubmitWeaponryTask`：在一个短事务入口中原子受理任务；提交后再发送 Progress 和
  Dispatcher 可丢唤醒，通知失败不回滚 accepted 事实；
- `RunWeaponryTask(task_id)`：只接受内部 `TaskId`，完成领取、expected TaskId 条件进度、
  字段执行、成功/失败单终态、Callback Guard Port 调用和资源 cleanup/quarantine 收敛；
- `WeaponryFieldExecutor`：按冻结字段和文档顺序串行执行 Guidance、Retrieval、Selection、
  单文档 Evidence Extraction、Translation 与纯领域组装；Candidate 不能绕过 Selection；
- `RecoverWeaponryCallbackSynchronously`：保留甲方规定的 check-task 请求内恢复副作用，并与
  正常 Worker 竞争同一个 latest-wins Callback Guard；
- `WeaponryResourceRecoveryService`：只对终态 execution 的已登记 owned 资源执行有界单项清理，
  处理 tracking/cleanup_pending、持久冷却、未知结果隔离和崩溃检查点保护；
- 字段级供应商容量或协议失败按已确认契约可以降级为空并成功回调，但会通过仅限内部的
  `diagnostic_error_codes` 汇总到 `RunWeaponryResult`；该摘要不进入数据库公开结果或 Callback，
  只供 Dispatcher 区分容量错误、输入契约错误和真正业务零结果；
- `errors.py`：稳定区分冲突、Port 契约、任务持久化结果未知、审计失败、普通执行失败、
  外部副作用现场保护和 stale。

关键不变量：

1. `Run` 不接收原始 HTTP 字典，也不在执行期重新查询文档范围或环境变量。
2. 每次慢 I/O 前后均通过审计或 latest 事实收敛；SQLite 短事务不会包住网络、模型或文件 I/O。
3. 零 Selected Evidence 不调用 Extraction/Translation；每个来源只接收其最终 `rows` 对应的
   完整 Evidence，禁止父会话或目标文档 workspace 二次 RAG 回退。
4. 终态条件写失败或结果未知时不补写第二终态；外部副作用结果未知、关键审计失败或崩溃遗留
   资源会进入 quarantine，等待后续对账。
5. Callback 是终态后的独立交付维度；投影、获取发送权、发送或完成失败均不得反向改写业务终态。
6. Audit reserve 只有 `reserved` 允许继续；历史 `pending/completed` 必须停止外部重放并隔离。
7. 资源收敛必须先可靠提交 `cleanup_pending`，再执行远端删除；意图提交失败时宁可保留可恢复
   资源，也不能产生没有本地权威事实的删除副作用。

本层不得直接导入 Flask、遗留 Service、具体 AnythingLLM Client、数据库 Repository 实现、
Callback Client 或运行时环境变量。1D-6 已在生产组合根注入同一 Callback Adapter、同步恢复源、
资源恢复和 Dispatcher，并完成公开路由切换；组合根构造仍不会启动线程或提前创建网络 Session，
生命周期只由应用容器显式启动和关闭。
