# 阶段 1F-3S 文件分析 Application 等价拆分执行记录

> - 实施日期：2026-07-26
> - 实施状态：已完成
> - 前置阶段：1F-3、1F-3R
> - 后续阶段：1F-4 批量原子受理与顺序协调
> - 公开契约唯一权威：`docs/接口文档/`
> - 实施性质：仅 Application 包内的机械等价拆分

## 1. 结论

1F-3S 已把 `app/modules/analysis/application/run_analysis.py` 从拆分前的 2,162 行收敛为 473 行
顶层 Facade。`RunAnalysisTask` 的公开导出、构造关键字参数、`execute(TaskId)`、既有私有
`_knowledge_idempotency_key` 入口、Port 调用顺序、Prompt/预算、审计、知识库、翻译、终态和
fail-closed 语义均保持不变。

未修改 `docs/接口文档/`、`app/blueprints/llm.py`、公开路由、请求/响应参数、状态码、Header、
Progress、Callback、Port、DTO、Codec、数据库 Schema、配置或默认值。未启动 `run.py`，未连接真实
AnythingLLM、模型、OCR、Callback 或其他后台服务。

## 2. 改造内容

| 文件 | 职责 | 说明 |
| --- | --- | --- |
| `app/modules/analysis/application/run_analysis.py` | Facade | 保留公开类型重导出、依赖校验、领取/顶层编排、RAG Factory 作用域和兼容幂等键入口。 |
| `app/modules/analysis/application/workflow_models.py` | 共享模型 | 承载公开结果/异常类型、`_AnalysisWorkflowPlan` 与单次 execution 的 `_RagWorkflowState`。 |
| `app/modules/analysis/application/model_workflow.py` | 模型工作流 | 原样迁入计划构建、Prompt、分类/repair/重选、RAG 调用、结果映射与 recall payload。 |
| `app/modules/analysis/application/audit_lifecycle.py` | 审计生命周期 | 原样迁入 recall reserve/finalize、interaction 审计、已审计 Session close。 |
| `app/modules/analysis/application/knowledge_handoff.py` | 知识库与翻译 | 原样迁入永久知识写入、三态现场保留、翻译可降级处理和幂等键纯实现。 |
| `app/modules/analysis/application/failure_convergence.py` | 失败收敛 | 原样迁入 expected TaskId Progress/终态写、latest owner、通知、pre-RAG/RAG failure 收敛。 |

协作器只保存构造时注入的 Port 或纯协作器引用；execution、plan 和 state 均按调用参数传递。没有新增
全局缓存、按 TaskId 的进程内表、锁、重试、线程、队列或异步任务。

## 3. 等价性证据

### 3.1 拆分前冻结

- 拆分前 `run_analysis.py`：2,162 行，SHA-256 为
  `9070EE7AF87604154303C95EB5255EC0AA889805C0172E8CA1B38F6861DEEBB8`。
- 拆分前的 Application/Adapter/Port/Task/Gateway/架构联合离线基线为 193 项，全部通过。
- 新增 `tests/fixtures/analysis_application_1f3s_happy_trace.json`，冻结单候选成功路径的 31 个
  Port 操作、RAG operation/attempt/Prompt digest、recall payload 稳定字段、交互 attempt digest、
  知识库分类/文档引用和幂等键；只排除由单调时钟产生的 `recall_elapsed_ms` 数值抖动。

### 3.2 自动化门禁

- `test_analysis_application.py` 继续覆盖 stale、recall audit、Factory、Context-only partial open、
  interaction audit、knowledge unknown、combined 调用预算、翻译降级、close unknown、Factory exit
  和终态条件写等既有故障场景；新增公开导出/构造签名测试及结构化成功轨迹差分。
- `test_architecture_boundaries.py` 新增 1F-3S AST 门禁：Facade 不超过 700 行，只保留 5 个顶层方法；
  必须装配四个协作器；不得重新直接导入 Prompt、分类规则内部函数或结果映射；协作器不得反向导入
  `run_analysis`，且必须直接拥有各自的 Port 算法。
- 所有新增模块显式使用 `app.modules.analysis.application.run_analysis` logger 名称，保持原日志分类。

## 4. 验证结果

| 验证项 | 结果 |
| --- | --- |
| `python -B -m unittest tests.test_analysis_application` | 12 项通过。 |
| 1F-3R 相关联合回归（Application、Adapter、Port、Task、RAG、架构） | 196 项通过。 |
| `python -B -m unittest discover -s tests -p "test_analysis*.py" -q` | 253 项通过。 |
| Application `compileall` | 通过。 |
| `git diff --check` | 通过。 |
| `git diff -- docs/接口文档 app/blueprints/llm.py` | 空差异。 |
| 动态安全全仓回归 | 发现 1,696 项，精确排除 13 项，执行 1,683 项；13 个无重叠批次全部为 0 failure / 0 error / 0 skipped。 |

安全全仓排除项保持既有范围：7 个会触发本地 Shell/服务的 `test_local_scripts.*`、5 个依赖被
`.gitignore` 排除本地样例的 `test_test_assets.*`，以及 1 个 Windows 不适用的 POSIX 权限位断言。
本阶段没有因拆分新增或扩大排除项。

测试输出中的审计、RAG、翻译和 SQLite 异常日志来自严格 Fake/故障注入断言，是预期分支，不是对真实
后台服务的连接或运行结果。

## 5. 公开接口与运行边界

- `docs/接口文档/` 保持零修改；不需要接口确认。
- 新 Application 仍未接入 `/llm/analysis`、Container、Dispatcher、Callback 或资源恢复；遗留
  `analysis_service` 仍是当前生产执行链。
- 当前运行模式仍是 `single_instance`。本次离线测试仅证明 Application 内部状态不跨 execution 共享，
  不证明可靠队列、多实例一致性、高并发吞吐或生产就绪。

## 6. 回滚与后续

本阶段未写数据库、未创建外部资源、未切换路由。若需要回滚，只需将上述 Application 内部模块和对应
测试/门禁恢复为 1F-3R 的单文件实现；不得删除 1F-3R 已修复的 partial-open 审计、attempt 隔离、
unknown close、stale 目录门禁和精确审计查回语义。

下一阶段为 1F-4，仅可在再次确认现有黄金与本记录的差分资产保持一致后，开始批量原子受理与顺序协调。
