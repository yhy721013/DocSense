# 阶段 1D-3B：生产 I/O Adapter 与唯一 Schema v2 执行记录

## 1. 执行结论

阶段 1D-3B 已于 2026-07-18 完成代码实现、离线故障验证和仓库级安全回归，本波次退出门禁
通过。武器谱内部任务输入已直接收敛为唯一 Schema v2，开发期 Schema v1 的运行时编码、解码和
策略字段已经删除；同时补齐了任务级 AnythingLLM Retrieval、Provided-Evidence Extraction、
可拔除术语辅助、Translation、SQLite Interaction Audit 与 Resource Store 等生产 I/O Adapter。

这里的“生产 I/O Adapter”表示这些实现可以在后续组合根中接入真实基础设施，并不表示已经部署
或切换生产链。当前边界如下：

- `/llm/weaponry` 仍由遗留 Blueprint 和执行链处理，新 Adapter 尚未绑定公开路由或组合根；
- 没有增删、重命名或改变请求、HTTP 响应、Callback、Progress 的任何公开参数，因此本波次没有
  修改 `docs/接口文档/`；
- 没有启动 `run.py`，没有调用真实 AnythingLLM、模型、翻译服务或甲方 Callback；
- 没有启动常驻 Worker、Dispatcher 或资源恢复线程，也没有连接 RabbitMQ、Redis、MinIO 或 MySQL；
- SQLite Adapter 仅在测试临时目录创建内部表；没有迁移历史数据，也没有写入现有业务数据库；
- 真实 AnythingLLM 的 score/rank、来源字段、空 workspace 模型行为及容量仍须在 1D-6 切流门禁中
  验证，不能用本次 Fake Transport 结果替代生产联调。

## 2. 唯一 Schema v2 与确定性 Selection

### 2.1 Schema v1 直接删除

根据“当前不存在需要兼容的历史数据和旧 Worker”的批准口径，`WEAPONRY_INPUT_SCHEMA_VERSION`
固定为 `2`，`WeaponryTaskCommandCodec` 只接受和输出 Schema v2：

1. 输入快照、任务结果和测试资产统一使用版本 2；
2. 版本 1 输入被明确拒绝，不提供双读、自动升级或回退分支；
3. 删除 v1 专属 reranker、anchor、绝对相关性阈值和缺失分数策略；
4. 严格 Codec 继续执行精确键集合校验，未知字段、缺失字段、类型漂移和非有限数值均失败关闭；
5. 公开 Callback Presenter 不暴露内部 schema version，故公开契约不受影响。

执行前已对现有 SQLite 做只读核对：活动及全部 weaponry execution 中均不存在 Schema v1 输入，
因此直接删除不会使当前任务成为不可读数据。静态扫描同时确认 weaponry 运行时代码中不存在
`schema_version: 1`、版本常量回退或已删除策略字段。

### 2.2 Production profile 的实际含义

`production_profile.py` 生成的 profile 是“可审计运行策略清单”，不是精度证书。它把以下事实以
精确字段写入任务快照，并由确定性 SHA-256 摘要生成稳定 `profile_id`：

- Provider、embedding 和文档处理指纹；
- Retrieval Query 版本；
- score 语义、score/rank 协议和稳定排序规则；
- INPUT/TABLE 的候选数量；
- 正文去重策略和引用型内容拒绝开关。

相同清单必然生成相同 profile，不同指纹或策略必然生成不同 profile。排队任务和重试只读取
execution 快照，不重新读取可能已变化的环境配置。

### 2.3 合法 score 或稳定 rank

Selection 采用本阶段批准的宽松策略，不设置绝对相关性阈值、独立 reranker 或精度验收目标：

- 整批 Candidate 都有合法、有限、非布尔 score 时，按 `score` 降序和稳定原始顺序选择；
- 整批都没有 score 时，按供应商返回 rank 和稳定原始顺序选择；
- 同一批中混用有/无 score、非法 score、冲突 score、非法 rank 或重复 Candidate ID 时整批拒绝；
- 保留文档来源边界、正文质量、引用型正文拒绝和同文档精确正文去重；
- 不限制单条 Evidence 字符数、Evidence 总字符数或单文档贡献内容量。

AnythingLLM Source DTO 现在区分“score 字段缺失”和“score 字段存在但非法”。顶层与 metadata
同时提供 score 时必须一致；顶层为 `null` 时可使用 metadata 中唯一合法值；JSON 布尔值不能按
Python 数值接受。这避免非法供应商响应被静默折叠为 rank-only 或 `0`。

## 3. 任务级 AnythingLLM Retrieval Adapter

`AnythingLLMTargetEvidenceRetrievalAdapter` 为每个 TaskId 创建独立、execution-owned 的临时
workspace，并只绑定该任务冻结的文档集合：

1. 外部创建前先确认任务资源记录存在，防止已经创建资源却无处登记；
2. 同一个 TaskId 的并发 `open_scope` 只允许一个创建者，第二个调用在发起第二次网络 I/O 前拒绝；
3. workspace 创建、文档绑定和文档列表核验均通过任务/调用级独立 Client Lease，禁止跨任务共享
   可变 Client 状态；
4. 按完整规范化 location、供应商 document id/raw id 建立强映射，缺失、污染、重复绑定、ID 漂移
   或多义来源全部失败关闭；
5. 查询只接受冻结允许文档集合，Candidate 的 `document_key` 只能由已验证映射得到；
6. 显式把供应商检索阈值设为 `0.0`，防止 AnythingLLM workspace 默认阈值暗中变成 Schema v2
   未声明的绝对阈值；最终仍按合法 score 或稳定 rank 做确定性选择；
7. Candidate ID 由稳定 call、rank、document key 和正文摘要生成，相同输入重试得到相同身份；
8. 永久文档 workspace 保持零写入，所有变更只发生在任务拥有的临时 workspace。

如果 workspace 已创建但资源登记失败，Adapter 会立即尝试补偿删除；删除成功时保留原始登记错误，
删除失败则升级为 `outcome_unknown`。创建超时、连接中断、协议中断和选定 HTTP 状态同样按“可能已
发生写入”分类为结果未知，不允许 Application 将其当作明确失败后盲目重建。

## 4. Provided-Evidence Extraction Adapter

`AnythingLLMProvidedEvidenceExtractionAdapter` 每个来源调用都创建新的、空的 execution-owned
workspace/thread，只把当前来源已经通过 Selection 的完整 rows 写入 Prompt：

- workspace 必须经列表验证为空，不上传、不绑定任何业务文档；
- `ask` 显式传入空 `document_ids`，供应商回答若返回任何 workspace source 则失败关闭；
- Prompt 的 document key、字段、Evidence ID 和 rows 必须与本次 Selected Evidence 逐项同序一致；
- A 文档和 B 文档即使模型返回相同文本，也必须具有不同 workspace、thread、Evidence 身份和
  来源 trace，禁止共享会话历史；
- 不存在“回答为空/失败后重新访问目标任务 workspace”的回退路径；
- 原始回答只记录 SHA-256 摘要和字符数，不在日志或任务结果中保存完整 Prompt/正文。

外部创建前资源前置校验、创建后登记补偿及 `outcome_unknown` 分类与 Retrieval 保持一致。跨进程
崩溃后遗留资源的扫描、隔离和清理属于 1D-6 Resource Recovery，不在 Adapter 内启动后台线程。

## 5. 可拔除术语辅助与 Translation

### 5.1 Auxiliary Guidance

- `NoAuxiliaryGuidanceAdapter` 是严格零 I/O 实现；关闭
  `WEAPONRY_TERMS_RULE_CONTEXT_ENABLED` 后不会读取术语库或创建 workspace；
- `ReadOnlyTermsRuleGuidanceAdapter` 只依赖通用只读 Provider，返回通用
  `AuxiliaryGuidance`，不把术语目录或 AnythingLLM 类型泄漏给 Application；
- 术语 Provider 失败按已批准口径降级为空辅助并记录稳定错误码，不升级为主任务失败；
- 将来删除术语规则功能时，可删除 Terms Adapter 与装配开关，不需要修改 Retrieval、Extraction
  或领域编排。

### 5.2 Translation

`WeaponryTranslationAdapter` 把遗留 Translator 包装为来源级 Port。每次调用只使用传入文本，
不跨任务缓存可变结果；成功返回兼容文本，失败记录稳定错误并返回空翻译，不改变主字段成功语义。
原始文件处理生命周期不再归 Translation 所有，后续由平级阶段 1H 的共享文档处理模块承接。

## 6. SQLite Interaction Audit 与 Resource Store

### 6.1 Interaction Audit

`SQLiteWeaponryInteractionAuditAdapter` 落地 1D-3A 冻结的 `reserve -> 外部调用 -> complete`：

- task/call/attempt、输入摘要、允许文档和来源 marker 形成唯一交互身份；
- 重复 reserve 返回相同审计事实，已 complete 的调用不能重新 reserve 为第二次外部调用；
- complete 原子收敛 pending，并严格核对输出摘要、Candidate/Selected 数量及来源分类；
- 崩溃遗留 pending 可按 TaskId 有界查询，不能误报为成功；
- SQLite 损坏 JSON、非法枚举或类型漂移统一转换为稳定 Port 状态错误，不向 Worker 泄漏随机
  `JSONDecodeError`、`ValueError` 或数据库转换异常。

### 6.2 Resource Store

`SQLiteWeaponryResourceStoreAdapter` 持久化任务资源事实：

- 资源登记具备幂等键、owned/shared 所有权、版本 CAS 和身份冲突校验；
- shared 文档映射禁止任务清理，owned workspace/thread/binding 才能进入清理状态；
- cleanup lease 使用随机 token 与单调 fencing，过期或迟到 owner 不能覆盖新清理者状态；
- 显式区分 pending、cleaned、failed、cleanup_unknown 与 quarantined；
- 过期租约、可恢复扫描、损坏持久化均有稳定失败语义；
- 数据库事务中只修改事实，不执行 AnythingLLM、文件或网络副作用。

`StoreBackedWeaponryResourceRegistrar` 负责“先确认任务记录可写，再创建外部资源，创建成功后立即
登记”的统一前置。无法登记时由具体 Adapter 补偿；补偿结果未知时保留冻结事实，交由 1D-6 恢复。

## 7. 并发、故障与安全补强

本波次在实现后复查中进一步修复了下列风险：

1. 同任务并发打开 Retrieval scope 可能重复创建 workspace：增加进程内 opening reservation；
2. Client factory 或 context enter 同步失败可能永久占用 reservation：所有退出路径统一释放；
3. 外部创建成功、资源登记失败可能形成孤儿：增加即时补偿删除和 unknown 升级；
4. create 超时发生在返回 slug 之前仍可能已写入：写调用开始即标记 mutation，按 unknown 处理；
5. AnythingLLM 顶层/metadata score 冲突、布尔 score 可能被误接受：增加 DTO 协议校验；
6. workspace 文档列表重复 location 或供应商 ID 漂移可能污染来源映射：增加完整绑定身份校验；
7. SQLite 损坏数据可能产生不可预测 500：统一转换为可观测的稳定 Port 错误；
8. 任务结果 Codec 曾固定输出版本 1：改为唯一版本常量 2，并增加往返断言；
9. 50 个并发 TaskId 同时写资源与审计：验证记录、call、lease 和完成状态互不串扰。

需要保留的后续责任也已明确：外部 create 在进程崩溃且尚未来得及登记时，单靠本地 Adapter 不可能
完全消除“远端可能存在、本地尚无资源事实”的窗口。1D-6 必须使用确定性资源名称、pending
Interaction Audit 和供应商唯一查找进行核对或隔离，禁止直接盲重试创建。

## 8. 验证结果

### 8.1 定向和模块测试

- 生产 Adapter + AnythingLLM Source 协议：**33 项全部通过**；
- 1D 契约、资产、领域、Port、Codec、Adapter 和架构合并：**185 项全部通过**；
- `test_weaponry*.py` 模块发现：**146 项全部通过**；
- `test_stage1d*.py` 阶段资产发现：**40 项全部通过**；
- 架构依赖门禁：**17 项全部通过**。

关键测试覆盖 score/rank 双路径、混合/非法/冲突分数拒绝、Schema v1 拒绝、最小多文档结构样例、
任务 workspace 强来源映射、永久 workspace 零写入、A/B 来源会话隔离、零辅助 I/O、登记补偿、
创建结果未知、审计幂等、资源 CAS/lease/fencing、损坏持久化和 50 任务并发隔离。

### 8.2 仓库级安全回归

动态收集根目录测试并排除既有 4 个环境型模块后：**88 个安全模块、1050 项测试全部通过**，末次
unittest 用时 `73.548s`，退出码为 0。

安全回归继续显式排除：

- `tests.test_local_scripts`：可能启动本地脚本或 `run.py`；
- `tests.test_multilingual_translation_integration`：依赖真实离线翻译模型；
- `tests.test_migrate_analysis_security`：当前 Windows 环境不满足其 POSIX `0640` 断言；
- `tests.test_test_assets`：依赖仓库未提供的历史请求夹具。

上述排除项不是 1D-3B 新增失败。测试输出中的 WARNING/ERROR/Traceback 均为故障注入、拒绝、
恢复和 fencing 场景的预期日志，最终 unittest 结果为 `OK`。

### 8.3 静态检查

- `venv\Scripts\python.exe -B -m compileall -q app` 通过；
- 1D-3B 关键源文件和测试通过 `py_compile`；
- weaponry 运行时代码的 Schema v1/已删除策略字段静态扫描无结果；
- `git diff --check` 通过，仅输出 Windows 工作区既有 LF/CRLF 转换提示；
- 新 Adapter 未导入 Flask Blueprint，公开路由仍没有装配生产 Adapter；
- 全程未运行 `run.py`，未访问真实外部服务。

## 9. 回滚、生产状态与下一步

1D-3B 新实现尚未进入生产执行链，也没有真实外部资源、公开接口或现有数据库迁移需要回滚。
若撤销本波次，可移除未接线的 production Adapter、Schema v2 内部资产、测试与文档；遗留
`/llm/weaponry` 运行行为不会因此改变。

下一步按计划执行 1D-4 Application：使用 1D-3A Port 和本波次 Adapter 契约实现
`SubmitWeaponryTask`、`RunWeaponryTask(task_id)` 及纯编排门禁，但仍先通过严格 Fake 测试，不提前
切换公开路由。随后 1D-5 实现 Dispatcher/Worker，1D-6 补齐真实供应商联调、Callback Guard、
资源恢复和组合根/路由切换，1D-7 执行兼容清理与阶段关闭验收。

因此当前准确状态是：**1D-3B 代码与离线退出门禁完成；生产执行链未切换，阶段 1D 尚未关闭。**
