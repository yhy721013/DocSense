# 任务模块说明

`tasks` 是面向业务的任务控制面模块，不是 Celery 包装层。它统一承载任务身份、状态读取、可靠回调恢复命令和进度协作所需的稳定内部契约，同时保持现有 HTTP/WebSocket/Callback 协议不变。

## 分层职责

| 目录 | 职责 | 允许依赖 |
| --- | --- | --- |
| `domain/` | 任务身份、业务引用、快照和内部状态规则 | Python 标准库及本模块纯领域类型 |
| `application/` | check-task 可靠命令登记、历史同步检查、Progress 等用例编排 | `domain/`、`ports/` |
| `ports/` | 任务读取、可靠命令登记、历史同步回调恢复、进度查询/发布/订阅协议 | `domain/` 与标准类型 |
| `adapters/` | 兼容遗留 Task Service/Hub，未来实现 MySQL、Redis、RabbitMQ 等端口 | 具体基础设施与本模块端口 |

## 数据与协作边界

- 任务模块未来拥有通用任务身份、执行状态、输入快照、事件、回调投递事实和进度事实；阶段 1A 不迁移或复制现有 SQLite 数据。
- chat 继续拥有会话、消息、ChatRun、租约和清理任务。tasks 不导入 chat persistence，也不跨模块更新 chat 表。
- report、analysis、weaponry 等业务模块通过任务应用入口或端口协作，不允许 tasks 导入这些模块的 Adapter。
- 内部 `task_id`、attempt、租约、队列名和事件序号均不得进入现有公开响应。

## 当前实施状态（阶段 1B-2）

阶段 1A～1B-1 已建立并通过 Fake Port 验证以下框架无关契约：

1. `domain/models.py`：TaskId、业务引用、任务/进度快照和有序订阅请求；
2. `ports/callback_recovery_commands.py`：只在共享事务内创建或复用恢复请求的批量原子
   Command Port；输入携带 expected TaskId，持久化 Adapter 生成/复用内部恢复请求 ID；
3. `application/request_callback_recovery.py`：保持请求顺序和缺失位置，一次调用批量命令
   端口，并校验返回长度、类型、业务键和 expected TaskId；事务失败原样传播；
4. `application/check_status.py`：阶段 1A 的同步恢复原型，继续用于持久化一致性审查，
   但不会创建生产 Adapter 或装配到新路径；
5. `application/progress.py` 与 `progress_delivery.py`：连接绑定的订阅令牌、快照选择、
   失败补偿、有界通知缓冲、初始顺序屏障和慢连接隔离；
6. `adapters/legacy_task_read.py`：把现有 `LLMTaskService` 只读投影转换为不可变 Task DTO，
   不执行回调或写入；
7. `adapters/in_memory_progress.py`：围绕唯一 `LLMProgressHub` 实现线程安全的快照与订阅
   Port，Hub 锁外通知并隔离订阅者异常。

当前 `/llm/progress` 已通过 Container 装配上述应用服务和兼容 Adapter；旧发布方与新
订阅路径共享同一个 Hub，不存在双份 latest。WebSocket 对象仍只存在于 Flask Adapter，
任务应用层和发布线程不持有连接。

当前仍没有 MySQL/Outbox、RabbitMQ/Worker、Redis 跨实例通知或 check-task 新路由绑定。
`app/presenters/task_status.py` 已冻结未来 200 零字节成功体与既有 400/404 映射，但
check-task Blueprint 继续使用旧同步实现，仅在阶段 3～6 的共享事务、Outbox、队列和
Worker 全部就绪后直接切换。
