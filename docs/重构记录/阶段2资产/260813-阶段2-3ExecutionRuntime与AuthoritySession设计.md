# 阶段 2-3 Execution Runtime 与 Authority Session 设计

> 决策日期：2026-08-13  
> 决策状态：按阶段 2-2 已确认边界实施  
> 性质：内部执行运行时契约，不是公开接口文档

## 1. 本步范围

本步只实现阶段 2-4～2-6 业务切换前必须具备的 v2 执行权链路：

```text
accepted Task
  -> claim（新 Attempt、lease token、fencing）
  -> start（同一完整 Authority）
  -> heartbeat（续租并原子替换 Authority 的 lease_expires_at）
  -> v2 Workflow Runner（只通过 Authority Session 取得写能力）
  -> heartbeat 失权/时钟异常/基础设施异常
  -> Session 发出停止信号，Runner 在下一个可停止边界停止
```

本步不修改公开 HTTP、SSE、WebSocket、Progress 或 Callback 契约，不修改 `app/container.py`，
不接入生产启动链，不把旧入口双写到 v2，也不改变现有 `RunReportTask`、`RunWeaponryTask`、
`RunAnalysisTask` 的调用签名。完整业务接入仍分别属于阶段 2-4、2-5、2-6。

## 2. 唯一控制面与能力来源

1. claim、start 和 heartbeat 只能调用阶段 2-2 的 `TaskExecutionUnitOfWorkFactory` 与
   `TaskExecutionPort`；Runtime 不读取旧 Task Service，也不自行执行 SQL。
2. Authority 只能来自本次 claim 成功返回的 `TaskAttempt.authority`。禁止从数据库查询“当前
   Attempt”后为旧调用者补齐能力，禁止从 owner、线程局部变量或业务键推导能力。
3. 每次写能力完整包含 `task_id + attempt_no + lease_token + fencing_token +
   lease_expires_at`。`owner_id` 仅用于诊断，不取代其中任何字段。
4. lease token 使用独立高熵工厂生成，任何日志、异常正文或结果对象都不得输出 token。

## 3. Authority Session

心跳成功会产生新的 `lease_expires_at`，旧 Authority 立即失效。仅让 Workflow 在每次写前读取
“最新快照”仍然不安全：读取后到事务提交前，心跳可能再次续租，导致本实例自己的合法条件写
被新 expiry 拒绝。

因此引入线程安全 `TaskExecutionAuthoritySessionPort`：

- `run_authorized(operation)`：在一个短临界区内向 operation 传入完整当前 Authority；只允许
  claim 之后的短 UoW 条件写，禁止在其中执行网络、模型、转换、对象删除、阻塞等待或长计算；
- `run_mutation(operation)`：所有执行期 Task 条件写的强制入口；`APPLIED` 和明确的
  `DUPLICATE_STEP_INTENT/DUPLICATE_TERMINAL` 返回给 Workflow，其余有限结果必须先设置不可逆 stop，
  再抛出协作停止异常，禁止调用方因漏判返回值而继续副作用；
- `renew_authority(operation)`：heartbeat 在同一能力门内使用当前 Authority 执行续租，只有续租
  UoW 已提交且返回 `APPLIED + 新 Authority` 后，才在释放能力门前原子替换当前快照；
- `current_authority()`：只供诊断和测试观察，不能据此执行写入；
- `request_stop(result)` / `stop_requested()`：监督器失权后设置单向停止事实，不能恢复为可运行。

Session 的进程内互斥只消除同一 Runtime 内 heartbeat 与短条件写的自竞争，不构成分布式锁。
SQLite Store 对完整 Authority 的 CAS 仍是最终写权限裁决；其他进程接管、租约过期或数据漂移仍会
让条件写失败关闭。

## 4. Runtime 顺序与事务边界

`TaskExecutionRuntime.run(task_id)` 使用以下固定顺序：

1. 从 `ClockPort` 读取 `claimed_at`，按冻结租期计算 `lease_expires_at`；
2. 生成一次高熵 lease token，以结构化 `TaskOwnerIdentity` 创建 claim 命令；
3. 在独立短 Execution UoW 内 claim，只有 `APPLIED` 才显式 commit；
4. 使用 claim 原样返回的 Authority，在第二个独立短 UoW 内 start；
5. 只有 start 已提交后才创建 Authority Session、启动 heartbeat supervisor 并调用 v2 Workflow；
6. Workflow 返回或抛出后，在 `finally` 中停止 heartbeat；Runtime 不替业务猜测成功、失败、
   Progress、Step 或 Callback 终态；
7. claim/start 的有限并发拒绝作为内部结果返回，不启动 heartbeat 或 Workflow。

先提交 start、再启动 heartbeat，避免 heartbeat 在 start 读取 Authority 后抢先旋转 expiry。claim 与
start 都没有外部 I/O；从 start 提交到 supervisor 启动只允许本地对象构造，租期配置必须在启动前
通过阶段 2 不等式门禁。`TaskLeaseRuntimeSettings` 作为不读取环境的内部值对象统一交给 Runtime
和 supervisor，并校验 `lease >= 3 * heartbeat + 2 * sqlite_busy + clock_jitter` 与
`stop_grace >= heartbeat + sqlite_busy`；环境键装载仍留给后续 Runtime Config 子步。

## 5. Heartbeat Supervisor

`ThreadedLeaseHeartbeatSupervisor` 是本地 Adapter，不进入 Application 或业务 Workflow：

1. 每个正在运行的 Task 使用一个 supervisor 实例，实例不可并发复用；
2. 周期等待使用可中断 pulse，测试可注入手动 pulse 并推进 FakeClock，不用真实 sleep 证明租约；
3. 每轮在 Session 的 `renew_authority` 临界区内读取 Authority、读取 Clock、构造新到期时间、打开
   独立 UoW、heartbeat 并 commit；
4. `APPLIED` 后 Session 在同一临界区替换 expiry；旧 expiry 从此不可再用于写；
5. `AUTHORITY_LOST`、`LEASE_EXPIRED`、`INVALID_STATE`、`MISSING` 等非成功结果统一停止继续心跳并
   请求 Workflow 停止；不把 `DUPLICATE_*` 或其他越界结果伪装为成功；
6. `ClockAnomalyError` 映射为 `CLOCK_UNSAFE`，其他基础设施异常映射为
   `INFRASTRUCTURE_ERROR`，两者都请求停止且不自动重置 Task；
7. 正常 `stop()` 只中断等待并在 `stop_grace` 内回收线程，不修改 Task 状态；超时不强杀线程，
   但必须使 Session/Executor 失败关闭并停止新领取；Pulse 等待器自身抛错也必须生成稳定结果。

成功 heartbeat 只允许 DEBUG 采样日志；失权、时钟异常和基础设施异常记录稳定 reason code、
`task_id/attempt_no/fencing_token`，禁止记录 lease token、业务正文、Callback 正文或未脱敏异常对象。

## 6. 失权停止合同

Python 线程不能安全强杀。阶段 2-3 冻结的是“协作停止 + 持久 CAS 双门禁”：

- supervisor 一旦失权，先原子设置 Session stop，再结束自身循环；
- v2 Workflow 必须在每个外部 Step intent 之前、外部 I/O 返回之后、Progress/Step/terminal 条件写
  之前检查 Session；长上游调用应把 `stop_requested` 作为取消探针传给支持取消的 Adapter；
- 所有执行期 Task 条件写必须通过 `run_mutation`，stop 后不得再取得写能力；
- 即使某个外部调用暂时不可中断，失权后的旧 Authority 仍会被 SQLite CAS 拒绝，Workflow 不得据此
  重读新 Authority 或继续下一项副作用；
- Runtime 不在失权时伪造业务失败、发送 Callback、释放 Callback Guard 或把 Task 重置为 accepted。
  后续只允许 Reaper/Recovery 按已经冻结的证据协议收敛。

## 7. v2 Runner 接入契约

新增 `TaskWorkflowRunnerPort.run(context)` 只服务 v2 Runtime。Runtime 在 claim 前通过独立只读 Query
UoW 和业务 Codec 加载冻结输入，`TaskWorkflowContext` 保存解码输入与可轮换 Authority Session，禁止
冻结一份会随 heartbeat 过期的 Authority。阶段 2-3 使用严格 Fake 证明 Authority 传递与停止；不适配
旧 Runner。阶段 2-4 起，每个业务以单独 Adapter 把冻结输入、Step、Progress、
终态和 Callback Control 一次切换到 v2；如实际适配需要改变旧 Runner 签名、生产启动策略或 Callback
语义，必须在相应阶段停止并重新确认，禁止在本步预埋双写兼容。

## 8. 验收与证据边界

本步至少验证：

1. claim 和 start 使用同一完整 Authority，start 失败时不启动 supervisor/Runner；
2. heartbeat 成功提交后 Session 原子换入新 expiry，旧 Authority 的后续 SQLite 写被拒绝；
3. Runner 的短 Authority 写与 heartbeat 不会在本实例内因 expiry 旋转互相竞态；
4. heartbeat 失权、时钟异常和基础设施异常都会请求停止，Runner 在下一个边界退出；
5. 日志和对象展示不泄露 lease token；
6. 临时 SQLite、严格 Fake、FakeClock 和手动 pulse 全部离线通过；不运行 `run.py`；
7. `docs/接口文档/`、`app/container.py`、`run.py` 和旧生产 Runner 零差异；不存在 v1/v2 双写。

这些证据只证明当前 Windows/Python、临时 SQLite 和进程内线程协作语义，不证明真实供应商取消、
多实例正确性、可靠队列、生产容量、exactly-once 或上线就绪。

## 9. 本步商讨检查

当前设计不改变生产启动策略、不改变旧 Runner Authority 传递方式、不改变 Callback 语义，也不涉及
公开接口文档修改，因此无需中断请求新的公开契约授权。Authority Session 是仅供 v2 的新增内部能力，
避免阶段 2-4 业务切换时再通过数据库补权；若后续发现旧 Runner 必须直接接收该 Session，必须在
对应业务切换前单独提出确认，不能把本设计解释为已获准修改旧签名。
