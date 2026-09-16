# 阶段 1E-2 分类节点变更 Port、严格 Fake 与 SQLite 事实执行记录

## 1. 执行结论

| 项目 | 结论 |
| --- | --- |
| 执行日期 | 2026-07-24 |
| 执行分支 | `refactor/concurrency` |
| 对应设计 | `../重构记录/260724-阶段1E分类节点变更同步Saga文件级实施设计.md` 的 1E-2 |
| 执行范围 | Repository/UoW/Knowledge Port、严格 Fake、SQLite Operation/Step/Event 事实、短事务、活动保护、lease/fencing、条件 CAS、追加审计和离线门禁 |
| 生产代码切换 | **无**；`app/blueprints/llm.py::llm_reassign()` 仍为当前公开实现，未导入、构造或调用新的组合对象 |
| 接口影响 | 未增删、改名或重新解释任何请求/响应参数；未改变 HTTP 状态码、JSON、同步语义、Callback、Progress、SSE、WebSocket 或 Header；未修改 `docs/接口文档/` |
| 数据迁移 | 仅新增可幂等初始化的内部 SQLite 表、索引和触发器；未修改既有 `documents`/`workspaces` 表结构，未对开发库或生产库执行实际初始化 |
| 运行边界 | 未启动 `run.py`，未连接真实 AnythingLLM、模型、甲方 Callback 或其他后台服务 |
| 阶段状态 | **1E-2 已完成**；1E-3～1E-6 的 Application Saga、真实外部适配、补偿恢复、Container 和公开路由切换仍未实施 |

本波次只建立后续同步 Saga 所需的可恢复本地事实边界。SQLite Adapter 能够持久化执行权、步骤、
lease/fencing、workspace 映射、本地条件提交和审计，但它不具备业务编排能力，也不代表当前
`/llm/reassign` 已有并发保护、真实补偿或生产可用的跨实例恢复。

---

## 2. 已完成的改造

### 2.1 供应商无关 Port 与内部 DTO

新增 `app/modules/reassign/ports/repository.py` 和 `knowledge.py`：

- `ReassignmentRepositoryPort` 只负责创建短生命周期 `ReassignmentUnitOfWork`；UoW 提供文档冻结、
  Operation/Step/Event 查询、原子保留、lease 续期/过期接管、步骤检查点、状态转换、workspace 映射
  和本地 CAS；
- 所有写请求都携带不可变 `ReassignmentLease`。Repository 必须同时校验 owner、token、到期时间和
  fencing token，失权时只能返回确定的内部写结果；
- `ReassignmentKnowledgePort` 表达目标 workspace 准备、精确成员探测、文档加入/删除和 Pin。每个结果
  明确分类为 `applied`、`already_in_desired_state`、`known_failure` 或 `outcome_unknown`，不存在以
  `None`、空字典或异常正文伪装成功的路径；
- DTO 只携带领域值对象、有界诊断和不透明外部引用，不暴露 SQLite connection、SQL、HTTP Response、
  AnythingLLM DTO、真实路径或公开响应字段；
- `ReassignmentPortBundle` 只验证未来 Application 依赖为同一组端口实例；它不读取环境变量、不创建
  HTTP Client、不接入 Flask 容器。

上述类型仍是内部实现细节。Presenter 和公开 JSON 禁止直接使用 Operation、lease、fencing、Step、
审计事件或内部错误码。

### 2.2 SQLite Operation/Step/Event 事实与短事务

新增 `app/modules/reassign/adapters/sqlite_repository.py`：

1. 初始化时仅校验既有 `documents` 表的必需列，并幂等创建 `reassign_operations`、`reassign_steps`、
   `reassign_events`；不重建、不迁移或删除公开文档记录。
2. `reassign_operations` 以冻结 `documents.id` 为文档身份，保存来源/目标原始 ID JSON、文档快照、
   workspace 引用、状态、lease、fencing、诊断和 UTC 时间；对 active 状态建立 `document_row_id` 部分
   唯一索引，避免不同分类中的同名文件被错误串行化。
3. `reassign_steps` 保存八个固定 Step、稳定幂等键、写意图、状态、尝试次数、探测结果和有界诊断；
   `reassign_events` 使用 `(operation_id, sequence_no)` 唯一约束，并由数据库触发器拒绝 UPDATE/DELETE，
   保持只追加审计。
4. 每个 `SQLiteReassignmentUnitOfWork` 创建独立连接并执行短 `BEGIN IMMEDIATE` 事务；提交成功后才输出
   成功日志，回滚不会留下“已完成”的假日志。Adapter 不导入网络库、AnythingLLM Client 或 Knowledge
   Port，因此外部 I/O 不会位于 SQLite 锁范围内。
5. 保留执行权时，按 `fileName + int(oldArchitectureId)` 冻结权威文档、读取源 workspace、创建八个
   Step 和首条审计事件，并为同一文档递增 fencing token。
6. lease 续期与过期接管均通过条件写保护；接管后旧 owner 的 token/fencing 不能再写入步骤。
7. workspace 映射、prepare Step 和 Operation 引用在同一事务提交。既有映射不得被当前 Operation
   标记为“由我创建”，避免后续补偿误删共享 workspace。
8. 本地提交条件同时复核 `documents.id`、`file_name`、来源分类、`anything_doc_id` 和 `doc_path`；
   `rowcount != 1` 或唯一约束冲突只保留冲突事实。成功路径将文档更新、commit Step、`succeeded` 终态
   和审计事件置于同一事务，步骤检查点写失败时文档更新一起回滚。

为保持接口文档冻结的兼容边界，端口和 SQLite 适配器不提前严格化 `newArchitectureId`。离线回归已
验证原始 `"12"` 和 `false` 可分别按现有 SQLite 存储亲和性完成本地条件提交；原始请求值仍保留在
Operation 中，未向公开接口新增解释规则。

### 2.3 严格 Fake 与无网络事务门禁

新增 `tests/fakes/reassign.py`：

- `FakeReassignmentRepository` 使用受锁保护的内存快照模拟短事务提交/回滚，拒绝重复 operation ID，
  并对 source workspace 查询、目标存储亲和性、活动文档保护、lease/fencing、Step 状态机和本地 CAS
  保持与 SQLite Adapter 一致的最小语义；
- `FakeReassignmentKnowledgePort` 要求测试显式声明每一项远端调用，拒绝未声明调用、请求不匹配、
  错误顺序，以及 UoW 活动期间的外部调用；
- 文档变更、workspace 准备的明确成功、结果未知和异常均视为可能已有外部副作用。再次调用必须在
  测试中显式 `allow_duplicate=True`，否则 fail-fast；明确失败才不会伪造已执行事实；
- 这些替身不访问 SQLite、文件系统或网络，不能被当作真实 AnythingLLM 行为或生产容量证明。

### 2.4 日志与安全边界

SQLite Adapter 的日志只记录内部 `operation_id`、文档安全摘要、fencing、步骤和受控错误分类；成功日志
在事务提交后才发出。日志不包含 API Key、Authorization、文档正文、完整供应商响应、真实文档路径或
公开响应中禁止出现的内部事实。当前没有新增公开日志字段，也没有更改遗留路由日志协议。

---

## 3. 修改文件

| 文件 | 实际改动 |
| --- | --- |
| `app/modules/reassign/ports/repository.py` | 新增 Repository/UoW Protocol、lease/fencing、Operation/Step/Event、workspace 映射、本地提交和接管 DTO。 |
| `app/modules/reassign/ports/knowledge.py` | 新增供应商无关 workspace、成员探测、文档变更/Pin 请求与显式结果协议。 |
| `app/modules/reassign/ports/__init__.py` | 统一导出内部端口类型。 |
| `app/modules/reassign/adapters/sqlite_repository.py` | 新增幂等 Schema 初始化、独立短事务 UoW、活动部分唯一索引、条件写、CAS 和 append-only 审计。 |
| `app/modules/reassign/adapters/__init__.py`、`composition.py` | 导出 SQLite Adapter，并提供不带基础设施创建副作用的端口依赖束校验。 |
| `tests/fakes/reassign.py`、`tests/fakes/__init__.py` | 新增严格 Repository/Knowledge Fake 和事务/重复副作用门禁。 |
| `tests/test_reassign_ports.py` | 新增 DTO、Protocol、组合和 SQLite Adapter 网络依赖静态检查。 |
| `tests/test_reassign_fake_repository.py` | 新增 Fake 与 SQLite 原始 ID/重复 operation ID 兼容回归。 |
| `tests/test_reassign_strict_fakes.py` | 新增未声明调用、事务内调用、顺序、重复/未知 workspace 副作用门禁。 |
| `tests/test_reassign_sqlite_adapter.py` | 新增 Schema、并发、跨分类、lease、workspace、CAS、审计和回滚测试。 |
| `app/modules/reassign/*/README.md`、`README.md`、`tests/README.md` | 更新当前阶段能力、验证命令和未接线边界。 |
| `docs/重构记录/*`、`docs/更新记录/README.md` | 更新 1E 实施状态、文件清单和执行记录索引。 |

`docs/接口文档/分类节点变更.md` 未在本波次修改；`app/blueprints/llm.py` 未切换到新模块。

---

## 4. 验证与检查

所有 Python 测试均使用 `venv\Scripts\python.exe -B` 执行，未运行 `run.py`。

| 验证项 | 结果 |
| --- | --- |
| Port、严格 Fake、SQLite Adapter 与架构门禁定向回归 | 46 项通过（含 50 并发、CAS、append-only 审计和无网络导入门禁） |
| 领域、1E-0 契约资产、1E-2、遗留路由与架构门禁联合回归 | 113 项通过（最终复跑；覆盖既有公开路由契约，用于证明新模块尚未改变公开路由） |
| 安全全仓动态发现 | 发现 1,257 项；精确排除 13 项后执行 1,244 项，**0 失败、0 错误、0 跳过** |
| 接口文档完整性 | `docs/接口文档/分类节点变更.md` 的 SHA-256 复核为 `70BE30F1E768E7B980114793CFA8908AC83E77DBE233EF185C3B4591201361A0`，与本波次开始前一致 |
| 静态与差异检查 | `git diff --check`、13 个本波次 Python 文件 AST 解析、尾随空白检查均通过 |

安全全仓沿用项目既有、逐项明确的 13 项排除：

1. `tests.test_local_scripts.LocalScriptTests` 的 7 项会启动本地 Shell、静态服务或 `run.py`；
2. `tests.test_test_assets.LLMTestAssetsTests` 的 5 项依赖被 `.gitignore` 排除的本地夹具；
3. `tests.test_migrate_analysis_security.AnalysisSecurityMigrationTests.test_apply_is_idempotent_and_preserves_callback_metadata_and_audit` 的 1 项断言 POSIX `0640` 权限位，Windows 无法可靠表达。

全仓输出中的异常栈、超时、回调、临时容器和 Dispatcher 日志来自既有显式故障注入或临时隔离测试；
最终结果为 `success:True failures:0 errors:0 skipped:0`。测试不连接真实 AnythingLLM，也不调用真实
外部服务。

---

## 5. 发布、回滚与后续边界

本波次没有公开路由切换、真实外部副作用或运行时数据库初始化，因此没有生产发布动作。SQLite Schema
是加法设计；未来任何实际初始化后的回滚都不得通过删除 `reassign_*` 表伪造恢复，因为它们用于保存
活动 Operation 与审计现场。

后续阶段必须遵守以下硬边界：

1. 1E-3 的 AnythingLLM Adapter 必须使用请求级 Client/Transport，并把 `false`、缺 slug、超时、断连和
   协议异常分类为明确失败或结果未知；绝不在 UoW 内执行网络调用。
2. 1E-3～1E-4 的 `DocumentReassignmentService` 必须在每次外部写前提交 `mutation_started`，写后提交
   明确结果或探测事实；不能因为 Port/Fake/Schema 已存在而提前宣称 Saga 完成。
3. 1E-5 必须实现写后探测、补偿、`recovery_required`、过期 lease 接管和人工恢复审计；结果未知时
   不得重放。
4. 1E-6 才能在再次确认接口文档不变后接入 Container、Parser → Application → Presenter 和公开路由；
   公开响应仍不得泄露 Operation、lease、fencing、步骤或审计。
5. 真实 AnythingLLM 集成验证、有限 HTTP/总预算/补偿预留的生产校准、跨进程恢复、MySQL/Outbox、
   可靠队列和多实例协调仍未完成，不能由本波次离线代码宣称已具备或 production ready。

> 后续说明：1E-2 完成后的全面审查又修复了恢复 fencing、终态/成功事实、只读恢复扫描、
> 严格 Fake 线程事务隔离和既有 Schema/索引升级一致性。实际结果见
> `260724-阶段1E-2R分类节点变更持久化一致性修正执行记录.md`；本历史记录保留当时验收口径。
