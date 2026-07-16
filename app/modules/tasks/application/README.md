# 任务应用层

本目录负责用例编排，例如任务状态检查与 Progress 订阅。应用服务只能依赖 `domain/` 和 `ports/`，返回框架无关结果，不创建 Flask Response、WebSocket 连接、数据库 Session 或后台线程。

禁止导入 Flask/FastAPI/Celery、`request`、`current_app`、Blueprint、具体 Adapter、供应商 Integration 和遗留 Service。

阶段 1A-3 已实现两个尚未接入生产路由的用例：

- `CheckTaskStatusService`：保持有序批量缺失位置，调用独立 Callback Recovery Port，并无条件按原 TaskId 重读、核对返回状态与持久化状态；
- `ProgressSubscriptionService`：先打开连接级初始屏障并建立类型化订阅，再选择 Progress/Task/missing 当前快照，返回连接 Registry 所需令牌，不调用 `ws.send`；
- `ProgressDeliveryBuffer`：提供连接级有界通知队列、初始快照顺序屏障、按 key 合并和慢连接隔离；只存内部快照，不是可靠任务队列。

端口异常不会被静默转换为成功；订阅建立中途失败会补偿释放本次新增令牌，连接关闭释放会遍历全部令牌后统一报告异常。补偿或释放失败的异常会携带完整令牌，Web Adapter 必须把它们保留在连接 Registry 中重试，不能只记录 ID 后丢失引用。
