# 阶段 2-2 根 Schema 与组件 Manifest 设计

> 决策日期：2026-08-12  
> 决策状态：已由用户确认  
> 性质：阶段 2 内部数据库身份和演进契约，不是公开接口文档

## 1. 决策背景

阶段 2-2 负责建立 Task/Callback Control 的新空 SQLite；阶段 2-4～2-7 还会在同一数据库中逐步
安装 Report、Weaponry、Analysis 等业务控制组件。如果一个数据库级 fingerprint 同时代表“当前
所有对象”，后续合法加表会让旧 fingerprint 失效；如果 fingerprint 只检查一部分表，未登记对象又
可能绕过身份门禁。当前阶段也不能预建尚未冻结列、约束和索引的业务表。

因此采用“不可变根 Schema + 版本化组件 Manifest”模型：

- 根 Schema 归 Tasks 模块所有，包含 Task/Attempt/Step/Recovery/Event、latest 投影、Callback
  Control、数据库 metadata 和组件注册机制；
- 根 fingerprint 只覆盖根 Manifest，后续业务组件禁止给根表补列、改约束或替换索引；
- 每个业务组件以独立名称、版本和 fingerprint 登记，拥有独立且不重叠的 SQLite 对象集合；
- 启动时实际结构必须精确等于“根 Manifest + 注册表中全部组件 Manifest”的并集；
- 当前代码不认识的组件、未登记对象、漏失对象、版本/fingerprint 不匹配全部失败关闭。

该模型只解决阶段 2 单实例 SQLite 的可验证演进，不宣称提供多实例在线迁移、滚动升级或数据库级
分布式锁。阶段 3A 切换 MySQL 时仍需重新设计数据库迁移和 N/N-1 兼容策略。

## 2. 身份层次

### 2.1 数据库根身份

根身份由以下事实共同构成，任何一项不符都不得构造 Store 或启动后台线程：

1. 新旧数据库解析后的绝对路径不同；
2. `PRAGMA application_id=1146307378`（`0x44534332`，ASCII `DSC2`）；
3. `PRAGMA user_version=2`；
4. `task_control_schema_metadata` 只有 `metadata_id=1` 一行；
5. `schema_name=docsense.task-control`、`schema_generation=2`、兼容范围 `2..2`；
6. `root_manifest_version=1`；
7. `schema_fingerprint` 等于当前代码内根 Manifest 的 canonical fingerprint；
8. `db_instance_uuid` 是非空 UUID，`created_at` 是 UTC RFC3339 微秒 `Z` 时间；
9. 实际根对象结构与根 Manifest 精确一致。

`schema_generation` 表示破坏根协议的数据库世代；`root_manifest_version` 表示该世代内根对象 Manifest
的精确版本。阶段 2 禁止原地改变二者。未来如果必须修改根表，应建立经独立评审的新 generation，
不能把变更伪装成业务组件安装。

### 2.2 组件身份

`task_control_schema_components` 每个组件恰有一行：

| 字段 | 约束与含义 |
| --- | --- |
| `component_name` | 稳定小写标识，主键；阶段 2 计划值为 `report_control`、`weaponry_control`、`analysis_control`。 |
| `component_version` | 正整数；只由该组件的显式迁移递增。 |
| `root_schema_generation` | 必须等于 2，禁止把组件挂载到未知根世代。 |
| `schema_fingerprint` | 该版本组件 Manifest 的大写 SHA-256。 |
| `manifest_profile` | 固定 `canonical_json_v1`。 |
| `installed_at` | UTC RFC3339 微秒 `Z` 时间。 |

注册表不保存任意 DDL，不从数据库动态下载 Manifest。当前发布版本必须在代码中声明：

- `known_components`：能够严格验证的组件名、版本和 Manifest；
- `required_components`：该运行拓扑启动前必须已经存在的组件名和版本。

阶段 2-2 两个集合均为空；后续业务波次在自身 Schema 完整冻结后增加对应声明。存在未知组件时，
即使当前进程不使用其表也必须拒绝启动，避免旧二进制错误解释新数据库。

## 3. Canonical Manifest 与 fingerprint

### 3.1 编码规则

根和组件使用同一 `canonical_json_v1`：

- UTF-8，无 BOM；对象键按 Unicode code point 升序；
- 分隔符固定为 `,` 和 `:`，不含无语义空白；
- 数组顺序是契约的一部分；禁止集合和依赖字典插入顺序的生成方式；
- 只允许 JSON object/array/string/integer/boolean/null；禁止 float、NaN、Infinity；
- SQLite 默认值、CHECK、partial index predicate 以稳定规范字符串记录；
- Manifest 不包含安装时间、数据库路径、UUID、环境值或 SQLite 运行版本。

根 fingerprint 材料：

```text
docsense.task-control\n2\n<canonical_root_manifest_json>
```

组件 fingerprint 材料：

```text
<component_name>\n<component_version>\n<canonical_component_manifest_json>
```

结果统一为大写 SHA-256 十六进制。`sqlite_master.sql` 不是 fingerprint 材料，避免 SQLite 版本或格式
差异改变身份。

### 3.2 Manifest 表达的语义

每个 Manifest 必须完整列出其拥有的对象，不允许只列“关键字段”：

- table：列顺序、名称、SQLite 类型、`NOT NULL`、规范默认值、主键序号和 hidden/generated 标记；
- table constraint：稳定约束 ID、UNIQUE 列顺序、CHECK 的规范表达式；
- foreign key：来源列、目标表/列、顺序、`ON UPDATE/DELETE` 和 deferrable 策略；
- index：名称、唯一性、列/表达式顺序、collation、ASC/DESC 和规范 partial predicate；当前根
  Manifest 的简单列索引统一显式继承 `indexTermDefaults={collation:BINARY,order:ASC}`，后续若有
  偏离必须在具体索引项中覆盖，不能依赖 SQLite 或运行平台的隐含默认值；
- 允许的对象类型仅为 table/index；根和阶段 2 组件禁止 trigger/view/virtual table；
- 每个对象只有一个 owner；组件不得声明根对象、其他组件对象或 `sqlite_*` 内部对象。

### 3.3 实际 SQLite 核验

fingerprint 证明“代码预期的 Manifest 身份”，不能代替实际结构检查。Bootstrap 必须另行：

1. 用 `sqlite_schema` 只枚举对象名称和类型，拒绝 Manifest 并集中不存在的业务对象；
2. 通过 `PRAGMA table_list/table_xinfo` 核对表与列；
3. 通过 `PRAGMA foreign_key_list` 核对外键；
4. 通过 `PRAGMA index_list/index_xinfo` 核对索引、列顺序、唯一性和 collation；
5. 仅为核对 PRAGMA 无法表达的 CHECK、partial predicate 与外键 deferrability，使用受控 tokenizer
   读取 `sqlite_schema.sql` 并与 Manifest 规范表达式比较；该 SQL 永不进入 fingerprint；
6. 自动索引按 Manifest 中 UNIQUE/PRIMARY KEY 语义验证，不依赖 `sqlite_autoindex_*` 的具体名字；
7. `sqlite_sequence` 不在允许集合，阶段 2 根和组件禁止 `AUTOINCREMENT`；
8. 执行 `PRAGMA integrity_check`、`PRAGMA foreign_key_check`，并确认 `foreign_keys=ON`。

不得以 `CREATE TABLE/INDEX IF NOT EXISTS`、自动补列或忽略未知索引修复不匹配数据库。

## 4. 根对象所有权

阶段 2-2 根 Manifest 固定拥有：

- 身份：`task_control_schema_metadata`、`task_control_schema_components`；
- Task/latest：`llm_task_executions`、`llm_tasks`；
- 执行权：`task_attempts`；
- Step：`task_steps`、`task_step_attempts`；
- Recovery：`task_recovery_cases`、`task_recovery_operations`、`task_recovery_observations`、
  `task_recovery_decisions`；
- 事件：`task_events`；
- Callback：`callback_delivery_guards`、`callback_guard_release_audits`、
  `callback_delivery_attempt_events`；
- 上述表所需的全部显式索引。

Callback HTTP、Progress 发布、模型调用、文件处理和资源清理不属于根 Schema；表进入根只表示它们的
控制事实需要与 Admission/terminal 共享事务，不表示 Tasks 模块拥有垂直业务发送语义。

根表禁止被业务组件 `ALTER TABLE`。业务表通过 `task_id/attempt_no/step_key/fencing_token` 等列引用根
事实，需要强外键时由组件 Manifest 明确声明；无法安全建立强外键的引用也必须由组件 Store 以
Authority CAS 验证，不能降级为仅凭字符串相等。

## 5. 组件命名与所有权

阶段 2 预留但尚未安装的组件：

| 组件 | 计划波次 | 对象命名空间 | 允许职责 |
| --- | --- | --- | --- |
| `report_control` | 2-4 | `report_*` | Report 资源、审计、产物引用及与 Task Step 的事务事实。 |
| `weaponry_control` | 2-5 | `weaponry_*` | Weaponry 资源、创建意图、交互审计和 operator audit；Terms 独立生命周期不迁入。 |
| `analysis_control` | 2-6 | `analysis_*` | Analysis 批次/资源/召回/交互审计控制事实；Knowledge 权威表不迁入。 |

对象前缀只是第一层门禁，不能替代完整对象白名单。组件之间禁止重名、禁止共同拥有一张表，也禁止
通过 view/trigger 跨组件隐藏写入。新增组件或改变上述归属必须先修改本文和机器契约并重新评审。

## 6. Bootstrap 与打开算法

### 6.1 新路径不存在

1. 解析并比较旧/新绝对路径，取得进程级启动锁；锁不等同于分布式 lease；
2. 在同一锁内重新执行旧库只读预检；必须得到 `safe_for_empty_v2_initialization`；
3. 新主文件、`-wal/-shm/-journal` 任一已存在均拒绝“新建”分支；
4. 在目标目录的同文件系统临时路径创建数据库，设置 application/user version 和严格 PRAGMA；
5. 在一个显式事务内建立全部根对象，最后写 metadata；组件注册表保持空；
6. 提交后关闭连接，确认没有遗留 journal/WAL/SHM，执行完整根身份和结构验证；
7. 以不覆盖目标的原子重命名发布主文件；若平台无法保证不覆盖，则先在锁内再次确认目标文件集为空，
   任一竞态都失败并保留诊断，不自动删除未知目标；
8. 重新按“已存在”路径打开并验证，成功后才允许构造 Factory/UoW/Store。

初始化失败只能清理本次进程创建且仍能证明归属的临时文件集；不得删除目标路径或旧库文件集。

### 6.2 新路径已存在

1. `-journal` 存在时拒绝；WAL/SHM 必须作为同一文件集打开和验证，不得单独删除；
2. 使用 `mode=rw` 打开但在身份验证完成前保持 `query_only=ON`，不执行 DDL/DML；
3. 校验 PRAGMA、metadata、根 fingerprint、根实际结构；
4. 读取全部组件注册行；拒绝未知名称、未知/更高/更低版本、重复对象和 fingerprint 不匹配；
5. 校验所有已登记组件的实际结构；校验本发布要求的组件均已安装；
6. 校验实际业务对象精确等于根和组件 Manifest 并集，再执行完整性/外键检查；
7. 全部通过后关闭验证连接，由 Connection Factory 创建业务连接。

验证失败不修复、不降级为旧库、不自动新建第二个文件，也不启动任何线程。

## 7. 组件安装和升级

组件只能通过显式版本化 Installer 安装，普通打开流程不得自动安装：

1. 停止新受理/claim，确认所有相关后台执行器未启动或已停止，取得同一启动/Schema 锁；
2. 严格验证根和全部现有组件；
3. 确认目标组件未安装，或升级路径明确从当前版本指向下一版本；禁止跳版、降级和覆盖注册行；
4. `BEGIN EXCLUSIVE`，按 Manifest 执行无 `IF NOT EXISTS` DDL；禁止网络、文件转换和业务回调；
5. 校验新组件对象，并复核全库对象并集、外键和完整性；
6. 最后写入/更新组件注册行，提交；任一步失败整体 rollback；
7. 提交后重新只读验证，成功后才恢复服务。

阶段 2-4～2-6 首次安装组件时必须先冻结完整组件 Manifest。组件升级不能修改根表；若确实需要根表
变化，必须停止当前阶段并设计新根 generation。安装组件后，尚不认识该组件的旧二进制将失败关闭，
所以代码回切与数据库回切必须作为受控发布动作，不能假设旧版本仍可打开新文件。

## 8. 并发、事务和错误分类

- 启动锁只防止同机进程同时 Bootstrap/安装；真正 Task 写入仍依赖 SQLite 事务和 Authority CAS；
- Schema 操作禁止自动重试，避免未知 DDL 是否提交；busy/locked 映射为明确基础设施错误；
- 初始化、组件安装、普通 UoW 使用不同能力对象；业务 Store 永远拿不到 Schema DDL 能力；
- Connection 不跨线程共享，`foreign_keys=ON`、busy timeout、事务所有者和创建线程必须验证；
- 至少区分：路径冲突、旧库预检阻塞、目标文件集冲突、数据库身份错误、根 generation 错误、
  root/component fingerprint 错误、未知组件、组件缺失、实际对象漂移、integrity/foreign key 错误、
  busy/locked 和初始化发布竞态；日志仅记录错误分类及路径/UUID/fingerprint 的短摘要。

## 9. 测试与关闭门禁

阶段 2-2 至少验证：

1. canonical JSON 与 SHA-256 测试向量可复现；同语义不同键顺序得到同 fingerprint；
2. 新旧同路径、旧库阻塞、目标残留主文件/侧车、未知 application/user version 全部拒绝；
3. 新空库只有根对象、metadata 单行、组件注册表为空；
4. metadata/fingerprint/UUID/时间损坏、根表/列/约束/索引增删改均拒绝且不修复；
5. 未登记表/索引、trigger/view、`sqlite_sequence` 均拒绝；
6. 未知组件、错误版本/fingerprint、对象越权/重叠、缺少 required component 均拒绝；
7. 组件安装全成功或全回滚，安装后旧 2-2 profile 失败关闭；
8. `integrity_check`、`foreign_key_check`、WAL/SHM 文件集和并发 Bootstrap 争用；
9. Schema 验证完成前不能构造 Store、UoW 或启动线程；
10. 接口文档、`run.py` 和生产 Container 在相应切换波次前保持零差异。

## 10. Control Store 编码前内部契约补全

根 Schema 的 `batch_id/batch_sequence` 和拆分 owner 列要求 Port 在持久化前就提供无歧义输入；
Recovery 五类结论也不能全部压缩成“创建 Case”。经用户确认，阶段 2-2 第 3 步编码前冻结：

1. `TaskBatchRef(batch_id, sequence)` 是 file/Analysis Admission 的强制内部身份；非 file 禁止携带。
   `admit_many` 对 Analysis 要求同一 batch ID、序号从 1 开始连续并与请求顺序完全一致，任一不符
   整批不落盘。Store 不读取 payload 猜测批次，也不补号或排序。
2. `TaskOwnerIdentity(instance_start_id, process_id, executor_name, worker_slot)` 是 claim 的结构化诊断
   身份；`instance_start_id` 必须是规范小写 UUID，组合 `owner_id` 只读派生。Attempt 同时保存组合
   文本与四个拆分字段，但 Authority 仍只由 attempt、lease token、fencing 和租约共同建立。
3. Recovery 只保留 `classify_candidate_if_current(TaskRecoveryClassificationCommand)`：命令携带完整
   Candidate/source CAS、分类、Policy 版本和分类时间；`reconcile_required`、
   `finalize_from_checkpoint` 必须携带 Case ID，`retry_safe`、`defer` 必须携带晚于分类时间的
   `next_action_at`，`mark_stale` 不携带二者。仅两个隔离类成功结果可返回新 Case。

这些是内部契约，不修改任何公开接口。精确机器定义位于
`tests/contracts/stage2_task_execution_contract.json` schema version 5；对应 Domain/Port/Fake 测试必须
先通过，才允许创建 SQLite Control Store。

## 11. 实施映射

- `app/modules/tasks/adapters/sqlite/schema.py`：根 Manifest、canonical fingerprint、实际结构核验和
  明确 DDL；不加载 `tests/contracts`；
- `app/modules/tasks/adapters/sqlite/bootstrap.py`：旧库预检编排、文件集保护、临时初始化、严格打开和
  显式组件安装；
- `app/modules/tasks/adapters/sqlite/connection.py`：身份验证后的业务 Connection Factory；
- `app/modules/tasks/adapters/sqlite/transaction.py`：显式短事务、busy 分类、线程/嵌套门禁；
- `app/modules/tasks/adapters/sqlite/unit_of_work.py`：三类窄 UoW 与同连接 Store 装配；
- `app/modules/tasks/adapters/sqlite/database_contract.json`：生产 Bootstrap 与本文共用的机器可读治理
  契约和测试向量；
- `tests/test_task_control_schema_contract.py`：文档/契约结构门禁；
- `tests/test_task_control_schema_bootstrap.py`：临时 SQLite 的实现验收。

生产代码必须内置 Manifest；测试负责证明内置 Manifest 与机器契约声明的对象所有权、算法版本和
fingerprint profile 一致，禁止运行时从测试目录读取身份定义。

## 12. 第 3 步编码前第二轮商讨结论（已确认）

完成第 10 节三项已确认补全后，继续把全部执行/恢复命令与状态机、根列和事务矩阵逐项对照，仍有
以下无法由 Store 安全猜测的内部契约缺口。它们均不涉及公开接口，但未确认前禁止开始 Control Store：

1. D2-07 规定证据充分的 `retry_safe` 把 `running` Task 原子收敛回 `accepted`，而当前冻结状态机
   没有这条转换，并把普通 `running -> accepted` 判为非法。建议新增只允许 Recovery 分类 CAS 调用的
   `RETRY_SAFE` 领域转换；它必须同时把旧 Attempt 收敛为 `expired/abandoned`、设置有限退避并追加
   Event，普通 Worker/SQL 仍不得调用，因而不放宽“通用 reset 禁止”。
2. `TaskStepCompletionCommand` 只有 checkpoint/error，当前 Fake 被迫以“有 checkpoint 即成功、否则
   失败”猜状态，无法持久化合法的 `outcome_unknown`，也无法明确区分 Step 转换。建议命令显式携带
   `TaskStepTransition`，执行 Authority 只允许 `SUCCEED/FAIL/MARK_OUTCOME_UNKNOWN`；`SKIP` 与
   `COMPENSATE` 分别由显式跳过命令和 Recovery Authority 路径处理，禁止用空 checkpoint 暗示。
3. Recovery 协议还缺三项收敛输入：Case 长探测没有 heartbeat/续租命令；`keep_quarantined` 无法写入
   `next_observation_at`；`finalize_from_checkpoint` 只有 Task 终态，缺少经 CAS 核对的来源 Step/
   Checkpoint 以及既有公开 latest 投影所需的 `public_status/message/result_ref`。建议新增 Recovery
   heartbeat；Decision 增加可选下一观察时间；并为 finalize 使用冻结的内部 Terminal Projection
   值对象，Store 核验来源 checkpoint 后在同一事务更新 Task/latest/Event，不从业务类型猜公开值。

上述建议已由用户在 2026-08-12 确认。实现仍必须按状态机、DTO/Port、Fake、机器契约、文档和聚合
验收的顺序完成；确认不等于已经实现。

## 13. 契约 v3 根 Schema 影响与第三轮商讨结论（已确认并落实）

把第 12 节方案继续映射到原子事务和当前根列时，发现两个不能由 Store 隐式处理的直接后果：

1. 执行 Authority 把 Step 标为 `outcome_unknown` 时，如果只结束 Step Attempt，Task 仍为 `running`、
   原 Task Attempt 仍为 `running`，旧 Worker 或后续业务代码仍可能继续写。建议
   `TaskStepCompletionCommand(MARK_OUTCOME_UNKNOWN)` 强制携带冻结的 Recovery Isolation 输入
   （Case ID、reason、policy version）；同一事务将 Step/Step Attempt 写为 unknown、把当前 Attempt
   以新增的显式 `ISOLATE_FOR_RECOVERY` 转换收敛为 `abandoned`、把 Task 置为
   `recovery_required`、递增 generation、创建 Case 并追加 Event。普通成功/失败完成不得携带该输入。
2. 已确认的 `keep_quarantined.next_observation_at` 和 Terminal Projection 当前无法写入
   `task_recovery_decisions`。建议在仍未接生产、只由临时库测试的根 Manifest 中增加：
   `next_observation_at TEXT NULL` 与 `terminal_projection_payload TEXT NULL`（canonical JSON），并增加
   时间、JSON 和 decision-kind/存在性 CHECK。Terminal Projection JSON 只含来源 Step/Attempt、
   checkpoint code/digest、既有公开投影值和内部 result ref，不含正文或秘密。该改动会重新计算根
   canonical bytes/fingerprint，并同步 `database_contract.json` 与全部 Bootstrap/漂移测试；不修改
   schema generation、root manifest version 或任何公开接口。

这两项已由用户在 2026-08-12 再次确认，并已落实到纯 Domain/Port/Fake、机器契约与根 Manifest：

- 根 `schema_generation=2`、`root_manifest_version=1` 保持不变；因为该新空库尚未接入生产，本次在
  第 3 步 Store 编码前完成最终冻结，不设计原地迁移；
- 根 canonical Manifest UTF-8 字节数为 `38349`，fingerprint 为
  `498DF94CD81C41701E8136DB2C7BE984B79E3E066C8CD4EB074077A516BBFDCA`；
- `task_recovery_decisions` 已增加 `next_observation_at`、`terminal_projection_payload` 及严格
  decision-kind、时间、JSON、4096 字节上限 CHECK；
- `MARK_OUTCOME_UNKNOWN`、专属 `RETRY_SAFE`、Recovery heartbeat、显式 Step skip 和 Terminal
  Projection 均有正反测试；仍未开始 Control Store 或生产装配。

## 14. Store 编码前 Recovery Intent 与 Step 重试收敛结论（已确认并落实）

契约 v3 完成 136 项聚合验收后，按步骤要求再次逐项核对 D2-07A 的“事务内 Intent、事务外 I/O、
事务内 Observation/Decision”流程，发现当前根表和 Port 仍缺两组不可省略的恢复事实：

1. `append_observation` 只能在探测/补偿完成后写结果，无法在 I/O 前持久化 operation intent。进程若在
   事务外补偿期间崩溃，新 Recovery owner 无法判断动作是未发送、已发送还是结果丢失，尤其不能安全
   防止补偿重复。建议新增根表 `task_recovery_operations`，保存 operation ID、Case/generation/fencing、
   `probe|compensation`、step、稳定幂等键、intent digest/外部引用、状态、intent/result 时间；
   Observation 必须引用 operation ID。Port 增加 `begin_operation`，严格遵循“先提交 Intent，再做 I/O，
   最后追加 Observation 并收敛 Operation”的三段式协议。
2. `retry_authorized` 当前只有 `retry_from_step_key`，但 `task_steps` 可能仍停在
   `outcome_unknown`；下一普通 Attempt 无法合法 `BEGIN`，也不能证明它重试的是 Policy 已授权的那个
   Step Attempt。建议增加冻结 `TaskRecoveryStepResolution`：携带 source step/step-attempt/row-version、
   授权依据 operation/observation/evidence digest 和目标动作。`retry_authorized` 必须在同一 Decision
   事务把已确认“未生效或补偿完成”的当前 Step 投影显式转回 `pending`，保留旧 Step Attempt 的
   immutable unknown 历史，然后由普通 claim/Step intent 创建新 Attempt；新增的
   `TaskStepTransition.RETRY_AUTHORIZED` 只允许 Recovery Decision 调用。

同时建议冻结 Case 接管规则：`open/awaiting_evidence` 可领取；`observing` 只有在数据库中的 recovery
lease 已不晚于 `claimed_at` 时才可直接 CAS 接管并递增 recovery fencing；未过期 owner 不被抢占。

这些改动仍是内部根控制事实，不改变公开接口。用户于 2026-08-12 确认后，已经完成：

- 新增 `TaskRecoveryOperation`、`begin_operation`、Operation/Observation 原子收敛，以及 Case 内稳定
  幂等键；Operation 原 fencing 的 Intent 可由接管后的当前 Recovery Authority 对账收敛；
- 新增 `TaskRecoveryStepResolution` 和 Recovery 专属 `TaskStepTransition.RETRY_AUTHORIZED`；关闭
  Decision 同时以 source Step Attempt/row version 和 operation/observation/digest 做 CAS，只重置当前
  Step 投影，旧 `outcome_unknown` Step Attempt 保持不可变；
- `open/awaiting_evidence` 可直接领取，`observing` 只允许在数据库租约不晚于 `claimed_at` 时接管；
- 根 Manifest 现有 15 张表、24 个显式索引；canonical UTF-8 字节数为 `41938`，fingerprint 为
  `BDDA34F9925AD69E8F8186F1ED7BED032372117E2E90D56E7C6C728E112D0499`；
- schema generation 2、root manifest version 1 保持不变；当前仍是未接生产的空库契约，不设计原地
  迁移，也不把 SQLite/Fake 结果表述成多实例或生产证明。

## 15. Store 编码时发现的 Step Intent 幂等冲突（已确认并落实）

开始实现 `begin_step` 的 SQLite 条件写时，发现 V4 重试结论与现有根索引、Port 返回值之间存在一组
不能由 Store 猜测的冲突：

1. `task_steps.idempotency_key` 是稳定 Step 外部效果键；V4 `retry_authorized` 只把当前 Step 投影从
   `outcome_unknown` 转回 `pending`，不修改该键，并要求普通执行权创建新的 Step Attempt。
2. 当前 `idx_task_step_attempts_idempotency` 却对 `(task_id, step_key, idempotency_key)` 建立唯一索引。
   因而新 Step Attempt 如果保留同一稳定键，会在 INSERT 时违反唯一约束；若临场换键，又会削弱
   外部供应商去重和恢复证据连续性。
3. `TaskExecutionMutationOutcome` 也没有“同一 Step Intent 已提交”的有限结果。一次 Intent 已提交但
   调用方未取得明确返回时，再次调用 `begin_step` 只能得到含义模糊的 `INVALID_STATE`，无法区分
   同一命令重放、其他 Attempt 已推进或真正非法状态。

用户于 2026-08-12 确认并已落实以下修订：

- 把 `idx_task_step_attempts_idempotency` 改为非唯一审计索引；Step Attempt 的不可变身份继续由主键
  `(task_id, step_key, step_attempt_no)` 保证，稳定幂等键允许跨恢复后的 Attempt 保持不变；
- 增加有限结果 `DUPLICATE_STEP_INTENT`。`begin_step` 只有在当前 running Step Attempt 的
  task attempt/fencing、幂等键、intent 时间和冻结 Step 定义均与命令完全一致时才返回该结果；任一
  不同仍返回 `AUTHORITY_LOST/INVALID_STATE` 中相应结果，不能把冲突伪装成幂等成功；
- 为执行 Port 增加内部只读 `get_step(task_id, step_key)` 与
  `get_step_attempt(task_id, step_key, step_attempt_no)`，供 Application 在重复 Intent、恢复和诊断时读取
  已提交事实；这些方法不授予写权限，也不新增公开接口字段；
- 严格 Fake、机器契约和根 Manifest/fingerprint 已同步；根 canonical UTF-8 字节数为 `41939`，
  fingerprint 为 `8947401B0E04AB68A3073A1D663386FAD9D786DBDCA2FF71BDE0849AC0B5479F`；
- 后续 Control Store 与两连接 CAS 测试必须沿用该语义，不得通过每次重试生成新的外部幂等键规避
  问题。

## 16. Control Store 实施结果与兼容入口停止点（已确认并落实）

V5 契约确认后，阶段 2-2 第 3～4 步已经完成以下内部实现：

- 新增 `SQLiteTaskControlStore`，直接实现 Admission、Execution、Recovery、Event、Callback
  Admission Conflict 和 latest 投影 SQL；Store 不创建连接、不 commit/rollback、不隐藏重试；
- 新增标准 UoW 装配，Admission/Execution/Recovery 每次使用独立短连接，同一原子组共用同一活动
  Connection；未显式 commit 正常退出也回滚；
- 实现 claim/start/heartbeat、精确 Step Intent 幂等、Step success/fail/unknown、Progress、终态、
  过期扫描与五类分类条件写；unknown 在同一事务撤销旧 Attempt、建立 Recovery Case 并隔离 Task；
- 实现 Recovery Case claim/heartbeat/过期接管、Operation Intent、Observation/Operation 原子收敛、
  Decision、Step Resolution 与 Terminal Projection；旧 recovery fencing 不能写入，接管者只能沿
  已提交的稳定 Operation 身份对账；
- 离线临时 SQLite 的 Store 专项 6 项、既有分层回归 106 项、架构/旧库预检/Store 聚合 43 项均通过；
  后一组含 6 项 Store 测试，因此本轮实际执行 149 个互异测试，零失败、零错误、零跳过；另外
  `compileall`、`git diff --check`、`integrity_check` 与 `foreign_key_check` 通过；
- 未运行 `run.py`、真实供应商、浏览器或后台服务；上述证据不外推为多实例、可靠队列、生产容量或
  exactly-once 证明。

完成第 4 步后审计第 5 步“所有 Task/Callback Control 兼容入口一次切到新 v2 Store”，发现当前计划
顺序存在不能由 Adapter 安全猜测的冲突：

1. 现行 `TaskCommandPort.claim(task_id)`、`update_progress_if_current(expected TaskId/latest)` 和
   `finish_if_current(expected TaskId/latest)` 不传递 Task Attempt、lease token、fencing token 或租约；
   V5 `TaskExecutionPort` 则把这组 Authority 作为每次写入的强制条件。兼容层若从数据库临时读取“当前
   Authority”替旧调用者补齐，旧 Worker 也可能取得新 Attempt 的能力并覆盖新 owner；若放入线程局部
   或进程内映射，进程重启、线程切换和多实例下均不可靠。
2. 阶段 2-2 当前只冻结 `CallbackAdmissionConflictPort`，完整 Callback Delivery claim/heartbeat/
   outcome_unknown/terminal 状态迁移 Port 尚未设计；因此“所有 Callback Control 入口”没有可委托的
   新抽象，直接迁移 SQL 会绕过阶段 2-1 的 Domain/Port/Fake 先行门禁。
3. v2 是独立新路径的空库，当前生产 Application/Runner 与 Dispatcher 仍依赖旧 TaskCommand 外观和
   旧数据库；在 Execution Runtime 尚未实施时切受理而保留旧执行，会让 accepted 事实写入无人扫描的
   v2，或要求新旧库双写。两者分别造成任务不执行或违反唯一 Writer/禁止双写规则。

因此当时在第 5 步前失败关闭：没有修改生产 Container、`LegacyTaskCommandAdapter` 构造方式、公开
接口文档或任何前后端参数。建议把“一次切全部兼容入口”修订为按业务切换：阶段 2-2 关闭于已完成的
Repository/UoW/CAS；阶段 2-3 先实现携带完整 Authority 的 Execution Runtime；阶段 2-4～2-6 每个业务
在受理、执行、进度、终态和 Callback Control 可同波切换时，旧入口才单向委托并立即清除对应旧 SQL。
如果仍要求阶段 2-2 完成一次性兼容切换，则必须先扩大本波范围，补充完整 Callback Delivery Port、
跨旧 Runner 的 Authority 传递契约、生产 DB 路径/启动失败策略和 Dispatcher 接线设计，再重新确认。

用户于 2026-08-12 确认采用上述按业务切换方案。计划及实现边界已经同步：

- 阶段 2-2 以新 v2 Repository/UoW/CAS、Schema/Bootstrap 和双连接离线验证关闭，不修改生产
  Container、不切换旧业务写路径；
- 阶段 2-3 实现完整 Authority Runtime，并显式把 claim 产生的能力传递到所有后续写命令；不能从
  数据库读取当前 Authority 代旧 Worker 补权；
- 阶段 2-4～2-6 分别以 Report、Weaponry、Analysis 为不可拆迁移单元，一次切换受理、执行、进度、
  终态和该业务 Callback Control；旧入口只允许单向委托，禁止双写；
- `SQLiteTaskControlStore` 已移除业务终态时对 `callback_delivery_guards` 的预写。阶段 2-2 只通过
  `CallbackAdmissionConflictPort` 读取 Guard；完整 Delivery 写入由后续唯一 Callback Control Store
  承担，并在业务切换波次通过共享 UoW 满足终态原子性。
