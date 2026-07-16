# 阶段 1B-2：Progress 控制面迁移执行记录

## 0. 执行结论

| 项目 | 结论 |
| --- | --- |
| 执行日期 | 2026-07-16 |
| 对应设计 | `../重构记录/260715-阶段1A-1B文件级实施设计.md` 波次 1B-2 |
| 完成状态 | 已完成当前代码运行路径迁移及全面审查修复，尚未部署生产环境 |
| 公开接口 | `/llm/progress` WebSocket；并统一当前三个 reportId 入站位置；未增删任何请求或响应字段 |
| 已批准差异 | 只接受无 action 订阅；显式 action 返回 error、保持连接且无 ack；任一非对象 params 元素使整条消息失败 |
| 数据与基础设施 | 无 Schema 和历史数据迁移；未引入 MySQL、Redis、RabbitMQ、MinIO 或外部服务 |
| 运行限制 | 未启动 `run.py`，未建立真实 WebSocket/AnythingLLM/模型/回调连接 |

阶段 1B-2 已把 `/llm/progress` 从 Blueprint 内部命令分支和直接 Hub 回调，迁移为
“Flask Request Adapter → 框架无关应用服务/Port → Presenter → 连接路由”的结构。
Progress 发布线程不再直接调用 `ws.send`；每个连接独占有界合并缓冲，由该连接的路由
线程统一发送。旧业务发布方和新订阅路径共用同一个权威 Hub，没有建立双份内存状态。

本次“当前代码运行路径已迁移”不表示代码已经部署到生产服务器，也不表示具备跨进程
通知、可靠重放或 50 条真实长连接容量承诺。

2026-07-16 全面审查后又修复了连接队列出队后同任务旧回调可重新入队的问题，把原
“50 个 key”测试改为 Barrier 同步的 50 个真实工作线程，并按负责人新确认口径统一
`/llm/generate-report`、报告类型 `/llm/check-task` 与 `/llm/progress` 的 `reportId`
入站规范化。上述修改均已完成本文第 6 节所列验收。

---

## 1. 契约切换结果

### 1.1 客户端消息

当前只处理既有的无 action 订阅格式：

```json
{
  "businessType": "file",
  "params": [{"fileName": "A.pdf"}]
}
```

实际行为如下：

1. 只要 JSON 对象中出现 `action` 字段，包括 `null` 或空字符串，整条消息拒绝；
2. 返回既有 `{"type":"error","message":"..."}` 结构，连接保持可用；
3. 不再处理显式 subscribe/query/unsubscribe，也不发送 ack；
4. `params` 必须是非空数组，任一元素不是对象或任一业务键无效时整条消息失败，不建立
   部分订阅；
5. 非法 JSON 返回 error 后仍可继续发送下一条合法消息；
6. 合法批量消息按 params 位置发送当前快照；重复业务键保留重复的当前快照位置，但只
   建立一条底层订阅，避免后续通知倍增。
7. 报告类型 `reportId` 接受 JSON 整数或十进制整数字符串，不设置 32/64 位业务范围；
   同整数值按同一业务键订阅。格式错误返回 error 后连接仍可继续处理合法消息。

公开 Progress 数据消息仍保持既有字段和类型：file 使用字符串 `fileName`，report 使用
JSON number `reportId`；weaponry Progress 仅作为当前内部兼容分支保留字符串
`architectureId`，没有扩张为新的外部承诺。任务不存在时仍发送 `progress=0.0` 与
`exists=false`。内部 TaskId、sequence、subscription ID、delivery ID 均不输出。

### 1.2 接口文档边界

阶段 0 契约资产已把 Progress 目标状态从 pending 更新为 `implemented / 1B-2`。经负责
人随后明确批准，`docs/接口文档/文件处理和报告生成.md` 仅同步了三处实施状态：

- check-task 从“波次 1B 切换”更正为“阶段 6 可靠链路就绪后一次性切换”；
- Progress 显式 action/ack 从“将在 1B 下线”更正为“已在 1B-2 下线”；
- Progress 实施状态更正为“已于 2026-07-16 在当前代码运行路径实现”。

上述三项是阶段 1B-2 首次完成时的文档同步，当时没有改变参数类型。随后负责人明确
批准 `reportId` 入站接受 JSON 整数或十进制整数字符串、无业务数值范围并按整数值
规范化；接口文档和阶段 0 黄金资产已据此增加该口径。请求字段集合、响应字段与
`data.reportId` 的 JSON number 输出类型仍未改变。

---

## 2. 结构改造

### 2.1 入站与展示边界

- `app/adapters/web/flask/progress_requests.py`：完整解析一条无 action 消息，输出不可变
  `ProgressSubscriptionRequest`；参数失败不会返回部分结果。
- `app/presenters/task_progress.py`：集中映射当前快照、缺失快照、error 和严格 JSON；
  Presenter 不导入 Flask、不持有 WebSocket，也不暴露内部身份。
- `app/adapters/web/flask/progress_connection.py`：每连接 Registry 拥有唯一投递缓冲和
  全部待释放令牌；发送前先登记令牌，释放失败保留对象并有限重试。

### 2.2 应用与基础设施适配

- `app/modules/tasks/adapters/legacy_task_read.py`：只读适配现有 `LLMTaskService`，把遗留
  字典转换为不可变 `TaskSnapshot`；不触发回调、写库或外部 I/O。
- `app/modules/tasks/adapters/in_memory_progress.py`：围绕注入的同一个 Hub 实现
  `ProgressSnapshotPort` 与 `ProgressSubscriptionPort`，保存不透明令牌并转换类型化
  快照；不建立第二份 latest。
- `app/services/core/progress_hub.py`：以 `RLock` 保护 latest 和订阅表；先在锁内更新并
  复制订阅者，再在锁外逐个通知。单个订阅者异常被记录和隔离，payload/事件按订阅者
  深拷贝，避免一个连接污染其他连接或 Hub 投影。
- `app/container.py`：生产组合根创建一个 Hub、一个类型化 Adapter、一个遗留 Task Read
  Adapter 和一个 `ProgressSubscriptionService`。Blueprint 中现有任务发布方与新
  WebSocket 路径共享该 Hub。
- `app/services/llm_service/task_service.py`：增加按 execution ID 的只读入口，供内部
  Task Read Port 锁定同一次执行；不改变公开接口。

### 2.3 WebSocket 单写入并发模型

`app/blueprints/llm.py` 当前只保留传输职责：

```text
任务/发布线程
  → Hub 锁内更新 latest、锁外通知
  → 类型化 Adapter 转换快照
  → 连接独占 ProgressDeliveryBuffer（短临界区入队）
  → 该 WebSocket 的 Flask 路由线程 drain
  → 唯一 ws.send 写入者
```

路由使用带超时的 `receive` 周期性排空通知。每个连接仍由当前 Web 服务器请求线程承载，
但不再为每条连接额外创建转发线程；50 条连接不会再额外产生 50 个发送线程。轮询间隔
和默认缓冲容量 256 只是当前安全默认值，须在阶段 7/8 与阶段 10 的真实压测中调优。

连接关闭时先关闭缓冲，使已经被发布线程复制出的迟到回调无害返回，再幂等释放令牌。
已知释放失败最多重试三次；仍失败的令牌会保留到 Registry 生命周期结束并输出告警，
不会把“记录日志后丢失令牌引用”误报为清理成功。

---

## 3. 并发与顺序修正

### 3.1 初始快照屏障

订阅采用“先注册、再读取当前快照”，避免读取与注册之间漏掉发布。初始快照发送完成前，
并发通知暂存于连接缓冲：

- 屏障建立前已排队的同 key 通知早于本次当前快照；TaskId 不同或序号不大于当前快照
  时均丢弃，防止旧执行/旧序号倒退；
- 屏障开启后、当前快照读取后到达的不同 TaskId 通知可能代表同一业务键的新执行，必须
  保留；相同 TaskId 仍按 sequence 去重；
- 初始 `send` 失败时撤销屏障、关闭缓冲并释放已经登记的全部令牌。

该规则修复了审查中发现的竞态：客户端可能先收到旧执行当前快照，随后本应收到的新执行
首条通知曾会因为 TaskId 不同被误删。新增测试同时证明屏障前排队的旧 TaskId 不会反向
覆盖新执行快照。

### 3.2 当前兼容身份边界

三类任务受理入口的首条 Progress 发布会携带真实 `execution_id`；同一业务键的后续遗留
发布在当前单实例 Hub 中沿用该身份并递增内部 sequence，新 TaskId 首次发布时 sequence
重新从 1 开始。TaskId 与 sequence 都只用于内部合并/排序，不进入 WebSocket 消息。

现有 worker 的后续数据库写入和 Progress 发布尚未全部显式携带 TaskId。阶段 1C 起仍
必须按总计划把所有任务写入改为 expected TaskId 条件更新，并由持久化 latest-wins
Guard 阻止旧执行覆盖新执行；本次内存兼容逻辑不能冒充该能力。

---

## 4. 测试与检查

### 4.1 改造前基线

在修改阶段 1B-2 代码前，运行 Progress、任务应用、容器和架构相关集合：**67 项全部
通过**。

### 4.2 阶段 1B-2 定向验证

覆盖请求适配、Presenter、Task Read 兼容适配、线程安全 Progress 适配、连接 Registry、
应用屏障、当前 WebSocket 路由、容器装配和架构边界。最终定向结果为 **85 项全部通过**。

重点场景包括：

- 50 个不同 key 的订阅/发布、同 key 多订阅者、发布/释放竞争和重复释放；该版线程池
  实际仅 20 worker，已由第 6 节的 Barrier 50 线程验收取代；
- 慢订阅者不持有 Hub 锁，异常订阅者不阻断其他订阅者；
- payload 深拷贝、Progress 归一化、TaskId 切换和 sequence；
- action 错误后继续连接、混合 params 整条拒绝、批量顺序和重复位置；
- 两连接同 key 相互隔离，断开一条不影响另一条；
- 实时通知的 `ws.send` 线程恒等于连接路由线程，而不是发布线程；
- 初始发送失败、补偿失败、释放失败令牌保留和自动重试；
- 初始屏障中新旧 TaskId 交错不会漏掉新执行或发送旧排队项。

### 4.3 现有业务回归

analysis、report、weaponry、Task Service、旧 check-task、路由、chat、回调恢复、Presenter、
容器和架构边界定向回归：**216 项全部通过**。故障注入测试会按预期输出异常日志，不是
测试失败。

### 4.4 安全扩大回归

排除可能启动 `run.py` 的 `test_local_scripts.py` 和可能检查/下载真实 Argos 包的
`test_multilingual_translation_integration.py` 后，共运行 **692 项**：**673 项通过，
4 项 failure、15 项 error**。19 项失败与阶段 1B-1 的既有基线类别完全一致：

- 4 个 error：`tests/fixtures/llm/` 历史请求夹具缺失；
- 11 个 error：Callback Debug 测试无参创建生产应用，默认 SQLite 在当前环境只读；
- 1 个 failure：Windows 不提供测试所假定的 POSIX `0640` 权限位语义；
- 2 个 failure：既有 MHTML 扩展名识别/规范化断言；
- 1 个 failure：既有 AnythingLLM Knowledge Gateway 不可变 metadata 冲突断言。

阶段 1B-2 新增/修改测试均通过，没有出现新的失败类别。本记录不把安全扩大回归写成
“全量通过”，也未越权修改与本阶段无关的旧测试问题。

### 4.5 静态检查

- 所有 `app/`、`tests/` Python 源码完成 AST 解析；
- 架构边界和禁止散落 `print()` 检查通过；
- `git diff --check` 通过（仅有工作区 LF/CRLF 提示时不视为源码错误）；
- 接口文档差异仅包含负责人批准的实施状态/时态，以及随后批准的 reportId 入站
  整数/整数字符串规范化；未增删字段，未改变输出类型、状态码和消息结构；
- Application/Port/Presenter 未导入 Flask、具体数据库、Redis、RabbitMQ 或 WebSocket。

---

## 5. 当前边界与后续工作

1. 本次代码处于当前开发分支工作区，尚未提交、部署或进入生产服务器；运行环境仍是
   当前开发环境。
2. Progress Hub/Adapter 是单进程内存实现。多 Worker 或多实例部署会形成独立 latest
   与订阅表，不能据此宣称跨实例一致。
3. Progress 是可合并的当前状态通知，不是可靠事件日志；客户端断线后只能重新订阅读取
   当前快照，没有游标、补发或断线续传。
4. 离线 Barrier 同步的 50 线程订阅/发布测试证明锁、隔离与资源释放契约，不等于 50
   条真实 WebSocket 稳态容量。阶段 0 已批准延期的真实基线仍是阶段 10 硬门禁。
5. `/llm/check-task` 继续使用旧同步恢复和旧成功 JSON；1B-1 的可靠命令/空响应只是内部
   目标边界，待 MySQL/Outbox、RabbitMQ 和 Worker 完成后于阶段 6 一次性切换。
6. 下一实施波次为 1C：报告生成垂直切片。开工前应按现有模板补充 L3 文件级设计，
   重点确定 Submit/Run 边界、活动任务 409 原子语义、TaskId 条件写入、产物/回调 Port
   和兼容调度器回滚方案。

阶段 1B-2 不需要新增接口参数或新的基础设施决策。负责人随后已明确 reportId 入站
兼容口径，本轮按该授权同步了接口文档；字段集合和响应类型保持不变。

---

## 6. 全面审查后的修复与验收

### 6.1 同任务乱序投递水位

原连接缓冲只会与“仍在 queue/pending 中”的同 key 快照比较。一旦较新通知已被连接
线程 drain，Hub 中较早发布但较晚完成的锁外 subscriber 回调就可能把旧 sequence
重新放入空队列，导致客户端进度从 0.8 倒退到 0.2。

修复后，每个 `ProgressDeliveryBuffer` 保存连接级“已接受快照水位”：

- 普通通知在进入队列前，先与相同 key、相同 TaskId 的已接受 sequence 比较；
- 水位在入队时更新，队列 drain、合并或容量淘汰后仍保留；
- 客户端完成初始快照发送时，以该权威快照建立水位；
- 重复订阅其他 key 时，屏障临时取出的既有队列项不会因与自身水位比较而误删；
- 连接关闭时清空队列、屏障暂存项和水位，不产生跨连接状态。

该水位只解决同一 TaskId 内的重复/乱序问题。不同 TaskId 的旧执行过期判断仍按计划
由阶段 1C 持久化 latest-wins Guard 完成，本轮没有提前猜测执行先后关系。

### 6.2 reportId 统一入站规范化

新增框架无关 `app/adapters/web/report_ids.py`，供当前 Flask 和未来 FastAPI 复用：

- 接受 JSON 整数，或去除首尾空白后由可选正负号和十进制数字组成的字符串；
- 不设置 32 位、64 位或正负号业务范围，测试覆盖超过 64 位的 80 位整数；
- `132`、`"132"`、`"+132"`、`"00132"` 统一为业务键 `"132"`；
- 显式拒绝 bool、float、数组、对象及非整数字符串；HTTP 返回既有单字段 400
  错误体，Progress 返回 error 并保持连接；
- 报告生成在写任务、发布首条进度和交给 worker 前把请求内部快照统一为整数；
  check-task 和 Progress 使用同一规范业务键；公开输出仍为 JSON number。

### 6.3 50 线程并发测试纠偏

原 Progress 并发测试虽创建 50 个 key，但线程池只有 20 个 worker，且没有起跑屏障，
不能证明 50 个调用同时在途。现改为订阅、发布分别使用 50 个 worker 和 50 方
`threading.Barrier`，全部线程到齐后才进入 Hub/Adapter；另保留 50 线程并发释放和
重复释放，确保资源最终归零。这仍是单实例离线线程安全验收，不冒充真实长连接压测。

### 6.4 验收结果

所有命令均使用 `venv\\Scripts\\python.exe -B`，未启动 `run.py`，未连接
AnythingLLM、模型、回调、MySQL、Redis 或 RabbitMQ：

| 验收范围 | 结果 |
| --- | --- |
| 首轮修复定向：reportId 三入口、Progress 路由/连接、应用水位、并发 Adapter | 87 项全部通过 |
| 黄金契约、请求适配、水位和并发快速复验 | 43 项全部通过 |
| 阶段 1B-1/1B-2、旧路由、容器与架构合并回归 | 159 项全部通过 |
| 安全扩大回归（排除本地主进程脚本与真实 Argos 集成） | 704 项：685 通过、4 failure、15 error |
| Python AST 解析 | `app/`、`tests/` 共 190 个 Python 文件全部通过（按 `utf-8-sig` 兼容既有 BOM 文件） |
| 差异与日志检查 | `git diff --check` 通过；本轮相关代码无散落 `print()` |

扩大回归的 19 项未通过与修复前记录完全同类，没有新增失败类别：4 个历史请求夹具
缺失、11 个 Callback Debug 无参生产应用导致当前只读 SQLite 错误、1 个 Windows
POSIX 权限位差异、2 个既有 MHTML 断言、1 个既有 AnythingLLM Knowledge metadata
冲突断言。故障注入用例输出的预期异常日志不属于测试失败。
