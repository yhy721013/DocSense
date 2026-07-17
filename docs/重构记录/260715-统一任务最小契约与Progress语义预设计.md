# 统一任务最小契约与 Progress 语义预设计

## 0. 文档信息

| 项目 | 内容 |
| --- | --- |
| 编写日期 | 2026-07-15 |
| 所属计划 | 总计划阶段 1～2、专项计划波次 1A～1B |
| 文档层级 | L3 跨阶段内部契约预设计 |
| 文档状态 | 阶段 1A 的 Task/Progress 最小契约、1B-1 check-task 可靠命令边界与 **1B-2 Progress 当前运行路径迁移**已于 2026-07-16 落地；后续状态、共享存储、可靠队列和跨实例通知待对应门禁处理 |
| 接口影响 | 内部 ID、状态、事件序号和消息版本均不对外暴露；已确认三类受理成功与 check-task 成功为空响应体、report 活动任务 409、check-task/Progress 严格 `params` 元素校验，以及 Progress 显式 action 错误后保持连接且无 ack；参数集合、错误字段结构、回调和 chat 契约不变 |
| check-task 队列决策 | **2026-07-17 修订**：甲方规定保留请求内同步恢复；可靠命令、Outbox 与 callback Worker 作为后台兜底，不替换同步入口。两种触发源必须共用 expected TaskId、latest-wins、lease/fencing 和同一 Callback Guard |

本文只定义阶段 1 开始拆分业务时必须稳定的最小任务、查询、回调恢复和进度契约。完整 MySQL DDL、租约、fencing、Outbox 和 RabbitMQ 消息实现在后续阶段另行设计。

---

## 1. 当前行为基线

### 1.1 当前任务身份

当前 `llm_tasks` 以 `(business_type, business_key)` 为主键，每次主动提交会生成新的 `execution_id` 并覆盖该业务键的最新行。现状含义为：

- `business_key` 是公开查询使用的业务定位键。
- `execution_id` 是本次执行的内部身份，不得向前端暴露。
- 当前表保存“最新投影”，并不保存每次执行的完整独立任务行。
- weaponry 的显式选中文档已按 `execution_id` 保存不可变快照。
- 部分任务状态更新仍只按业务键执行；未来并发重跑时存在旧执行覆盖新投影的风险。

### 1.2 当前业务状态

| 业务 | 公开状态 | 当前含义 |
| --- | --- | --- |
| file | `0` | 未解析/批量中尚未开始 |
| file | `1` | 解析中 |
| file | `2` | 已解析 |
| file | `3` | 解析失败 |
| report | `0` | 生成中 |
| report | `1` | 已生效 |
| report | `2` | 生成失败 |
| weaponry | `0` | 未解析 |
| weaponry | `1` | 解析中 |
| weaponry | `2` | 已解析 |
| weaponry | `3` | 解析失败 |

这些值是公开契约，不能为了统一内部模型而改变。

### 1.3 当前重复提交差异

| 业务 | 当前行为 | 未来要求 |
| --- | --- | --- |
| file | 任一同名任务处于 `0/1` 时拒绝，返回现有 409；批量请求整体拒绝 | 必须保持，除非另行确认接口行为变更 |
| weaponry | 同一 architectureId 处于 `0/1` 时拒绝，返回现有 409 | 必须保持 |
| report | 当前代码没有活动任务检查；同一 reportId 会再次返回 202，并覆盖最新任务投影 | D0-08 已确认目标改为 409；波次 1C 必须原子拒绝活动任务重复提交 |

在波次 1C 实现完成前，当前代码基线仍存在 report 重复受理行为。接口文档已把 409 标记为“已确认、待实现”，不得误报为当前代码已具备该能力。

### 1.4 当前 check-task 行为

`/llm/check-task` 是带恢复副作用的应用用例，不是纯查询：

1. 读取当前任务。
2. 若任务为终态且回调状态为 `pending/failed`，并且配置了 Callback URL，则尝试补发。
3. 未配置 Callback URL 时，终态 `pending` 会转为 `skipped`。
4. 补发后重新读取任务，形成内部恢复结果并记录日志/审计。
5. 单项缺失返回 404；批量缺失在对应元素返回 `exists=false`。
6. 当前代码会序列化任务状态与 `callbackReplayed`；已批准目标成功体仍为空，单项缺失
   404 和参数错误 400 保持。报告类型当前已在请求内调用同步恢复应用服务，file/weaponry
   在各自业务波次收口。

同步目标由业务级 `RecoverXxxCallbackSynchronously` 组合 Recovery Source 与 Callback
Guard Port；Web 只调用用例，不直接执行 HTTP。阶段 1B-1 的
`RequestCallbackRecoveryService` 和可靠命令边界继续保留，后续作为后台 Outbox 兜底的
触发模型，而不是取代同步入口。

### 1.5 当前与目标 Progress 行为

- 支持 file、report、weaponry。
- 当前只接受不带 action 的订阅消息，不发送 ack；这是阶段 1B-2 已切换的公开格式。
- 阶段 1B-2 以前的代码曾支持显式 subscribe/query/unsubscribe 并发送 ack；仓库没有甲方或生产前端需求证据，现已下线。任何显式 action 都返回 error 消息并保持连接。
- subscribe 会为每个 key 发送当前 Hub 最新值；Hub 无值时发送数据库任务快照。
- 连接关闭时释放该连接拥有的全部订阅；目标协议不再提供显式 query/unsubscribe 控制消息。
- 批量响应顺序遵循 params 顺序。
- 任务不存在时发送 `progress=0.0` 和 `exists=false`。
- progress 统一归一化到 0～1，并消除可见浮点尾差。
- 当前没有公开事件 ID、游标、断线重放或续传承诺。

---

## 2. 最小任务领域术语

### 2.1 内部标识

| 名称 | 定义 | 公开可见性 |
| --- | --- | --- |
| `TaskId` | 一次具体执行的不可变内部 ID；兼容期复用现有 `execution_id` | 不可见 |
| `TaskType` | `file_analysis`、`report_generation`、`weaponry_extraction` 等内部执行类型 | 不可见 |
| `BusinessType` | 与现有协议对应的 file/report/weaponry | 已存在，但不得新增值改变协议 |
| `BusinessKey` | 内部规范化字符串；fileName/reportId/architectureId 的统一查询键 | reportId 入站已批准接受整数或整数字符串并按整数值归一；既有输出类型保持不变 |
| `BatchId` | file 批量请求内部关联 ID | 不可见 |
| `AttemptNo` | 同一 Task 被领取执行的次数 | 不可见 |
| `TraceId` | Web、Outbox、消息和 Worker 的追踪 ID | 不可见 |

`TaskId` 与业务键必须分离。目标数据库应保存每次任务执行，并维护“业务键 → 最新可见 TaskId”的投影；不能继续通过覆盖唯一任务行保存全部历史。

### 2.2 最小任务快照

阶段 1 需要的框架无关快照至少包含：

```text
TaskSnapshot
├── task_id
├── task_type
├── business_type
├── business_key
├── execution_state
├── public_status
├── progress
├── message
├── input_schema_version
├── input_snapshot_ref / input_snapshot
├── result_ref / result_snapshot
├── callback_status
├── created_at
└── updated_at
```

`public_status` 仅由 Presenter 计算或从兼容存储读取，不能反向驱动领域状态转换。

### 2.3 不可变输入

提交成功前必须冻结：

- 原始业务请求的规范化副本。
- 输入 Schema 版本和摘要。
- 文件/对象引用及必要元数据。
- weaponry 显式选中文档快照和顺序。
- report 文件列表、模板引用和报告 ID。
- analysis 批量顺序及每个 file 子任务引用。
- 影响结果且运行中不允许漂移的配置版本，例如 Prompt 版本和业务模式。

密钥、数据库 Session、打开的文件句柄、Flask request、网络 Session 和回调函数不得进入快照。

---

## 3. 内部状态预设计

### 3.1 候选统一状态

```text
accepted → queued → leased → running → succeeded
                               ├──────→ failed
                               ├──────→ cancelling → cancelled
                               ├──────→ superseded
                               └──────→ outcome_unknown
```

阶段 1 只要求 DTO 能表达状态，阶段 2 才实现完整迁移、租约和 fencing。

### 3.2 公开状态映射

| 内部状态 | file | report | weaponry |
| --- | --- | --- | --- |
| accepted/queued | 保持当前批量/提交语义，通常为 `0` 或 `1` | `0` | `1` |
| leased/running | `1` | `0` | `1` |
| succeeded | `2` | `1` | `2` |
| failed | `3` | `2` | `3` |
| cancelled/superseded/outcome_unknown | **无现成公开值，不得自行映射** | **无现成公开值，不得自行映射** | **无现成公开值，不得自行映射** |

最后一行若需要对调用方可见，必须停止并确认。内部可以保存这些状态，但 Presenter 只能按已确认的兼容策略输出。

### 3.3 写入保护

从阶段 1C 开始，所有任务进度、结果、回调和产物提交至少满足：

- 更新条件包含 `task_id`，不能只按业务键。
- 只有最新投影拥有者可以更新公开查询投影。
- 旧 task 即使完成，也不能覆盖新 task 的状态和结果。
- 终态不可被普通进度更新重新打开。
- callback attempt 必须关联 task ID，不能误补发另一轮执行的结果。

阶段 2 在此基础上增加 attempt、lease owner、lease expiry、fencing token 和版本条件。

---

## 4. 最小应用与端口契约

以下名称是内部设计基线，实际实现可在不改变语义的情况下调整。

### 4.1 Task Read

```python
class TaskReadPort(Protocol):
    def get_by_id(self, task_id: TaskId) -> TaskSnapshot | None: ...
    def get_latest(self, business_ref: TaskBusinessRef) -> TaskSnapshot | None: ...
    def get_latest_many(
        self,
        business_refs: tuple[TaskBusinessRef, ...],
    ) -> tuple[TaskSnapshot | None, ...]: ...
```

返回顺序必须与请求业务键顺序一致；Repository 不执行回调或网络 I/O。

> 实施状态（阶段 1A-3）：已按上述领域类型落地。应用服务会校验批量返回长度、
> 业务引用，以及恢复后按 TaskId 重读的身份一致性；Adapter 违反契约时显式失败。

### 4.2 Task Submission

```python
class TaskSubmissionPort(Protocol):
    def create(self, request: NewTaskRequest) -> TaskSnapshot: ...
```

目标实现应一次写入任务和不可变输入。阶段 4 再把 Outbox 事件加入同一事务。

report 提交还必须在同一事务中执行“活动 `reportId` 唯一”约束。不能只在 Web 层先查询再创建，否则两个并发请求可能同时通过检查；冲突结果由 Presenter 映射为已确认的 HTTP 409 和既有错误体。

### 4.3 Dispatcher

```python
class TaskDispatcher(Protocol):
    def dispatch(self, request: TaskDispatchRef) -> None: ...

TaskDispatchRef = {
    "task_id": "...",
    "task_type": "...",
    "schema_version": 1,
    "trace_id": "...",
}
```

Dispatcher 只接收稳定内部标识和信封元数据，不接收业务大对象。Worker 必须按 task ID 重新读取输入。

### 4.4 Callback Recovery Command 与 Worker Delivery

```python
class CallbackRecoveryCommandPort(Protocol):
    def request_many(
        self,
        commands: tuple[CallbackRecoveryCommand, ...],
    ) -> tuple[CallbackRecoveryCommandResult, ...]: ...

class CallbackDeliveryPort(Protocol):
    def deliver(self, recovery_request_id: str) -> CallbackDeliveryResult: ...
```

这里的 Command Port 专用于后台兜底：只允许在 MySQL 事务内创建或复用 recovery request
并写 Outbox，禁止执行外部 HTTP。同步 check-task 不经该队列等待，而是调用业务恢复用例；
同步用例与 Worker 最终都必须进入同一个 Delivery/Guard Port 执行 latest-wins、外发和
条件持久化，二者都不得把发送权藏进 Task Read。

阶段 1B-1 已固定批量原子签名：输入只含 expected TaskId、业务引用、固定触发源、Schema
版本和追踪信息；recovery request ID 由持久化 Adapter 在事务内生成/复用，并通过
`created/already_active` 结果返回。端口必须返回等长同序 tuple，事务失败不得部分提交。

Callback Worker 的自动恢复与 `/llm/check-task` 的显式同步恢复必须分开记录触发来源，
但不能分开实现发送权。两者都先竞争同一 TaskId/业务键 Guard；未取得 lease 时不得发起
HTTP。每次真正投递形成独立 delivery attempt 和审计记录，并遵守 D0-06 保守重试策略。

### 4.5 RequestCallbackRecoveryService

输入为已经由 Web Adapter 按当前协议解析的 typed request，输出为框架无关结果：

- 保留 params 顺序。
- 任一 params 元素不是对象时整次请求失败，不过滤后部分处理。
- 单项和批量的缺失语义不同。
- 每项记录同步恢复内部结果（acquired/busy/not_needed/stale/outcome_unknown），但不把
  这些内部分类扩张成新的公开响应字段。
- 不直接生成 Flask Response。
- 除已批准的 reportId 入站“JSON 整数或十进制整数字符串、数字部分最多 128 位”外，
  不改变 reportId 的既有输出类型，也不改变 architectureId 的公开类型。正负号不计入、
  前导零计入该字符上限；内部业务键仍按整数值规范化。

同步恢复用例完成本次尝试后，Presenter 才按既有契约返回 HTTP 200；RabbitMQ 是否可用
不改变同步调用职责。后台 Outbox 负责无请求场景的持续恢复，并在消费时再次竞争同一
Guard。报告类型已装配该同步模式。

---

## 5. Callback 状态与可靠性

### 5.1 当前状态必须保留

- `pending`：尚未成功、失败或跳过。
- `success`：当前任务回调成功。
- `failed`：当前任务回调失败，可由 check-task 补发。
- `skipped`：任务完成时未配置 Callback URL，不再由 check-task 自动补发。

业务任务成功与回调成功是两个正交状态。不能因回调失败把业务任务公开状态改成业务失败。

### 5.2 D0-06 已决策的保守策略

接收方幂等能力和结果查询能力均无法确认，因此按“不保证幂等、不可查询”设计：

- 当前回调契约禁止新增参数，不能通过新增 callback ID 字段解决幂等。
- 阶段 4～6 必须按 task ID 持久化 delivery attempt、触发来源、错误阶段和内部 delivery outcome，但出站 Presenter 仍使用现有载荷。
- 仅 DNS/连接建立等能够确认请求尚未送达的失败允许有限自动重试。
- 请求发出后的读取超时、连接中断或响应丢失标记为内部 `delivery_outcome_unknown`，callback Worker 不自动重试。
- 同业务键使用 callback Guard 串行化提交与外发：旧 callback 已先取得发送权时，新提交
  最多等待当前 callback timeout；结果未知或等待到期仍被占用时，新提交返回对应接口
  既有 409 并冻结该键，直至内部人工核查解除。
- 非 2xx 响应记录为失败并进入死信/人工处理，不自动重试。
- `/llm/check-task` 保留显式同步补发；投递记录 `trigger=check_task`。后台 Worker 使用
  独立 trigger，所有重复请求/消息都由同一 Guard 抑制，不能形成双发送。
- `delivery_outcome_unknown` 不是新的公开 callback 状态；对外继续兼容映射为 `failed`，业务任务状态保持独立。

---

## 6. Progress 内部语义

### 6.1 内部对象

```text
ProgressKey(task_type/business_type, business_key)
ProgressSnapshot(task_id, progress, message, internal_state, sequence_no, updated_at)
ProgressSubscriptionRequest(ordered_keys)
ProgressNotification(task_id, sequence_no)
```

`sequence_no` 和 task ID 是内部字段，不得加入当前 WebSocket 响应。

### 6.2 端口

```python
class ProgressSnapshotPort(Protocol):
    def get_latest(self, key: ProgressKey) -> ProgressSnapshot | None: ...

class ProgressPublisher(Protocol):
    def publish(self, snapshot: ProgressSnapshot) -> None: ...

class ProgressSubscriptionPort(Protocol):
    def subscribe(self, key: ProgressKey, subscriber: ProgressSubscriber) -> Subscription: ...
    def unsubscribe(self, subscription: Subscription) -> None: ...
```

阶段 1B 的 InMemory Adapter 仍是单实例兼容实现；阶段 7 的实现以 MySQL 快照为事实、Redis 为唤醒。

> 实施状态（阶段 1B-2）：应用服务只返回当前快照项和不透明订阅令牌，不持有或
> 调用 WebSocket；连接级 Registry 留在 Web Adapter。缺失订阅先注册再读当前快照，
> 建立失败时只补偿本次新增令牌。线程安全 InMemory Adapter 已接入唯一 Hub，并在
> Hub 锁外通知；当前实现仍只具备单实例内存语义。

### 6.3 快照选择规则

为了保持已批准的目标协议：

1. 每次无 action 订阅必须为每个 key 生成一个当前快照并建立连接级订阅。
2. 内存适配器存在更近的最新值时优先使用；否则读取 Task Read 投影。
3. 任务不存在时 Presenter 输出现有 `progress=0.0, exists=false`。
4. 目标公开范围只冻结接口文档已描述的 file/report 消息；当前代码中的 weaponry Progress 视为内部实现扩展，未经确认不得新增为外部承诺。
5. 不输出 ack，也不接受显式 action；收到 action 时拒绝整条消息，发送既有结构的 error 消息并保持连接。
6. `params` 任一元素不是对象时拒绝整条消息，不建立部分订阅；发送 error 消息并保持连接。

### 6.4 并发与失败约束

- InMemory Adapter 必须线程安全；订阅表和最新快照更新使用锁。
- 不能在持有内部锁时执行 WebSocket send 或任意外部回调。
- 单个断开的订阅者不能阻止其他订阅者收到通知。
- 订阅释放必须幂等，连接关闭后不保留 callback 引用。
- 业务 Worker 发布进度不能无限阻塞在某个慢 WebSocket 上。
- 阶段 1B-2 已接入连接级有界合并缓冲、初始快照屏障和单写入发送循环；缓冲容量的
  生产调优、50 条真实长连接验收及跨实例唤醒仍在阶段 7/8 和阶段 10 完成。

### 6.5 持久化与通知顺序

目标顺序为：

```text
Worker 条件更新任务/事件并提交 MySQL
  → 发布 ProgressNotification
  → Web 实例收到唤醒
  → 从 MySQL 读取最新快照
  → 使用现有 Presenter 发送 WebSocket/SSE
```

Redis 通知丢失不影响任务事实；客户端重新建立连接并发送原订阅即可取得最新快照。当前接口不增加断线重放能力。

---

## 7. 批量 file 任务要求

当前 `/llm/analysis` 批量请求具有以下公开/可观察语义：

- params 顺序被保留。
- 同一请求内 fileName 不能重复。
- 任一活动同名任务会使整个请求返回当前 409。
- 返回 `tasks` 数组，顺序对应输入。
- 当前后台流程顺序处理，第一项公开状态为处理中，后续项处于尚未开始状态。

未来队列化不能简单把所有文件无序并行消费。阶段 2 需要在以下内部方案中选择，但公开行为保持：

- 父 Batch + 有序子 Task；或
- 单一 Batch Coordinator 按 sequence_no 派发子任务。

具体表结构和调度算法后置，阶段 1 先在输入快照中保留 batch ID、sequence no 和每个 task ID。

---

## 8. chat 与统一任务的关系

chat 已有独立 `ChatRun` 领域状态机，不应被通用任务状态强行替代。阶段 2 前需要在以下方向中完成工程评审：

- 推荐方向：ChatRun 保持 chat 领域事实；通用 Task 只跟踪可调度执行、attempt、租约和队列状态，并持有 chat run 引用。
- 不推荐方向：把 chat 所有消息、终态和中断语义直接塞入通用 tasks 表。

任何选择都不能泄露内部 task/run ID，也不能改变 `/llm/chat*` 契约。

---

## 9. 待确认和后置决策

| ID | 问题 | 当前处理 |
| --- | --- | --- |
| TASK-01 | report 活动任务重复提交如何处理 | **已确认**：HTTP 409 拒绝；波次 1C 使用事务条件/唯一约束实现，不创建新任务或执行器 |
| TASK-02 | cancelled/outcome_unknown 如何映射公开状态 | 当前无公开值；不得自行映射 |
| TASK-03 | file 批量采用父子任务还是 Coordinator | 阶段 2 工程设计，必须保持顺序语义 |
| TASK-04 | chat run 与通用 task 的确切表关系 | 阶段 2/9 联合设计 |
| TASK-05 | 回调超时后能否自动重试 | **已决策**：请求发送后的超时不自动重试，标记内部 `delivery_outcome_unknown`；仅明确未送达错误有限重试，check-task 显式补发除外 |
| TASK-06 | Progress 连接有界缓冲实现 | **1B-2 已接入**：每连接唯一缓冲、初始快照屏障、按 key 合并、慢连接隔离、丢弃计数和路由线程单写入已进入当前运行路径；容量值仍在阶段 7/8 结合 50 连接压测调优 |
| TASK-07 | 显式 Progress action/ack 是否保留 | **已实现**：无甲方或生产前端需求证据；1B-2 已删除显式动作处理，只保留无 action 订阅与连接关闭清理；收到 action 时返回 error 消息、保持连接且无 ack |
| TASK-08 | 混合 `params` 数组是否过滤非对象元素 | **已实现**：不兼容过滤；任一非对象元素使整次 HTTP 请求或 WebSocket 消息报参数错误，不做部分处理 |
| TASK-09 | 旧终态执行恢复回调期间，同一业务键已提交新执行时是否继续发送旧回调 | **完整语义已确认（2026-07-16）**：采用 latest-wins，判定旧执行回调过期并跳过。`fileName` 是前端唯一逻辑任务键；同名文件的不同 `execution_id` 只是该逻辑任务的不同执行代次。实现必须在外发前于同业务键串行化边界内复核最新 TaskId；不匹配时禁止网络调用，并持久化 stale/skipped 审计原因。旧 callback 已先取得发送权时，新提交最多等待当前 callback timeout；若进入 `delivery_outcome_unknown` 或等待到期仍被占用，新提交返回既有 409 并冻结同键，直至内部人工解除。冻结 Callback 载荷下仍无法撤回已发请求，因此不自动重试 |
| TASK-10 | check-task 批量恢复采用同步串行、同步并行还是可靠队列 | **最终修订（2026-07-17）**：甲方要求永久保留请求内同步恢复。当前按请求顺序处理，每个实际发送都由共享 Guard 授权；不增加无界同步并行、本地线程或内存队列。阶段 4～5 建设可靠队列后台兜底，阶段 6 不删除同步入口 |

---

## 10. 分层完成门禁

### 10.1 阶段 1B-1 内部边界（已完成）

- [x] `TaskId`、`TaskBusinessRef`、`TaskSnapshot` 和有序 Task Read 已有框架无关类型。
- [x] `RequestCallbackRecoveryService` 只组合 Task Read 与批量原子 Command Port；不依赖
  Flask、HTTP Transport、RabbitMQ 或 Celery。
- [x] created、already_active、not_needed、stale、重复活动命令复用和事务失败整批回滚
  均有 Fake 测试。
- [x] 空成功 Presenter、单项 404、既有 400 结构及内部 TaskId/recovery request ID 不
  泄露已有测试；当前生产 route/container 明确保留旧实现。

### 10.2 阶段 1B-2 Progress 门禁（已完成）

- [x] `/llm/progress` 由 Subscription/Port/Presenter 处理，仅接受无 action 订阅；显式 action 或非对象 params 元素返回 error 消息并保持连接，不输出 ack。
- [x] InMemory Progress Adapter 线程安全且不在 Hub 锁内调用订阅者；单个订阅者异常不会阻断其他订阅者。
- [x] 每连接 Registry 拥有唯一有界缓冲和全部令牌；初始发送失败、断连及释放失败均有补偿/重试测试，任务线程不执行 `ws.send`。
- [x] 当前路由、旧发布方和类型化 Adapter 共用一个权威 Hub；没有把单实例内存通知误写为 Redis、可靠队列或跨实例能力。

### 10.3 阶段 2～6 后置门禁（不得由 1B Fake 或 1C 单机实现冒充）

- [ ] TaskType、输入快照和 Dispatch 信封随对应任务波次形成完整框架无关类型。
- [ ] 生产 Task Read Repository 不执行网络 I/O；MySQL 中的 Callback Guard/Delivery 与
  后台 Outbox 使用一致的 expected TaskId 和事务边界。
- [ ] callback Worker 独立执行后台恢复；同步 Web 用例与 Worker 共用 Guard；RabbitMQ、
  ACK/DLQ、重复触发竞争和 delivery attempt 审计通过故障注入。
- [ ] `/llm/check-task` 保持请求内同步尝试和 HTTP 200 契约；任一非对象 params 元素整次
  400，其他 400/404 不变。RabbitMQ 故障不应切换到第二套发送实现。
- [ ] 所有任务写入携带 task ID；TASK-09 latest-wins 已在 Worker Guard、提交端串行化和
  expected TaskId 条件持久化中实现。
- [ ] `delivery_outcome_unknown` 后同业务键新执行按已确认的等待/409/人工解除策略进入
  1C 兼容 Guard 和阶段 4～6 可靠实现；TASK-01～TASK-10 的到期事项均已实现或通过明确后置门禁。
- [ ] 全部公开 HTTP、WebSocket、SSE 和 Callback 回归证明未暴露内部 ID/序号。
