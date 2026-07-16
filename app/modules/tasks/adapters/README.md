# 任务适配层

本目录实现 `ports/` 中定义的协议，并隔离具体数据库、缓存、消息队列、外部回调和迁移期遗留服务。

- Adapter 可以依赖端口、领域类型和具体基础设施，但不能被 `domain/` 或 `application/` 反向导入。
- 迁移期兼容 Adapter 必须明确单实例、线程安全和一致性能力，不能把进程内实现描述成可靠队列。
- Adapter 不负责 HTTP/WebSocket 参数解析或公开响应拼装；这些职责属于 Web Adapter 与 Presenter。
- 阶段 1B-2 已加入 `LegacyTaskReadAdapter` 与 `InMemoryProgressAdapter`：前者只读现有
  Task Service，后者围绕生产容器中的同一个 Hub 实现线程安全快照/订阅并在锁外通知。
- 当前 Progress Adapter 仅具备单实例内存语义，不提供跨进程通知、持久化或重放；阶段
  7 由 MySQL 事实与 Redis 唤醒实现替换。check-task 不建设遗留同步恢复 Adapter，未来
  直接加入 MySQL/Outbox 实现。
