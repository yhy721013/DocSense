# 分类节点变更适配层

阶段 1E-2 已实现 `sqlite_repository.py`，1E-2R 完成持久化一致性修正。在既有
`knowledge_base.sqlite3` 中加法创建
`reassign_operations`、`reassign_steps`、`reassign_events`、
`reassign_workspace_preparation_claims` 与 `reassign_recovery_observations` 五张内部事实表、活动
文档部分唯一索引和审计表
UPDATE/DELETE 拒绝触发器。初始化只校验既有 `documents`/`workspaces` 权威表所需列，
不重建或迁移公开文档记录；早期 1E-2 开发库使用加列回填升级，活动索引谓词不一致时在同一事务内
重建。若新约束与历史数据冲突则明确拒绝启动，不静默削弱并发保护。

`SQLiteReassignmentRepository` 的每个 UoW 使用独立连接；只读 UoW 使用短 `BEGIN`，写 UoW 使用
短 `BEGIN IMMEDIATE`，同一线程的嵌套 UoW 会立即拒绝而不是等待 busy timeout，提供：

- 原子冻结 `fileName + int(oldArchitectureId)` 命中的文档快照、八个固定 Step、源 workspace 映射与
  单调 fencing；
- owner/token/expiry/fencing 保护的 lease 续期、过期接管、步骤检查点和 Operation 状态转换；
- 以 `(lease_expires_at, operation_id)` 稳定游标分页的有界恢复扫描，以及带 fencing、尝试次数、
  探测结论、脱敏操作者和原因码的追加审计；
- 尚无本地 mapping 的目标 workspace 使用持久化 preparation claim（独立 fencing、释放行保留和
  过期接管）；新 mapping、prepare Step 与 claim 释放同事务提交，并拒绝把既有共享映射标为本
  Operation 创建；
- `documents.id + file_name + source_architecture_id + anything_doc_id + doc_path` 条件 CAS、commit Step
  和 `succeeded` 终态的同事务提交；提交前校验必需远端步骤与目标绑定，0 行或唯一约束冲突只返回
  内部冲突事实，绝不伪造成功；
- 恢复时的过期 lease 接管与匹配 preparation claim 转移、恢复观测追加，以及引用同一 fencing 最新
  观测的专用终态收口。恢复成功还会复用前向成功门禁，不能以一次远端探测替代 target mapping 或
  Step 检查点；终态与已接管 claim 释放保持同一短事务；
- 终态 Operation 禁止续租或启动新步骤；同一 fencing 不能离开恢复隔离或重试已知失败步骤。
- 只追加审计事件和提交后才发出的脱敏日志。

阶段 1E-3 已新增 AnythingLLM 网络适配器；Application 通过注入的
`ReassignmentKnowledgePortFactory` 为每次执行取得新的 Knowledge Port，但不依赖本目录的具体类。
阶段 1E-6 的 Container 只构造一次无状态 Factory 和 `SQLiteReassignmentRepository`，再交给组合根
装配；Flask 路由不直接构造 Adapter 或 Client。它包含按原子调用新建并关闭 Transport 的 Client Factory、严格有限 HTTP/总预算/补偿预留
配置、按确定性 workspace 名称/slug 的无副作用查回与创建，以及旧解绑、新挂载、Pin、完整 doc_path
成员探测的四分类结果。timeout、断连、协议异常、缺 slug 和多重 workspace 身份均不会被伪装为成功。
创建结果不确定时只做一次只读查回；查回到唯一资源时以 `unknown` 归属登记并允许后续使用，
但不会声称该 workspace 由当前 Operation 创建，也不能据此自动删除整个 workspace。

SQLite Adapter 仍不创建 HTTP Client、不导入网络库、不调用 Knowledge Port，因此网络等待不能进入
数据库锁范围。1E-4 Application 已在提交写意图后调用注入的网络 Port，并把确定结果或未知现场
通过 Repository 持久化。

阶段 1E-5 的 `scripts/inspect_reassign_operations.py` 默认以
`SQLiteReassignmentRepository(..., initialize_schema=False)` 打开既有数据库，只列出有上限的过期
可恢复 Operation，不触发 Schema DDL、网络调用或状态写入。只有显式 `--apply` 且提供 operation ID、
预期 fencing、操作者、原因、lease owner 与时长时，脚本才构造恢复服务并执行单个 Operation。

Adapter 可以依赖基础设施，但必须遵守以下边界：

- 外部网络调用绝不包在 SQLite 写事务内；
- 每次外部写之前必须已有可恢复的持久化意图；
- 远端 `false`、超时、断连或探测矛盾不能静默当作成功；
- 普通写与常规确认使用前向预算；不确定写查回、恢复和补偿 Step 才能使用保留预算；
- 删除、加入和 Pin 必须先核对固定 Saga Step，错误动作在任何 HTTP 调用前失败；
- 结构化日志只记录脱敏的错误分类、步骤、状态、耗时和不透明引用摘要；
- 不自行拼装公开 JSON，也不把供应商原始响应透传给领域层或前端。
