# 阶段 2-3 Execution Runtime 实施记录

> 实施日期：2026-08-13  
> 工作区：`feat/weaponry-chat`  
> 基线提交：`fcc0a74e05a19c6d468d55904a9d46c592759aa9`  
> 公开接口影响：无

## 1. 本次完成内容

本次按
[阶段 2-3 Execution Runtime 与 Authority Session 设计](../重构记录/阶段2资产/260813-阶段2-3ExecutionRuntime与AuthoritySession设计.md)
完成 v2 控制面的领取、启动、续租和失权停止核心链路：

1. 新增 `TaskExecutionAuthoritySessionPort` 与线程安全实现，完整保存 claim 返回的
   `task_id/attempt_no/lease_token/fencing_token/lease_expires_at`；
2. 新增 `TaskExecutionRuntime`，严格使用阶段 2-2 Execution UoW 依次提交 claim 和 start，
   start 成功后才启动 heartbeat 和 v2 Workflow；
3. 新增 `ThreadedLeaseHeartbeatSupervisor`，每轮通过独立短 UoW 续租，提交成功后在同一个
   Authority 能力门内换入新 expiry；
4. heartbeat 非成功、时钟异常或基础设施异常会在释放能力门前设置不可逆 stop；Workflow
   后续 `run_mutation` 立即失败，SQLite 完整 Authority CAS 继续作为最终门禁；
5. 新增高熵 `SecureTaskLeaseTokenFactory`；`TaskExecutionAuthority` 和 `TaskClaimRequest`
   的 repr 已隐藏 token，新增日志只记录内部 Task、Attempt、fencing、有限 outcome/reason code，
   不记录 token 或业务正文；
6. 新增严格 Runtime Fake、固定测试 token 工厂和手动 heartbeat pulse，租约测试通过
   FakeClock 确定性推进，不使用真实 sleep 证明到期；
7. 新增机器契约 `tests/contracts/stage2_execution_runtime_contract.json`，冻结本波范围、
   Authority Session 竞态处理、协作停止和证据边界。
8. 新增纯内部 `TaskLeaseRuntimeSettings` 与 `TaskRuntimeConfig`；后者显式从 mapping/environment
   装载全部 Task Runtime 键，在后台线程启动前校验 worker、容量、Task/Recovery lease、scan、
   SQLite busy、clock jitter 与 stop grace，并更新 `.env.example`，但没有接入生产启动策略；
9. 修复 Task/Recovery heartbeat 的严格单调续租：DTO、Strict Fake、SQLite Store 和 Session 四层均
   拒绝缩短或不推进 expiry；Runtime 将本机无法推进 expiry 分类为 `CLOCK_UNSAFE`；
10. 新增 `run_mutation` 结果矩阵、正常取消探针和有界 heartbeat join；Pulse 异常、失权和停止超时
    均产生稳定失败关闭结果，不强杀 Python 线程；
11. 新增只读 `TaskControlQueryUnitOfWork` 与 `CodecTaskExecutionSnapshotLoader`，在 claim 前解码受理时
    冻结输入；v2 Runner 接收 `TaskWorkflowContext`，其中保存冻结输入与可轮换 Authority Session，
    不再使用会缓存旧 expiry 的设计期 `TaskExecutionContext`；
12. 新增 `LocalTaskExecutor`、Report/Weaponry/file 公平轮转容量和持久派发冷却；内存仅保存不超过
    worker_count 的在途 TaskId，启动/周期扫描是恢复真相，重复 wake 只合并提示；
13. 新增与业务 Executor 分离的 `LocalMaintenanceScheduler` 和只持久化 `DEFER` 的
    `ConservativeTaskReaper`；阶段 2-3 不因 lease 过期自动开放 `retry_safe`；
14. 修复 Recovery Authority/Claim Request repr 的 token 泄漏、Admission task type/业务引用不一致、
    Domain/Port 日历时间校验分叉、根 Manifest 重复键及 JSON Loader 静默覆盖；
15. 按确认决策将 Analysis 控制面 Executor、持久化 task type 和未来队列路由统一为 `file`。

## 2. 关键并发结论

heartbeat 每次成功都会改变 `lease_expires_at`，因此“先读取最新 Authority、再执行写入”仍有
读取后被 heartbeat 抢先轮换的竞态。本次用同一 Authority Session 能力门串行化两类短操作：

- Workflow 的 Task 条件写通过 `run_mutation` 执行；诊断读取才允许使用 `run_authorized`；
- heartbeat 通过 `renew_authority` 执行，并在数据库 commit 后、释放能力门前替换 expiry。

外部 I/O 明确禁止进入该能力门。该锁只消除本进程内自竞争，不构成分布式锁；其他实例接管和
租约到期仍由 SQLite 的完整 Authority CAS 拒绝。

## 3. 验证结果

使用项目 `venv` 执行，没有运行 `run.py`：

```text
阶段 2 专项（test_stage2*.py）：
发现/执行 65，失败 0，错误 0，跳过 0

Task Domain/Port/SQLite/Schema 选择性回归：
发现/执行 81，失败 0，错误 0，跳过 0

全量离线回归：
发现/执行 2342，失败 0，错误 0，跳过 13
```

上述专项集合包含：2-3 Runtime、阶段 2 冻结资产与接口哈希、Task Domain/Port/Strict Fake、
Schema Contract/Bootstrap、SQLite UoW/Store、旧库预检、架构边界、禁用 print 和日志配置。
其中预期故障注入日志包括时钟异常、start Authority 拒绝、heartbeat 到期、Schema 漂移、SQLite
busy 和 Step unknown 隔离；这些用例均按预期通过，不是测试失败。

另行通过：

- `compileall`：新增及 Tasks 模块源码可编译；
- `git diff --check`：无空白错误；
- 新机器契约 JSON 可解析；
- `docs/接口文档/` 冻结哈希测试通过；
- `app/container.py`、`run.py`、公开接口目录和旧 Runner 无差异。

## 4. 明确保留边界

本次没有完成或宣称以下能力：

- 没有把 Report、Weaponry、Analysis 生产受理或旧 Runner 接入 v2；
- 已实现但尚未接入生产组合根的 `LocalTaskExecutor`、Report/Weaponry/file 轮转公平容量、
  `LocalMaintenanceScheduler`、只做 `DEFER` 的保守 Reaper、Tasks Runtime Config、SystemSafeClock、
  只读 Query UoW 和 Codec Snapshot Loader；生产启动仍未接线；
- 没有迁移 Progress 所有权，没有修改 Callback Delivery/Guard 或 `/llm/check-task`；
- 没有双写 v1/v2，没有从数据库读取当前 Authority 为旧 Worker 补权；
- 没有运行真实 AnythingLLM、模型、MinIO、浏览器、RabbitMQ、MySQL 或生产负载。

因此当前证据只证明 Windows/Python、临时 SQLite、严格 Fake 和单进程线程协作下的核心 Runtime
合同，不证明多实例、可靠队列、跨进程取消、供应商调用可中断、生产容量或 exactly-once。

## 5. 商讨检查

实施和回归均未发现需要修改公开接口、生产启动策略、旧 Runner Authority 传递或 Callback 语义的
事项。本次新增 v2 Runner Port 没有适配旧 Runner。后续 Report 试点若无法通过独立 Adapter 保持旧
Runner 外观，或必须改变生产启动/Callback 合同，应在修改前停止并另行确认。
