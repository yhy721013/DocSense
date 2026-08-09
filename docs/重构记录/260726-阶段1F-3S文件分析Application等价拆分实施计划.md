# 阶段 1F-3S 文件分析 Application 等价拆分实施计划

> - 文档状态：已完成
> - 制订日期：2026-07-26
> - 前置阶段：1F-3、1F-3R 已完成
> - 后续阶段：1F-4 批量原子受理与顺序协调
> - 基线分支：`main`
> - 基线提交：`a6c1332`（PR #79 已合并；实施前还必须冻结当前 1F-3R 工作区）
> - 公开契约唯一权威：`docs/接口文档/`
> - 阶段性质：只做内部代码等价拆分，不改变任何已有功能

## 1. 结论

1F-3S 安排在 1F-3R 之后、1F-4 之前，专门拆分当前 2,162 行的
`app/modules/analysis/application/run_analysis.py`。本阶段只允许把已经存在的方法、内部状态和依赖
按职责机械搬移到 Application 层内部协作器；不允许借拆分之名修改算法、Prompt、判断条件、调用顺序、
异常语义、日志语义、Port 契约、持久化 Schema 或公开接口。

本阶段完成后：

- `RunAnalysisTask` 仍是唯一文件分析执行入口，构造参数和 `execute(TaskId)` 行为保持不变；
- 原有模型工作流、审计生命周期、知识转交、失败收敛分别由无跨任务可变状态的内部协作器承担；
- `run_analysis.py` 只保留公开 Application 类型、顶层顺序编排和必要的兼容转发；
- 所有现有黄金、故障、调用顺序和安全全仓测试必须原样通过；
- 新增差分轨迹与永久结构门禁，防止后续重新膨胀。

如果任何测试差异必须通过改变业务期望才能通过，应立即停止 1F-3S，而不是更新黄金结果接受差异。

### 1.1 实施结果（2026-07-26）

- 已将拆分前 2,162 行的 `run_analysis.py` 收敛为 473 行 Facade，并新建计划/状态、模型工作流、
  审计生命周期、知识/翻译和失败收敛五个包内模块；没有保留两套可执行算法。
- 已新增结构化成功轨迹资产，冻结 31 个 Port 调用、RAG Prompt digest、recall payload 稳定字段、
  interaction attempt、知识库幂等键和文档引用；单调时钟产生的 `recall_elapsed_ms` 单独按动态值处理。
- 已新增签名/导出、Facade 规模、禁止导入、协作器反向依赖和真实 Port 算法 AST 门禁。
- Application 12 项、1F-3R 联合 196 项、`test_analysis*.py` 发现 253 项以及安全全仓 1,683 项均通过；
  安全全仓动态发现 1,696 项，精确排除既有 13 项，0 failure / 0 error / 0 skipped。
- `docs/接口文档/`、`app/blueprints/llm.py`、Port、Schema、配置与公开契约均为零修改；实际证据见
  `docs/更新记录/260727-阶段1F文件分析整合执行记录.md`。

## 2. 当前实现事实

### 2.1 当前规模

截至 1F-3R，`run_analysis.py` 共 2,162 行，`RunAnalysisTask` 主要方法规模如下：

| 职责 | 代表方法 | 当前规模 |
| --- | --- | ---: |
| 顶层领取与编排 | `execute`、`_execute_in_rag_factory`、`_execute_with_rag` | 313 行 |
| 模型与分类抽取 | `_run_model_workflow`、分类、重选、抽取、RAG 查询 | 536 行 |
| 计划构建 | `_build_plan`、召回 payload | 279 行 |
| 知识与翻译 | `_persist_knowledge`、`_enrich_translations` | 109 行 |
| 审计与 RAG 收口 | recall、interaction、close 相关方法 | 181 行 |
| 失败、终态与 Progress | failure、finish、expected 写、latest-wins | 300 行以上 |

行数只用于说明拆分必要性，不作为业务正确性的替代证据。拆分后总行数可能因模块说明、类型标注和中文
注释略有增加，但单个文件不得再次同时承担上述全部职责。

### 2.2 当前生产边界

- 新 `RunAnalysisTask` 尚未接入公开 `/llm/analysis` 路由；生产仍由旧执行器负责。
- 1F-3S 不接入路由、Container、Dispatcher、Callback 或资源恢复。
- 当前仍是显式 `single_instance`，离线拆分测试不代表多实例或生产容量已经完成。
- 1F-6 仍负责资源记录与 Callback 闭环；1F-5B 才允许执行唯一生产链切换。

## 3. 不可改变的等价性契约

### 3.1 公开与模块入口

以下内容必须保持完全兼容：

1. `app.modules.analysis.application` 的 `__all__` 名称和导入方式不变。
2. `app.modules.analysis.application.run_analysis` 仍可导入：
   `RunAnalysisTask`、`RunAnalysisResult`、`RunAnalysisOutcome`、`AnalysisTaskCompletion`、
   `AnalysisApplicationContractError`、`AnalysisTaskPersistenceError`。
3. `RunAnalysisTask.__init__` 参数名、必选性、默认值和注入对象不变。
4. `RunAnalysisTask.execute(TaskId)` 的输入、返回类型、outcome 和异常类型不变。
5. 现有测试使用的 `_knowledge_idempotency_key` 保留兼容入口，结果必须逐字节相等。
6. 不修改 `docs/接口文档/`、`app/blueprints/llm.py`、请求/响应字段、状态码、Header、Progress、
   Callback、SSE 或 WebSocket 格式。

### 3.2 算法和调用顺序

以下执行顺序必须保持不变：

```text
claim
-> expected TaskId 初始 Progress 门禁
-> 创建任务目录
-> 文件准备
-> 构建有效范围与确定性召回计划
-> 召回审计 reserve
-> RAG Factory / open / upload / bind
-> 分类第一阶段与有限 JSON repair
-> 必要的身份受限重选
-> 抽取第二阶段与有限 JSON repair
-> 现有字段校验、回退和结果映射
-> 召回审计 finalize
-> 交互审计硬门禁
-> 永久知识库写入
-> 翻译富化或既有可降级处理
-> expected TaskId 单终态
-> RAG close 或现场保留
```

不得改变：

- 分类模式、候选范围、节点选择、rank、范围保护、数据标准和 Jane 规则；
- Prompt 文本、拼接顺序、字符限制、JSON 序列化参数和 digest 输入；
- 单阶段及整文件模型调用预算、repair 次数与计数时点；
- RAG request 的 operation、prompt、session/document/execution 身份和幂等键；
- 映射字段、空值、回退值、知识库 metadata、翻译输入输出和 recall payload；
- `time`、hash、序列化和 Port 调用的次数及时点。

任何 Prompt、规则、精度、性能或重试“优化”均不属于 1F-3S，必须另立需求。

### 3.3 失败、审计与副作用

以下故障语义必须保持不变：

- stale、missing、not-claimed 在原位置停止，且不增加任何副作用；
- 召回审计失败禁止创建 RAG Session；交互审计失败禁止永久知识入库；
- Context-only 部分创建失败仍写失败审计并保留可恢复 lifecycle；
- 每次 RAG execute 只消费本次新增 attempt，不串用前一阶段 response/sources；
- unknown 外部副作用继续 fail closed，保留现场，不自动重放或删除；
- knowledge `committed`、`known_not_applied`、`local_pending`、`retention_required`、
  `outcome_unknown` 的分支不变；
- 翻译失败继续按现有规则降级，不能扩大或缩小成功条件；
- close 首次结果及重复调用语义不变；成功终态后 close/factory exit 失败不能反写业务失败；
- expected TaskId、latest-wins、单终态、Progress Guard 的调用顺序、参数和返回处理不变；
- 异常类型、既有中文错误文本、日志级别、日志模板和关键字段保持不变。

协作器移动后应显式使用原 `run_analysis` logger 名称，避免仅因源码文件移动改变日志分类维度。

### 3.4 数据与并发边界

- 不增删或修改 Port、DTO、Task snapshot、Codec、数据库表、列、索引和约束。
- 不引入全局可变状态、模块级 Session、按 TaskId 的进程内状态表或后台线程。
- `_AnalysisWorkflowPlan`、`_RagWorkflowState` 仍为单次 execution 的内部状态，不得跨任务缓存。
- 内部协作器只保存构造时注入的无状态 Port 引用；execution、plan、state 必须作为调用参数传递。
- 不新增锁、重试、超时、批处理、并行执行、队列或缓存。
- 不把 SQLite、Flask、HTTP 客户端、旧 `analysis_service` 或 Adapter 实现导入 Application 层。

## 4. 目标文件与职责

目标目录仍限定在 `app/modules/analysis/application/`：

```text
application/
├── __init__.py
├── run_analysis.py
├── workflow_models.py
├── model_workflow.py
├── audit_lifecycle.py
├── knowledge_handoff.py
└── failure_convergence.py
```

| 文件 | 唯一职责 | 计划迁入的方法/类型 |
| --- | --- | --- |
| `run_analysis.py` | 公开类型、依赖装配、TaskId 顶层编排 | `execute`、RAG Factory 作用域、顶层 success/failure 分支 |
| `workflow_models.py` | 公开结果/异常类型及单次调用的计划、可变审计聚合 | 公开 Application DTO/异常、`_AnalysisWorkflowPlan`、`_RagWorkflowState` |
| `model_workflow.py` | 纯计划构建及现有模型/RAG 分类抽取流程 | `_build_plan`、payload、分类、重选、抽取、模型 RAG 调用 |
| `audit_lifecycle.py` | recall/interaction 审计及 RAG close 证据收口 | reserve/finalize、interaction persist、close audit |
| `knowledge_handoff.py` | 永久知识写入和翻译富化 | knowledge request/result 校验、translation enrich |
| `failure_convergence.py` | expected 写、Progress、失败终态和现场保留决策 | pre-RAG/RAG failure、finish、latest、guarded progress |

目标名称描述的是职责，不代表新增对外抽象。所有新协作器保持包内私有，不加入
`app.modules.analysis.application.__all__`，也不得被 Blueprint、Container 或其他业务模块直接依赖。

### 4.1 依赖方向

```text
RunAnalysisTask
├── _AnalysisModelWorkflow
├── _AnalysisAuditLifecycle
├── _AnalysisKnowledgeHandoff
└── _AnalysisFailureConvergence

内部协作器 -> Analysis Domain / Analysis Port / Tasks Domain / Tasks Port
内部协作器 -X-> Adapter / SQLite / Flask / requests / legacy analysis_service
```

协作器之间禁止形成循环依赖。共享 DTO 只放在 `workflow_models.py`；不得为减少参数而创建包含全部 Port
和可变 execution 状态的“万能 Context”对象。

## 5. 分步实施

### 5.1 1F-3S-0：冻结等价性证据

实施任何搬移前：

1. 记录 `run_analysis.py` 的行数、公开导出、构造签名、方法清单和 Application 架构扫描结果。
2. 以严格 Fake 固化成功路径完整 Port 调用轨迹，包括调用顺序、次数和请求 DTO。
3. 固化现有 11 类 Application 故障场景：stale、recall audit、factory、partial open、interaction audit、
   knowledge unknown、分类预算、翻译降级、close unknown、factory exit 和终态条件写。
4. 为 legacy、topk_single、topk_two_stage、单候选、数据标准、Jane、身份重选建立拆分前结果快照。
5. 固化 Prompt digest、recall payload、knowledge idempotency key、终态 completion 和日志事件序列。

此步骤只能新增测试/测试资产，不修改生产实现。

### 5.2 1F-3S-1：搬移内部状态

1. 将 `_AnalysisWorkflowPlan`、`_RagWorkflowState` 机械搬到 `workflow_models.py`。
2. 字段名称、顺序、类型、默认值、可变性及创建时点全部保持不变。
3. `run_analysis.py` 改为内部导入，不增加兼容别名之外的逻辑。
4. 立即运行 Application、Ports 和架构边界测试；失败则回退本小步。

### 5.3 1F-3S-2：搬移模型工作流

1. 先原样搬移 `_build_plan`、`_recall_payload`、`_direct_recall_payload`。
2. 再按原顺序搬移 `_run_model_workflow`、分类、combined 重选、抽取和 `_execute_rag`。
3. 只允许调整 `self` 所属、显式依赖参数和内部导入；不得重命名局部变量或重排条件分支。
4. 原有异常必须以相同具体类型和文本抛出；原有日志必须保持 logger、级别、模板和参数。
5. 每搬移一个方法组即运行模型黄金、Prompt、预算、RAG attempt 与 Application 轨迹测试。

### 5.4 1F-3S-3：搬移审计与知识转交

1. 将 recall reserve/finalize、interaction audit、close audit 搬入 `audit_lifecycle.py`。
2. 将永久知识写入与翻译富化搬入 `knowledge_handoff.py`。
3. 保持 audit-before-knowledge、knowledge-before-translation、terminal-before-close 的现有先后关系。
4. 保持 lifecycle/attempt 的追加顺序、receipt 校验、幂等键以及 retain/preserve 标志写入时点。
5. `RunAnalysisTask._knowledge_idempotency_key` 保留原签名并转发到同一个纯实现。
6. 运行 partial-open、audit hard gate、knowledge 四分类、translation degrade、close 三态测试。

### 5.5 1F-3S-4：搬移失败收敛

1. 将 pre-RAG/RAG failure、recall failure、expected Progress/终态写、latest 查询和通知发布搬入
   `failure_convergence.py`。
2. 原 `try/except/finally` 边界不得改变；尤其不能把 Port 调用移出原异常捕获范围。
3. 不合并看似重复的失败分支，不改变 `_safe_error_code`、`_failure_stage` 或中文错误文本。
4. 保持成功终态、失败终态、stale 返回及持久化异常的优先级。
5. 运行全部故障矩阵，并逐项比较终态次数、Progress 次数、审计次数、close 次数和保留现场标志。

### 5.6 1F-3S-5：收薄 Facade 与建立永久门禁

1. `run_analysis.py` 只保留公开类型、构造装配、`execute`、RAG Factory 作用域和顶层流程。
2. 删除已经完成迁移的重复实现，禁止长期保留新旧两套可执行算法。
3. 增加 AST 门禁：Facade 不得重新定义已迁出的模型、审计、知识或失败算法方法。
4. 增加依赖门禁：Application 所有新文件继续只依赖允许的 Domain、Port 和 Application 内部模块。
5. 更新 Application README、1F 总体设计和测试说明；实现完成后另写 `docs/更新记录/`。

## 6. 实施纪律

每个方法组必须执行“搬移前绿 -> 机械搬移 -> 定向测试绿 -> 差分检查”四步，不允许累计多个红色步骤后
统一修复。单个提交或可回退修改单元只能处理一个职责组。

允许的改动只有：

- 新建 Application 内部文件；
- 移动已有类型和方法；
- 为显式依赖传递调整内部调用表达式；
- 为保持原公开导入增加薄转发或重导出；
- 新增中文模块/职责注释、等价性测试和结构门禁；
- 删除已迁移且确认无调用的原重复代码。

禁止的改动包括：

- 修改条件表达式、布尔短路顺序、循环次数、集合排序或异常捕获范围；
- 重写、归并、抽象或“清理”业务算法；
- 修改 Prompt、JSON、hash、时间计算、idempotency key 或 payload；
- 修改 Port/DTO/Fake 宽严程度、Schema、配置或默认值；
- 增加重试、缓存、锁、异步、线程、队列或性能优化；
- 修改旧生产执行器、公开路由或接口文档；
- 为通过测试而更新现有黄金业务结果。

## 7. 验收矩阵

### 7.1 差分等价验收

对每个冻结场景比较拆分前后：

| 维度 | 等价要求 |
| --- | --- |
| 返回 | 类型、outcome、task_id、字段值完全相同 |
| 异常 | 具体类型、错误文本、抛出时点相同 |
| Port 轨迹 | 调用名称、先后、次数、请求 DTO 完全相同 |
| 模型 | Prompt、operation、预算、repair 次数完全相同 |
| 审计 | lifecycle、attempt、receipt、outcome、顺序完全相同 |
| 持久化 | Progress、completion、expected TaskId、幂等键完全相同 |
| 外部副作用 | RAG、knowledge、translation、close 次数和参数完全相同 |
| 现场 | preserve/retain/close/factory-exit 决策完全相同 |
| 日志 | 级别、logger、模板、关键字段和先后保持一致 |

差分比较应基于结构化 Fake 轨迹和稳定字段；时间耗时等天然变化值只比较调用次数、类型和合法范围，
不能用放宽全部字段的方式掩盖差异。

### 7.2 必跑测试

每个子步骤至少执行：

```powershell
venv\Scripts\python.exe -B -m unittest tests.test_analysis_application
venv\Scripts\python.exe -B -m unittest tests.test_analysis_ports tests.test_analysis_production_adapters
venv\Scripts\python.exe -B -m unittest tests.test_architecture_boundaries
venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_analysis*.py"
```

最终还必须：

1. 执行 `tests/README.md` 定义的安全全仓测试，打印 discovered、excluded、executed、failure、error、
   skipped 数量；不得启动 `run.py`。
2. 对 Application 文件执行 `compileall`、`git diff --check` 和尾随空白检查。
3. 确认 `docs/接口文档/`、`app/blueprints/llm.py`、Port、Schema 的差异均为空。
4. 扫描禁止依赖和旧方法重复定义。
5. 未获得单独授权时，不连接真实 AnythingLLM、模型、OCR、Callback 或其他后台服务。

### 7.3 结构完成门禁

- `run_analysis.py` 不超过 700 行；超过时必须说明为何仍属于顶层编排并重新评审。
- Facade 不直接导入 Prompt builder、分类规则内部函数或结果映射算法。
- 每个迁出职责只有一个生产实现，不允许 facade wrapper 与协作器各保留一份算法。
- 新协作器没有跨 execution 可变字段，没有模块级任务缓存。
- Application 依赖方向测试通过。
- `RunAnalysisTask.execute` 的严格 Fake 完整调用轨迹与拆分前快照一致。

行数门禁只防止 Facade 反向膨胀；不能为了满足 700 行而压缩中文注释、合并语句或制造低可读性代码。

## 8. 停止条件

出现以下任一情况必须立即停止并与负责人确认：

1. 现有黄金、Prompt digest、调用轨迹、异常或日志出现非机械性差异。
2. 需要修改任何公开接口、Progress、Callback 或接口文档。
3. 需要修改 Port、DTO、Task snapshot、Codec、Schema、配置或默认值。
4. 需要调整 `try/except/finally` 边界才能完成拆分。
5. 发现当前代码存在新的业务错误；应另立审查修复阶段，不能混入 1F-3S。
6. 协作器必须持有跨任务可变状态或形成循环依赖。
7. 需要新增重试、锁、并发、缓存或后台任务才能让测试通过。
8. 安全全仓出现无法证明与拆分无关的失败。

## 9. 回滚

- 1F-3S 尚未生产接线，回滚只涉及 Application 内部文件和结构测试，不涉及数据库或外部资源回滚。
- 每个子步骤独立可回退；若任一步不能保持差分等价，回退该方法组到 `run_analysis.py`。
- 禁止在回滚时删除 1F-3R 的审计、unknown、stale 或 close 语义修复。
- 不使用破坏性 Git 命令覆盖工作区；按文件和方法组恢复。

## 10. 完成定义

只有同时满足以下条件，1F-3S 才能标记完成：

- 本计划第 3 节全部不变量有自动化证据；
- 拆分前后全部稳定差分字段一致；
- 现有 Application、Analysis、架构及安全全仓回归通过；
- `run_analysis.py` 收敛为不超过 700 行的顶层 Facade；
- 新协作器职责单一、无循环依赖、无跨任务可变状态；
- 公开接口、生产路由、Port、Schema、配置均为零修改；
- `docs/更新记录/` 记录实际文件、测试数量、回滚点和未验证边界；
- 1F-4 开工前再次确认新旧算法黄金完全等价。

完成 1F-3S 只表示内部 Application 已等价拆分，不表示 1F-4～1F-7B、可靠队列、多实例或生产切换已经
完成。
