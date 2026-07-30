# `main` 与 `refactor/concurrency` 分支集成实施计划

## 0. 文档信息

| 项目 | 内容 |
| --- | --- |
| 编写日期 | 2026-07-25 |
| 文档状态 | 执行中；已完成 M0 基线冻结、M1 独立工作树和 M2 未提交合并现场，尚未解决任何冲突或创建 merge commit |
| 文档层级 | L3 分支集成实施计划 |
| 目标主线基线 | `main@4472065a44cfd6e049f8fbe46cfc728ab26d3e58` |
| 待集成分支基线 | `refactor/concurrency@d2228ef9d6dd4d860bf74ad726340904ab40ecc9` |
| 共同基点 | `0023c4726b49791728a30b053e9da772f005e178` |
| 当前分叉 | `main` 独有 37 个提交；`refactor/concurrency` 独有 13 个提交 |
| 建议集成分支 | `refactor/integration` |
| 公开契约依据 | `docs/接口文档/` |
| 运行限制 | 默认不启动 `run.py`，不连接真实 AnythingLLM、模型、Callback 或其他后台服务 |
| 验证解释器 | Windows：`venv\Scripts\python.exe -B` |
| 被取代计划 | `260722-refactor与analysis优化分支合并实施计划.md` 的执行方向和基线已过时；其 Analysis 语义移植细节继续作为参考 |

本计划的目标是把 `refactor/concurrency` 已完成的阶段 0、1A、1B、1C、1D、1E 成果安全地
集成进最新 `main`，同时完整保留 `main` 已经合入的 Analysis 领域树、Top-K 召回、两阶段
RAG、文件任务并发保护、召回审计，以及 2026-07-22 新增的摘要、关键词和所属技术约束。

本计划不修改任何公开接口参数。实施过程中如果发现两个已知分支之外的新接口变化，必须立即
停止，先确认 HTTP 状态码、JSON 字段、Header、SSE、WebSocket 或 Callback 契约，再继续修改。

---

## 1. 执行结论

### 1.1 推荐合并方向

从最新 `origin/main` 创建独立集成分支，再把 `origin/refactor/concurrency` 作为第二父分支
执行一次非快进合并：

```text
origin/main
    \
     merge commit  <- origin/refactor/concurrency
```

最终提交的第一父提交必须是 `main`，第二父提交必须是 `refactor/concurrency`。冲突解决时以
`refactor/concurrency` 的模块化单体结构、任务事实和恢复边界作为架构基线，以 `main` 的最新
Analysis 能力作为需要完整保留的功能增量。

这种方向与 2026-07-22 旧计划相反。旧计划当时面对的是尚未进入主线的 Analysis 功能分支；
现在 Analysis 已由 PR #78 合入 `main`，本次目标也变为把重构成果送入主线，因此必须调整第一
父提交方向。

### 1.2 为什么不使用 rebase 或逐提交 cherry-pick

- 两条分支均已发布到远端，原地 rebase 会重写共享历史。
- 13 个重构提交跨越模块骨架、数据库、Dispatcher、报告、武器谱和 Saga，逐提交 cherry-pick
  会重复解决同一核心文件冲突，并容易产生只在中间提交存在的错误组合。
- 单次 merge commit 可以保留两个原始历史，并允许使用 `git revert -m 1` 做可审计回滚。
- 从 `main` 建集成分支后，PR 相对 `main` 展示的是最终有效增量，不会把主线已有 Analysis
  提交重复表现为待合并内容。

### 1.3 当前虚拟合并结论

在下列固定基线上运行只读 `git merge-tree`：

```text
main                 4472065a44cfd6e049f8fbe46cfc728ab26d3e58
refactor/concurrency d2228ef9d6dd4d860bf74ad726340904ab40ecc9
merge-base           0023c4726b49791728a30b053e9da772f005e178
```

结果为：

- 双方共同修改 14 个文件；
- 8 个文件产生文本冲突；
- 共 24 个文本冲突块；
- 6 个共同修改文件会被 Git 自动合并，但必须人工做语义审查；
- `main` 另有 28 个单边文件；
- `refactor/concurrency` 另有 365 个单边文件。

> **实际 M2 基线（2026-07-25，已确认）**：在上述 tip 未发生漂移的前提下，独立集成工作树执行
> `git merge --no-commit --no-ff origin/refactor/concurrency` 后，仍得到相同的 8 个冲突文件，
> 但实际产生 22 个文本冲突块。其中 `app/services/llm_service/task_service.py` 由虚拟合并
> 预估的 5 块收敛为实际 3 块；其余文件的冲突块数量与原记录一致。本次后续实施、测试、
> 完成定义和提交说明均以“8 个文件、22 个冲突块”为准。上方 24 块仅保留为先前虚拟合并
> 的历史观测，不得据此跳过任何语义审查。

只要任一 tip 发生变化，就必须重新运行本节所有统计，不能继续依赖当前冲突数量。

---

## 2. 集成目标与非目标

### 2.1 必须实现的目标

1. 保留 `main` 的完整 Analysis 最新能力：
   - 完整领域树严格校验、不可变索引和 LRU 缓存；
   - Top-K 本地召回、字符 BM25、树路由、规则召回和 RRF 融合；
   - 分类与字段抽取两阶段隔离 Thread；
   - 数据标准正文保护、简氏作用域保护和装备身份受限重选；
   - 文件任务批量原子受理、`execution_id` 所有权和旧 Worker 防串写；
   - 文件 Callback 发送租约与补发期间的同名任务保护；
   - `llm_architecture_recall_decisions` 召回审计；
   - 最新摘要、关键词、所属技术及内部证据核验规则。
2. 保留 `refactor/concurrency` 的模块化单体和阶段 0～1E 成果：
   - Tasks、Report、Weaponry、Reassign 四层模块；
   - Report/Weaponry SQLite accepted 积压和 Dispatcher/Worker；
   - Callback Guard、资源恢复、Creation Intent、Evidence 和审计门禁；
   - Reassign 持久化 Saga、租约、栅栏、补偿、恢复观测和 `recovery_required`；
   - Progress 新控制面和当前 Chat 持久化边界。
3. 保持接口参数集合不增加、不删除、不重命名。
4. 保持 `main` 已确认的 Analysis 状态码、容量和分类语义。
5. 保持 `refactor/concurrency` 已切换的 Report、Weaponry、Progress 和 Reassign 路由语义。
6. 形成可重复执行的 SQLite 增量迁移、离线测试证据、回滚路径和完成定义。
7. 原始 `main` 与 `refactor/concurrency` 分支均不在集成过程中被改写。

### 2.2 本次明确不做的工作

1. 不实施阶段 1F 的完整 Analysis 模块化重构。
2. 不把 Analysis 接入 `llm_task_executions` Dispatcher；当前仍保留一个请求对应一个批量
   daemon thread 的迁移期实现。
3. 不提前切换 `/llm/analysis` 的 202 空响应体目标契约。
4. 不提前切换 `/llm/check-task` 的 200 空响应体目标契约。
5. 不引入 RabbitMQ、Celery、MySQL、MinIO、Redis 或 FastAPI。
6. 不宣称 SQLite、进程锁或进程内 Hub 已经支持多实例。
7. 不把离线 50 线程状态隔离测试解释为 50 路真实模型推理吞吐。
8. 不启动 `run.py`，不自动执行真实 AnythingLLM 三文件 E2E。
9. 不顺带搬迁 Weaponry 业务 Adapter 到 `app/integrations/anythingllm`。
10. 不删除历史方案、更新记录或审计表。

---

## 3. 不可破坏的契约和架构基线

### 3.1 对外契约基线

- `docs/接口文档/` 是 HTTP、SSE、WebSocket 和 Callback 契约的唯一权威来源。
- 禁止因解决 Git 冲突而自行增加、删除或重命名接口字段。
- 内部 `execution_id`、`run_id`、lease、fencing token、audit ID、Operation ID 不得新增到
  公开响应或 Callback。
- `/llm/chat*` 的 `chatId`、SSE 事件名、行格式、Header 和状态码保持冻结。
- `/llm/reassign` 的同步语义、稳定错误信息和 `newArchitectureId` 兼容白名单保持不变。
- Report 和 Weaponry 的 202 空响应体、409 行为和严格参数解析保持不变。
- Progress 保持不支持 `action=subscribe/query/unsubscribe`，错误后保持连接且不发送 ack。

### 3.2 Analysis 已确认决策

2026-07-22 已确认完整采纳以下行为，本次只做既有主线能力保留，不重新扩张解释：

| ID | 已确认行为 | 本次处理 |
| --- | --- | --- |
| C-01 | `params` 最多 32 项 | 保留 main 实现与测试 |
| C-02 | JSON 请求体最大 64 MiB，超限 HTTP 413 | 保留 main 实现与部署说明 |
| C-03 | 单树最多 10,000 节点及字段/JSON 长度上限 | 保留严格树校验 |
| C-04 | 非法节点、重复 ID、非法父链或成环时整批 HTTP 400 | 保留原子失败顺序 |
| C-05 | 默认启用 Top-K 两阶段、scope guard、数据标准保护和装备身份受限重选 | 保留四项配置及默认值 |
| C-06 | Callback 交接或补发租约期间，同名 Analysis 重跑 HTTP 409 | 与文件 callback claim 一并保留 |
| C-07 | 三份 PDF E2E 样例允许进入仓库 | 保留 blob，规范错误的可执行位 |

2026-07-22 后 `main` 又增加摘要、关键词和所属技术规则。它们不增加 Callback 字段：模型内部
`relatedTechnologyEvidence` 仅用于服务端核验，必须在正式 Callback 映射前移除。该变化作为
当前 `main` 最新功能完整保留。

### 3.3 架构基线

- `app/container.py` 是唯一生产组合根。
- `app/modules/<business>/` 保存业务 Domain、Application、Ports 和业务 Adapter。
- `app/integrations/anythingllm/` 只保存通用供应商协议、Transport、Client、Factory 和 Gateway。
- Factory 只持有不可变配置；每个任务或请求单独创建并关闭 Transport/Session。
- 网络调用、文件 IO 和模型调用不得发生在 SQLite 写事务内。
- 外部副作用结果未知时 fail closed，不得盲目重试或补偿。
- 审计成功是永久业务成功的硬前置；审计失败不能继续永久存储、成功 Callback 或成功清理。
- 当前运行模式仍是单实例，不能通过共享 SQLite 文件伪装集群能力。

---

## 4. 合并后目标能力矩阵

| 路由或能力 | 最终来源 | 必须保留的实现 | 禁止恢复的实现 |
| --- | --- | --- | --- |
| `/llm/analysis` | `main` 功能 + `refactor` 组合根 | 严格校验、原子受理、execution、Top-K、两阶段、召回审计、文件 callback claim | 无界完整树 Prompt、按文件启动多个线程、旧 execution 写新任务 |
| `/llm/generate-report` | `refactor/concurrency` | Parser → Submit → SQLite accepted → Dispatcher/Worker → Presenter | main 的路由线程和遗留 `run_report_task` |
| `/llm/weaponry` | `refactor/concurrency` | 冻结文档范围、Evidence、Provided-Evidence、资源/审计/Guard、新 Dispatcher | main 的遗留 `run_weaponry_task` 和 `WEAPONRY_ANALYSE_MODE` |
| `/llm/reassign` | `refactor/concurrency` | 同步 Saga、Operation/Step/Event、补偿恢复、lease/fencing | main 的路由内 AnythingLLM 编排 |
| `/llm/check-task` | `refactor` 路由 + `main` 文件租约 | file 使用 callback claim；report/weaponry 使用通用 Guard | 把两种 Guard 混成一个表或一个方法 |
| `/llm/progress` | `refactor/concurrency` | 无 action、严格 params、消息错误保持连接、单写入出口 | main 的 subscribe/query/unsubscribe/ack 扩展 |
| `/llm/chat*` | 当前共同基线与 `refactor` 门禁 | committed 本地历史、一个 chat 一个活动流、持久化取消和清理 | 内部 ID 泄漏、跨实例能力虚假声明 |
| 任务数据库 | 两侧能力并集 | execution 队列、Guard、资源、召回审计、文件 claim | 删除重建、复用不兼容状态语义 |

---

## 5. Git 执行流程

### M0：冻结输入

在任何文件修改前执行：

```powershell
git status --short --branch
git fetch origin main refactor/concurrency

git rev-parse main
git rev-parse origin/main
git rev-parse refactor/concurrency
git rev-parse origin/refactor/concurrency
git merge-base origin/main origin/refactor/concurrency
git rev-list --left-right --count origin/main...origin/refactor/concurrency
```

预期基线：

```text
main == origin/main == 4472065a44cfd6e049f8fbe46cfc728ab26d3e58
refactor/concurrency == origin/refactor/concurrency
    == d2228ef9d6dd4d860bf74ad726340904ab40ecc9
merge-base == 0023c4726b49791728a30b053e9da772f005e178
```

若任一值变化：

1. 不创建集成分支；
2. 重新生成独有提交、共同修改文件、冲突文件和冲突块统计；
3. 检查是否出现新的接口文档变更；
4. 更新本计划的观测基线或生成补充执行记录；
5. 确认后再继续。

### M1：创建独立集成工作树

建议使用独立工作树，避免改变当前 `refactor/concurrency` 工作区：

```powershell
$integrationPath = 'C:\.me\codes\DocSense-integration'

Test-Path -LiteralPath $integrationPath
git worktree list

git worktree add `
  -b refactor/integration `
  $integrationPath `
  origin/main

Set-Location -LiteralPath $integrationPath
git status --short --branch
```

若目标目录已存在，不得覆盖或递归删除；先检查目录内容和 `git worktree list`，再由负责人决定
新路径。

### M2：产生冲突现场但不提交

```powershell
git merge --no-commit --no-ff origin/refactor/concurrency
git status --short
git diff --name-only --diff-filter=U
```

在固定基线上预期有 8 个冲突文件。若实际数量或文件集合不同，立即执行只读差异分析；在确认
新范围前不要继续解决冲突。

冲突解决期间禁止使用：

```text
git checkout --ours .
git checkout --theirs .
git restore --ours .
git restore --theirs .
```

因为集成分支从 `main` 创建，此时：

- `ours` 表示 `main`；
- `theirs` 表示 `refactor/concurrency`。

评审和注释中应直接写分支名，不要只写 ours/theirs，防止方向误判。

### M3～M8：按下文里程碑逐层解决

每完成一个文件：

1. 检查冲突标记；
2. 运行该文件对应的最小测试；
3. 查看 `git diff -- <file>`；
4. 确认后才 `git add -- <file>` 标记为已解决；
5. 不在全部门禁通过前创建 merge commit。

---

## 6. 里程碑总览

| 里程碑 | 内容 | 退出门禁 |
| --- | --- | --- |
| M0 | 固定 refs、契约和冲突范围 | tip、merge-base、工作区、接口差异已记录 |
| M1 | 创建独立集成工作树 | 第一父分支为最新 main，原分支不变 |
| M2 | 产生 merge 冲突现场 | 冲突集合与计划一致，无意外删除/二进制 |
| M3 | Port、Gateway、Policy、Config、Prompt | 公共合同定向测试全绿 |
| M4 | `LLMTaskService` Schema、迁移和执行所有权 | 四类数据库迁移与并发测试全绿 |
| M5 | Container、Analysis 应用主链和 Flask 路由 | 路由/生命周期/架构门禁全绿 |
| M6 | 文档、配置、README、PDF 资产 | 契约与实现一致，无新增接口参数 |
| M7 | 业务分层回归和跨分支集成测试 | Analysis + Report + Weaponry + Reassign + Chat 全绿 |
| M8 | 安全全仓、最终 diff、merge commit | 零新增失败/错误，审查完成 |
| M9 | PR、主线漂移复核和发布前交接 | PR 证据完整，未虚假声明生产能力 |

---

## 7. 八个文本冲突文件的详细处理

### 7.1 `.env.example`：1 个冲突块

#### 修改位置

- 文件：`.env.example`
- 锚点：`LLM 知识抽取模式配置`、Weaponry 配置、Report 配置、Chat/Reassign 单实例配置。

#### 处理步骤

1. 以 `refactor/concurrency` 的配置分区和注释为底稿。
2. 增加 `main` 的四项 Analysis 配置：

   ```ini
   DOCSENSE_ANALYSIS_CLASSIFICATION_MODE=topk_two_stage
   DOCSENSE_ANALYSIS_FILENAME_CONSTRAINT_MODE=scope_guard
   DOCSENSE_ANALYSIS_DATA_STANDARD_MODE=scope_guard
   DOCSENSE_ANALYSIS_IDENTITY_RESELECT_MODE=enforce
   ```

3. 保留每项配置的回滚值和显式空值 fail-fast 说明。
4. 保留 Weaponry 四类能力指纹、production attestation、Terms、Dispatcher 和恢复配置。
5. 保留 Report、Chat、Reassign 的 `single_instance` 配置和注释。
6. 不恢复 `WEAPONRY_ANALYSE_MODE=2`；Weaponry 新链固定为 `file_aggregate_v1`。
7. 不写入真实 API Key、Callback URL、本机绝对路径或 E2E fixture 路径。

#### 验证

- `tests.test_analysis_classification_config`
- `tests.test_analysis_deployment_config`
- `tests.test_weaponry_production_gate`
- `tests.test_dependency_container`
- 静态搜索确认 `WEAPONRY_ANALYSE_MODE` 只存在于明确的历史说明或禁止恢复断言中。

### 7.2 `README.md`：2 个冲突块

#### 修改位置

- 目录结构；
- 请求到任务/回调的链路；
- `/llm/analysis` 行为说明；
- 当前测试基线说明。

#### 处理步骤

1. 以 `refactor/concurrency` 当前模块化目录和运行链路为事实底稿。
2. 加入 `main` 的：
   - `architecture_tree.py`；
   - `architecture_recall_service.py`；
   - Analysis Top-K 和两阶段流程；
   - 四项运行模式；
   - 召回审计；
   - 摘要、关键词、所属技术规则。
3. `report_service.py`、`weaponry_service.py` 继续标记为遗留兼容实现，公开路由不得描述为调用它们。
4. Report/Weaponry 主链写为新模块的 Submit/Dispatcher/Worker/Application。
5. Reassign 写为同步 Saga，不写成路由直接操作 AnythingLLM。
6. 保留 `/debug/*` 不随 `APP_DEBUG=false` 自动关闭的部署风险说明。
7. 不复制 `main` 的历史“666 项、14 失败、4 错误”。
8. 不复制 `refactor` 的历史通过数作为合并后结论；只在实际执行后写入新结果。
9. 明确 Analysis 尚未完成阶段 1F Dispatcher 化，当前仍是迁移期线程。

#### 验证

- 与 `app/container.py`、`app/blueprints/llm.py`、`app/modules/README.md` 人工交叉核对。
- 搜索遗留错误描述：`run_report_task`、`run_weaponry_task`、Progress action、旧 Reassign Client。

### 7.3 `app/blueprints/llm.py`：3 个冲突块

#### 修改位置

- import 区；
- `llm_analysis()`；
- 路由测试依赖的 Report/Weaponry/Reassign/Progress 代码区域。

#### 目标结构

以 `refactor/concurrency` 文件为主体，只移植 `main` 的 Analysis 导入和 `llm_analysis()` 行为。

#### 必须移植的 Analysis 能力

1. 从 `analysis_service` 导入：
   - `MAX_ANALYSIS_PARAMS_PER_REQUEST`；
   - `MAX_ANALYSIS_REQUEST_BYTES`；
   - `run_file_analysis_task`；
   - `run_file_analysis_batch_task`；
   - `validate_analysis_architecture_ranges`。
2. 导入领域树校验错误和文件任务受理错误。
3. 有 `Content-Length` 时在 JSON 解析前检查 64 MiB。
4. 无 `Content-Length` 时最多读取上限加一个字节。
5. 顶层必须为 JSON 对象，拒绝非有限数字和非法 Unicode。
6. `params` 必须为非空数组，最多 32 项，所有元素必须是对象。
7. 在产生任何任务、Progress 或线程前验证所有 `fileName`、`filePath` 和树范围。
8. 同一批请求内 `fileName` 不得重复。
9. 使用 `create_file_tasks_if_available()` 一次事务受理整批。
10. 每个任务发布初始 Progress 后只创建一个批量线程。
11. 单文件传 `execution_id`，批量传 `fileName -> execution_id` 映射。
12. 透传四项 Analysis 运行模式。

#### 必须保留的 refactor 路由

- Report Parser、Submit 和 202 空体 Presenter；
- Weaponry Parser、Document Scope、Submit 和 Presenter；
- check-task 中 Report/Weaponry Callback Guard 同步恢复；
- Progress 请求适配、连接注册和单写入出口；
- Reassign Parser → Application → Presenter；
- Chat、title、history、abort、delete。

#### 禁止事项

- 不恢复旧 `run_report_task`、`run_weaponry_task` 导入。
- 不在 Report/Weaponry 路由中创建 `threading.Thread`。
- 不恢复路由内 AnythingLLM Reassign 操作。
- 不修改 Analysis 当前 202 JSON 响应；202 空体留待阶段 1F。
- 不修改 check-task 当前 JSON 响应；200 空体留待既定后续阶段。

#### 最小验证

- `tests.test_routes`
- `tests.test_report_contract`
- `tests.test_weaponry_contract`
- `tests.test_stage1e0_reassign_contract_assets`
- `tests.test_progress_and_check_task`
- `tests.test_chat`
- 路由 AST 门禁：Report/Weaponry/Reassign 不得直接构造线程、Client 或 SQLite Repository。

### 7.4 `app/container.py`：8 个冲突块

#### 修改位置

- import 区；
- `ApplicationServices` dataclass；
- `ApplicationServices.__post_init__()`；
- `create_application_services()`；
- 生命周期 `start_background_dispatchers()`、`close()`；
- 最终日志。

#### 必须增加的 main 能力

1. 导入 `AnalysisClassificationConfig` 和 `load_analysis_classification_config()`。
2. 在 `ApplicationServices` 增加不可变 `analysis_classification_config` 字段。
3. 在 `__post_init__()` 校验其精确类型。
4. 创建服务时加载并记录四项 Analysis 模式。
5. `document_rag_factory` 显式使用 `analysis_rag_workspace_settings()`。
6. `knowledge_index_factory` 显式使用 `knowledge_index_workspace_settings()`。

#### 必须保留的 refactor 能力

- Report Submit、Callback Recovery、Dispatcher 和 Infrastructure Config；
- Weaponry Application Services、Dispatcher、production gate 和资源恢复；
- Reassign Application Services 和 `single_instance` fail-fast；
- Chat Store、Run Dispatcher、Cleanup Dispatcher 和容量配置；
- Progress Hub、连接注册表和共享 `UploadTaskLimiter`；
- 注入 `services` 时不构造生产依赖、不启动 Dispatcher。

#### 初始化顺序

1. 读取并验证 Chat、Report、Weaponry、Reassign 运行模式；
2. 读取 Analysis 配置并 fail-fast；
3. 再读取 AnythingLLM/OCR/LLM 外部配置；
4. 创建 SQLite 服务；
5. 创建通用 Factory；
6. 组合 Report、Weaponry、Reassign 和 Chat；
7. 构造唯一 `ApplicationServices`；
8. 由 Flask App 生命周期启动允许启动的 Dispatcher。

关键原则：无效运行模式必须在 HTTP Client、数据库文件或后台线程产生前失败。

#### 最小验证

- `tests.test_dependency_container`
- `tests.test_analysis_classification_config`
- `tests.test_analysis_deployment_config`
- `tests.test_report_dispatcher`
- `tests.test_weaponry_dispatcher`
- `tests.test_reassign_application`
- `tests.test_chat`
- `tests.test_architecture_boundaries`

### 7.5 `app/services/llm_service/task_service.py`：3 个冲突块

这是本次集成最高风险文件，不得按冲突块机械拼接。

#### 修改位置

- 模块常量和异常类型；
- `LLMTaskService._init_db()`；
- `_row_to_task()`；
- execution/Guard/资源内部辅助方法；
- 文件任务受理方法；
- 召回审计方法；
- Callback 领取、CAS 和补发方法；
- 通用任务读取方法。

#### 最终 Schema 并集

必须同时存在：

```text
llm_tasks
llm_interactions
llm_interaction_attempts
llm_interaction_lifecycle_events
llm_task_executions
callback_delivery_guards
callback_guard_release_audits
report_resource_records
weaponry_task_document_snapshots
llm_architecture_recall_decisions
```

`llm_tasks` 还必须包含：

```text
execution_id
callback_status
callback_attempts
last_callback_error
callback_claim_id
callback_claim_expires_at
```

#### 必须保留的 refactor 方法

- `_immediate_connection()`；
- `create_task_execution_if_allowed()`；
- `claim_task_execution()`；
- `update_task_execution_progress_if_current()`；
- `finish_task_execution_if_current()`；
- `defer_accepted_task_execution()`；
- `get_task_execution()` / `get_task_by_execution_id()`；
- `acquire/validate/complete/release_callback_delivery_guard()`；
- `freeze_expired_callback_delivery_guards()`；
- Report Resource Record 的创建、恢复和延迟方法；
- Dispatcher 队列检查与 accepted ID 列举。

#### 必须加入的 main 方法

- `create_file_tasks_if_available()`；
- `require_current_execution()`；
- `claim_callback_delivery()`；
- `upsert_architecture_recall_decision()`；
- `finalize_architecture_recall_decision()`；
- `get_architecture_recall_decision()`；
- 对应召回审计规范化、摘要和幂等辅助方法。

#### Guard 边界

- `callback_claim_id/expires_at` 只服务于 `business_type=file` 的迁移期 Callback 交接。
- `callback_delivery_guards` 服务于 Report/Weaponry execution 级 Guard。
- 两者不能共享状态机、租约字段或人工解除接口。
- file claim 的过期只允许有界接管，不等价于外部 Callback 一定未送达。
- Report/Weaponry 的 timeout/disconnect 继续收敛为 `delivery_outcome_unknown`，不能被 file
  claim 的普通 failed 语义覆盖。

#### 事务边界

- 批量文件受理使用一个 `BEGIN IMMEDIATE`；任一业务键冲突时整批零写入。
- JSON 序列化、UUID 创建和输入规范化在获取写锁前完成。
- `require_current_execution()` 是外部副作用前门禁，不替代最终状态写入的 CAS。
- 所有远端调用、Callback、下载和文件处理在事务外执行。

#### 最小验证

- `tests.test_task_service`
- `tests.test_report_task_adapter`
- `tests.test_report_callback_guard`
- `tests.test_weaponry_task_adapter`
- `tests.test_weaponry_stage1d6`
- `tests.test_task_callback_recovery_application`

### 7.6 `docker/.env.docker`：1 个冲突块

处理方式与 `.env.example` 一致：

- 增加四项 Analysis 配置；
- 保留单实例、Dispatcher、production gate 和指纹配置；
- 不恢复 `WEAPONRY_ANALYSE_MODE`；
- 保留 `APP_DEBUG=false`；
- 不写入真实密钥。

验证 `docker/deploy/README-OFFLINE.md` 中的容器重建命令和回滚说明与最终变量一致。

### 7.7 `tests/test_dependency_container.py`：1 个冲突块

最终测试必须同时覆盖：

- Analysis Factory 每次租约创建独立 Transport；
- Analysis/Knowledge Workspace Policy 显式分离；
- Analysis 配置 fail-fast；
- Report/Weaponry/Reassign/Chat 组合实例唯一；
- Report 与 Weaponry 共用受控 limiter；
- 注入服务创建 App 不构造生产依赖；
- Container 源码不直接构造共享 Session；
- Blueprint 模块导入时不创建服务。

不得删除 refactor 已有的实例一致性、Dispatcher 生命周期或 Reassign 组合根断言来迁就 main
旧对象图。

### 7.8 `tests/test_routes.py`：3 个冲突块

#### 处理原则

1. 保留 refactor 的 Report、Weaponry、Progress、Reassign 路由测试。
2. 加入 main 的 Analysis 严格校验、原子受理、execution 映射和单批量线程测试。
3. 删除或改写 main 中针对旧 Report/Weaponry 路由线程的测试。
4. 删除 main 中直接 Mock 旧 Reassign AnythingLLM Client 的成功测试；以 refactor 的
   Application/Fake/契约资产替代。
5. 保证每个测试显式注入临时服务，不启动生产 Dispatcher 或真实 HTTP。

#### 必测路由行为

- Analysis 非对象 JSON、超限、非法树、重复文件、混合 params、同键并发和 Callback 交接 409；
- Report 202 空体、活动任务/Guard 409、无线程；
- Weaponry 202 空体、严格 ArchitectureId、文档范围冻结；
- check-task file claim 与 Report/Weaponry Guard；
- Progress action 错误且连接保持；
- Reassign 成功和全部稳定错误投影；
- Chat 协议不变。

---

## 8. 六个自动合并文件的强制语义审查

### 8.1 `app/ports/rag.py`

最终 `RagPromptKind` 必须是两侧枚举的并集，并增加：

- `MAX_RAG_FRESH_CONVERSATION_SWITCHES`；
- `DocumentRagSession.start_fresh_conversation()`；
- `DocumentRagSession.ask_optional()`。

`REPORT_GENERATION` 不能因采用 main 文件而丢失。Protocol、Fake 和生产 Gateway 的签名必须一致。

### 8.2 `app/services/core/config.py`

必须同时保留：

- Analysis 四类模式和硬上限；
- Report Infrastructure Config；
- Chat `single_instance` Config；
- 原有 AnythingLLM、OCR 和 LLM Config。

所有布尔值、整数和有限浮点校验不得把 `bool` 当作普通整数接受。

### 8.3 `app/services/core/prompts.py`

最终应包含：

- Analysis 分类、数据标准、抽取、repair、reselect Prompt；
- 最新 summary/keyword/relatedTechnology 规则；
- Report Prompt；
- Chat Title Prompt；
- 通用 JSON/Architecture Repair Prompt。

最终不应包含已经迁入 Weaponry Domain 的旧字段、Chunk、Table、Terms Rule Prompt 构建器。

### 8.4 `docs/接口文档/文件处理和报告生成.md`

人工合并以下内容：

- main 的 Analysis 32 项、64 MiB、完整树、Top-K、两阶段、四项模式、摘要和关键词规则；
- refactor 的 Analysis 202 空体“已批准但 1F 尚未实现”说明；
- refactor 的 Report 严格 Parser、128 位 reportId、202/409 和 Callback Guard；
- refactor 的 check-task 当前/目标双状态；
- refactor 的 Progress 无 action 契约；
- Weaponry 类型 check-task/Progress 和 Callback 说明。

该文件允许解决两个分支已经存在的文档冲突，但不得借机新增字段。如发现超出两侧既有内容的
新契约，立即停止并确认。

### 8.5 `tests/test_rag_port_contract.py`

最终同时覆盖：

- Analysis Prompt Kind；
- Report Prompt Kind；
- fresh conversation 最多两次；
- optional ask 的受控失败开放；
- 会话失败不得回退到旧 Thread；
- Source 身份和不可变 trace；
- 知识库 Port 的并发幂等。

### 8.6 `tests/test_task_service.py`

最终同时覆盖：

- 文件批量原子受理；
- file callback claim；
- 旧 execution CAS；
- 召回审计；
- `llm_task_executions`；
- Report/Weaponry Guard；
- 资源记录；
- 旧库迁移和重复初始化。

自动合并后测试方法数增加不代表测试语义完整，必须按上述能力逐项建立映射。

---

## 9. main 单边 Analysis 文件的接入检查

下列文件通常不会产生文本冲突，但会直接进入最终树，必须逐个检查与 refactor 基础设施的兼容性。

### 9.1 领域树与召回

| 文件 | 检查重点 |
| --- | --- |
| `app/services/core/architecture_tree.py` | 正整数/字符串 ID、重复 ID、有限树边界、环、深度/字符/JSON 上限、不可变 DTO、LRU 并发冷构建 |
| `app/services/llm_service/architecture_recall_service.py` | 输入信号有界、BM25/tree/rule/RRF、候选不超过 128、Prompt 预算、无正文审计 |
| `scripts/benchmark_architecture_recall.py` | 输出有界、不打印正文、门禁退出码、父节点 gold 语义 |

### 9.2 AnythingLLM 通用集成

| 文件 | 检查重点 |
| --- | --- |
| `app/integrations/anythingllm/factory.py` | 每次租约新建 Transport；Analysis 与 Knowledge 默认 Policy 分开 |
| `app/integrations/anythingllm/policies.py` | `analysis_rag_workspace_settings()` 与 `knowledge_index_workspace_settings()` 独立返回字典 |
| `app/integrations/anythingllm/rag_gateway.py` | primary/active Thread 身份、最多两次切换、可选失败、trace 和清理 |
| `app/ports/__init__.py` | 新 DTO/枚举导出完整，不导出测试 Fake |
| `tests/fakes/rag.py` | 与最终 Protocol 一致，未声明调用 fail-fast |

### 9.3 Analysis 应用主链

文件：`app/services/llm_service/analysis_service.py`。

重点锚点：

- `validate_analysis_architecture_ranges()`；
- `_compose_analysis_keywords()`；
- `_sanitize_related_technologies()`；
- `_log_analysis_content_warnings()`；
- `_submit_callback()`；
- `_finalize_file_failure()`；
- `_execute_file_analysis_task()`；
- `run_file_analysis_task()`；
- `run_file_analysis_batch_task()`。

必须确认：

1. `download_to_temp_file()` 使用 refactor 的流式下载、大小上限、总时限和原子替换实现。
2. `post_callback_payload()` 使用 refactor 的严格 2xx、响应关闭和原子历史文件创建实现。
3. 永久知识库存储使用 refactor 已修正的 prepared-document 身份冲突校验。
4. Interaction Audit 使用 Schema v3，且审计失败阻断永久存储。
5. 每个远端副作用前调用 `require_current_execution()`。
6. 最终任务写入继续使用 execution CAS，不能只依赖前置检查。
7. `relatedTechnologyEvidence` 不进入 Callback。
8. 非关键内容质量不合格只记录 warning；领域分类、审计、资源或身份失败仍 fail closed。
9. Analysis 批量中文件按顺序执行，但每个文件使用独立 RAG 和 Knowledge Factory 租约。
10. 完整树只在本地校验、召回和存储使用，不存在发送完整大树的降级路径。

---

## 10. refactor 单边模块的保留检查

### 10.1 Tasks

- `app/modules/tasks/**` 必须完整保留。
- Progress Hub 是当前单实例内存通知，不得被描述为跨实例事实源。
- `LocalPersistentTaskDispatcher` 是兼容 Dispatcher，不是 RabbitMQ 可靠队列。
- `running` 孤儿任务仍需明确报告当前恢复边界，不得因合并 Analysis claim 而宣称已有完整 Reaper。

### 10.2 Report

- `app/modules/report/**` 完整保留。
- Report 使用自己的 AnythingLLM Adapter，不改用 Analysis `DocumentRagSession` 取代已验证链路。
- 空 RAG 结果、审计原子性、Artifact、Callback Guard 和资源恢复行为保持当前实现。
- `RagPromptKind.REPORT_GENERATION` 只用于通用审计类型，不改变 Report Adapter 自己的业务 DTO。

### 10.3 Weaponry

- `app/modules/weaponry/**` 完整保留。
- Evidence Selection、Provided-Evidence、来源身份和 TABLE 行身份不能因 main 旧 Prompt 回流而弱化。
- 真实 provider/embedding/document-processing/extraction 指纹未冻结时继续 fail-fast。
- timeout/disconnect 继续隔离为 `OUTCOME_UNKNOWN`。

### 10.4 Reassign

- `app/modules/reassign/**` 完整保留。
- `reassign_operations`、`reassign_steps`、`reassign_events`、workspace preparation claim 和恢复观测表
  位于知识库数据库边界，不得被 `LLMTaskService` 合并误删。
- 文档删除继续在一个 `BEGIN IMMEDIATE` 中检查活动 Saga 并删除。
- `reserved/running/compensating/recovery_required` 继续阻止删除。
- 不启动后台恢复线程；诊断脚本默认只读。

---

## 11. SQLite 增量迁移实施方案

### 11.1 测试输入数据库

所有迁移测试使用临时目录或脱敏副本，不直接修改开发机 `.runtime`。

必须准备四类输入：

1. 共同基点数据库：只有 2026-07-12 时代基础表；
2. main 数据库：包含文件 execution、callback claim 和召回审计；
3. refactor 数据库：包含 `llm_task_executions`、Guard、Report Resource、Weaponry Snapshot；
4. 空数据库：验证全新安装。

### 11.2 每类数据库的验证步骤

1. 初始化前记录：
   - 文件 SHA-256；
   - 表清单；
   - `sqlite_master` 建表/索引 SQL；
   - 每表行数；
   - 关键业务行摘要。
2. 使用最终 `LLMTaskService` 初始化一次。
3. 验证全部目标表、列和索引存在。
4. 验证原有行值、业务键、execution 和 Callback 状态未改变。
5. 再初始化一次，验证幂等。
6. 对新增能力写入一条临时测试数据并完整回滚或删除临时数据库。

### 11.3 必须新增或保留的迁移测试

- 旧 `llm_tasks` 自动补齐 `execution_id`，且唯一索引可创建；
- callback claim 两列缺失时幂等增列；
- `llm_architecture_recall_decisions` 增量创建；
- refactor 四类表和所有索引不丢失；
- 初始化过程中任一 SQL 失败时事务回滚；
- 重复初始化不重复插入 migration marker；
- 不通过删除/重建 `llm_tasks` 迁移。

### 11.4 并发和 CAS 验证

1. 50 个线程同时提交同一 `fileName`：只允许一个成功。
2. 50 个不同 `fileName`：全部受理，execution 不重复。
3. 批量中任一键冲突：整批零新增。
4. 旧 execution 更新 Progress、成功、失败、知识库或 Callback：全部拒绝。
5. file claim 有效期间：第二发送者和同名新任务均拒绝。
6. file claim 过期：一个接管者成功，其余失败。
7. Report/Weaponry Guard 不受 file claim 影响。
8. Callback Guard timeout unknown 不得被普通失败覆盖。
9. 数据库持续 locked/busy：返回稳定内部繁忙结果，不启动线程和外部调用。

---

## 12. 测试实施计划

### 12.1 通用静态检查

```powershell
rg -n "^(<<<<<<<|=======|>>>>>>>)" . -g '!venv/**'
git diff --check
git status --short
```

还必须运行现有架构测试，确认：

- Domain/Application/Ports 依赖方向；
- Flask 路由不构造具体业务基础设施；
- Report/Weaponry/Reassign 不回流遗留 Worker；
- Analysis 新代码不把 Flask Request/Response 传入业务服务；
- 内部执行身份不进入 Presenter 或 Callback DTO。

### 12.2 M3：Analysis Port、Tree、Recall、Prompt、Gateway

```powershell
venv\Scripts\python.exe -B -m unittest `
  tests.test_architecture_tree `
  tests.test_architecture_recall_service `
  tests.test_architecture_recall_benchmark `
  tests.test_analysis_prompts `
  tests.test_analysis_classification_config `
  tests.test_analysis_deployment_config `
  tests.test_anythingllm_policies `
  tests.test_anythingllm_rag_gateway `
  tests.test_rag_port_contract `
  tests.test_dependency_container -q
```

### 12.3 M4：任务、迁移、Callback 和并发

```powershell
venv\Scripts\python.exe -B -m unittest `
  tests.test_task_service `
  tests.test_task_check_application `
  tests.test_task_callback_recovery_application `
  tests.test_report_task_adapter `
  tests.test_report_callback_guard `
  tests.test_report_callback_recovery `
  tests.test_weaponry_task_adapter `
  tests.test_weaponry_stage1d6 -q
```

### 12.4 M5：Analysis 主链和路由

```powershell
venv\Scripts\python.exe -B -m unittest `
  tests.test_analysis_service `
  tests.test_analysis_two_stage `
  tests.test_analysis_scope_guard `
  tests.test_analysis_identity_reselect `
  tests.test_routes `
  tests.test_progress_and_check_task `
  tests.test_dependency_container -q
```

### 12.5 Report 回归

```powershell
venv\Scripts\python.exe -B -m unittest discover `
  -s tests `
  -p "test_report*.py" `
  -q
```

并单独运行：

```powershell
venv\Scripts\python.exe -B -m unittest `
  tests.test_report_contract `
  tests.test_report_application `
  tests.test_report_dispatcher `
  tests.test_report_resource_recovery `
  tests.test_report_runtime_adapters -q
```

### 12.6 Weaponry 回归

```powershell
venv\Scripts\python.exe -B -m unittest discover `
  -s tests `
  -p "test_weaponry*.py" `
  -q

venv\Scripts\python.exe -B -m unittest discover `
  -s tests `
  -p "test_stage1d*.py" `
  -q
```

### 12.7 Reassign 回归

```powershell
venv\Scripts\python.exe -B -m unittest discover `
  -s tests `
  -p "test_reassign*.py" `
  -q

venv\Scripts\python.exe -B -m unittest `
  tests.test_stage1e0_reassign_contract_assets `
  tests.test_architecture_boundaries -q
```

### 12.8 Chat 与 Progress 回归

```powershell
venv\Scripts\python.exe -B -m unittest discover `
  -s tests `
  -p "test_chat*.py" `
  -q

venv\Scripts\python.exe -B -m unittest `
  tests.test_progress_and_check_task `
  tests.test_progress_connection_registry `
  tests.test_progress_request_adapter `
  tests.test_in_memory_progress_adapter -q
```

### 12.9 安全全仓测试

不得直接把原始 `unittest discover` 结果称为“全量通过”。正式执行时应：

1. 动态发现全部 TestCase ID；
2. 打印完整排除列表和原因；
3. 检查排除 ID 的真实前缀，避免把 `test_*` 错写成 `tests.test_*`；
4. 排除可能启动本地 `run.py`/Shell 的环境测试；
5. 排除仍依赖 `.gitignore` 本地 fixture 的资产测试；
6. Windows 下排除无法表达 POSIX `0640` 的单项断言；
7. 检查 main 新增 PDF 后，原先 fixture 排除项是否仍然成立；
8. 将剩余用例分成有界批次执行；
9. 最终报告真实统计。

报告格式：

```text
DISCOVERED=<发现总数>
EXCLUDED=<排除数>
SELECTED=<实际执行数>
PASSED=<通过数>
FAILURES=<失败数>
ERRORS=<错误数>
SKIPPED=<跳过数>
```

任何批次失败后先定位根因，不得通过扩大排除范围获得“全绿”。

### 12.10 真实 E2E 边界

- main 的三文件 E2E 是历史证据，基于另一对象图，不能替代合并后验证。
- 6,822 节点真实树不在受控仓库输入中时，不得声称完成真实大树复核。
- 本计划默认不启动 `run.py`，不连接真实 AnythingLLM。
- 如果需要真实 E2E，必须另行确认：
  - 服务和模型版本；
  - 完整领域树来源；
  - Callback 地址；
  - 临时 workspace/thread/document 命名；
  - 数据清理和保留方案；
  - 失败后是否允许补偿。

---

## 13. 文档、配置和二进制资产

### 13.1 权威接口文档

除共同修改的 `文件处理和报告生成.md` 外，其余接口文档应按分支来源精确保留：

- `main` 与 `refactor` 相同文件不应产生无关格式化差异；
- `refactor` 新增的接口 README、知识谱系解析和分类节点变更文档必须保留；
- `/llm/reassign` 文档除已确认实现状态外不应因本次合并变化；
- 合并结束后对 `docs/接口文档/` 逐文件执行 diff 审查。

### 13.2 重构和更新记录

- 保留双方全部历史文档；
- 本文作为本次执行主计划；
- 旧 `260722-refactor与analysis优化分支合并实施计划.md` 标记为历史输入，不删除；
- 实施完成后在 `docs/更新记录/` 新增实际执行记录，写入真实冲突处理和测试结果。

### 13.3 PDF 资产

必须只保留下列已批准对象：

| 文件 | 字节数 | Git blob | main 当前模式 | 目标模式 |
| --- | ---: | --- | --- | --- |
| `测试文件/GJB 9001C-2017.pdf` | 538,144 | `80b26d48a2d8a59647dddb32400f3eeb077fa4cf` | `100644` | `100644` |
| `测试文件/Gerald R Ford (CVN 78) class (CVNM)-14-Jul-2023.pdf` | 665,361 | `ee6c32c1ba565fedbe4e8058ff5864cc75e28735` | `100755` | `100644` |
| `测试文件/Nimitz (CVN 68) class (CVNM) 16-Aug-2023.pdf` | 639,843 | `fcd1324b430c0e874ca880ace65278e3d3722e42` | `100755` | `100644` |

文件模式规范化只去除错误的可执行位，不得修改 blob 内容。最终必须计算 SHA-256，并与直接
从 `main` Git 对象导出的文件逐一比对。

---

## 14. 风险矩阵和停止条件

| 风险 | 级别 | 检测 | 控制 |
| --- | --- | --- | --- |
| 整文件选边导致 Report/Weaponry/Reassign 回退 | 阻断 | 路由/Container/AST diff | 以 refactor 为架构底稿，逐能力移植 |
| Analysis 最新 7 文件增量遗漏 | 阻断 | `a6dec0a..main` 专项 diff | 单独验收 summary/keyword/technology |
| `task_service.py` Schema 丢失 | 阻断 | 四类旧库迁移和 `sqlite_master` 对比 | 只做增量迁移，禁止删除重建 |
| file claim 与通用 Callback Guard 混用 | 阻断 | 并发/过期/unknown 测试 | 分表、分方法、分业务状态机 |
| main 旧 Reassign 路由恢复 | 阻断 | AST、路由测试 | 只保留 Parser/Application/Presenter |
| main 旧 Progress action 恢复 | 阻断 | WS 契约测试 | 以 refactor 已实现行为为准 |
| Analysis 完整树重新进入 Prompt | 阻断 | Prompt 长度和调用 trace | 无候选时 fail closed，不做完整树降级 |
| 旧 execution 继续产生副作用 | 阻断 | CAS/并发/外部调用计数 | 每个副作用前复核，最终写入再 CAS |
| 审计失败后仍永久存储 | 阻断 | 故障注入 | 审计提交为硬前置 |
| Container 初始化产生共享 Session | 高 | 生命周期与源码测试 | Factory 按任务租约创建 Transport |
| 自动合并恢复旧 Weaponry Prompt | 高 | Prompt 符号清单和 AST | 只保留 Domain Prompt |
| 测试排除前缀错误而启动 `run.py` | 高 | 打印真实 Test ID | 运行前人工核对排除列表 |
| 主线在集成期间继续变化 | 高 | PR 前再次 fetch | 对交叉核心文件重新虚拟合并 |
| PDF 内容或模式异常 | 中 | blob、大小、SHA-256、mode | 只改 mode，不改内容 |
| 历史测试结果被写成新基线 | 中 | README/执行记录审查 | 只记录本次实际运行数据 |

出现以下任一情况必须停止：

1. 新的公开接口字段、状态码、Header、SSE 或 WS 语义；
2. 需要访问真实 `.runtime` 或生产数据库才能继续；
3. 需要启动 `run.py` 或真实后台服务才能解决代码冲突；
4. 需要重放结果未知的 Callback、AnythingLLM 写入或 Reassign 补偿；
5. 发现用户未提交改动与集成文件重叠；
6. 分支 tip 与冻结值不一致；
7. 数据库迁移需要删除表、删除列或重写历史审计；
8. 安全全仓只能通过扩大排除项才能通过。

---

## 15. 回滚方案

### 15.1 Merge commit 前

确认当前确实处于本次集成分支的 merge 状态后：

```powershell
git merge --abort
```

原始两个分支和主工作区不会改变。

### 15.2 已提交但未共享

- 保留必要的测试和 diff 证据；
- 删除独立集成工作树或分支前确认没有用户提交；
- 重新从固定 `main` 创建集成分支；
- 不使用 `git reset --hard` 处理共享或不明确工作区。

### 15.3 已经推送或进入 PR

- 使用普通修复提交；
- 不 force push；
- 若方向整体错误，关闭 PR 后从固定基线重建。

### 15.4 已经合入 main

由于第一父提交是 `main`，使用：

```powershell
git revert -m 1 <merge_commit>
```

数据库变化必须向前兼容。代码回滚后新增表、列和审计记录可以保留，不能要求破坏性删除。
对于外部副作用结果未知的任务，继续保留原隔离状态，不因代码回滚盲目重放。

---

## 16. Merge commit、PR 和主线漂移

### 16.1 Merge commit 前最终检查

```powershell
git status --short
git diff --check
git diff --cached --stat
git diff --cached --name-status
git diff --cached
```

逐项确认：

- 没有冲突标记；
- 没有真实密钥和本机路径；
- 没有未批准二进制；
- 没有接口字段增删；
- 没有旧 Report/Weaponry/Reassign 路由回流；
- 测试报告是本次真实结果。

### 16.2 建议提交信息

```text
merge: 将并发重构阶段 0-1E 集成到最新 main
```

提交正文至少包含：

- 两个父提交 SHA；
- 8 个冲突文件和 22 个冲突块的处理摘要；
- Analysis 最新能力保留说明；
- Report/Weaponry/Reassign 新架构保留说明；
- SQLite 增量迁移说明；
- 接口参数集合未增删；
- 实际测试命令、发现/排除/执行/结果数量；
- 未执行真实 AnythingLLM E2E 的边界。

### 16.3 PR 前主线漂移复核

```powershell
git fetch origin main
git rev-list --left-right --count origin/main...HEAD
git log --left-right --cherry-pick --oneline origin/main...HEAD
```

- 主线未变化：进入最终人工审查。
- 只有无交叉文档变化：合并最新 main 后重跑受影响门禁。
- 修改 `llm.py`、`container.py`、`task_service.py`、接口文档或 Analysis 主链：重新生成完整
  虚拟合并和契约审查。
- 集成分支已经共享后不得通过 rebase 改写历史。

---

## 17. 完成定义

只有同时满足以下条件，才允许把本次集成标记为完成：

1. 执行前固定并记录了两个 tip 和共同基点。
2. 最终 merge commit 的第一父提交是最新 main，第二父提交是 refactor/concurrency。
3. 8 个文本冲突文件全部人工解决，22 个冲突块均有处理依据。
4. 6 个自动合并文件完成语义审查。
5. main 的 Analysis Top-K、两阶段、四项模式、召回审计、文件 claim 和最新内容规则全部保留。
6. refactor 的 Tasks、Report、Weaponry、Reassign、Progress 和 Chat 边界全部保留。
7. Report、Weaponry、Reassign 公开路由没有回流遗留实现。
8. 最终 Schema 同时包含两侧全部目标表、列和索引。
9. 四类 SQLite 输入完成增量迁移和重复初始化验证。
10. 旧 execution、Callback claim 和通用 Guard 的并发/故障测试全部通过。
11. 权威接口文档与最终实现当前/目标状态一致。
12. 没有增加、删除或重命名任何前后端接口参数。
13. 三份 PDF blob 内容不变，目标模式均为 `100644`。
14. 所有定向测试和安全全仓测试零新增失败、零新增错误。
15. 测试报告列出发现、排除、执行、通过、失败、错误和跳过数量。
16. 未启动 `run.py`；真实 E2E 未执行时明确说明。
17. 未把 SQLite 离线验证描述为可靠队列、多实例或生产容量验收。
18. 最终 diff、提交说明和 PR 描述均完成人工复核。

---

## 18. 执行记录模板

实施时在 `docs/更新记录/` 新增执行记录，并至少填写：

### 18.1 基线

```text
main_tip=
refactor_tip=
merge_base=
integration_branch=
merge_commit=
```

### 18.2 冲突处理

| 文件 | 预期冲突块 | 实际冲突块 | 处理结论 | 复核人/证据 |
| --- | ---: | ---: | --- | --- |
| `.env.example` | 1 |  |  |  |
| `README.md` | 2 |  |  |  |
| `app/blueprints/llm.py` | 3 |  |  |  |
| `app/container.py` | 8 |  |  |  |
| `app/services/llm_service/task_service.py` | 3 |  |  |  |
| `docker/.env.docker` | 1 |  |  |  |
| `tests/test_dependency_container.py` | 1 |  |  |  |
| `tests/test_routes.py` | 3 |  |  |  |

### 18.3 数据库迁移

| 输入类型 | 初始化一次 | 重复初始化 | 表/索引完整 | 原行不变 | 结果 |
| --- | --- | --- | --- | --- | --- |
| 共同基点库 |  |  |  |  |  |
| main 库 |  |  |  |  |  |
| refactor 库 |  |  |  |  |  |
| 空库 |  |  |  |  |  |

### 18.4 测试结果

| 测试层 | 发现/选择数 | 通过 | 失败 | 错误 | 跳过 | 备注 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Analysis 定向 |  |  |  |  |  |  |
| Task/迁移/Callback |  |  |  |  |  |  |
| Report |  |  |  |  |  |  |
| Weaponry |  |  |  |  |  |  |
| Reassign |  |  |  |  |  |  |
| Chat/Progress |  |  |  |  |  |  |
| 安全全仓 |  |  |  |  |  |  |

### 18.5 未验证边界

- 是否启动 `run.py`：默认否；
- 是否连接真实 AnythingLLM：默认否；
- 是否执行真实三文件 E2E：默认否；
- 是否验证多实例：否；
- 是否验证生产吞吐：否；
- 是否生成 Weaponry production attestation：按实际记录。

---

## 19. 推荐实际执行顺序

最终推荐顺序如下：

1. M0 固定 refs、契约 Hash 和冲突范围；
2. M1 创建独立集成工作树；
3. M2 产生 merge 冲突现场；
4. 先审查 main 单边 Analysis 文件和依赖，不改业务路由；
5. M3 合并 Port、Policy、Gateway、Config、Prompt；
6. M4 合并 `LLMTaskService` 和四类数据库迁移；
7. M5 合并 Container；
8. M5 移植 Analysis 路由并保留其他 refactor 路由；
9. 解决两个冲突测试文件并运行最小测试；
10. M6 合并 README、环境配置、接口文档和 PDF 模式；
11. M7 依次运行 Analysis、Task、Report、Weaponry、Reassign、Chat/Progress 回归；
12. 运行安全全仓测试并记录真实统计；
13. M8 人工审查最终 staged diff；
14. 创建单个 merge commit；
15. M9 再次检查 `origin/main` 漂移；
16. 推送集成分支并创建 PR；
17. PR 合入前再次核对接口文档、数据库迁移和回滚说明。

任何里程碑未通过时停留在当前阶段，不得以“后续再修”方式跨过阻断门禁。
