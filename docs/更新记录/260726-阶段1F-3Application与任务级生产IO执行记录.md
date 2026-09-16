# 阶段 1F-3 Application 与任务级生产 I/O 执行记录

> - 执行日期：2026-07-26
> - 对应设计：[阶段 1F 文件分析高内聚收口文件级实施设计](../重构记录/260726-阶段1F文件分析高内聚收口文件级实施设计.md)
> - 实施范围：只完成 1F-3 的未接线 `TaskId` Application 与任务级 I/O Adapter；不实施批量受理、资源恢复、Callback、Dispatcher、Container 或路由切换
> - 公开契约结论：未修改 `docs/接口文档/`，未改变 `/llm/analysis`、file 类型 `/llm/check-task`、Progress、callback、前后端接口参数、请求/响应字段、状态码或 Header

## 1. 完成内容

### 1.1 只按 TaskId 运行的 Application

- 新增 `RunAnalysisTask`，入口只接收内部 `TaskId`，不接受 HTTP 请求对象、文件路径、Prompt、
  Session 或 callback 参数。它先领取任务，再只读取受理期冻结的 `AnalysisTaskInputV1`；所有 Progress
  与终态写入都使用 expected TaskId 条件写。
- 缺失、未领取或首次 expected TaskId 进度门禁判定 stale 的 execution，会在创建工作目录、RAG
  会话和永久知识库写入之前停止。已经进入文件准备后才发现 stale 的任务级文件，由后续 1F-6
  资源记录按明确归属处理，当前阶段不执行无恢复事实的递归删除。Factory 进入失败只收敛一次任务
  终态；业务终态已写入后的 Transport 退出异常只记录日志，不会反向写失败终态。
- 调用顺序固定为：文件准备 → 召回审计 reserve → 任务级 RAG → 分类/抽取 → 召回 finalize →
  交互审计 → 永久知识库 → 可降级翻译 → 终态 → RAG close。callback 载荷只写入已有任务结果投影，
  本阶段不发送 HTTP callback，避免与遗留 Worker 双发。

### 1.2 审计、知识库和 RAG 的 fail-closed 收口

- 召回审计 reserve 必须先于 RAG Factory；reserve 失败时不会创建远端会话或写永久知识库。
- 交互审计提交是永久知识库写入的硬门禁。调用结果不确定时明确标记审计失败、禁止重试和清理，
  保留 RAG 文档/会话现场等待后续恢复。
- 知识库 Adapter 将既有结果区分为 confirmed、known-not-applied 和 outcome-unknown：已确认未接管时
  写失败终态；结果未知时保留文档现场，不猜测补偿或删除。RAG close 结果未知同样保留现场。
- 分类/抽取继续复用 1F-1 Domain。JSON repair 会计入阶段模型调用预算；combined 阶段已用尽预算时
  不会发出第三个 architecture repair；数据标准强制回退沿用旧链路的快照和约束语义。

### 1.3 任务级遗留 I/O Adapter

- `LocalAnalysisTaskWorkspaceAdapter` 为每个 execution 创建独立目录；
  `LegacyAnalysisFilePreparationAdapter` 将下载、规范化、OCR/MinerU 缓存限制在该目录。路径越界、
  非文件产物或错误 execution 均 fail closed。
- `LegacyAnalysisRagAdapterFactory` 每次创建独立旧 RAG Transport 租约；Adapter 显式映射
  `open → execute → close`，首次操作绑定文档，阶段隔离对话同步到不可变 SessionRef，且不维护任何
  进程全局 task/session 映射。
- `LegacyAnalysisKnowledgeAdapter` 和 `LegacyAnalysisAuditAdapter` 分别复用既有知识索引与 SQLite
  审计服务，保留 execution、文档引用、来源、attempt、lifecycle 和 Receipt 的关联校验。
- 既有注入式翻译 Adapter 继续仅以单进程协调器串行共享 Translator/MinerU；翻译失败可降级为空展示
  字段，但不能覆盖知识库已提交事实。

## 2. 接口、接线与运行边界

- `git diff -- docs/接口文档/ app/blueprints/llm.py` 为空；本轮没有需要确认的公开接口修改，也没有
  增删任何前后端接口参数。
- 新 Application、Factory 和 Adapter 没有接入 Flask Blueprint、Container、当前后台线程、Dispatcher
  或生产路由；`app/services/llm_service/analysis_service.py` 仍是唯一生产文件分析执行链。
- 未启动 `run.py`，未连接真实 AnythingLLM、模型、OCR、callback 或其他后台服务。所有 I/O 验证使用
  临时目录、临时 SQLite、严格 Fake 或替身 Transport。
- 当前 SQLite `single_instance` 运行约束不变。本轮不代表可靠任务队列、Task Attempt 自动恢复、
  多实例 fencing、分布式锁、数据库一致性或生产高并发容量已经实现。

## 3. 验证结果

| 检查项 | 结果 |
| --- | --- |
| Application 与生产 Adapter 严格回归 | 15 项通过 |
| 1F-3 Application、Port、Codec、Web、翻译、Domain、黄金与架构组合回归 | 82 项通过 |
| `test_analysis*.py` 定向发现 | 245 项通过 |
| `test_architecture_boundaries` 复跑 | 21 项通过 |
| 新增/变更 Analysis Python 静态编译 | 通过 |
| 安全全仓动态发现 | 发现 1,686 项；精确排除 13 项后执行 1,673 项，12 个无重叠批次全部通过，0 failure / 0 error / 0 skipped |
| `git diff --check` | 通过；仅保留既有 Windows LF/CRLF 提示 |
| `docs/接口文档/` 与生产 Blueprint 差异 | 0 文件 |

安全全仓排除集合在执行前固定并验证为 13 项：7 项 `LocalScriptTests` 可能启动本地 Shell、文件服务或
`run.py`；5 项 `LLMTestAssetsTests` 依赖被 `.gitignore` 排除的个人联调夹具；
`test_migrate_analysis_security.AnalysisSecurityMigrationTests.test_apply_is_idempotent_and_preserves_callback_metadata_and_audit`
断言 Windows 无法稳定表达的 POSIX `0640` 权限位。排除项没有计入通过数，也没有因测试失败扩大集合。

## 4. 后续阶段硬门禁

1. 1F-4 才能实现批量原子受理、持久任务输入与顺序协调；不得让当前未接线 Application 代替生产任务
   受理或绕过现有 409 语义。
2. 1F-6 才能引入资源记录、Callback Guard、HTTP 投递、同步恢复和可判定清理；outcome-unknown 仍须
   保持现场，不能通过自动重试伪造成功。
3. 1F-5A/1F-5B 之前不得把新链路接入 Container、Dispatcher 或公开路由；切换前必须完成旧活动任务、
   Guard 和资源的只读预检，并确认没有新旧双执行。
4. 真实 AnythingLLM/OCR/模型联调、外部故障演练、可靠队列、多实例和容量验收继续属于后续受控阶段；
   离线 SQLite 结果不能替代这些证明。

> 2026-07-26 全面审查后的异常审计、阶段 Conversation、close 三态幂等、stale 门禁及精确审计
> 查回修正，见《阶段 1F-3R Application 与任务级 I/O 审查修复执行记录》。原始验证计数保留为
> 1F-3 当时快照，不应替代 1F-3R 的复验结果。
