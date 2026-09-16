# `refactor/concurrency` 与 Analysis 优化分支合并实施计划

## 0. 文档信息

| 项目 | 内容 |
| --- | --- |
| 编写日期 | 2026-07-22 |
| 文档状态 | 接口决策已确认，待执行 |
| 文档层级 | L3 分支集成实施计划 |
| 集成基线 | `refactor/concurrency@2cd7ec629bdd6a879e22a3f4e2425c165636a5b7` |
| 待集成分支 | `feat/analysis-architectureId-optimization@a6dec0a721dd13a09f9579741dd19cb0e7aaaf52` |
| 共同基点 | `main@0023c4726b49791728a30b053e9da772f005e178` |
| 建议集成分支 | `codex/integrate-analysis-architecture-optimization` |
| 公开契约依据 | `docs/接口文档/` |
| 决策确认 | 2026-07-22：采用模式 A，完整采纳 C-01～C-06；C-07 三份 PDF 同意提交 |
| 运行限制 | 不启动 `run.py`；验证只使用 `/venv` 对应解释器和离线测试 |

本文只规定两个并行分支的安全集成过程。第 4 节列出的 Analysis 公开行为已于 2026-07-22
按模式 A 获得确认，C-07 三份 PDF 也已确认允许提交；本文仍不代表已经执行合并。

---

## 1. 现状与结论

### 1.1 分支拓扑

两个分支均从 `main@0023c47` 分叉，且本地分支与远端同名分支一致：

| 分支 | 独有提交 | 变更文件 | 定位 |
| --- | ---: | ---: | --- |
| `refactor/concurrency` | 7 | 314 | 阶段 0、1A、1B、1C、1D 的任务、报告、武器谱和并发重构 |
| `feat/analysis-architectureId-optimization` | 34 | 42 | Analysis 领域树索引、Top-K 召回、两阶段 RAG、审计及配套修复 |

两侧共有 14 个共同修改文件。只读 `git merge-tree` 虚拟合并确认会产生：

- 8 个冲突文件；
- 23 个文本冲突块；
- 6 个虽然能够自动合并、但必须人工做语义审查的共同修改文件。

因此，本次不能在 `refactor/concurrency` 上直接执行普通合并并立即提交，也不能对冲突文件
整体选择 `ours` 或 `theirs`。

### 1.2 总体集成原则

1. 以 `refactor/concurrency` 作为唯一架构基线。
2. 保留阶段 1A～1D 已完成的模块边界、任务事实、Callback Guard、Dispatcher、
   production gate 和测试门禁。
3. 将 Analysis 分支的领域树索引、召回、Prompt、RAG 两阶段能力和审计能力按语义移植到
   当前对象图，不恢复已经下线的 Report/Weaponry 遗留路径。
4. 对外请求和回调字段集合不增加、不删除、不重命名。
5. 状态码、参数限制、错误文本、分类规则和回调交接行为如有变化，必须先确认并同步权威
   接口文档，不能仅因为 Git 能自动合并就默认采纳。
6. 原始两个分支均保持不变；所有冲突处理都在新的集成分支完成。

---

## 2. 集成目标与非目标

### 2.1 本次目标

1. 解决超长 `architectureList` 被完整放入模型 Prompt 导致的上下文长度和分类质量问题。
2. 保留调用方继续提交完整 `architectureList` 的现有字段结构。
3. 在服务端建立不可变领域树索引和有界候选召回。
4. 将领域分类和字段抽取拆分为隔离的两阶段 RAG 调用。
5. 为召回决策、候选、Prompt 大小、返回排名和失败阶段提供可审计事实。
6. 保证 Analysis 任务仍使用任务级 AnythingLLM Transport/Session，不跨任务共享有状态客户端。
7. 保证 Report、Weaponry、Chat、Progress 和现有任务恢复能力不发生回归。
8. 形成可回滚、可复核、测试证据完整的集成提交。

### 2.2 本次非目标

1. 不实施阶段 1F 的完整 Analysis 模块化重构。
2. 不引入 RabbitMQ、Celery、MySQL、MinIO、Redis 或多实例运行模式。
3. 不启动 `run.py`，不连接未经单独批准的真实后台服务。
4. 不顺带修改 `/llm/check-task` 的目标空响应体。
5. 不修改 Report、Weaponry、Chat 的公开字段和协议。
6. 不以本次合并宣称生产分类准确率、生产并发或多实例能力已经完成验收。

---

## 3. 不可破坏的基线

### 3.1 接口基线

- `docs/接口文档/` 是 HTTP、SSE、WebSocket 和 callback 契约的唯一权威来源。
- 禁止新增、删除或重命名前后端接口参数。
- Analysis 内部的 `execution_id`、`task_id`、召回审计 ID、租约 ID 不得进入公开响应或回调。
- 除第 4 节已经确认的 C-01～C-06 外，当前状态码、错误体、批量语义和 callback 结构
  保持不变；出现新的公开变化必须再次确认。

### 3.2 架构基线

- `app/container.py` 继续作为唯一生产组合根。
- AnythingLLM Factory 只保存不可变配置；实际 Transport/Session 必须在任务作用域内创建和关闭。
- Report 和 Weaponry 继续走当前新模块链路，禁止恢复路由线程或遗留 Worker。
- 当前共享重型执行许可继续使用 FIFO `UploadTaskLimiter`，Analysis 不新增平行的无界线程池。
- SQLite 仍是当前单实例兼容事实；不得把本次合并描述为可靠队列或多实例实现。

### 3.3 数据基线

- 必须保留 `llm_tasks` 的既有公开投影语义。
- 必须保留 `llm_task_executions`、Report/Weaponry 资源、Callback Guard、Creation Intent、
  audit 和 Dispatcher 所需表、列、索引及 CAS 语义。
- 新增 Analysis 召回审计时不得通过删除重建表、覆盖旧列或更改既有数据含义完成迁移。
- 所有数据库验证只使用临时数据库或脱敏副本，不直接修改开发机 `.runtime` 数据。

---

## 4. 已确认接口决策

Analysis 分支已经把下列行为写入其接口文档和实现。它们没有增删请求字段，但属于公开校验、
状态码或业务语义变化。2026-07-22 已明确选择模式 A，C-01～C-06 全部采纳，C-07 同意
提交；实施时必须让代码、测试、配置和权威接口文档保持一致。

| ID | Analysis 分支行为 | 影响 | 已确认决策 |
| --- | --- | --- | --- |
| C-01 | `params` 最多 32 项 | 超限请求从既有行为变为同步失败 | 采纳；同步写入接口文档和路由契约测试 |
| C-02 | 整个 JSON 请求体最大 64 MiB，超限返回 HTTP `413` | 新增状态码和容量合同 | 采纳；实施时核对网关/反向代理上限不得更小 |
| C-03 | 单个 `architectureList` 最多 10,000 节点及字段/JSON 长度上限 | 新增严格输入限制和 HTTP `400` 场景 | 采纳分支现有数值；同步冻结边界测试 |
| C-04 | 非法节点、重复 ID、非法父链或成环时整批 HTTP `400` | 从历史宽松过滤变为整批拒绝 | 采纳整批原子失败语义 |
| C-05 | 默认启用 Top-K 两阶段分类、scope guard、数据标准保护和装备身份受限重选 | 改变分类决策过程和部分失败条件 | 采纳分支现有模式、默认值和分类规则 |
| C-06 | callback pending 或补发租约期间，同名 Analysis 重跑返回 HTTP `409` | 改变终态任务的再次受理窗口 | 采纳，并与 execution latest-wins/CAS 一并验收 |
| C-07 | 提交三份 PDF 作为 E2E 样例 | 增加约 1.84 MiB 二进制历史及可能的授权风险 | 同意提交分支现有三份 PDF；不得夹带其他二进制文件 |

C-07 已批准对象基线：

| 文件 | 字节数 | 功能分支 Git blob | 合并结果文件模式 |
| --- | ---: | --- | --- |
| `测试文件/GJB 9001C-2017.pdf` | 538,144 | `80b26d48a2d8a59647dddb32400f3eeb077fa4cf` | `100644` |
| `测试文件/Gerald R Ford (CVN 78) class (CVNM)-14-Jul-2023.pdf` | 665,361 | `ee6c32c1ba565fedbe4e8058ff5864cc75e28735` | 从分支中的 `100755` 规范为 `100644` |
| `测试文件/Nimitz (CVN 68) class (CVNM) 16-Aug-2023.pdf` | 639,843 | `fcd1324b430c0e874ca880ace65278e3d3722e42` | 从分支中的 `100755` 规范为 `100644` |

文件模式规范化只去除不合理的可执行位，不修改 PDF 内容。合并结果还须重新计算 SHA-256，
并与直接从功能分支对象计算的 SHA-256 逐一比对。

决策记录如下：

### 模式 A：完整采纳（已选择）

完整合入 C-01～C-06 对应代码、测试、配置和接口文档；C-07 三份 PDF 一并保留。

### 模式 B：只采纳内部优化（未选择）

只合入领域树索引、Top-K 召回、Prompt 拆分、RAG 两阶段调用和内部审计；公开请求限制、
状态码、错误文本和再次受理语义保持 `refactor/concurrency` 当前口径。未采纳的分支测试和
文档必须同步调整，不能保留与实际实现不一致的说明。本次不得按此模式实施。

### 剩余讨论事项

当前没有阻塞合并实施的产品或接口决策。下列事项属于 M0/M5/M6 的执行核验，不需要现在
重新选择方案：

1. 开工时重新核对两个远端分支 tip；如发生漂移，重新生成虚拟合并报告。
2. 核对实际网关/反向代理请求体上限不低于已确认的 64 MiB；若现有部署配置更小，按同一
   已确认合同调整部署配置，不能让代理提前返回另一套错误语义。
3. 按上述对象基线核对 C-07 的文件名、大小、Git blob 和 SHA-256；两份 `100755` PDF 只
   规范文件模式为 `100644`，不得改变文件内容，也不得夹带其他二进制文件。
4. 真实 AnythingLLM 三文件 E2E 是否重新执行不阻塞代码合并和离线验收；若后续需要执行，
   因其涉及后台服务和 `run.py`，仍须另行确认运行与清理方案。

本节原接口确认门禁已经解除。进入真实 Git 合并前仍须完成 M0 的分支 tip、工作区和冲突
范围复核；实施过程中如发现 C-01～C-07 之外的新接口变化，必须立即停止并再次确认。

---

## 5. Git 集成策略

### 5.1 推荐方式

默认采用“新集成分支 + 单次非快进合并 + 人工语义解决”的方式：

```powershell
git status --short --branch
git switch refactor/concurrency
git switch -c codex/integrate-analysis-architecture-optimization
git merge --no-commit --no-ff feat/analysis-architectureId-optimization
```

执行前必须再次确认：

```powershell
git rev-parse refactor/concurrency
git rev-parse feat/analysis-architectureId-optimization
git merge-base refactor/concurrency feat/analysis-architectureId-optimization
git ls-remote --heads origin refactor/concurrency feat/analysis-architectureId-optimization
```

若任一分支 tip 与本文记录不一致，立即停止，重新生成差异和虚拟合并报告。

### 5.2 为什么不直接 rebase 原分支

- Analysis 分支已经公开到远端，原地 rebase 会重写 34 个提交的历史。
- 多个后续修复依赖前序提交，逐提交 rebase 会重复处理相同核心文件冲突。
- 单独集成分支允许一次性形成目标树，同时保留两个原分支完整历史。

### 5.3 禁止的做法

- 禁止在 `refactor/concurrency` 上直接边解决边提交。
- 禁止使用 `git checkout --ours .` 或 `git checkout --theirs .`。
- 禁止对 `llm.py`、`container.py`、`task_service.py` 整文件选边。
- 禁止为了通过测试删除阶段 1A～1D 的架构门禁或放宽接口断言。
- 禁止使用 `git reset --hard` 作为失败回滚方式。

---

## 6. 文本冲突逐文件处理方案

### 6.1 冲突清单

| 文件 | 冲突块 | 处理原则 | 最小验证 |
| --- | ---: | --- | --- |
| `.env.example` | 1 | 保留 Weaponry/Report/Chat 当前配置，新增 Analysis 配置独立分区 | 配置加载测试 |
| `README.md` | 2 | 保留最新架构和测试基线，加入经确认的 Analysis 现状，不覆盖 1C/1D 状态 | 文档差异审查 |
| `app/blueprints/llm.py` | 3 | 保留当前 Report/Weaponry/Chat 路由，只人工移植 Analysis 路由变化 | 路由与契约测试 |
| `app/container.py` | 8 | 保留完整组合根，增量加入 Analysis Config/Factory/Policy | 容器与生命周期测试 |
| `app/services/llm_service/task_service.py` | 5 | 保留全部阶段 1A～1D Schema/事务，再合入 Analysis 原子受理、CAS、审计和租约 | 旧库升级、并发和回调测试 |
| `docker/.env.docker` | 1 | 保留 Weaponry production gate，加入经确认的 Analysis 配置 | 部署配置静态测试 |
| `tests/test_dependency_container.py` | 1 | 合并两侧能力断言，不删 Report/Weaponry/Chat 覆盖 | 定向运行本模块 |
| `tests/test_routes.py` | 2 | 合并 Analysis 新测试与当前 Report/Weaponry 路由门禁 | 定向运行本模块 |

### 6.2 `.env.example` 与 `docker/.env.docker`

1. 以 `refactor/concurrency` 内容为底稿。
2. 在独立“Analysis 分类”分区加入最终确认的配置项。
3. 保留 Weaponry 四类 fingerprint、production attestation 和 fail-fast 配置。
4. 不重新引入 `WEAPONRY_ANALYSE_MODE=2`；新 Weaponry 链已经固定为 `file_aggregate_v1`。
5. Analysis 默认值、回滚值和非法值 fail-fast 语义必须与 Config Loader 测试一致。
6. 不写入真实 API Key、绝对开发机路径或本地 E2E 资源路径。

### 6.3 `README.md`

1. 保留当前模块化单体目录结构和阶段 1D 状态。
2. `report_service.py`、`weaponry_service.py` 继续标记为遗留兼容实现，不能恢复为公开主链。
3. 增加 `architecture_tree.py`、`architecture_recall_service.py` 和 Analysis 两阶段流程说明。
4. 保留当前安全测试基线说明，并把新增 Analysis 测试计入新的实际运行结果。
5. 不把旧分支记录的“666 项、14 失败、4 错误”当作合并后可接受基线。
6. 只有模式 A 被确认时，才写入 32 项、64 MiB、HTTP `413` 等公开限制。

### 6.4 `app/blueprints/llm.py`

1. 以当前文件为底稿，完整保留：
   - Report/Weaponry 新提交用例和 202 空体 Presenter；
   - `check-task` 的 Report/Weaponry Callback Guard 恢复；
   - Progress 新控制面；
   - Chat 全部路由及冻结协议。
2. 仅在 `/llm/analysis` 区域移植：
   - 经确认的请求大小与批量数量校验；
   - `architectureList`/`architectureStandardList` 预校验；
   - 文件任务批量原子受理；
   - `execution_id` 到后台执行参数的冻结映射；
   - Analysis 分类模式配置透传。
3. 不恢复旧 `run_report_task`、旧 `run_weaponry_task` 或旧选文解析导入。
4. 保证失败发生在建任务、发 Progress 和启动线程之前。
5. 当前 Analysis 路由仍会启动 daemon thread；本次只允许在现有 1F 前置边界内修复原子受理，
   不得借此宣称完整 1F Dispatcher 已完成。
6. 检查所有成功/错误响应与第 4 节最终确认结果一致。

### 6.5 `app/container.py`

1. 保留当前 `ApplicationServices` 的所有字段和实例一致性校验。
2. 加入 `AnalysisClassificationConfig` 及 Loader，但不能覆盖 `ReportInfrastructureConfig`。
3. 保留 Report/Weaponry Dispatcher、readiness 和 production gate 生命周期。
4. Analysis 临时 RAG 与永久 Knowledge Index 使用显式、独立的 Workspace Policy：
   - `analysis_rag_workspace_settings()`；
   - `knowledge_index_workspace_settings()`。
5. Report Adapter 继续使用其已验证策略，不因 Analysis 的线程历史配置发生变化。
6. Factory 不保存 Session；每个文件、每个任务仍创建并关闭自己的 Transport。
7. 离线依赖注入测试必须能够构造应用而不启动后台服务。

### 6.6 `app/services/llm_service/task_service.py`

这是本次合并风险最高的文件，必须按数据结构和事务能力拆分处理，不能按冲突块机械拼接。

处理顺序：

1. 先保留当前 `_initialize_database()` 中阶段 1A～1D 的全部建表、增列和索引逻辑。
2. 再加入 Analysis 所需内容：
   - `llm_architecture_recall_decisions`；
   - file callback claim 列及有界租约；
   - 批量文件任务原子受理；
   - 按 `execution_id` 的当前执行校验和 CAS；
   - 召回审计的幂等创建、finalize 和失败记录。
3. 对 `llm_tasks` 的新增列使用幂等增量迁移，禁止删除重建表。
4. 检查同名方法是否表达不同语义：
   - `get_task_by_execution_id()` 必须保留；
   - `require_current_execution()` 可以作为更严格门禁新增，不能取代通用读取；
   - file 专用 callback claim 不能覆盖 Report/Weaponry 的 execution 级 Guard。
5. 批量受理必须使用单个 `BEGIN IMMEDIATE` 事务，任一业务键冲突时整批不创建。
6. 旧 execution 的进度、结果、失败和 callback 写入必须因 expected execution 不匹配而失败，
   不能覆盖新任务。
7. 召回审计只保存摘要、候选和有界 trace，不保存文件正文、完整 Prompt 或密钥。
8. 初始化和迁移至少覆盖三类临时数据库：
   - 空数据库；
   - `main@0023c47` 时代数据库；
   - 当前 `refactor/concurrency` 阶段 1D 数据库。
9. 验证迁移可重复执行，并确认所有既有表、索引和数据行保持不变。

### 6.7 两个冲突测试文件

- `tests/test_dependency_container.py`：保留 Report/Weaponry/Chat/Progress/Factory 生命周期断言，
  再加入 Analysis Config、Policy、两阶段 RAG 能力断言。
- `tests/test_routes.py`：保留当前 Report/Weaponry 202/409、无线程、Callback Guard 和 Chat 契约
  测试，再加入经确认的 Analysis 路由校验、原子受理及执行身份测试。

---

## 7. 自动合并文件的强制语义审查

以下文件即使没有冲突标记，也不能直接暂存：

| 文件 | 必查事项 |
| --- | --- |
| `app/ports/rag.py` | 同时保留 `REPORT_GENERATION`，并加入 `ARCHITECTURE_CLASSIFICATION`、`ANALYSIS_EXTRACTION`、`ARCHITECTURE_RESELECT`；新方法签名不能破坏现有 Adapter/Fake |
| `app/services/core/config.py` | 同时存在 Report 基础设施配置与 Analysis 分类配置；Loader 均 fail-fast，默认值无覆盖 |
| `app/services/core/prompts.py` | 保留 Report Prompt，加入 Analysis Prompt；不得恢复已经迁出到 Weaponry Domain 的旧字段 Prompt |
| `docs/接口文档/文件处理和报告生成.md` | 根据第 4 节确认结果人工编辑；自动合并成功不等于契约已获批准 |
| `tests/test_rag_port_contract.py` | 同时覆盖 Report Prompt Kind、Analysis 两阶段、fresh conversation 和 optional ask 失败语义 |
| `tests/test_task_service.py` | 同时覆盖阶段 1C/1D 持久化事实、Analysis 原子受理、旧执行 CAS、召回审计和 callback claim |

另外必须检查以下非共同修改但直接影响 Analysis 主链的文件：

- `app/services/llm_service/analysis_service.py`；
- `app/services/core/architecture_tree.py`；
- `app/services/llm_service/architecture_recall_service.py`；
- `app/integrations/anythingllm/factory.py`；
- `app/integrations/anythingllm/policies.py`；
- `app/integrations/anythingllm/rag_gateway.py`；
- `tests/fakes/rag.py`；
- `scripts/benchmark_architecture_recall.py`。

审查重点是：阶段调用预算、fresh Thread 隔离、失败分类、审计先后顺序、Transport 关闭、
旧执行停止副作用、完整树只在本地使用，以及任何降级路径都不能重新把完整树发送给模型。

---

## 8. 推荐实施波次

### 波次 M0：冻结输入与决策

1. 确认两个远端 tip 未变化。
2. 记录共同基点、分支 tip、变更文件和虚拟合并结果。
3. 核对第 4 节模式 A 与 C-07 决策记录仍适用于当前两个 tip。
4. 确认工作区干净，没有未提交的用户改动。

门禁：当前已知接口项均已确认；如分支变化引入新的公开行为，不进入 M1，并重新发起确认。

### 波次 M1：创建集成分支并产生冲突现场

1. 从 `refactor/concurrency` 创建集成分支。
2. 执行 `git merge --no-commit --no-ff`。
3. 保存 `git status`、冲突清单和冲突块数量。
4. 不在此波次提交。

门禁：冲突范围与预期明显不一致时执行 `git merge --abort`，重新分析。

### 波次 M2：先合并稳定内部合同

1. 合并 `rag.py` 的 Prompt Kind 和两阶段 Session Port。
2. 合并 Factory、Policy 和 Gateway。
3. 合并 Analysis Config，但保留 Report/Weaponry 配置。
4. 合并领域树索引、召回服务和 Prompt。
5. 更新严格 Fake，确保未知调用立即失败。

门禁：Port、Gateway、Policy、Config 和 Prompt 定向测试全部通过。

### 波次 M3：合并持久化与任务所有权

1. 按第 6.6 节合并 `task_service.py`。
2. 完成空库和两类旧库增量迁移测试。
3. 完成同键并发受理、批量回滚和旧 execution CAS 测试。
4. 完成召回审计与 callback claim 测试。

门禁：不能丢失任何阶段 1C/1D 表、列、索引或测试；数据库锁失败不能被伪装为成功。

### 波次 M4：合并 Analysis 应用主链和路由

1. 合并 `analysis_service.py` 两阶段分类与抽取。
2. 将 `execution_id` 贯穿进度、结果、审计、永久知识库和 callback 前置校验。
3. 合并当前路由中的 Analysis 部分，保留其他业务路由。
4. 根据已确认的模式 A 落实 C-01～C-06 公开校验和响应。

门禁：候选外 ID、完整树 Prompt 降级、旧 execution 外部副作用均为零。

### 波次 M5：文档和配置收口

1. 合并 `.env.example`、Docker 配置和 README。
2. 只把已确认行为写入权威接口文档。
3. 更新重构计划状态，明确这仍不是完整阶段 1F。
4. 检查没有真实密钥、本机绝对路径或未批准二进制文件进入结果树。

门禁：代码、配置、README 和接口文档描述一致。

### 波次 M6：扩大回归与合并提交

1. 运行第 9 节全部离线验证。
2. 检查冲突标记、静态边界和 `git diff --check`。
3. 人工审查最终 merge diff。
4. 所有门禁通过后创建一次 merge commit。
5. 在本地审查通过前不推送、不创建 PR。

---

## 9. 验证计划

### 9.1 静态检查

```powershell
rg -n "^(<<<<<<<|=======|>>>>>>>)" . -g '!venv/**'
git diff --check
git status --short
```

还需运行现有 AST 架构门禁，确认：

- 新 Analysis 代码没有导入 Flask Request/Response；
- Domain/Port 不导入具体 AnythingLLM Client；
- Report/Weaponry 公开路由没有回流遗留 Worker；
- 内部执行标识没有进入公开 Presenter 或 callback DTO。

### 9.2 Analysis 定向测试

使用当前工作区的 `venv`：

```powershell
venv\Scripts\python.exe -B -m unittest `
  tests.test_architecture_tree `
  tests.test_architecture_recall_service `
  tests.test_architecture_recall_benchmark `
  tests.test_analysis_prompts `
  tests.test_analysis_classification_config `
  tests.test_analysis_deployment_config `
  tests.test_analysis_identity_reselect `
  tests.test_analysis_scope_guard `
  tests.test_analysis_two_stage `
  tests.test_analysis_service `
  tests.test_anythingllm_policies `
  tests.test_anythingllm_rag_gateway `
  tests.test_rag_port_contract `
  tests.test_task_service `
  tests.test_routes `
  tests.test_dependency_container -q
```

### 9.3 重构基线回归

至少覆盖：

```powershell
venv\Scripts\python.exe -B -m unittest `
  tests.test_architecture_boundaries `
  tests.test_progress_and_check_task `
  tests.test_report_contract `
  tests.test_report_application `
  tests.test_report_dispatcher `
  tests.test_report_callback_guard `
  tests.test_weaponry_contract `
  tests.test_weaponry_application `
  tests.test_weaponry_dispatcher `
  tests.test_weaponry_stage1d6 `
  tests.test_weaponry_stage1d7 `
  tests.test_chat -q
```

如实际模块名或测试拆分变化，以合并后测试清单为准，但不得省略对应能力。

### 9.4 SQLite 迁移与并发测试

必须新增或保留以下验证：

1. 空库初始化后同时存在阶段 1A～1D 和 Analysis 新表/列。
2. `main@0023c47` 旧库升级后原行不变。
3. `refactor/concurrency` 当前库升级后 Report/Weaponry 事实不变。
4. 初始化重复运行两次结果一致。
5. 50 个同文件名并发受理只有一个成功，其余稳定冲突。
6. 批量受理中任一文件冲突时整批零新增。
7. 旧 execution 不能更新新任务的 Progress、结果、失败或 callback。
8. callback claim 过期可接管，未过期不可双发送。
9. Report/Weaponry Callback Guard 不受 file callback claim 影响。

### 9.5 安全全仓测试

不得直接把原始 `unittest discover` 结果称为全量通过。应继续按照 `tests/README.md` 排除：

- 7 个可能启动本地 `run.py`/Shell 的环境测试；
- 5 个依赖 `.gitignore` 本地 fixture 的资产测试；
- 1 个 Windows 无法表达的 POSIX `0640` 权限断言。

合并后测试数量会增加，不预设固定通过数。最终报告必须列出：发现总数、排除项名称、排除
原因、实际执行数、通过数、失败数和错误数。

### 9.6 大树与真实服务验证边界

- 离线测试必须覆盖合成大树、10,000 节点边界、深度、重复 ID、环、预算和稳定排序。
- 6,822 节点真实领域树不在仓库内；缺少该受控 fixture 时不得声称完成真实大树复核。
- Analysis 分支已有三文件 E2E 历史记录，但它基于另一代码基线，不能替代合并后的验证。
- 如需重新执行真实 AnythingLLM E2E，必须另行确认运行方式、后台服务、数据范围和清理方案；
  本计划默认不启动 `run.py`。

---

## 10. 风险与控制

| 风险 | 级别 | 控制措施 |
| --- | --- | --- |
| 整文件选择一侧导致 1C/1D 能力丢失 | 阻断 | 当前重构分支为底稿，逐能力移植；架构和回归门禁 |
| `task_service.py` 增量 Schema 顺序错误 | 阻断 | 三类旧库迁移、重复初始化、行/索引对比 |
| Analysis 新 callback claim 与 Report/Weaponry Guard 混用 | 阻断 | file 专用条件、跨业务隔离测试 |
| 自动合并的接口文档未经确认即进入提交 | 阻断 | 第 4 节确认记录和人工文档 diff |
| RAG Prompt Kind 扩展破坏 Report Adapter | 高 | 保留全部枚举值，运行 Report RAG/Port 回归 |
| 两阶段调用复用历史 Thread 导致上下文串扰 | 高 | fresh conversation 合同、任务级 Session、失败无回退测试 |
| 完整树在降级路径重新发送给模型 | 高 | Prompt 长度门禁、AST/单测和调用 trace 断言 |
| 旧 execution 在新任务后继续写入或回调 | 高 | expected execution CAS、外部副作用前复核 |
| 新容量限制与网关限制不一致 | 高 | C-01～C-03 明确确认并核对部署配置 |
| 二进制 E2E 样例进入历史 | 中 | 按已批准 C-07 核对三份文件的名称、大小和 SHA-256，禁止夹带其他二进制文件 |
| Feature 历史失败被误当作合并基线 | 中 | 只承认合并后实际运行证据 |

---

## 11. 回滚方案

### 11.1 合并提交前

冲突解决或测试发现方向错误时，在确认当前确实处于本次集成分支的 merge 状态后执行：

```powershell
git merge --abort
```

原始两个分支不会改变，可以重新创建集成分支分析。

### 11.2 合并提交后、尚未共享

优先删除未共享的集成分支并重新创建，不修改两个原始分支。任何删除操作前必须确认分支
未推送且没有需要保留的用户提交。

### 11.3 合并提交已经共享

使用 `git revert -m 1 <merge_commit>` 创建可审计反向提交，不对共享历史执行 force push，
不使用 `git reset --hard` 回滚共享分支。

数据库变更必须保持向前兼容：即使代码回滚，新增表/列也不应要求破坏性删除；旧代码应能
忽略这些新增结构。

---

## 12. 完成定义

只有同时满足以下条件，本次分支集成才可标记完成：

1. C-01～C-07 已按 2026-07-22 模式 A 决策执行，无未确认扩展。
2. 两个原始分支和共同基点与执行前记录一致。
3. 8 个冲突文件均按本计划完成人工语义解决。
4. 6 个自动合并文件均完成强制语义审查。
5. 结果树中不存在冲突标记、真实密钥、未批准绝对路径或未批准二进制文件。
6. Report、Weaponry、Chat、Progress 和阶段 1A～1D 架构门禁全部通过。
7. Analysis 大树索引、召回、两阶段 RAG、审计、原子受理和旧 execution CAS 测试通过。
8. 三类 SQLite 数据库升级和重复初始化测试通过。
9. 安全全仓测试按真实数量报告，零新增失败、零新增错误。
10. 权威接口文档与最终实现完全一致，且没有增删任何前后端接口参数。
11. 未启动 `run.py`；如另行批准真实 E2E，其环境、数据、结果和清理证据独立记录。
12. 最终 merge diff 经人工复核后，才创建 merge commit、推送和 PR。

---

## 13. 建议提交与交付记录

建议最终使用一个明确的 merge commit：

```text
merge: 集成 Analysis 领域树 Top-K 两阶段分类优化
```

提交说明至少包含：

- 两个父提交 SHA；
- 接口决策采用模式 A，完整采纳 C-01～C-06；
- C-07 三份 PDF 已批准提交，最终树未夹带其他二进制文件；
- 8 个冲突文件的解决摘要；
- SQLite 增量迁移说明；
- 实际测试命令、数量与结果；
- 未执行真实 AnythingLLM E2E 时的明确边界；
- 已知剩余问题和后续阶段 1F 的责任。
