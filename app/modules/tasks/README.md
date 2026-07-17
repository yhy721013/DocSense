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

## 当前实施状态（阶段 1C-6）

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

阶段 1C-2 为首个通用任务写入消费者（report）增加：

8. `TaskExecutionSnapshot[TInput]`：一次执行身份、状态与不可变业务输入的泛型快照；
9. `ports/task_commands.py`：原子受理分类、按 TaskId 读取/领取、expected TaskId 进度与
   终态条件写、latest 检查和 accepted 扫描；
10. `ProgressPublisherPort`：只发布已经完成条件持久化的类型化通知，不携带公开 JSON；
    `GuardedProgressPublisherPort` 又把持久化 latest owner 复核与内存 latest 写入放进同一
    业务键原子发布临界区，并把慢持久化查询移出全局 Hub 锁，封闭旧 accepted 的预检查
    竞态且不阻塞其他业务键；
11. 严格 report Fake：按 TaskId 保存历史执行、按业务键保存 latest，并支持各步骤返回
    stale、异常和非法端口结果，生产 tasks 包不依赖 report 类型。

阶段 1C-3 进一步增加：

12. `adapters/legacy_task_commands.py`：以业务 Codec 隔离 report 输入/结果，在独立 SQLite
    连接的短事务内实现追加 execution、原子受理/领取、latest、accepted 扫描和 expected
    TaskId 进度/终态双表条件写；tasks 包仍不导入 report；
13. `LLMTaskService` 的增量 `llm_task_executions`/`callback_delivery_guards` Schema 和兼容
    事务方法；旧 `llm_tasks` 继续作为最新公开投影，file/weaponry 的 check-task 路径不迁移；
    report 后续通过业务模块应用服务复用这些 execution/Guard 事实。

阶段 1C-6 进一步增加 `TaskQueueInspectionPort/TaskQueueSnapshot`、可中断执行许可和跨进程
单实例运行端口。报告 Dispatcher 可读取 accepted/running/终态数量、最老时间和有界
running TaskId 样本，但不得在诊断中修改状态；`TaskCommandPort` 还可对仍为 accepted 的
领取前故障设置持久冷却。`LegacyTaskCommandAdapter` 已作为报告组合根的任务事实与诊断
Adapter 使用。

当前 `/llm/progress` 已通过 Container 装配上述应用服务和兼容 Adapter；旧发布方与新
订阅路径共享同一个 Hub，不存在双份 latest。WebSocket 对象仍只存在于 Flask Adapter，
任务应用层和发布线程不持有连接。

当前仍没有 MySQL/Outbox、RabbitMQ/Worker 或 Redis 跨实例通知。
SQLite Task Command Adapter 已装配当前开发分支的 `/llm/generate-report` 组合根，并支撑
公开 202/409 与本地持久积压，但只能证明单实例内部原子语义，不能作为可靠任务队列或
多实例一致性已经实现、部署或通过生产容量验收的证据。
`/llm/check-task` 继续按甲方规定保留请求内同步恢复：report 类型现已绑定 report 模块的
`RecoverReportCallbackSynchronously`，并与正常 Worker 竞争同一个 execution 级 Callback
Guard；file/weaponry 暂走旧同步兼容实现。阶段 3～6 只增加 MySQL/Outbox/RabbitMQ 后台
兜底，不替换同步入口，也不得建立绕过 Guard 的并行发送链。
