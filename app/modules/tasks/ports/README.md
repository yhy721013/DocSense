# 任务端口层

本目录定义应用层所需的能力协议。阶段 1A～1B-1 已包含：

| 文件 | 契约 |
| --- | --- |
| `task_read.py` | 按 TaskId、最新业务引用及有序业务引用批量读取；批量结果必须等长并保留缺失位置。 |
| `callback_recovery.py` | 阶段 1A 同步原型：按 TaskId 立即恢复并返回 attempted/replayed；保留作一致性证据，不建设生产 Adapter。 |
| `callback_recovery_commands.py` | 生产目标：在一个共享事务中按顺序创建/复用整批恢复命令，返回 created/already_active/not_needed/stale；不执行 HTTP 或发布 RabbitMQ。 |
| `progress.py` | 查询最新 ProgressSnapshot，注册类型化 subscriber，并用绑定 delivery_id 的不透明令牌幂等释放订阅。 |

可靠命令输入只携带 expected TaskId、业务引用、固定 `check_task` 触发源、Schema 版本和
追踪信息；recovery request ID 必须由持久化 Adapter 在事务内生成或复用并通过结果返回。
同一 TaskId 在 pending/queued/running 中至多一个活动请求。整批事务提交失败必须抛出
异常且不得留下部分命令；RabbitMQ 发布由后续 Outbox Relay 完成。

Task Read 禁止网络副作用；Progress 实现必须线程安全、锁外通知，且隔离单个 subscriber 异常。连接侧的有界缓冲、初始快照屏障和慢连接隔离已由应用层组件及离线并发边界测试冻结；底层 InMemory Adapter 的锁外发布与故障隔离仍在波次 1B 实现和验证。

端口不得导入 Flask/FastAPI、Celery、SQLAlchemy、`sqlite3`、`requests` 或具体 Adapter；实现细节必须位于 `adapters/`。
