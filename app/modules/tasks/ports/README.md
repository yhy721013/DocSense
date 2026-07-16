# 任务端口层

本目录定义应用层所需的能力协议。阶段 1A-3 已包含：

| 文件 | 契约 |
| --- | --- |
| `task_read.py` | 按 TaskId、最新业务引用及有序业务引用批量读取；批量结果必须等长并保留缺失位置。 |
| `callback_recovery.py` | 按 TaskId 触发一次 check-task 显式恢复，返回 attempted/replayed/最终状态与内部 delivery outcome。 |
| `progress.py` | 查询最新 ProgressSnapshot，注册类型化 subscriber，并用绑定 delivery_id 的不透明令牌幂等释放订阅。 |

Task Read 禁止网络副作用；Progress 实现必须线程安全、锁外通知，且隔离单个 subscriber 异常。连接侧的有界缓冲、初始快照屏障和慢连接隔离已由应用层组件及离线并发边界测试冻结；底层 InMemory Adapter 的锁外发布与故障隔离仍在波次 1B 实现和验证。

端口不得导入 Flask/FastAPI、Celery、SQLAlchemy、`sqlite3`、`requests` 或具体 Adapter；实现细节必须位于 `adapters/`。
