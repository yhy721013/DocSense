# 分类节点变更模块

`reassign` 是 `/llm/reassign` 当前同步 Saga 的内部业务模块。它服务于同一文档的执行权隔离、
可审计步骤、条件本地提交、外部结果探测和补偿恢复；它不是后台任务接口，也不会向公开响应
暴露 operation、lease、fencing 或步骤信息。

## 分层职责

| 目录 | 当前职责 | 允许依赖 |
| --- | --- | --- |
| `domain/` | 已完成：不可变命令/快照/状态、状态机、幂等键与补偿决策 | Python 标准库和本模块领域类型 |
| `application/` | 已完成 1E-7：同步 Saga 前向路径、local-only、步骤续租、恢复探测、补偿、过期 lease 接管与显式人工恢复；四个协作器直接拥有恢复算法，Facade 仅做接管与流程选择 | `domain/`、`ports/` |
| `ports/` | 已完成：Repository/UoW、lease/fencing、Operation/Step/Event/恢复观测与 Knowledge 协议 | `domain/`、标准类型 |
| `adapters/` | 已完成 SQLite 事实/恢复观测适配器，以及 1E-3 请求级 AnythingLLM 预算/Knowledge Adapter | 具体基础设施和本模块端口 |

## 当前实施状态（阶段 1E-7）

本阶段已完成四层目录、领域 DTO、错误分类、Operation/Step 状态机、端口、严格 Fake、SQLite
Operation/Step/Event 本地事实及其持久化一致性审查修正、请求级 AnythingLLM Knowledge Adapter，
以及 1E-4/1E-4R 的 `DocumentReassignmentService` 前向成功路径和一致性修正。1E-5/1E-5R 已补齐
`RecoverReassignmentOperation`、补偿检查点、过期 lease 接管、恢复观测和只读诊断脚本。1E-6 已由
`ReassignApplicationServices` 统一装配前向与恢复用例，并切换 Flask 路由为 Parser → Application →
Presenter；仍不启动后台线程或任务队列。1E-7 已将恢复原子流程下沉到
`recovery_observer.py`、`recovery_checkpoints.py`、`recovery_compensator.py` 和
`recovery_finalizer.py`：四者分别直接拥有观察/续租/观察事实、检查点收敛、固定顺序补偿、终态收口/隔离。
`recovery_facade.py` 中的 `RecoverReassignmentOperation` 仅保留命令校验、过期 lease 接管和高层流程选择；
`recover_reassignment.py` 只保留兼容导出路径，不再承载恢复算法。

- `ReassignDocumentCommand` 深冻结新旧 ArchitectureId 原始 JSON 值，同时只保存由 Web
  Adapter 完成的旧 ID 查询值；领域层不拥有首次 `int(...)` 转换或 HTTP 失败语义，只执行
  原始值与查询值的一致性断言，不把 `1` 与 `"1"` 合并。目标 ID 只允许已冻结的 JSON `false`、
  有符号 64 位整数和十进制整数字符串进入 Operation，拒绝浮点数、容器及越界值污染本地事实；
- `ReassignmentOperation` 和 `ReassignmentStep` 只描述内部事实。`recovery_required` 继续占用
  同文档保护，普通请求不能把它改写为成功或重新开始；
- Repository 的通用状态转换只处理非终态；成功必须通过校验远端步骤事实并原子提交本地 CAS 的
  专用入口，失败只能由“无副作用证据已确认”的专用入口收敛，不能绕过事实校验提前释放文档保护；
  前向和补偿外部写统一使用独立 Step，不在前向 Step 上记录第二套补偿状态；
- `decide_compensation()` 只依赖已确认副作用。任何结果未知都进入 `recovery_required`，不会
  盲目重放；本地提交生效但目标绑定缺失或未知时同样保留现场；目标绑定已确认时固定先删除
  目标、再恢复来源；
- lease 到期时间统一规范化为 UTC ISO-8601；外部引用、错误码和错误摘要具有确定长度上限；
- 公开 message 只能从接口文档批准的 `ReassignmentPublicMessage` 枚举选择，不能动态拼接异常；
- 纯领域函数不产生日志或外部副作用。后续 Application/Adapter 必须在写意图、外部调用、探测、
  补偿、lease/fencing 和终态边界记录脱敏结构化日志。
- `SQLiteReassignmentRepository` 每个 UoW 使用独立、短生命周期事务；只读扫描使用 `BEGIN`，
  写事务使用 `BEGIN IMMEDIATE`：原子
  保留文档、冻结快照、创建八个固定 Step、递增 fencing、追加审计、登记 workspace 映射，以及把
  `documents` 条件 CAS、commit Step 和成功终态放在同一事务；它不创建 HTTP Client，也不调用
  Knowledge Port；
- 活动 Operation 以 `documents.id` 的部分唯一索引隔离，而非只按 `fileName`；因此不同分类中同名
  文件互不串扰。初始化会核验既有索引谓词并在事务内有条件重建；lease 续期/过期接管和所有后续
  写都比对 owner、token、到期时间和 fencing；
- 尚无本地 workspace mapping 的目标分类额外使用 `reassign_workspace_preparation_claims` 持久化
  准备权。claim 按目标 SQLite 存储投影唯一、独立递增 fencing，并保留释放行避免 ABA；过期后才可
  被另一 Operation 接管。目标 mapping、prepare Step 成功事实与 claim 释放必须在同一短事务提交；
- 远端 workspace 创建结果未知时 claim 保持活动；远端创建成功但 mapping 冲突或提交异常时，
  Repository 单独保存准确 slug、三态归属和脱敏恢复事件，prepare Step 不会被伪装为完成；
- 既有 mapping 使用独立的按 slug 查回 Port，大小写比较与 Adapter 的 `casefold` 身份规则一致，
  不受远端展示名修改或后续确定性命名规则变化影响；
- `recovery_required` 只能由取得更大 fencing 的接管者离开；同一 fencing 不能借
  `recovery_authorized` 逃逸或重试已知失败步骤。恢复扫描使用只读 UoW、稳定游标与显式上限；
- Step 完成状态与探测结果使用强类型一致性矩阵，重试会清除旧探测结果并记录本次 fencing；
  审计事件保存 fencing、尝试次数、探测结论、脱敏操作者和原因码；
- `tests/fakes/reassign.py` 的严格替身要求测试逐次声明外部调用，拒绝事务内网络调用、错误顺序、
  未声明调用、未经授权的重复副作用，以及结果未知后的盲重放。Fake 同时保留 `"12"`、`false`
  等已冻结的新分类原始值兼容边界。

- AnythingLLMReassignmentClientFactory 每个原子调用新建并关闭 Transport；关闭失败不会覆盖已有业务异常。
  AnythingLLMReassignmentKnowledgeAdapterFactory 每个同步请求新建 deadline，禁止跨 Operation/线程复用
  可变预算或 Transport；
- ReassignmentInfrastructureConfig 严格拒绝空值、布尔值、非有限值、非正值和无法保留前向窗口的预算
  组合。普通前向写及其确定性写后确认使用前向窗口；不确定写查回、显式恢复和补偿 Step 才能使用
  补偿预留，预算用途由固定 `step_name` 自动选择。Container 已使用同一配置装配生产运行链；
  `runtime_mode` 当前只允许 `single_instance`，其他值在启动装配时失败，避免把进程本地时钟误当成
  多实例权威 lease 时间；
- 目标 workspace 仅按确定性名称或 slug 精确匹配；多重身份、缺 slug、协议异常、超时、断连以及
  408/409/425/429 不会盲目重试。创建后查回到唯一资源时保存可继续使用的 slug 与 `unknown`
  创建归属，绝不把它标成当前 Operation 可删除资源；
- 删除/加入一律以完整规范化 doc_path 查回确认，绝不按 basename、展示名或 doc ID 兜底。供应商 false、
  明确 4xx、超时、断连和写后探测矛盾分别保守收敛为已知失败、已处于目标状态或结果未知；
- 生产 Adapter 与严格 Fake 使用相同的步骤—动作白名单，错误 Step 在创建 Transport 前即被拒绝；
  Transport 关闭失败日志只记录异常类型，不输出原始异常正文或 traceback；
- Adapter 不访问 SQLite、不创建 Operation/Step，也不在数据库事务内执行网络 I/O。1E-4 Application
  已先提交写意图，再调用 Adapter，并持久化结果、目标映射和本地 CAS；1E-4R 为非关键 Pin
  增加调用前意图和调用后结果审计，审计意图失败时跳过 Pin。
- `DocumentReassignmentService` 每次执行只返回最小 `ReassignmentResult`。已确认无副作用的失败
  记录为 `failed` 并释放文档保护；远端明确失败或本地 CAS 冲突且本地仍为来源分类时，会在同一
  请求级 Knowledge Port 与有限 deadline 内同步执行“解绑目标、恢复来源”，成功收口为
  `compensated`。明确补偿失败、检查点冲突、预算耗尽、未知远端副作用或无法证明本地前置状态时，
  才保留为 `recovery_required` 并交由显式恢复服务处理。
- `ReassignmentExecutionSettings` 显式接收远端总预算与 lease 安全余量，并拒绝短于二者之和的
  lease；远端预算从 Application 收到命令时起算并扣除前置锁等待。初始保留、续租和恢复接管均在
  取得写事务后计算新到期时间，Repository 同事务延长当前活动 preparation claim。
- 本地 CAS 与恢复终态若在“事务已提交、确认阶段异常”窗口失去返回确认，会重读权威 Operation；
  只有持久终态与预期一致才返回真实结果，否则继续隔离，不会成功后误报或错误触发反向补偿。
- `RecoverReassignmentOperation` 仅接管精确且已过期的 Operation；它先持久化新的 fencing，再在
  UoW 外探测本地分类、workspace 和两侧成员关系。补偿固定先删除目标、后恢复来源；每次写后重新
  探测，检查点丢失时只能按已证实状态补齐。恢复成功同样需要完整的前向 workspace/Step 事实，
  不能用远端一致性掩盖本地事实缺口。
- `reassign_recovery_observations` 只追加脱敏枚举观测。成功、无副作用失败和补偿终态都必须引用当前
  fencing 的最新观测，并与精确 claim 释放在同一事务提交；默认诊断脚本只读且不初始化 Schema。
- 恢复按既有 slug 查回时会再次绑定校验返回引用；接管和续租必须返回同一 Operation 的精确
  lease/claim 身份，Port 契约错误与数据库读取异常统一保留为可重试恢复现场，不会误报不存在。
- 诊断脚本只有在 Operation 已恢复到终态时返回退出码 0；未找到、未接管和仍待恢复使用稳定非零
  退出码，供 Shell、CI 和未来运维编排可靠判定。

## 公开契约边界

接口文档 `docs/接口文档/分类节点变更.md` 是唯一公开契约。1E-6 已替换公开路由的内部编排，但未
增删或改名任何请求/响应参数、状态码、JSON 结构、SSE/WS 字段或 Header。经确认，接口文档已
冻结远端失败、CAS 冲突、并发占用、补偿失败和恢复待处理五类稳定 `data.message`；Presenter 仅
映射这些稳定文案，不透传异常正文。

后续切换必须保持 Parser → Application → Presenter 方向。Presenter 只能从最小
`ReassignmentResult` 和已冻结命令生成既有 200/400/500 结构，绝不能向前端透传内部错误码、
operation ID、lease token、fencing token、步骤名、探测或恢复事实。

## 后续边界

- 真实 AnythingLLM 故障演练、生产预算校准、可靠任务队列和多实例容量验收仍未完成，不能因为离线
  恢复测试、组合根接线或公开路由切换通过而标记 production ready；
- 后续 Dispatcher/Worker 只能持有 `ReassignApplicationServices` 的前向或恢复用例，不能绕过
  Application 直接调用 Repository 的终态收口入口。
- 1E-7 已完成恢复实现下沉和协作器直接单测；永久 AST 门禁同时禁止 callback-wrapper、协作器越过最小
  Port 依赖，以及恢复 Facade 的文件规模/圈复杂度反向增长。后续可靠队列或多实例接入仍须复用该唯一
  Application 用例，不能以新编排绕过 lease/fencing、观察或终态事实门禁。
- 文档删除门禁、领域保护谓词与 Repository 活动索引由一致性测试冻结；新增 Operation 状态时若三者
  未同步，测试必须阻断。数据库权威时间和可取消锁等待仍属于阶段 3 多实例前置工作。
