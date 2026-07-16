# 任务模块说明

`tasks` 是面向业务的任务控制面模块，不是 Celery 包装层。它将在后续波次统一承载任务身份、状态读取、回调恢复和进度协作所需的稳定内部契约，同时保持现有 HTTP/WebSocket/Callback 协议不变。

## 分层职责

| 目录 | 职责 | 允许依赖 |
| --- | --- | --- |
| `domain/` | 任务身份、业务引用、快照和内部状态规则 | Python 标准库及本模块纯领域类型 |
| `application/` | check-task、Progress 等用例编排 | `domain/`、`ports/` |
| `ports/` | 任务读取、回调恢复、进度查询/发布/订阅协议 | `domain/` 与标准类型 |
| `adapters/` | 兼容遗留 Task Service/Hub，未来实现 MySQL、Redis、RabbitMQ 等端口 | 具体基础设施与本模块端口 |

## 数据与协作边界

- 任务模块未来拥有通用任务身份、执行状态、输入快照、事件、回调投递事实和进度事实；阶段 1A 不迁移或复制现有 SQLite 数据。
- chat 继续拥有会话、消息、ChatRun、租约和清理任务。tasks 不导入 chat persistence，也不跨模块更新 chat 表。
- report、analysis、weaponry 等业务模块通过任务应用入口或端口协作，不允许 tasks 导入这些模块的 Adapter。
- 内部 `task_id`、attempt、租约、队列名和事件序号均不得进入现有公开响应。

## 当前实施状态

阶段 1A 已建立并通过 Fake Port 验证以下框架无关契约：

1. `domain/models.py`：TaskId、业务引用、任务/进度快照和有序订阅请求；
2. `ports/`：Task Read、Callback Recovery、Progress Snapshot/Subscription；
3. `application/check_status.py`：按业务键批量读取、按同一 TaskId 恢复并无条件重读，校验恢复结果与持久化状态一致；
4. `application/progress.py`：连接绑定的订阅令牌、快照选择、失败补偿与幂等释放；
5. `application/progress_delivery.py`：连接级有界通知缓冲、初始快照顺序屏障、慢连接隔离与可观测丢弃计数。

当前仍没有模块级单例、数据库/网络实现或路由绑定。波次 1B 才会加入遗留兼容
Adapter、Presenter 和 Flask 请求解析器，并按已批准契约切换 check-task/Progress。
MySQL、Redis、RabbitMQ/Celery 与跨实例能力分别由后续阶段实现。
