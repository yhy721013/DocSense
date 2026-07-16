# 任务应用层

本目录负责用例编排，例如任务状态检查与 Progress 订阅。应用服务只能依赖 `domain/` 和 `ports/`，返回框架无关结果，不创建 Flask Response、WebSocket 连接、数据库 Session 或后台线程。

禁止导入 Flask/FastAPI/Celery、`request`、`current_app`、Blueprint、具体 Adapter、供应商 Integration 和遗留 Service。

阶段 1A-3 已实现两个框架无关原始用例：

- `CheckTaskStatusService`：保持有序批量缺失位置，调用独立 Callback Recovery Port，并无条件按原 TaskId 重读、核对返回状态与持久化状态；
- `ProgressSubscriptionService`：先打开连接级初始屏障并建立类型化订阅，再选择 Progress/Task/missing 当前快照，返回连接 Registry 所需令牌，不调用 `ws.send`；
- `ProgressDeliveryBuffer`：提供连接级有界通知队列、初始快照顺序屏障、按 key 合并和慢连接隔离；只存内部快照，不是可靠任务队列。

阶段 1B-1 又完成了 check-task 的生产目标内部边界：

- `CheckTaskRequest`：历史同步原型和可靠命令用例共享的已校验有序请求；旧
  `CheckTaskStatusRequest` 名称保留为同一类型的兼容别名；
- `RequestCallbackRecoveryService`：先用 Task Read 保持单项/批量缺失语义，再把全部
  命中项一次性交给批量原子 Command Port；不逐项提交、不执行 Callback HTTP；
- `RequestCallbackRecoveryResult`：保留请求位置并记录 created、already_active、
  not_needed、stale 内部分类，Presenter 不得公开这些字段。

端口异常不会被静默转换为成功；订阅建立中途失败会补偿释放本次新增令牌，连接关闭释放会遍历全部令牌后统一报告异常。补偿或释放失败的异常会携带完整令牌，Web Adapter 必须把它们保留在连接 Registry 中重试，不能只记录 ID 后丢失引用。

可靠命令端口的数据库事务异常同样必须原样传播。当前 Fake 成功只证明应用契约，不能
映射为生产 HTTP 200；MySQL/Outbox Adapter 和生产装配分别在阶段 3～4、阶段 6 实现。

阶段 1B-2 已把 `ProgressSubscriptionService` 和 `ProgressDeliveryBuffer` 接入当前
`/llm/progress` 运行路径。每个 WebSocket 连接独占一个 Registry/缓冲；初始快照成功
发送后才放行并发通知，发送失败或断连时关闭缓冲并有限重试释放失败令牌。发布线程只
提交类型化快照，不执行网络 I/O。该接入不改变 check-task 的上述生产边界。
