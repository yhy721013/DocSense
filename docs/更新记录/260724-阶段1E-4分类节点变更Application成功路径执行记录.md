# 阶段 1E-4：分类节点变更 Application 成功路径执行记录

## 1. 执行结论

| 项目 | 结论 |
| --- | --- |
| 执行日期 | 2026-07-24 |
| 执行分支 | `refactor/concurrency` |
| 对应设计 | `../重构记录/260724-阶段1E分类节点变更同步Saga文件级实施设计.md` 的 1E-4 |
| 执行范围 | Application 前向成功路径、目标 workspace 持久化 preparation claim、无副作用失败专用收口、条件 CAS 与离线组合验证 |
| 生产代码切换 | **无**；`app/blueprints/llm.py::llm_reassign()` 与 `app/container.py` 未引用 `DocumentReassignmentService` |
| 接口影响 | **无**；未修改 `docs/接口文档/`，未增删或改名请求/响应字段、状态码、Header、JSON/SSE/WS 字段或同步语义 |
| 外部副作用 | 无；未启动 `run.py`，未连接真实 AnythingLLM、模型、Callback 或其他后台服务 |
| 阶段状态 | **1E-4 已完成**；1E-5 补偿/恢复、1E-6 组合根/公开路由切换仍待实施 |

本波次完成的是可离线组合验证的内部前向编排，不是公开接口的生产切换。`/llm/reassign`
当前仍由遗留蓝图完成同步流程；因此本次不改变任何对外请求、响应或运行行为。

---

## 2. 已完成的改造

### 2.1 Application 只依赖 Domain 与 Port

1. 新增 `DocumentReassignmentService` 和显式注入的 `ReassignmentExecutionSettings`。服务不读取环境
   变量、不创建 HTTP Client、不创建线程、不生成 Flask Response，也不依赖 `ReassignmentPortBundle`
   或具体 SQLite/AnythingLLM Adapter。
2. 每次 `execute()` 通过 `ReassignmentKnowledgePortFactory` 获取新的请求级 Knowledge Port，避免
   deadline、Transport 或严格 Fake 调用期望跨请求共享。
3. 所有本地写使用独立短 Unit of Work；任何外部调用都在事务关闭后执行。前向远端顺序是来源解绑、
   目标 workspace 复用或准备、目标成员挂载、Pin best-effort、条件 CAS 本地提交。空 `doc_path`
   保持已有 local-only 兼容分支，不发起远端成员关系调用。
4. 对外仅返回既有 `ReassignmentResult` 与已批准的 `ReassignmentPublicMessage`，不暴露 operation ID、
   lease、fencing、步骤、claim 或远端异常正文。

### 2.2 多实例目标 workspace 创建隔离

1. 在 SQLite 增加 `reassign_workspace_preparation_claims`。它按目标分类的既有 SQLite 存储投影唯一，
   记录 operation、owner、token、独立 fencing、到期时间和 `active/released` 状态。
2. 首次取得、已释放后再取得、以及过期接管都会递增独立 fencing；已释放行不删除，避免重建为
   fencing=1 的 ABA 问题。
3. 新 mapping 必须携带仍有效且匹配的 claim。目标 mapping、`prepare_target_workspace` 成功事实和
   claim 释放放在同一 SQLite 事务中，不能留下“mapping 已写、旧 claim 仍有效”的中间状态。
4. 同一目标正在准备时，另一份尚未发生远端写的 Operation 安全失败并释放自己的文档保护；它不会
   泄露内部 claim，也不会并发创建第二个同名 workspace。不同目标与不同文档仍可并行。

### 2.3 成功、失败与恢复隔离

1. 成功只能经 Repository 专用入口，将 `documents` 的 `rowcount == 1` 条件 CAS、commit Step 和
   `succeeded` 终态原子提交；远端步骤、目标 workspace 事实不足时禁止成功。
2. 新增“无副作用失败”专用收口。Repository 会逐步核验：没有 `mutation_started`/`outcome_unknown`、
   没有已确认远端效果、没有 `created_by_operation` 或 `unknown` 的目标 workspace 归属，且每个已知
   失败 Step 都有 `confirmed_no_effect` 事实，才允许写入 `failed` 并释放同文档保护。
3. 一旦来源解绑、目标创建、目标挂载或本地提交可能已经生效，或者结果无法确认，当前请求一律写为
   `recovery_required`。1E-4 不补偿、不盲重发；1E-5 将负责探测、补偿、过期 lease 接管与人工恢复。
4. Pin 保持遗留 best-effort 语义：失败记录脱敏告警，但不推翻已经确认的成员关系和本地成功提交。

---

## 3. 修改范围

| 文件 | 修改内容 |
| --- | --- |
| `app/modules/reassign/application/service.py` | 新增同步 Application Service、显式执行设置、写意图/检查点编排、目标 claim、保守失败与条件 CAS 收口日志。 |
| `app/modules/reassign/__init__.py`、`application/__init__.py`、`application/README.md`、`adapters/__init__.py`、`adapters/README.md` | 导出服务并说明依赖方向、实际 Factory 使用边界、成功路径与 1E-5/1E-6 未实施边界。 |
| `app/modules/reassign/ports/repository.py`、`ports/__init__.py` | preparation claim DTO/Port、无副作用失败请求、审计事件与 mapping 请求中的 claim 事实。 |
| `app/modules/reassign/adapters/sqlite_repository.py` | claim 加法 Schema、独立 fencing/过期接管、mapping 原子释放和失败终态事实核验。 |
| `tests/fakes/reassign.py` | 严格 Fake 对应的 claim、fencing、原子 mapping 和安全失败语义。 |
| `tests/test_reassign_application.py` | 新增 7 项 Application/真实 SQLite 组合测试。 |
| `tests/test_reassign_sqlite_adapter.py` | 覆盖无 claim mapping 拒绝、同目标竞争与过期接管。 |
| `README.md`、模块 README、`tests/README.md`、重构记录索引与实施设计 | 同步 1E-4 实施事实、测试入口和后续边界。 |

`docs/接口文档/分类节点变更.md` 未在本波次修改。

---

## 4. 验证与检查

所有 Python 验证均使用项目 `venv\Scripts\python.exe -B`；没有执行 `run.py`。

| 验证项 | 结果 |
| --- | --- |
| 新增模块与测试编译 | `compileall` 通过。 |
| 1E-0～1E-4、AnythingLLM Adapter、Application 与完整架构边界联合回归 | 143 项通过，耗时 14.215 秒。 |
| 安全全仓动态发现 | 发现 1,314 项；按既定安全口径排除 13 项后，1,301 项全部通过，0 failure、0 error、0 skip，耗时 108.5 秒。 |
| 最终包说明与测试导入复核 | `py_compile` 通过；Application 与完整架构边界 24 项通过。 |
| Application/路由分层 | `app/blueprints/llm.py` 与 `app/container.py` 对 `DocumentReassignmentService`、`ReassignmentExecutionSettings`、`SQLiteReassignmentRepository` 的引用为 0。 |
| `git diff --check` | 通过；仅有既有工作区的 LF/CRLF 转换提示，无空白错误。 |
| 公开接口文档哈希 | SHA-256 为 `70BE30F1E768E7B980114793CFA8908AC83E77DBE233EF185C3B4591201361A0`，与 1E-3 记录一致。 |
| 真实外部集成 | 未执行；离线通过不等同于生产 AnythingLLM 或容量验证。 |

安全全仓明确排除以下 13 项环境/平台测试，而非笼统宣称原始发现全绿：

1. `tests.test_local_scripts.LocalScriptTests.*` 的 7 项：会启动本地 Shell、静态服务或 `run.py`；
2. `tests.test_test_assets.LLMTestAssetsTests.*` 的 5 项：依赖被 `.gitignore` 排除的本地样例夹具；
3. `tests.test_migrate_analysis_security.AnalysisSecurityMigrationTests.`
   `test_apply_is_idempotent_and_preserves_callback_metadata_and_audit` 的 1 项：Windows 无法可靠表达
   POSIX `0640` 权限位断言。

全仓输出中的异常栈、超时、Callback、容器和 Dispatcher 日志来自既有显式故障注入测试；最终
`SAFE_FULL_RESULT=success:True failures:0 errors:0 skipped:0`。未访问真实 AnythingLLM 或其他外部服务。

---

## 5. 后续硬边界

1. 1E-5 必须在取得更大 fencing 后，针对每个写后检查点执行只读探测、补偿或人工恢复；不能把
   `recovery_required` 重新包装为普通失败或成功。
2. 1E-6 只有在接口文档再次确认不变、公开黄金测试通过后，才能由 Container 装配执行设置、
   SQLite Repository 和 Knowledge Factory，并把 `/llm/reassign` 切换为 Parser → Application → Presenter。
3. 当前 claim/lease/fencing 解决的是 SQLite 单库事实下的跨实例写入权，不能替代可靠任务队列、
   跨数据库事务或真实供应商幂等协议；多实例部署仍需在 1E-5～后续阶段完成容量和恢复治理。
4. 在隔离测试 workspace、测试文档以及目标部署拓扑下完成真实集成验收前，不得标记为 production ready。
