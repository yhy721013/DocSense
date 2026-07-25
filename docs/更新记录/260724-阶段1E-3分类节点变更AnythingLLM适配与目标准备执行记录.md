# 阶段 1E-3 分类节点变更 AnythingLLM 适配与目标准备执行记录

## 1. 执行结论

| 项目 | 结论 |
| --- | --- |
| 执行日期 | 2026-07-24 |
| 执行分支 | `refactor/concurrency` |
| 对应设计 | `../重构记录/260724-阶段1E分类节点变更同步Saga文件级实施设计.md` 的 1E-3 |
| 执行范围 | 请求级 AnythingLLM Client Factory、有限预算/单调 deadline、Knowledge Port Adapter、目标 workspace 准备和离线故障验证 |
| 生产代码切换 | **无**；`app/blueprints/llm.py::llm_reassign()`、`app/container.py` 均未构造新 Adapter |
| 接口影响 | **无**；未修改 `docs/接口文档/`，未增删或改名请求/响应字段、状态码、Header、JSON/SSE/WS 字段或同步语义 |
| 外部副作用 | 无；未启动 `run.py`，未连接真实 AnythingLLM、模型、Callback 或其他后台服务 |
| 阶段状态 | **1E-3 已完成**；1E-4～1E-6 仍待实施 |

本波次交付供应商适配的离线可验证边界，而不是把现有同步路由直接接到远端。所有远端原子操作都经过
请求级 deadline 和强类型结果分类；任何无法证明的外部结果保留为结果未知，后续恢复不得盲目重发。

---

## 2. 已完成的改造

### 2.1 严格内部预算与请求隔离

1. 新增 `ReassignmentInfrastructureConfig`，默认单次 HTTP 15 秒、总预算 75 秒、补偿预留 30 秒。
   配置拒绝 `None`、布尔值、NaN、Infinity、非正数以及总预算无法留下前向窗口的组合。
2. 新增 `ReassignmentExecutionDeadline`。它只使用单调时钟，前向调用会裁剪为剩余总预算减补偿预留，
   探测/恢复调用使用剩余总预算；注入的测试时钟倒退也不能延长调用时间。
3. 新增三个保留内部环境变量：`DOCSENSE_REASSIGN_HTTP_TIMEOUT_SECONDS`、
   `DOCSENSE_REASSIGN_TOTAL_TIMEOUT_SECONDS`、`DOCSENSE_REASSIGN_COMPENSATION_RESERVE_SECONDS`。
   当前 Container 和遗留路由尚未加载它们，因此设置这些值不会改变现有公开接口运行行为。
4. `AnythingLLMReassignmentKnowledgeAdapterFactory` 每次创建独立 Adapter 和 deadline，禁止跨文档、
   跨线程或跨请求共享可变预算。

### 2.2 请求级 AnythingLLM Client Factory

1. `AnythingLLMReassignmentClientFactory` 每次原子调用创建新的 `AnythingLLMTransport` 与
   `AnythingLLMWorkspaceClient`，超时必须由 deadline 显式传入，不能回退到通用配置超时。
2. 正常和异常路径都关闭 Transport。若已有业务异常，关闭失败只记录脱敏异常日志且保留原异常；
   正常路径的关闭失败会继续上抛并由 Adapter 保守分类。
3. Factory 不保存 HTTP Session、Cookie 或其他连接级可变状态，为后续多线程任务隔离和多实例
   组合根保留无状态基础。

### 2.3 Knowledge Port Adapter 与目标 workspace 准备

1. 目标 workspace 只按期望名称或 slug 的 casefold 后精确匹配，不使用模糊匹配或任意选择。
   唯一匹配返回复用事实；多个不同 slug 的精确匹配返回 `OUTCOME_UNKNOWN`。
2. 未找到后才创建 workspace。供应商显式 `false` 直接归为已知失败；缺 slug、身份不一致、超时、
   断连和协议异常只允许进行一次纯读取查回，绝不自动再次创建。
3. 只读查回找到的 workspace 的 `created_by_operation` 固定为 `None`。因为现有供应商协议没有
   可验证创建幂等键，不能把共享永久 workspace 声称为当前 Operation 的可自动删除资源。
4. 成员关系只使用完整规范化 `doc_path`。返回同 basename、展示名或其他 document ID 的对象
   都不能证明同一文档已经挂载或解绑。
5. 解绑和挂载调用 `update_embeddings` 后都会重新探测成员关系。明确成功且状态达成返回 `APPLIED`；
   调用失败但探测已达目标状态返回 `ALREADY_IN_DESIRED_STATE`；探测仍未知返回
   `OUTCOME_UNKNOWN`，不会盲目重发；明确状态未达成返回 `KNOWN_FAILURE`。
6. Pin 保持历史 best-effort 语义：失败会产生脱敏警告和强类型结果，但不会把已经确认的成员关系
   伪造为回滚失败。
7. 超时、断连、4xx、5xx、Transport 关闭、协议异常和异常响应均映射为受限错误码/摘要；日志不记录
   API Key、Authorization、供应商响应正文或公开响应不允许出现的内部事实。

### 2.4 分层与未实施边界

1. Adapter 不导入 SQLite Repository、不创建 Operation/Step、不执行本地映射写入，并且不在数据库
   事务内发起网络 I/O。
2. 设计中所述的本地 workspace 映射复用、创建意图、步骤检查点和条件 CAS 必须由 1E-4
   `DocumentReassignmentService` 协调 Repository 完成。本波次只返回外部事实，避免出现没有
   写前意图的半成品跨系统副作用链。
3. 本波次未实现补偿执行、过期 lease 接管、恢复服务、可靠队列、Container 生产装配或 Flask
   路由切换。现有 `/llm/reassign` 仍是遗留同步实现。

---

## 3. 修改范围

| 文件 | 修改内容 |
| --- | --- |
| `app/modules/reassign/adapters/infrastructure_config.py` | 内部预算配置、环境加载、有限值校验和单调 deadline。 |
| `app/modules/reassign/adapters/anythingllm_clients.py` | 无状态请求级 Transport/Workspace Client Factory 与关闭语义。 |
| `app/modules/reassign/adapters/anythingllm_knowledge.py` | 目标 workspace 查回/创建、精确成员探测、解绑/挂载/Pin、异常与结果分类、脱敏日志。 |
| `app/modules/reassign/adapters/__init__.py` | 导出 1E-3 Adapter、配置和 Factory。 |
| `tests/test_reassign_anythingllm_adapter.py` | 22 项离线 Adapter、预算、关闭、协议和故障矩阵测试。 |
| `app/modules/reassign/__init__.py`、各层 README、根 README | 同步当前能力、保留配置和未接线边界。 |
| `tests/README.md` | 增加 1E-3 测试说明与联合回归命令。 |
| `docs/重构记录/260724-阶段1E分类节点变更同步Saga文件级实施设计.md`、`docs/重构记录/README.md`、`docs/更新记录/README.md` | 将 1E-3 标记为已完成，并记录 1E-4～1E-6 的剩余职责。 |

`docs/接口文档/分类节点变更.md` 未在本波次修改。

---

## 4. 验证与检查

所有 Python 验证使用项目 `venv\Scripts\python.exe -B`，没有执行 `run.py`。

| 验证项 | 结果 |
| --- | --- |
| 1E-3 Adapter 定向测试 | 22 项通过。 |
| 1E 领域、契约、Port、Fake、SQLite、Adapter、遗留路由、架构、Container 与 DatabaseService 联合回归 | 187 项通过。 |
| 安全全仓动态发现 | 发现 1,299 项；按既定安全口径排除 13 项后，1,286 项全部通过，耗时 84.957 秒。 |
| `git diff --check` | 通过。 |
| 新增 Adapter 与测试源码 AST 解析 | 4 个文件通过。 |
| 公开接口文档哈希 | SHA-256 为 `70BE30F1E768E7B980114793CFA8908AC83E77DBE233EF185C3B4591201361A0`，与实施前记录一致。 |
| 新 Adapter 在路由/Container 的引用 | 0 处。 |
| 真实外部集成 | 未执行；本波次不得据此宣称生产 AnythingLLM 已验证。 |

安全全仓验证通过内联 `unittest` 发现器执行，并在运行前显式排除以下 13 项：

1. 7 项 `tests.test_local_scripts.LocalScriptTests.*`：会执行 Shell、访问本地应用或启动测试文件服务；
2. 5 项 `tests.test_test_assets.LLMTestAssetsTests.*`：依赖被 `.gitignore` 排除的外部样例资产；
3. 1 项 `tests.test_migrate_analysis_security.AnalysisSecurityMigrationTests.test_apply_is_idempotent_and_preserves_callback_metadata_and_audit`：Windows 不支持的 POSIX 权限位断言。

因此“1,286 项通过”是已说明排除范围的安全全仓结果，不把未运行的环境项误写为全量通过。

---

## 5. 后续硬边界

1. 1E-4 必须先经 Repository 持久化 Operation、写意图和步骤检查点，再调用本 Adapter；本地 workspace
   映射登记和 `documents` 条件 CAS 必须走现有专用事务入口。
2. 1E-5 必须按 fencing 接管后进行写后探测、补偿和恢复。任何 `OUTCOME_UNKNOWN` 都只能保留现场或
   走明确恢复流程，不能重放外部写。
3. 1E-6 只有在接口文档再次确认不变、公开黄金测试通过后，才能由 Container 装配 Factory，并把
   `/llm/reassign` 切换为 Parser → Application → Presenter。
4. 当前有限同步预算只约束未来 Adapter 请求，不能替代可靠任务队列、跨实例协调或分布式数据库
   一致性；后续演进仍需以 lease、fencing、条件写、稳定扫描和显式迁移治理为基础。
5. 在隔离测试 workspace 和测试文档的真实运行验收完成前，不得标记为 production ready。
