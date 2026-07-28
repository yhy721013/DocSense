# `main` 与 `refactor/file-analysis` Legacy Office 集成实施计划

## 0. 文档信息

| 项目 | 内容 |
| --- | --- |
| 编写日期 | 2026-07-28 |
| 文档层级 | L3 分支集成与跨模块文件级实施计划 |
| 文档状态 | 执行中：M0—M7 已完成，M8 待执行 |
| 当前开发分支基线 | `refactor/file-analysis@9776f711...`（M0 收口提交前） |
| 目标主线 | `main@fb758cda...` |
| 共同基点 | `2c886a94...` |
| `main` 功能提交 | `2eee53c feat: add legacy Office local conversion support` |
| 公开契约依据 | `docs/接口文档/`，本计划不替代接口文档 |
| 验证环境 | 项目 `/venv`，Fake、临时目录和临时 SQLite |
| 运行限制 | 不执行 `run.py`，默认不连接真实 AnythingLLM、模型、Callback 或生产数据库 |

本计划用于把 `main` 的 Legacy Office 本地转换、AnythingLLM XLSX 解析、报告支持、离线交付
和安全清理能力完整集成到当前分支，同时保持 `/llm/analysis` 只经过阶段 1F 的 Parser、批量
原子受理、SQLite、Dispatcher、`RunAnalysisTask(TaskId)`、资源审计和 Callback 恢复链。

本计划不增加、删除、重命名任何 HTTP、Callback、Progress、SSE 或 WebSocket 参数。负责人已于
2026-07-28 确认：XLS/XLSX 继续只允许一个可解析 Sheet；采用“部署默认开启、代码缺省安全关闭”
策略；交付平台维持 `main` 当前范围；历史 XLSX Folder 按可观测、无所有权证明不删除的方案治理。
接口文档只允许在实现和离线验收通过后同步这些已确认语义。

### 0.1 阶段执行状态

| 阶段 | 状态 | 结果或进入条件 |
| --- | --- | --- |
| M0 基线冻结 | 已完成 | 远端引用、49 文件处置矩阵、契约摘要和 493 项离线回归均已冻结 |
| M1 Git 图合并 | 已完成 | 独立工作树完成 7 个冲突的人工处置，101 项定向门禁通过 |
| M2 转换内核与交付 | 已完成 | Windows 所有权标记修复，52 项专用测试与 23 项架构测试通过 |
| M3 AnythingLLM 单 Sheet 协议 | 已完成 | 两组共 394 次测试调用通过，生产代码无需追加修复 |
| M4 Report 集成 | 已完成 | 全套 16 个 Report 测试模块、194 项用例全部通过 |
| M5 Stage 1F Analysis 集成 | 已完成 | 318 项 Analysis 全量回归及 116 项共享/契约回归通过，公开接口契约无变化 |
| M6 容器、默认开关和生命周期 | 已完成 | 68 项门禁通过（其中 1 项为仅限 macOS 实机的预期 Skip），启动失败统一关闭容器 |
| M7 共享业务回归与存储治理 | 已完成 | 191 项共享回归通过，新增无删除入口的 XLSX Folder 只读库存工具 |
| M8—M10 | 待执行 | 严格逐阶段通过门禁后推进 |

M0—M7 的详细证据见：

- `docs/更新记录/260728-main与file-analysis分支Legacy-Office集成M0执行记录.md`；
- `docs/更新记录/260728-main与file-analysis分支Legacy-Office集成M1执行记录.md`；
- `docs/更新记录/260728-main与file-analysis分支Legacy-Office集成M2执行记录.md`；
- `docs/更新记录/260728-main与file-analysis分支Legacy-Office集成M3执行记录.md`；
- `docs/更新记录/260728-main与file-analysis分支Legacy-Office集成M4执行记录.md`；
- `docs/更新记录/260728-main与file-analysis分支Legacy-Office集成M5执行记录.md`；
- `docs/更新记录/260728-main与file-analysis分支Legacy-Office集成M6执行记录.md`；
- `docs/更新记录/260728-main与file-analysis分支Legacy-Office集成M7执行记录.md`。

---

## 1. 执行结论

### 1.1 推荐集成方式

不能机械接受 `main` 在 `app/blueprints/llm.py` 和遗留 `analysis_service.py` 中的 Analysis
接线。推荐从最新 `main` 创建独立集成分支，再把 `refactor/file-analysis` 作为第二父分支执行
非快进合并；在合并后的后续提交中，把 Legacy Office 能力移植到当前 Analysis Port/Adapter：

```text
main
  \
   merge commit -> Legacy Office 语义移植与验收提交
  /
refactor/file-analysis
```

最终结果必须同时满足：

1. Git 历史包含最新 `main`；
2. `main` 新增的用户能力和运维能力无遗漏；
3. `/llm/analysis` 不恢复路由线程或旧 `run_file_analysis_task` 生产链；
4. Analysis、Report 和共享 AnythingLLM 只通过明确 Port/Adapter 获取转换和单 Sheet XLSX 能力；
5. 接口字段集合、HTTP 状态码、Callback 字段和 Progress 格式保持不变。

### 1.2 当前冲突事实

当前分支相对 `main` 领先 7 个提交、落后 2 个提交。`main` 的功能提交修改 49 个文件，约
`+9428/-70`。只读虚拟合并发现 7 个文本冲突文件：

- `.env.example`；
- `.gitignore`；
- `app/blueprints/llm.py`；
- `app/container.py`；
- `app/services/core/config.py`；
- `app/services/llm_service/analysis_service.py`；
- `tests/test_dependency_container.py`。

README、接口文档、Report 和 AnythingLLM 文件即使可自动合并，也必须做语义审查，不得把
“Git 无冲突”解释为“架构和功能无冲突”。

---

## 2. 已确认目标与不变项

### 2.1 已确认目标

1. 完整集成 `.doc -> .docx`、`.ppt -> .pptx`、`.xls -> .xlsx` 本地转换；
2. 完整集成 Analysis、Report、AnythingLLM、离线安装包和第三方清单；
3. Analysis 使用转换后的 OOXML 完成 RAG、正文读取和全文翻译；
4. Report 来源文件支持 Legacy Office，模板继续只支持 `.docx`；
5. Analysis 与 Report 共享一个进程级转换容量许可，但不共享任务目录、文件或回调状态；
6. 转换失败 fail-closed，不把旧格式二进制文件当作成功降级结果上传；
7. 共享 AnythingLLM 变更必须回归 Weaponry 术语目录和普通文档上传；
8. 当前只声明单实例能力，不把进程内 Semaphore 或 SQLite 结果解释为多实例能力。

### 2.2 公开契约不变项

- 不新增、删除或重命名请求参数；
- 不新增内部 `execution_id`、document group、Sheet location、cleanup token 等公开字段；
- 保持 `/llm/analysis` 和 `/llm/generate-report` 的既有 HTTP 成功响应；
- 保持文件 Callback 和报告 Callback 的字段集合；
- `fileName`、`originalFileName`、`format`、`dataFormat` 继续使用业务请求值；
- 内部 `prepared-<uuid>`、Sheet JSON 名和 Collector Folder 名不得进入 Callback 或报告正文。

---

## 3. 已确认决策

### D-01：XLS/XLSX 继续只支持单 Sheet

AnythingLLM 会把一个 XLS/XLSX 工作簿解析为一个 Collector Folder 下的多个 Sheet 文档。
`main` 已能识别和安全清理这些成员，但为了适配现有“一份业务文件对应一个远端文档”的合同，
在成员数量大于 1 时拒绝并清理整个 Folder。

当前单文档假设至少存在于：

- `AnythingLLMDocumentClient.upload_document()` 的单对象返回值；
- `DocumentRagSession` 的单 `document_ref`、单 location 和单 pin；
- Analysis `AnalysisRagSessionRef` 的单文档四元组；
- RAG 来源隔离条件 `bound_locations == {external_location}`；
- 永久知识库协调记录的单 `document_ref`、单 `external_location` 和单 `superseded_location`；
- Analysis 资源事实、清理和知识转交的单文档状态。

因此，多 Sheet 不能通过删除 `len(documents) == 1` 校验实现。否则只记录第一个 Sheet 会导致：

- 其余 Sheet 已上传但未登记，形成孤儿资源；
- RAG 只查询第一张表或来源归属不可证明；
- 永久知识库替换只能解绑一个 Sheet；
- 失败清理、恢复和审计无法覆盖全部成员；
- 多实例或崩溃恢复时无法判断一个工作簿是否已完整提交。

本次明确选择保持 `main` 的单 Sheet 语义：

1. 原生 `.xlsx` 和 `.xls -> .xlsx` 转换结果都必须只产生一个可解析 Sheet 文档；
2. AnythingLLM 返回多个 Sheet 时，不静默选择第一张表；
3. 服务端先以全部可信成员签发 Folder Cleanup Token，再拒绝该业务文件并尝试整 Folder 清理；
4. 清理结果未知时保留 Token 和资源现场，禁止把未知状态改写为已清理；
5. Analysis 当前文件失败但同批后续文件继续；Report 任一来源失败则整体失败；
6. 不新增 `DocumentGroup`，不扩展单文档 RAG/知识协调 Schema，也不增加 Sheet 数量配置。

多 Sheet 原生文档组作为后续独立需求保留，不属于本次分支集成范围。未来若重新启动，必须重新
确认接口语义、容量上限、永久知识替换和多成员恢复设计，不能在本次单 Sheet 实现上直接放宽校验。

### D-02：采用部署默认开启

技术上可以缺省开启，但这意味着 LibreOffice 从“可选增强能力”变成“所有部署的强制运行依赖”：

- 未安装 LibreOffice 时应用拒绝启动；
- 安装了错误版本、Development 版本或不受支持架构时应用拒绝启动；
- 开发机、CI、离线测试和每一种容器镜像都必须显式注入 Fake 或安装受支持版本；
- 所有正式平台必须先有安装包、Hash、安装脚本、Preflight 和真实 Smoke 证据。

“不能把旧格式原文件直接上传作为降级”的含义是：`.doc/.ppt/.xls` 是 OLE2 二进制格式；如果
转换功能关闭或转换失败，不能继续把这个原文件交给 OCR、AnythingLLM 或翻译服务并把任务当作
正常处理。供应商可能拒绝、只解析到空文本，或在没有有效正文时返回看似成功的结果，从而污染
永久知识库和 Callback。正确行为是在任何远端副作用前明确失败。现代 `.docx/.pptx/.xlsx`、
PDF、Markdown 等原本支持的格式仍可按既有链路处理，不受此规则影响。

负责人确认采用“部署默认开启”：

- 标准 `.env.example` 和正式部署配置显式设置 `DOCSENSE_LEGACY_OFFICE_ENABLED=true`；
- 代码在环境变量完全缺失时仍使用安全默认 `false`，不探测主机、不启动子进程；
- 正式部署显式开启后，LibreOffice 缺失、版本不匹配或 Preflight 失败会阻止应用完整启动；
- 单元测试和离线测试显式关闭能力或注入 Fake，不依赖开发机安装 LibreOffice；
- 回滚或临时关闭前必须执行第 4.1 节的任务排空门禁。

### D-03：交付平台维持 `main` 当前范围

`main` 当前离线资产只覆盖：

- Windows x64：静态/mock 验证，尚未实机认证；
- macOS Apple Silicon：已有锁文件条目，但仍需真实 Smoke。

现有资产尚未完整覆盖 macOS Intel、Linux x64、Linux ARM64 和不同基础镜像的 Docker，Windows
ARM64 也不在范围内。本次不新增这些平台的安装包、锁文件、交付脚本或 production-ready 声明。

| 平台 | 本次状态 | 当前缺口 |
| --- | --- | --- |
| Windows x64 | 保留现有候选交付资产 | 真实安装、三格式、超时进程树和残留进程测试 |
| macOS Apple Silicon | 保留现有候选交付资产 | 真实安装、权限/隔离 Profile 和三格式测试 |
| 其他平台 | 本次不纳入交付与认证 | 后续独立确认平台、包来源、脚本和真实测试 |

代码继续保留现有跨平台发现和进程隔离逻辑，但“代码可能运行”不等于“平台已认证”。Windows
x64 和 macOS Apple Silicon 未完成真实 Smoke 前仍不得标记 production ready；其他平台的部署
必须在后续补充专项计划和真实证据后再开放。

---

## 4. 两项关键运行一致性问题

### 4.1 功能开关在任务受理后变化

当前 Analysis 是持久化任务链。一个典型时序是：

```text
T0: Legacy Office 开启，.xls 请求返回 202 并写入 accepted
T1: 服务在任务执行前停止
T2: 新部署把功能关闭，或新机器没有兼容 LibreOffice
T3: Dispatcher 恢复 T0 的 accepted 任务
```

如果 Worker 只读取 T3 的当前环境，会出现“受理时允许、执行时不允许”的漂移。直接上传原 `.xls`
会造成数据污染；立即写失败 Callback 虽然安全，却改变了受理时的执行承诺。

本计划推荐两层处理：

1. **任务策略快照**：把内部文档处理策略加入 Analysis 的不可变输入快照，至少保存
   `legacy_office_required`、`processing_profile_id`、允许版本系列、固定的
   `single-sheet-v1` XLSX 策略和非敏感策略指纹；不保存可执行文件绝对路径或密钥；
2. **发布门禁**：关闭/改变转换能力前停止新受理，等待 Accepted/Running Legacy 任务排空；无法
   排空时保持兼容能力，不能靠改变默认值处理存量任务。

当前任务表已有 `input_schema_version` 和 JSON payload，可通过 `AnalysisTaskInputV2`、V1/V2 双
解码实现，不需要向公开 API 增加字段。执行时若所需能力不可用：

- 禁止原样上传；
- 服务因强制 Preflight 失败时不启动 Dispatcher，让任务保持数据库事实；
- 如果是运行期能力丢失，按明确基础设施故障记录并保留可恢复现场；
- 在没有可靠重试状态前，不新增公开“等待能力”状态。

Report 也必须冻结同类内部处理策略，防止同一报告中的多个来源在重启前后使用不同规则。

### 4.2 历史 XLSX Collector Folder 存储累积

负责人已确认采用本节“可观测、无所有权证明不删除”的治理方案。

AnythingLLM 将 XLSX 上传为一个 Folder，内部每张 Sheet 是一个文档。新版本成功入库时，可以
从当前 Workspace 解绑旧 Sheet；但全局删除旧 Folder 可能破坏：

- 其他 Workspace 对旧 Sheet 的引用；
- 历史对话或尚未完成任务的引用；
- 网络超时后实际已经成功的绑定；
- 当前进程不知道的其他实例操作。

因此 `main` 选择“解绑但不全局删除”来保护数据一致性。代价是同一单 Sheet 业务文件反复更新后，
旧 Folder 继续占用磁盘和向量存储。未来如果另行支持多 Sheet，单个 Folder 的成员数量还会增加，
但这不属于本次实施范围。

资源分为三类处理：

| 资源类型 | 是否可自动删除 | 原因 |
| --- | --- | --- |
| 上传被拒绝且尚未绑定的 Folder | 可以，在成员集合和所有权完全匹配时整 Folder 删除 | 本次操作拥有完整排他证据 |
| 临时 RAG 成功结束且未转交永久知识的 Folder | 可以，先登记清理意图并确认无永久接管 | 任务资源事实能够证明所有权 |
| 已提交永久知识或曾被其他 Workspace 引用的历史 Folder | 当前不自动删除 | 缺少跨 Workspace 引用计数和全局所有权证明 |

短期治理方案：

1. 记录 Folder、全部 Sheet 成员、业务文件、创建任务、绑定集合和清理状态；
2. 提供只读库存/容量报告，不自动删除；
3. 对未绑定拒绝上传和临时任务执行有界恢复清理；
4. 设置数量、字节量和最老年龄告警；
5. 运维手工删除前必须重新核对远端成员集合和本地引用。

长期安全回收需要引用注册表或引用计数、跨实例 lease/fencing、删除意图、远端查回和
`outcome_unknown` 对账。没有这些证明时，宁可产生可观测存储债务，也不能自动误删业务数据。

---

## 5. Weaponry 术语目录和普通 Markdown 回归的含义

`app/integrations/anythingllm/documents.py` 是共享上传客户端，不只服务 XLSX。Weaponry 启动门禁
会把术语规则生成 Markdown 文档并通过同一个 Client 上传、绑定和校验完整性；普通 Markdown、
PDF 等也经过相同的上传响应解析。

因此“运行 Weaponry 术语目录和普通 Markdown 上传回归”是指运行隔离自动化测试，证明：

1. Markdown 上传返回一个 `custom-documents/...` 成员时仍按普通文档解析；
2. 不会误判为 XLSX Folder，也不会调用 `folder-list` 或 `remove-folder`；
3. 术语目录上传、Workspace 绑定和完整性校验仍成功；
4. 普通文件失败仍沿用原有清理协议；
5. 不执行 `run.py`，不连接或修改真实生产 AnythingLLM Workspace。

它是共享基础设施回归，不是要求重新上传生产术语库。

---

## 6. 目标内部模型

### 6.1 文件准备三路径

`PreparedAnalysisDocument` 目标上明确区分：

- `source_path`：原始下载文件，用于溯源和资源审计；
- `processing_path`：转换后、供全文翻译使用的任务内 OOXML；
- `upload_path`：经过规范化/OCR/MinerU 后交给 RAG 和正文读取的文件。

当前全文翻译使用 `prepared.source_path`，所以本字段拆分是 Legacy Office 正确性的硬前置。

### 6.2 单 Sheet 供应商边界

本次继续保持现有单文档内部合同：成功的普通文件或 XLS/XLSX 上传都只向上层返回一个完整的
`document_ref`、`external_location`、`content_sha256` 和 `ingested_file_name` 四元组。

多 Sheet 只在拒绝和清理边界内暂时出现：上传客户端先校验所有成员属于同一受控 Folder，使用
完整成员集合签发 opaque Cleanup Token，然后拒绝业务上传并尝试整 Folder 清理。上层不得选择
第一张表、不得把多个成员压入单文档字段，也不得新增部分成功语义。

---

## 7. 分阶段实施计划

### M0：决策落盘与基线冻结

1. 固化 D-01 单 Sheet、D-02 部署默认开启和 D-03 现有平台范围；
2. 固定双方 refs、共同基点、接口文档 Hash、冲突文件和工作树状态；
3. 生成 `main` 49 个功能文件的处置矩阵；
4. 运行当前分支安全离线基线。

**退出门禁：** 已确认决策与接口语义形成黄金资产；基线无未解释失败。

### M1：独立集成工作树与 Git 合并

1. 从最新 `main` 创建独立集成分支/工作树；
2. 执行 `git merge --no-commit --no-ff refactor/file-analysis`；
3. 重新核对实际冲突集合；
4. `llm.py` 保留 Stage 1F 薄路由；
5. `analysis_service.py` 不接入 Legacy Office 生产能力；
6. 配置、容器和依赖测试逐块人工合并；
7. 接口文档按 M0 决策处理，不接受未经确认的自动合并文本。

**测试：** `git diff --check`、路由/架构 AST、Analysis contract assets、最小 import 测试。

**效果：** 建立两侧历史关系，原开发分支和主分支不被改写。

### M2：Legacy Office 内核和现有平台交付

文件范围：

- `app/modules/document_processing/{domain,ports,libreoffice,ooxml_validator}.py`；
- `scripts/legacy_office/*`；
- `.gitignore`；
- `tests/test_legacy_office_{config,conversion,delivery}.py`。

实施内容：

- 保留 OLE2、OOXML ZIP/ZIP64、大小、超时、进程树、私有 Profile 和清理门禁；
- 保留并审查 Windows x64、macOS Apple Silicon 的锁文件、Hash、安装、Preflight 和离线打包；
- 不在本次新增 macOS Intel、Linux、Docker 或 Windows ARM64 交付资产；
- 每个平台独立记录“静态/mock”“真实 smoke”“production ready”状态；
- 日志只记录 task_id、格式、大小、耗时、版本和稳定错误码，不记录正文、URL 或绝对路径。

**测试：** 三格式、伪造后缀、损坏/加密文件、超限、超时、残留进程、并发许可、目录逃逸、
离线包 Hash 和 Manifest；真实平台测试需另行授权。

**效果：** 获得可独立验证、平台声明不越界的转换基础设施。

### M3：AnythingLLM 单 Sheet XLSX 协议

文件范围：

- `app/integrations/anythingllm/{documents,models,errors,rag_gateway,knowledge_gateway}.py`；
- `app/services/llm_service/task_service.py`；
- `tests/test_anythingllm_*`、`tests/test_task_service.py`、`tests/test_weaponry_production_adapters.py`。

实施内容：

1. 普通上传仍必须返回恰好一个 `custom-documents` 成员；
2. XLS/XLSX 返回一个 Sheet 时按单文档合同继续处理；
3. 返回多个 Sheet 时签发包含全部可信成员的 Folder Cleanup Token；
4. 多 Sheet 明确拒绝并尝试整 Folder 清理，不选择第一张表；
5. 清理不确定时把 opaque Token 纳入生命周期审计和资源恢复事实；
6. 单 Sheet 新版本入库后只解绑旧 Sheet，不全局删除旧 Collector Folder；
7. 普通 Markdown/PDF/DOCX 不触发 XLSX Folder 端点。

**测试：** 单 Sheet 成功；2 个及更多 Sheet 明确拒绝；重复/混合 Folder；畸形成员；Folder
清理确认/不确定；旧单文档记录兼容；普通 Markdown/PDF 零行为漂移。

**效果：** 单 Sheet 能力稳定可用，多 Sheet 不产生静默截断或无记录孤儿资源。

### M4：Report 集成

文件范围：

- `app/modules/report/adapters/{legacy_files,anythingllm_rag}.py`；
- `app/modules/report/application/run_report.py`；
- `app/modules/report/domain/{rules,__init__}.py`；
- Report Fakes 和测试。

实施内容：Legacy 来源先转换并复制到任务目录；XLS/XLSX 只接受单 Sheet；多 Sheet 或任一转换/
绑定失败按报告既有失败合同整体收口；`.doc` 模板继续拒绝；最终 HTML 清除内部转换名、Sheet
文件名和 Folder 名。

**测试：** 三种 Legacy 来源、现代/Legacy 混合、单 Sheet 成功、多 Sheet 整体失败并清理、模板
拒绝、报告正文脱敏、资源恢复和 Callback Guard。

**效果：** Report 完整获得新能力，不恢复旧路由线程和 raw fallback。

### M5：Stage 1F Analysis 集成

文件范围：

- `app/modules/analysis/ports/files.py`；
- `app/modules/analysis/domain/task_inputs.py`；
- `app/modules/analysis/adapters/{legacy_files,legacy_rag,task_codec,task_commands}.py`；
- `app/modules/analysis/application/{run_analysis,knowledge_handoff,recover_resources}.py`；
- `app/modules/analysis/composition.py`；
- Analysis Fakes 和全部 Stage 1F 定向测试。

实施内容：

1. 增加 `processing_path`；
2. 转换发生在下载后、远端 Session 创建前；
3. RAG、正文读取和全文翻译只使用转换后产物；
4. XLS/XLSX 单 Sheet 继续使用单文档 RAG，多个 Sheet 明确失败并触发 Folder 清理；
5. `AnalysisTaskInputV2` 冻结 Legacy Office 和 `single-sheet-v1` 策略并兼容读取 V1；
6. 资源事实登记 raw/processing/upload 路径、单文档身份和待恢复 Folder Cleanup Token；
7. 知识转交继续以单文档 committed 为成功门禁；
8. 单文件失败不阻断同一批次后续 execution；
9. 不修改 Parser、202 响应或 Callback 字段。

**测试：** RAG/翻译/正文路径一致；单 Sheet 成功；多 Sheet 当前文件失败且后续文件继续；Folder
清理结果未知；策略快照跨重启；50 任务目录、单文档身份和 Callback 隔离；旧 runner AST 零引用。

**效果：** `main` 新功能完整落在 Stage 1F 唯一生产链。

### M6：容器、默认开关和生命周期

文件范围：

- `app/container.py`；
- `app/services/core/config.py`；
- `.env.example`；
- `tests/test_dependency_container.py`；
- Analysis/Report composition tests。

实施内容：容器只构造一个共享容量许可的 Preparer；代码缺省保持 `false`，标准 `.env.example`
和正式部署模板显式设置 `true`；Preflight 必须先于 Dispatcher 启动；启动失败释放已经取得的
进程锁和后台服务；陈旧目录扫描只处理带有所有权标记的转换目录。

**测试：** 缺省配置、显式关闭、非法配置、可执行文件缺失、版本冲突、Fake 注入、全局并发 1、
启动中途失败的 Dispatcher/进程锁释放。

**效果：** 默认行为和失败模式可预测，不出现部分服务已启动的半初始化状态。

### M7：共享业务回归与存储治理

1. 运行 Weaponry 术语目录 Markdown 上传、绑定和完整性校验测试；
2. 运行普通 Markdown/PDF/DOCX 上传回归；
3. 增加 XLSX Folder 只读库存诊断脚本、结构化日志和指标采集点，不新增公开 HTTP 接口；
4. 只自动清理有完整排他所有权证明的拒绝/临时 Folder；
5. 已提交永久知识的历史 Folder 只报告，不自动删除。

**效果：** 共享 Client 不破坏非 XLSX 业务，并使存储债务可观测、可审计。

### M8：接口文档与项目记录

前提是对应实现和离线契约测试已经通过。修改范围：

- `docs/接口文档/文件处理和报告生成.md`；
- `README.md`；
- 对应 `docs/更新记录/260728-*.md`；
- `docs/重构记录/README.md` 索引。

文档仅同步格式支持、部署默认开启、单 Sheet 限制、多 Sheet 拒绝/清理和失败语义，不增加任何
公开参数。接口契约资产必须证明字段、状态码、Header 和 Callback 结构无变化。

### M9：离线关闭验收

按以下顺序执行：

1. Legacy Office 内核/配置/交付；
2. AnythingLLM Documents/RAG/Knowledge；
3. Report；
4. Analysis Port/Adapter/Application/Batch/Dispatcher/Recovery；
5. Weaponry 术语目录；
6. Chat 和普通文档上传；
7. Dependency Container；
8. 接口契约资产；
9. 安全全仓测试。

全部使用项目 `/venv`、`-B`、Fake 和临时 SQLite，不执行 `run.py`。验收报告必须列出 discovered、
excluded、executed、failure、error、skip，不复制历史测试数量。

### M10：真实平台认证与发布

1. 分别核对现有 Windows x64、macOS Apple Silicon 离线包和 `SHA256SUMS`；
2. 执行 `soffice --version`、三格式 Smoke、连续转换、超时和残留进程测试；
3. 停止新受理并排空 Accepted/Running Legacy 任务；
4. 发布后先验证 Preflight 和只读诊断，再启动正常服务；
5. 观察转换耗时、单/多 Sheet 判定、Folder 增长、清理未知和 Dispatcher 状态；
6. Windows x64、macOS Apple Silicon 未完成真实证明时保持非 production-ready 标记；
7. 本次不为其他平台生成交付或认证结论。

回滚只能关闭新受理并恢复兼容运行配置，不删除任务数据库，不盲目重放上传，不清理无法证明
所有权的 Folder。

---

## 8. 文件范围总表

| 范围 | 主要文件 |
| --- | --- |
| Git/配置 | `.env.example`、`.gitignore`、`README.md` |
| 公开路由/组合根 | `app/blueprints/llm.py`、`app/container.py`、`app/services/core/config.py` |
| 转换内核 | `app/modules/document_processing/*` |
| Analysis | `app/modules/analysis/{domain,ports,adapters,application,composition.py}` |
| Report | `app/modules/report/{domain,adapters,application}` |
| AnythingLLM | `app/integrations/anythingllm/{documents,models,errors,rag_gateway,knowledge_gateway}.py` |
| 共享任务审计 | `app/services/llm_service/task_service.py` 中 XLSX Folder Cleanup Token 生命周期 |
| 遗留兼容审查 | `app/services/llm_service/analysis_service.py`，不得恢复为生产入口 |
| 交付 | `scripts/legacy_office/*` |
| 契约/记录 | `docs/接口文档/文件处理和报告生成.md`、README、重构/更新记录 |
| 测试 | `tests/test_legacy_office_*`、`test_anythingllm_*`、`test_report_*`、`test_analysis_*`、`test_weaponry_terms_catalog.py`、`test_dependency_container.py`、契约资产 |

---

## 9. 完成定义

只有同时满足以下条件，才能宣称集成完成：

1. 最终提交同时包含最新 `main` 和 `refactor/file-analysis` 历史；
2. 49 个 `main` 功能文件均有“继承、人工合并、架构重实现或经确认排除”的处置记录；
3. `/llm/analysis` 唯一生产入口仍是 Stage 1F；
4. Legacy Office 的 RAG、正文和全文翻译都使用转换后文件；
5. 单 Sheet 可完整 bind、pin、知识转交和替换；多 Sheet 明确拒绝，整 Folder 清理和未知恢复有故障证据；
6. 公开参数、响应、Callback 和 Progress 结构零变化；
7. 标准部署显式默认开启、代码无配置时安全关闭，且平台声明不超出现有交付范围；
8. Weaponry 术语目录和普通 Markdown 上传无回归；
9. 历史 Folder 存储债务可观测，但未执行无所有权证明的删除；
10. 安全全仓离线测试无新增失败，真实平台能力只按实际 Smoke 结果声明；
11. 未宣称可靠任务队列、多实例、跨实例全局限流或生产吞吐已经完成。
