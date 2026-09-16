# 阶段 1H-1：共享处理内核与本地 Artifact 执行记录

## 0. 执行结论

阶段 1H-1 已完成并通过离线门禁：

- 将原扁平 `domain.py`、`ports.py` 重组为包，并通过包根兼容导出保持 Legacy Office 既有调用；
- 建立不可变 `ArtifactRef/Metadata`、`ProcessingProfile`、`ProcessingRequest/Result`、
  `LineageEvent`、稳定步骤键、Artifact ID 和错误分类；
- 建立 `DocumentProcessorPort`、`ArtifactStorePort`、`ProcessingRecordPort`、
  `ResourcePort` 及严格 Fake；
- 实现 `PrepareDocument` 幂等编排、本地原子 Artifact Store 和模块自有 SQLite Processing
  Record；
- 阶段 1 共用物理 `llm_tasks.sqlite3`，但不导入遗留 `TaskService`，不关联遗留任务表外键；
- 完成 160 项阶段定向与扩大回归，失败 0、错误 0、跳过 3；
- 未运行 `run.py`，未调用真实 LibreOffice、AnythingLLM、MinerU、OCR、浏览器、模型或 Callback；
- 未修改 `docs/接口文档/`，未增加、删除或重命名任何公开参数。

1H-1 没有接入任何生产调用方；Legacy Office 纳入通用 Processor/Artifact 谱系属于 1H-2。

---

## 1. 稳定内核

### 1.1 Domain

新增 `app/modules/document_processing/domain/`：

- `models.py`：冻结文档表示、Artifact 角色、Profile、请求、结果与 Lineage；
- `errors.py`：区分 Artifact、Processing Record、确定冲突和结果未知；
- `legacy_office.py`：承接原 `domain.py`，保持既有行为；
- `__init__.py`：提供兼容导出。

关键约束：

1. Domain 不含 `Path`、SQLite Row、Flask Request 或供应商响应；
2. Profile 只接受可稳定 JSON 序列化的参数，字段集合和枚举严格反序列化；
3. 步骤键由 task、逻辑步骤、源 Artifact/SHA-256 与 profile 推导；
4. Artifact ID 由步骤键、角色、表示与序号推导，不包含任务明文或宿主路径；
5. 非成功结果不能暴露尚未获得数据库成功事实所有权的 Artifact/Lineage。

### 1.2 Ports 与严格 Fake

新增：

- `app/modules/document_processing/ports/processing.py`；
- `tests/fakes/document_processing.py`。

Application 只通过 `ArtifactContent.open_reader()` 使用二进制流，不读取本地路径。严格 Fake 要求
测试预先登记精确调用；任何额外调用、参数漂移或未消费期望均立即失败，防止重复 Processor、
Artifact 发布或记录写入被宽松 Mock 隐藏。

---

## 2. Application 与故障语义

`PrepareDocument` 的顺序为：

1. 计算稳定步骤键并向 Processing Record 领取 claim；
2. 已成功记录只在 Artifact 完整性通过后复用；
3. running、failed、outcome_unknown 均不调用 Processor；
4. 领取成功后在数据库事务外执行 Processor；
5. 在数据库事务外流式发布并复核 Artifact；
6. 构造 Lineage；
7. 在一个短事务中提交 Artifact 元数据、Lineage 与步骤成功状态。

关键故障语义：

- Processor 或发布确定失败：持久化 `failed`；
- 外部结果本身未知：持久化 `outcome_unknown`；
- Artifact 已发布而成功记录提交失败：保留确定性文件，尝试标记 `outcome_unknown`，禁止盲删、
  盲重试或向调用方暴露成功 Artifact；
- 失败状态也无法提交：对外只返回模块内 `outcome_unknown`，不虚假承诺可安全重试；
- Application 日志只记录任务内部 ID、步骤/Artifact/摘要前缀、大小和耗时，不记录宿主路径或正文。

---

## 3. 本地 Artifact Store

`LocalArtifactStoreAdapter` 实现：

- TaskId 完整 SHA-256 命名空间；
- 真实路径解析、Windows `\\?\` 等价前缀规范化和包含关系校验；
- 同目录随机 `.part`、流式 SHA-256/size、文件 `fsync`、`os.replace` 和发布后复核；
- 单实例内同 Artifact 串行化；
- 相同确定性身份与相同内容幂等复用，内容不同 fail closed；
- `verify/open_reader/delete_if_owned` 所有权与完整性校验；
- 符号链接逃逸拒绝。

该 Adapter 只证明当前单实例本地能力。阶段 3 的 MinIO Adapter 需要使用对象存储条件写与共享
数据库所有权，不能把这里的进程内锁解释为跨实例 fencing。

---

## 4. Processing Record 存储边界

`SQLiteProcessingRecordAdapter` 在调用方显式传入的 `llm_tasks.sqlite3` 中独立创建：

- `document_processing_steps`；
- `document_processing_artifacts`；
- `document_processing_lineage`。

边界：

1. 不导入或调用遗留 `TaskService`；
2. 不对 `llm_tasks` 等遗留表建立外键；
3. 每次调用创建独立 SQLite 连接，启用 WAL、foreign key、NORMAL synchronous 和 30 秒
   busy timeout；
4. 领取、失败、未知与成功提交只使用短 `BEGIN IMMEDIATE`；
5. 成功提交中的 Artifact 元数据、Lineage 和步骤状态同事务写入；
6. 事务中没有文件、subprocess、网络、OCR 或大文件复制。

未来迁移已经同步到总计划和滚动计划：

- 阶段 2 纳入 Attempt/Step/Checkpoint、lease/fencing；
- 阶段 3 映射共享 MySQL 同步 SQLAlchemy UoW，并把内容迁至 MinIO；
- 阶段 4 将异步处理步骤事实与 Outbox 派发意图同事务提交。

---

## 5. 验证结果

所有命令使用 `venv\Scripts\python.exe -B`。

### 5.1 1H-1 与既有基线

覆盖 Domain、Ports/Fake、Application、Artifact、Processing Record、架构和 1H-0 基线。

```text
Ran 29 tests
OK (skipped=1)
```

跳过项是当前 Windows 权限不允许创建测试符号链接；路径规范化、包含校验和正常并发发布仍已执行。

### 5.2 Legacy Office 与业务扩大回归

覆盖 Analysis Production Adapter、Legacy Office 配置/转换/交付、MHTML、OCR、Translation、
Report I/O/Runtime 和 Analysis/Report 契约资产。

```text
Ran 131 tests
OK (skipped=2)
```

合并去重后本阶段共执行 160 项，失败 0、错误 0、跳过 3。ERROR/WARNING 日志来自故障注入，
包括 Processor/审计/Callback/清理/结果未知路径，不是测试失败。

### 5.3 静态门禁

- Domain/Application AST 禁止导入 Flask、SQLite、subprocess、Translator 或具体 Adapter；
- SQLite Adapter AST 禁止导入遗留 `TaskService`；
- `compileall` 通过；
- `git diff --check` 通过，仅有 Git 的预期 CRLF 提示；
- 1H-0 公开接口文档只读 Hash 门禁通过。

---

## 6. 阶段边界与下一步

1H-1 已经形成可复用的 `Domain → Application → Ports ← Adapters` 骨架，但尚未证明：

- Legacy Office、MHTML、MinerU/OCR 或 Translation 已通过新用例运行；
- 现有业务调用方已经切换；
- SQLite 多实例安全、可靠队列、MySQL/MinIO 或跨实例 fencing 已完成；
- 真实供应商与生产容量已验收。

本阶段复核未发现需要新增公开字段、改变接口契约或扩大已确认存储边界的事项，可以进入 1H-2，
按原计划将现有 Legacy Office 安全内核纳入通用 Processor 与 Artifact 谱系。
