# Task Control SQLite Adapter

本目录承载阶段 2 的任务控制数据库基础设施，公开 HTTP/Callback/SSE/WebSocket 协议不在此定义。

## 阶段 2-2 第 1 步边界

- `database_contract.json`：生产 Bootstrap 与测试共同读取的数据库路径、身份、旧库预检和组合治理契约；
- `root_schema_manifest.json`：根表、约束、外键与索引的唯一 Manifest；
- `schema.py`：canonical fingerprint、确定性 DDL 和实际 SQLite 精确核验；
- `legacy_preflight.py`：旧 Task 数据库只读预检，命令行脚本仅作单向委托；
- `bootstrap.py`：启动锁、文件集门禁、临时新建、不覆盖发布和严格打开。

Bootstrap 成功前不得构造 Connection Factory、UoW、Store 或启动后台线程。普通打开不会执行
`CREATE IF NOT EXISTS`、`ALTER TABLE` 或组件安装；身份、对象并集、完整性和外键任一不匹配均失败关闭。
启动文件锁只提供同机进程互斥，不是多实例分布式租约，也不替代后续 Task Authority fencing。

## 阶段 2-2 第 2 步边界

- `connection.py`：只能由成功 Bootstrap 结果构造的短连接 Factory；每个连接启用外键、busy
  timeout、同线程门禁，并复核根 metadata、组件注册摘要和 `schema_version`；
- `transaction.py`：写事务固定 `BEGIN IMMEDIATE`，显式 commit，未提交正常退出/异常/取消默认
  rollback；同线程同数据库禁止嵌套，不提供 savepoint 或调用方逻辑重放；
- `unit_of_work.py`：Admission、Execution、Recovery 三类窄 UoW；同一 UoW 的 Store 共用一个
  Connection，Application 看不到原始连接。

完整结构和 `integrity_check` 在 Bootstrap 执行；高频短连接使用 Bootstrap 返回的 `schema_version`
和完整身份摘要做轻量复核。该复核用于发现进程运行期间的意外 DDL/身份漂移，不是对恶意数据库
管理员的防篡改安全边界。网络、模型、文件转换、对象删除和容量等待均不得进入这些事务。

## 阶段 2-2 第 3～4 步边界

- `control_store.py`：Task、latest、Attempt、Step、Recovery Operation/Observation/Decision 与 Event
  的唯一 SQLite 写实现；所有公开写方法都要求调用方已经进入显式 UoW；
- `composition.py`：把同一 Control Store 装配到 Admission/Execution/Recovery 三类窄 UoW，不启动
  Worker、不访问外部系统，也不修改生产 Container；
- Admission 批量受理、claim/start/heartbeat、Step Intent/结果、Progress、业务终态、过期分类和
  Recovery Decision 均在各自短事务内完成 Task/latest/历史/Event 条件写；
- Recovery 外部探测或补偿严格分为“事务内 Operation Intent → 事务外 I/O → 事务内 Observation
  与 Operation 收敛 → Decision”四段，Store 本身不执行任何网络或文件 I/O；
- 两个独立 SQLite 连接的离线测试已经验证单次 claim CAS、单调 Task/Recovery fencing、旧 owner
  拒绝，以及接管者沿稳定 operation ID 对账旧 Intent。

阶段 2-4、2-5 已分别将 Report、Weaponry 的 Task 全生命周期与 Callback Control 一次切到本 Store；
Analysis/file 尚未迁移，继续读取和写入旧控制面，禁止双写。Control Store 在 Admission DTO 之外，
还会在 runnable 扫描、claim 和 Task 重建边界复核批次身份：`file` 必须且只能携带完整 batch_id 与
正 batch_sequence，其他业务必须不携带批次。绕过正常受理写入的异常行会失败关闭，不会被 Worker
领取。

保守 Reaper 已接入生产组合根，但只写 `DEFER`。`MARK_STALE` 在 abandon 旧 Attempt 前必须确认
latest 已不再指向当前 execution；拒绝路径不得产生部分写。阶段 2-7 若实现自动
`FINALIZE_FROM_CHECKPOINT`，必须由业务组合 UoW 校验并收敛 Report Artifact/Weaponry Result 等业务
事实，不能仅凭通用 Task 投影绕过业务 Store。

这些结果只证明当前 Windows/临时 SQLite 下的单库协议与单进程线程协作，不证明浏览器、真实供应商、
多实例、可靠队列、共享数据库一致性、容量或 exactly-once。阶段 2-2 Control Store 仍不写 Callback
Delivery 表；该表由专用 Callback Control Store 独占。
