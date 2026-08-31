# 任务适配层

本目录实现 `ports/` 中定义的协议，并隔离具体数据库、缓存、消息队列、外部回调和迁移期遗留服务。

- Adapter 可以依赖端口、领域类型和具体基础设施，但不能被 `domain/` 或 `application/` 反向导入。
- 迁移期兼容 Adapter 必须明确单实例、线程安全和一致性能力，不能把进程内实现描述成可靠队列。
- Adapter 不负责 HTTP/WebSocket 参数解析或公开响应拼装；这些职责属于 Web Adapter 与 Presenter。
- 阶段 1B-2 已加入 `LegacyTaskReadAdapter` 与 `InMemoryProgressAdapter`：前者只读现有
  Task Service，后者围绕生产容器中的同一个 Hub 实现线程安全快照/订阅并在锁外通知。
- 阶段 1C-3 已加入通用 `LegacyTaskCommandAdapter`：通过业务 Codec 持久化不可变输入和
  结果，在独立 SQLite 连接的短 `BEGIN IMMEDIATE` 事务中完成原子受理、领取、expected
  TaskId 进度/终态条件写、accepted 扫描和队列只读汇总。Adapter 不持有线程、内存任务
  队列或外部 I/O。
- 阶段 1C-6 已加入 `LatestTaskProgressPublisherAdapter`：通过
  `GuardedProgressPublisherPort` 在 Hub 同业务键发布锁内复核 SQLite latest owner，再原子
  更新内存 latest；Guard 运行在全局状态锁外，不同业务键不会因一个慢查询互相阻塞，旧
  任务通知也不会在“先查数据库、后写 Hub”的间隙覆盖新任务。
- `UploadTaskLimiter` 实现共享重型资源许可。迁移期 analysis 可继续使用同步 `run`；报告
  Dispatcher 使用可中断获取，使停机时尚未获得许可的 accepted 任务不会在停止后启动。
- 阶段 1D-5 新增 `LocalPersistentTaskDispatcher` 与 `FileProcessSingletonGuard`。前者只把
  `Event` 当作常量空间唤醒，任务事实始终保存在 Repository；它统一提供 FIFO 有界扫描、
  领取前毒任务持久冷却、running 只观察、隔离维护线程、共享 limiter、真实关闭和 fatal
  快照。后者使用 OS 文件锁拒绝第二进程，并在锁释放异常时暴露 fatal，不以锁文件是否存在
  猜测所有权。通用实现不导入 report/weaponry；两类业务仅保留指标和维护语义薄包装。
- 业务 Codec 由各业务模块 Adapter 提供；tasks Adapter 不得为了序列化输入而反向导入
  report/analysis/weaponry。当前首个 Codec 位于 `report/adapters/task_codec.py`。
- 当前 Progress Adapter 仅具备单实例内存语义，不提供跨进程通知、持久化或重放；阶段
  7 由 MySQL 事实与 Redis 唤醒实现替换。报告与 Weaponry 当前均使用通用本地内核消费 SQLite
  accepted 事实，Weaponry 已在 1D-6 完成生产组合根和公开路由绑定；这仍是兼容执行器，不是
  tasks 可靠队列实现。
  check-task 按甲方要求保留请求内同步恢复：report 业务模块已装配共享 Callback Guard 的
  专用 Adapter/Application，weaponry 也已装配等价的独立业务 Guard/Application，file 已路由到
  Analysis 模块的 `RecoverAnalysisCallbackSynchronously`，缺少该链时明确失败并禁止回退到旧恢复器。
  `LegacyTaskReadAdapter` 只读取现有任务投影，不拥有业务回调恢复逻辑。未来 MySQL/Outbox 只增加
  后台可靠兜底，不替换同步入口。
