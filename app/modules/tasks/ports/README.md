# 任务端口层

本目录定义应用层所需的能力协议。阶段 1A～1B-1 已包含：

| 文件 | 契约 |
| --- | --- |
| `task_read.py` | 按 TaskId、最新业务引用及有序业务引用批量读取；批量结果必须等长并保留缺失位置。 |
| `callback_recovery.py` | 阶段 1A 同步原型：按 TaskId 立即恢复并返回 attempted/replayed；保留作一致性证据，不建设生产 Adapter。 |
| `callback_recovery_commands.py` | 生产目标：在一个共享事务中按顺序创建/复用整批恢复命令，返回 created/already_active/not_needed/stale；不执行 HTTP 或发布 RabbitMQ。 |
| `progress.py` | 查询最新 ProgressSnapshot，注册类型化 subscriber，用绑定 delivery_id 的不透明令牌幂等释放订阅，并提供同业务键持久化 owner 复核与 latest 写入原子化的 Guarded Publisher。 |
| `task_commands.py` | 泛型原子受理、携带输入的 execution 读取/领取、expected TaskId 进度/终态条件写、latest 检查、ready accepted 扫描和领取前条件冷却。 |
| `task_queue.py` | 只读 accepted/running/终态数量、最老时间和有界 running TaskId 样本；严禁诊断时重置状态。 |
| `runtime.py` | 可中断共享执行许可和跨进程单实例所有权；不暴露信号量、文件锁或供应商实现。 |

可靠命令输入只携带 expected TaskId、业务引用、固定 `check_task` 触发源、Schema 版本和
追踪信息；recovery request ID 必须由持久化 Adapter 在事务内生成或复用并通过结果返回。
同一 TaskId 在 pending/queued/running 中至多一个活动请求。整批事务提交失败必须抛出
异常且不得留下部分命令；RabbitMQ 发布由后续 Outbox Relay 完成。

Task Read 禁止网络副作用；Progress 实现必须线程安全、锁外通知，且隔离单个 subscriber
异常。Guarded Publisher 的 owner 判断必须只读、无外部副作用且不持有数据库写事务；实现
需在同业务键通知投影临界区内执行判断，且不得持有全局 Hub 状态锁。连接侧的有界缓冲、初始快照屏障和慢连接隔离已由应用层
组件及离线并发边界测试冻结；底层 InMemory Adapter 的锁外发布、故障隔离和原子 owner
Guard 已完成实现和验证。

`TaskCommandPort` 使用泛型业务命令/输入/结果，tasks 模块不得因此导入 report 等业务模块。
create-if-allowed 必须原子提交 execution 与 latest 投影；`False` 条件写属于正常 stale 结果。
Repository 不得执行 Progress 通知、Dispatcher、网络回调或任何业务 I/O。

端口不得导入 Flask/FastAPI、Celery、SQLAlchemy、`sqlite3`、`requests` 或具体 Adapter；实现细节必须位于 `adapters/`。
