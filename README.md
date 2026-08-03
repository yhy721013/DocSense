# DocSense - 甲方协议 LLM 接口后端

DocSense 当前以甲方协议后端接口服务为主，聚焦 LLM 任务处理能力；同时提供调试路由，用于查看已落盘的回调结果及联调文件对话模块。`/debug/*` 不属于甲方协议或生产回调链路，当前也不会随 `APP_DEBUG=false` 自动关闭，部署时必须在网络或反向代理层限制访问。

## 1. 核心能力

- 文件解析：`POST /llm/analysis`
- 报告生成：`POST /llm/generate-report`
- 武器装备知识谱系解析：`POST /llm/weaponry`
- 文件对话：`POST /llm/chat`（附加标题、历史、中断和删除接口）
- 知识谱系对话：`POST /llm/weaponry-chat`（附加标题、历史、中断和删除接口）
- 分类节点变更：`POST /llm/reassign`
- 任务查询与回调补发：`POST /llm/check-task`
- 任务进度推送：`WS /llm/progress`
- 结果回调：服务端主动 `POST` 到 `CALLBACK_URL`
- 本地回调调试页：`GET /debug/callback`
- 本地回调数据接口：`GET /debug/api/callback`
- 本地文件对话调试页：`GET /debug/chat`
- 本地文件对话初始化数据接口：`GET /debug/api/chat/bootstrap`

## 2. 分层架构与调用关系

### 2.1 分层职责

| 层级 | 目录 | 职责 | 代表文件 |
| --- | --- | --- | --- |
| 接口层 | `app/blueprints/`、`app/adapters/web/`、`app/presenters/` | HTTP/WS/SSE 入参解析、框架映射、协议流式与响应展示、本地调试入口 | `llm.py` `report_requests.py` `report_submission.py` |
| 应用端口层 | `app/ports/` | 定义供应商无关的 RAG、长期知识库及任务级 Factory 契约；Chat 专属端口归属 Chat 模块 | `rag.py` `knowledge_index.py` |
| 模块业务层 | `app/modules/` | 按业务聚合 Domain/Application/Ports/Adapters；Chat、文件分析、报告、武器谱、共享文档处理、翻译和统一任务边界均已迁入 | `chat/` `analysis/` `report/` `weaponry/` `document_processing/` `translation/` `tasks/` |
| 兼容业务层 | `app/services/llm_service/` | 保留现役任务兼容存储；旧 Analysis、Report、Weaponry、Translation 执行器已在 1G-5 删除 | `task_service.py` |
| Chat 业务模块 | `app/modules/chat/` | 文件对话与知识谱系对话共用的 Domain/Application/Ports/Adapters；当前 SQLite 与内联调度只支持单实例 | `composition.py` `application/` `adapters/` |
| 应用装配层 | `app/container.py` | 组装应用服务、供应商 Factory、配置和上传并发限制器 | `ApplicationServices` `create_application_services()` |
| 核心基础层 | `app/services/core/` | 配置、路径、日志、数据库、进度中枢和 Prompt 构建 | `config.py` `settings.py` `database.py` `progress_hub.py` `prompts.py` |
| 外部集成层 | `app/integrations/anythingllm/` | AnythingLLM Transport、执行策略、原子 Client、纯方案 B Gateway 与任务级 Factory | `transport.py` `policies.py` `documents.py` `workspaces.py` `threads.py` `rag_gateway.py` `factory.py` |
| 通用工具层 | `app/services/utils/` | 保留回调发送、受控下载等窄工具；旧 AnythingLLM/RAG 编排、Debug Preview 和文档处理 Facade 已在 1G-5 删除 | `callback_client.py` `file_downloader.py` `word_extractor.py` |
| 共享文档处理层 | `app/modules/document_processing/` | Artifact/Profile/Lineage、MHTML/LibreOffice/MinerU/OCR Processor、本地 Store/Record 与统一准备流水线 | `domain/` `application/` `ports/` `adapters/` |
| 翻译能力层 | `app/modules/translation/` | 只读取 prepared Markdown/Text Artifact；语言引擎、分段和安全 Renderer 与格式转换解耦 | `domain/` `application/` `ports/` `adapters/` |

### 2.2 主要调用方向

1. `blueprints -> web adapter/application/presenter`：报告、武器谱、分类节点变更、文件分析、任务检查和 Chat 均遵循 Parser → Application → Presenter；蓝图只保留 Flask/SSE/WebSocket 协议桥接，不得构造数据库、供应商 Client 或后台线程。
2. `blueprints -> app.container`：从 Flask 应用扩展读取服务与无状态 Factory，不创建模块级服务单例。
3. `modules -> ports <- adapters`：现行业务 Application 只依赖供应商无关 Port/Factory；`llm_service/task_service.py` 只作为兼容 SQLite 事实底座，由模块 Adapter 隔离，不再承载旧业务 Worker。
4. `integrations.anythingllm/report/weaponry adapters -> ports`：Gateway/Adapter 实现端口；新链路每次进入 Factory 租约时创建独立 Transport，不跨任务共享 HTTP Session。
5. `report/analysis adapters -> document_processing`：下载后的原始文件只进入一条 Artifact 流水线；
   RAG、正文读取和全文翻译共享 prepared Artifact。
6. `analysis/weaponry adapters -> translation`：Analysis 全文只调用
   `TranslatePreparedDocument`；Weaponry 纯文本只调用实例级 TranslationEngine。
7. `check-task -> report/weaponry/analysis application`：三类业务分别通过
   `RecoverReportCallbackSynchronously`、`RecoverWeaponryCallbackSynchronously`、
   `RecoverAnalysisCallbackSynchronously` 与正常 Worker 共用 execution 级 Callback Guard；file 不再
   回退到兼容 Task Service。三类业务均保留甲方规定的请求内同步恢复副作用。

阶段 1H-0～1H-7 及关闭后 1H-R 全面审查修复已完成代码与安全离线验收。1H-R 恢复扫描
PDF 的 RAG-only 原件降级、保留结构化 Markdown/图片、增加显式 Processing Record 对账、
Artifact Catalog、读租约/锁回收和有界本地容量。上游系统保存的原始上传文档不由
DocumentProcessing 删除；任务共享 Store 中的 source/prepared Artifact 在当前阶段继续保留，
只即时清理能证明所有权的 Processor scratch、失败候选和 `.part`。旧 Handler/Service/Facade 已在
1G-5 完成测试价值迁移和条件物理删除；永久架构与引用门禁继续禁止当前生产链重新从旧转换路径取得执行权。
未来 Artifact GC 必须基于数据库引用、保留期、删除资格、lease/fencing 和审计状态实施。
Analysis/Report accepted 时冻结全部处理/翻译运行参数仍属于阶段 2 的内部 Task Schema 升级，
MinIO、可靠队列、多实例一致性与生产容量证明仍属于后续阶段。

### 2.3 analysis/report/weaponry 请求到回调的链路

```text
Client Request
  -> app/blueprints/llm.py
    -> report：Parser -> SubmitReportTask -> SQLite accepted -> Event 唤醒
       -> 单报告执行 Worker -> RunReportTask(task_id) -> Report Ports/Adapters
       -> 两条独立维护线程执行资源恢复和队列诊断
    -> weaponry：Parser -> 文档范围冻结 -> SubmitWeaponryTask -> SQLite accepted -> Event 唤醒
       -> 单武器谱执行 Worker -> RunWeaponryTask(task_id) -> Weaponry Ports/Adapters
       -> 两条独立维护线程执行资源恢复和 Callback Guard 过期扫描
    -> analysis：Parser -> SubmitAnalysisBatch -> SQLite 批量 accepted -> Event 唤醒
       -> 单文件分析 Dispatcher -> RunAnalysisTask(task_id) -> Analysis Ports/Adapters
       -> 独立维护线程执行资源恢复和 Callback Guard 过期扫描
    -> 写入任务/进度事实并按各业务 Callback Guard/兼容链回调
```

Analysis 的上述链路已完成 1F-5B 代码接线与 1F-7B 代码/离线关闭验收。当前发布制度要求每次更新先停止
DocSense，再由 `clean.py` 完整删除任务库并在新进程中重建空库；该流程下
`scripts/inspect_analysis_cutover.py` 不再是每次发布的强制门禁，只保留给“保留存量数据库、从备份恢复或
排查清理失败”的迁移诊断场景。不得在线双跑新旧链路，也不能把离线回归表述为可靠队列、多实例或生产容量证明。

## 3. 当前目录（关键部分）

```text
app/
  __init__.py                       # Flask App 工厂，安装依赖容器并注册蓝图
  container.py                      # 应用装配根、ApplicationServices 与 analysis/report 任务限制器
  modules/
    tasks/                          # 统一任务 Domain/Application/Ports/兼容 Adapter
    analysis/                       # 文件分析 Domain/Application/Ports/生产 Adapter
    report/                         # 报告 Domain/Application/Ports/生产形态 Adapter
    weaponry/                       # 武器谱检索、证据、抽取、任务与资源闭环
    reassign/                       # 分类节点变更同步 Saga 与持久恢复
    debug/                          # /debug/* 只读 Query/Port/Adapter
    document_processing/            # Artifact/Profile/Lineage 与通用文档 Processor
    translation/                    # prepared Artifact 翻译与实例级语言引擎
  ports/                            # RAG、知识库、文件对话等供应商无关 Port、DTO 与 Factory Protocol
  integrations/
    anythingllm/                    # Transport、策略、原子 Client、Gateway 与任务级 Factory
  blueprints/
    llm.py                          # /llm/* 路由 + WebSocket 进度通道
    debug.py                        # /debug/* 本地调试路由
  services/
    core/
      config.py                     # 环境变量与配置加载
      settings.py                   # 路径常量与限制（上传目录、DB 路径等）
      logging.py                    # 日志初始化
      database.py                   # 知识库映射持久化（architecture_id <-> workspace_slug）
      progress_hub.py               # 进度发布/订阅中枢
      prompts.py                    # 统一 Prompt 构建
      architecture_tree.py          # 完整领域树校验、不可变索引与进程内 LRU 缓存
    llm_service/
      task_service.py               # 任务状态、结果、回调状态与领域召回审计持久化
    chat/
      application/                  # 对话命令、历史、标题、中断、删除与运行执行器
      persistence/                  # 本地消息、会话及资源租约持久化
      locking/                      # 会话级锁服务
    utils/
      callback_client.py            # 回调发送
      file_downloader.py            # 下载到临时文件
  templates/
    debug/
      callback.html                 # 本地回调结果调试页模板
      chat.html                     # 本地文件对话调试页模板

run.py                              # 服务启动入口
docs/接口文档/
  文件处理和报告生成.md
  知识谱系解析.md
  文件对话.md
  文件对话新增接口.md
  知识谱系类别文件对话.md
  分类节点变更.md
scripts/                            # 本地联调脚本
tests/                              # unittest 测试用例
clean.py                            # 清理测试数据
requirements.txt                    # 当前根目录实际提供的 Python 依赖清单
```

## 4. 任务模型与状态

`file`、`report`、`weaponry` 三类异步回调任务统一持久化到任务库（默认 `${DOCSENSE_RUNTIME_DIR}/llm_tasks.sqlite3`），查询键如下；文件对话使用独立的对话库，title/history/abort/delete/reassign 不进入该任务模型。

- `file`：`fileName`
- `report`：`reportId`
- `weaponry`：`architectureId`

`reportId` 的三个入站位置（`/llm/generate-report`、报告类型 `/llm/check-task` 和
报告类型 `/llm/progress`）统一接受 JSON 整数或十进制整数字符串，不设置 32/64 位
业务范围，并按整数值规范化为同一任务键；例如 `132` 与 `"00132"` 指向同一报告。
公开响应与 Progress 推送中的 `reportId` 仍保持 JSON number。

业务状态码：

| businessType | 状态含义 |
| --- | --- |
| `file` | `0` 未解析 / `1` 解析中 / `2` 已解析 / `3` 解析失败 |
| `report` | `0` 生成中 / `1` 已生效 / `2` 生成失败 |
| `weaponry` | `0` 未解析 / `1` 解析中 / `2` 已解析 / `3` 解析失败 |

回调状态（任务表 `callback_status`）：

- `pending`：任务初始回调状态，尚未记录回调成功、失败或跳过
- `success`：回调成功
- `failed`：回调失败（可通过 `/llm/check-task` 触发补发）
- `outcome_unknown`：请求可能已经送达但响应结果未知；后台不自动重试，file、report、weaponry 三类业务均只能由新的 `/llm/check-task` 请求明确按至少一次语义授权补发
- `skipped`：任务已完成，但当前部署未配置 `CALLBACK_URL`，因此无需向外部系统回调

`/llm/check-task` 只会在当前已配置 `CALLBACK_URL` 时补发 `pending`、`failed`，以及由新的
check-task 请求明确授权的 `outcome_unknown` 终态结果。路由会在任何回调副作用前完整校验全部
`params`，再分别按 `fileName`、规范化 `reportId` 或规范化 `architectureId` 稳定去重；同一请求中的
每个唯一业务键最多进入一次检查和一次可能的发送权竞争。`outcome_unknown` 补发可能产生重复业务
回调，接收方必须按对应业务键和业务结果幂等处理；普通 Worker 和后台维护线程仍不得自动补发。
`skipped` 不增加回调尝试次数且不可重放；任务进入该状态后再配置 URL，也不会通过
check-task 自动补发。三类业务每次实际外发前都会原子复核 latest execution 并取得 Callback
Guard 租约：同一 callback attempt 的并发恢复最多发送一次，新任务已提交时旧回调判定 stale 并跳过。
HTTP 仅以 2xx 作为投递成功；发送结果未知时不自动重发。

同名文件任务处于 `status=0/1` 时不能重复受理；任务已进入业务终态但 `callback_status=pending` 时也会暂时返回 HTTP `409`，以保护结果提交到首次回调完成之间的交接窗口。`failed` 或 `outcome_unknown` 任务在 `/llm/check-task` 实际补发期间会持有有界 SQLite 发送租约，同样暂不接受重跑；补发结束会以 execution ID、attempt 与租约 fencing 条件写回状态。进程中断后的过期租约先冻结为 `outcome_unknown`，不会由后台自动接管发送；下一次对应业务类型的 `/llm/check-task` 请求可明确授权至少一次补发。首次回调成功、失败或明确跳过且没有在途补发时可重新受理；若进程在首次交接窗口中断，可先通过 `/llm/check-task` 补发或把空回调配置迁移为 `skipped`。

## 5. 接口行为说明（按当前代码核对）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/llm/analysis` | 文件解析，支持 `params` 多文件顺序串行处理 |
| POST | `/llm/generate-report` | 报告生成，使用 `params[0]` |
| POST | `/llm/weaponry` | 武器装备知识谱系字段提取 |
| POST | `/llm/check-task` | 查询任务状态，必要时补发回调 |
| WS | `/llm/progress` | 任务进度订阅（目标公开契约只保留无 action 格式） |
| POST | `/llm/chat` | 基于 `chatId + fileNames` 发起文件对话（SSE 流式响应下发） |
| POST | `/llm/chat/title` | 根据指定会话的本地已提交消息生成标题 |
| GET | `/llm/chat/history` | 查询指定会话在本地持久化的已提交消息 |
| POST | `/llm/chat/abort` | 请求中断指定会话当前活跃的生成任务 |
| POST | `/llm/chat/delete` | 清理远端资源，全部成功后将本地会话标记为 `deleted` |
| POST | `/llm/weaponry-chat` | 基于 `userId + architectureId` 发起知识谱系对话（SSE 流式响应下发） |
| POST | `/llm/weaponry-chat/title` | 为知识谱系对话生成标题 |
| GET | `/llm/weaponry-chat/history` | 查询知识谱系对话的本地已提交消息及 assistant 来源 Chunk |
| POST | `/llm/weaponry-chat/abort` | 请求中断知识谱系对话当前活跃的生成任务 |
| POST | `/llm/weaponry-chat/delete` | 清理远端资源和正文/Chunk，并释放复合业务身份 |
| POST | `/llm/reassign` | 调整文档分类节点并迁移其 RAG workspace 关联 |

本地调试路由（非甲方协议接口）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/debug/callback` | 本地回调结果调试页，面向人工阅读 |
| GET | `/debug/api/callback` | 默认读取 `${DOCSENSE_RUNTIME_DIR}/callback/` 中最新一条回调记录，也可通过 `record=<json文件名>` 读取指定历史记录 |
| GET | `/debug/chat` | 本地文件对话调试页，联调发送、历史、删除三个接口；当前未接入标题生成和中断接口 |
| GET | `/debug/api/chat/bootstrap` | 读取本地会话列表与已解析文件列表，供 `/debug/chat` 初始化使用 |

关键补充：

1. `/llm/analysis`
   - 同请求可提交最多 32 个文件，服务端按数组顺序串行执行；整个 JSON 请求体最大 64 MiB，超出时同步返回 HTTP `413`。
   - 支持 `mhtml/mht`，会先归一化正文再进入解析。
   - 可选支持 Microsoft Office 97–2003 二进制文件：`.doc → .docx`、`.ppt → .pptx`、
     `.xls → .xlsx`。服务端在下载后、上传 AnythingLLM 前使用本机 LibreOffice headless
     转换；甲方请求、进度、任务查询、回调、知识库来源名和 `fileName`/`originalFileName`/
     `format`/`dataFormat` 均继续使用业务原值，内部 OOXML 文件名不会对外返回。标准部署样例
     默认开启；代码在环境变量完全缺失时仍安全关闭。关闭时 legacy 文件明确进入既有失败合同，
     不会原样上传。
   - XLS/XLSX 当前只支持**恰好一个可解析工作表**。AnythingLLM 返回多个 Sheet 文档时，
     服务端会明确拒绝该文件并清理本次上传的完整内部文件夹，不会静默只使用第一张表。
     同一 `fileName` 的新版本成功入库后，只从当前知识库解绑旧 Sheet，不全局删除旧
     Collector 文件夹，以免破坏历史对话或其他仍在引用旧版本的位置；旧文件夹可能继续
     占用 AnythingLLM 存储，当前版本不提供引用计数式自动回收。
   - 扫描件 PDF 在 `/llm/analysis` 中默认先经 MinerU 解析为 Markdown，再上传到 AnythingLLM；MinerU 失败时降级为既有 OCR Markdown，再失败才直传原 PDF。
   - `params[].originalFileName` 表示业务原始文件名；服务端保留其请求原值，除作为文件解析提示词上下文外，还用于知识谱系回调中的来源展示。
   - `params[].channel` 表示资料来源机构候选范围（字典编码），服务端只使用请求中提供的候选，不再注入“装发、军情、科技、训练”等默认值；未传、传空数组或没有有效候选对象时，Prompt 要求模型输出空字符串，回调 `data.channel` 也返回空字符串。
   - `params[].security` 表示密级候选范围（字典编码），回调 `data.security` 返回密级解析结果；解析时根据文档开头内容判断，未见密级相关说明时，候选包含“公开”则返回“公开”，否则返回密级候选中的第一个 `value`。
   - 请求与 callback 结构保持不变。甲方继续在每个 `params[]` 中传完整 `architectureList`；服务端保留完整树用于结果合法性、数据标准判断、知识库存储归并和 callback，仅将本地 Top-K 召回后的有界候选投影发送给模型。
   - `architectureList` 节点结构不变：`id` 为节点唯一标识，`name` 为节点名称，`parentId` 为父节点 id，`path` 为 id 路径链，`pathName` 为名称路径链，`remark` 为可选的节点名词概述。节点 ID 兼容正数字符串，但布尔值、非正数、重复 ID、父链成环或越界输入会在建任务和下载文件前同步返回 HTTP `400`；同一批次任一项无效时整批不受理。
   - 单个 `architectureList` 或非空 `architectureStandardList` 最多 10,000 个节点、可见深度最多 128 层，`name`/`path`/`pathName`/`remark` 分别最多 256/2,048/2,048/4,096 字符，四个文本字段累计最多 2,000,000 字符，包含扩展字段的紧凑 JSON 最多 4,000,000 字符。缺失、`null` 或空 `architectureList` 为兼容历史调用继续使用服务端默认树；显式非空列表不得包含被静默过滤的坏节点。
   - 默认 `topk_two_stage` 模式先用文件名、标题、型号/标准号及有界正文片段在本地执行 exact、字符 BM25、树路由和规则召回，再让模型对最多 128 个候选分类；分类 Prompt 最多 32,000 字符，超限或召回失败不会降级为发送完整领域树。
   - 若 `architectureList` 只有一个合法节点，直接确定该节点 ID，并以字段抽取作为首次模型查询；多候选时先分类，再在同一文档 Session 的全新独立 Thread 中抽取字段，避免分类回答污染字段抽取上下文。抽取 Prompt 不再包含完整领域树，只携带已确认 ID、语义路径和节点类型。
   - 多候选优先选择证据充分的叶子。证据不足时只允许返回模型可见的最深可靠父节点；父节点必须属于完整树、深度至少为 2 且通过召回资格检查，八个根节点和“数据标准”父节点均禁止返回。
   - 分类返回空值或非法 ID 时，仅在相同候选集合和阶段预算内执行有限 repair；普通资料无有效召回信号，或分类与 repair 后仍无法确定时，文件任务置为 `status=3`，不会默认回退到 ID `1`，也不会成功回调或写入永久知识库。已确认的数据标准正文适用下述独立保护规则。
   - 数据标准正文保护由 `DOCSENSE_ANALYSIS_DATA_STANDARD_MODE` 独立控制，默认 `scope_guard`。只有文件名与首页标准号等证据共同确认的 GJB 标准正文才会启用：召回候选限制为“数据标准”分支的六类叶子，分类 Prompt 使用各叶子的用途语义，并禁止以目录中的通用“术语和定义”章节直接判为“术语与定义”标准。模型与有限 repair 仍无法确认专业类别时定向归入“通用要求”（兼容节点名“通用要求标准”）；候选树缺少该叶子时任务置为 `status=3`。正文仅引用 GJB 标准而自身并非标准文件时不启用该保护；可显式设置为 `legacy` 即时回滚到既有候选召回。
   - 旧 GJB 兜底不再按 `architectureList` 请求顺序选择第一个数据标准叶子；无论请求顺序如何，只能定向选择“通用要求”叶子。
   - `architectureStandardList` 仍是独立的数据标准扩展字段开关；只有最终 `architectureId` 命中该范围或其子孙节点时，`fileDataItem` 才额外返回 `militaryName`、`num`、`startTime`、`implTime`、`approvalDept`。它不替代完整 `architectureList`，也不改变领域分类候选。
   - 运行模式由 `DOCSENSE_ANALYSIS_CLASSIFICATION_MODE` 控制：`topk_two_stage` 为默认模式；`topk_single` 保留 Top-K 有界候选但回滚为单阶段分类与抽取；`legacy` 仅适用于完整树候选不超过 128 且最终完整 Prompt 不超过 32,000 字符的小树。
   - 文件名分类约束由 `DOCSENSE_ANALYSIS_FILENAME_CONSTRAINT_MODE` 控制：默认 `scope_guard`，启用简氏作用域识别，以及 `originalFileName` 与首页真实标题的双源分支约束；可显式设置为 `legacy` 即时回滚到既有文件名硬约束。文件名只作为优先分类信号，不能在证据冲突或不足时单独决定最终分类；显式空值或未知值会使服务拒绝启动。
   - 装备身份受限重选由 `DOCSENSE_ANALYSIS_IDENTITY_RESELECT_MODE` 独立控制，默认 `enforce`。只有描述性 `originalFileName` 与文档开头/标题两路相互独立的证据共同确认同一唯一装备型号、该型号唯一映射到一个装备父节点，且父节点及七类直属明细叶子均在模型可见候选中时，门禁才可能通过；正文中偶然提及的部件或同级舰不能单独触发。初次分类已在该装备分支内时保持原结果，只有落在分支外或仅命中过粗祖先时才允许在“确认的装备父节点 + 七类直属明细叶子”内重选一次。`shadow` 会记录这次重选但不采纳，`enforce` 会采纳合法结果；重选返回空值、越界节点或调用失败时 fail-open 保留初次分类，不扩大候选、不循环重试。需要即时关闭该优化时可显式设置为 `off`。
   - 数据标准正文保护与文件名分类约束相互独立；启用时日志会记录标准号、标准标题、证据来源、候选作用域，以及最终是模型叶子命中还是 `data_standard_general_fallback` 受控兜底。
   - 召回决策按任务 execution 持久化 tree fingerprint、query digest、候选与排名、Prompt 字符数、返回 ID/rank、耗时和失败阶段，不保存正文。AnythingLLM 标签向量召回不在本轮实现范围；在人工 gold 门禁通过前，不据此宣称生产分类准确率已经提升。
   - 当最终分类名称严格符合 `<武器装备名称>-基础数据`、`<武器装备名称>-战技指标`、`<武器装备名称>-运用数据` 或 `<武器装备名称>-效能数据` 时，回调 `data.architectureId` 返回具体子分类 ID；本地知识库关系和 AnythingLLM workspace 按解析出的装备级父节点 ID 归并。业务 metadata 以 DocSense 本地数据库为准，AnythingLLM 上传 metadata 当前仅写入用于来源追踪的 `docSource`，不写入分类 ID。
   - 新建永久知识谱系 Workspace 的展示名称固定为 `archId-{规范化后的 architectureId}`；Analysis 入库、Reassign 前向与恢复共用同一无 I/O 命名规则。开发阶段不兼容或迁移旧 `architectureId-*` Workspace，也不会回退查找、自动认领或按前缀删除所有权不明的远端资源。
   - 主 Prompt 要求 `fileDataItem.score` 必填且只能是 `95`、`85`、`75`、`65`、`55`，分别对应闭源渠道或权威机构公开发布，专业科研单位/知名智库/装备研制单位，专业信息网站，普通信息网站，未明确数据来源资料；服务端映射会将缺失、无法转为数值、数值不是整数值或候选外的评分归一化为 `55`，可转换为整值的 `95.0`、`"95"` 等输入会保留为对应整数档位。
   - `fileDataItem.summary` 按全文材料类型生成客观中文摘要：明确识别为新闻或科普资讯文稿时使用对应固定前缀并限制为 100～300 个字符，明确识别为报告时使用固定前缀并限制为 300～500 个字符，前缀计入长度；标准、规范、手册、目录、表格等不强制归入这三类，不添加材料类型前缀，只遵守非空、客观且最多 500 个字符的通用约束。
   - `fileDataItem.keyword` 使用英文逗号分隔，目标数量为 5～10 个。服务端按最终 `architectureId` 的已验证 `parentId` 祖先链提取最多 4 个节点原名作为分类路径关键词并排列在前，再接收优先能够在摘要中、必要时能够在正文中直接核验的内容关键词；重复、超长或缺少文本证据的内容关键词会被丢弃，不会为满足数量补造无关词。
   - `fileDataItem.relatedTechnology` 使用英文逗号分隔，最多返回 10 个具有正文证据的中文规范技术名称；装备名称、型号、部队番号、地名、作战概念、单次战术动作和宽泛概括词不属于所属技术。外文资料允许将原文技术术语准确翻译为中文，模型同时生成的 `relatedTechnologyEvidence` 仅供服务端核验原文术语，不新增回调字段；无法核验的所属技术会被丢弃。
   - 摘要缺失时服务端保留标题回退；已标注材料类型的摘要长度越界、最终关键词少于 5 个或所属技术证据不合格时，文件任务仍按成功处理，并对可检测到的违约记录 warning。因此验收摘要、关键词和所属技术质量时不能只检查任务 `status=2` 或 callback 成功。
   - 解析后可进入翻译流程（由 `translation_service` 编排）。

2. `/llm/generate-report`
   - `filePathList` 支持多文件，统一汇总后生成 HTML 报告。
   - `mhtml/mht` 文件会先归一化再参与报告生成。
   - 启用 Legacy Office 转换后，`filePathList` 也支持 `.doc/.ppt/.xls`，按原顺序转换为
     `.docx/.pptx/.xlsx` 后作为报告来源上传；任一 legacy 来源转换失败时报告整体按既有
     `status="2"` 失败回调收口，不会进入 MHTML 原文件回退。
   - 报告中的 XLS/XLSX 来源同样只接受单工作表；多工作表会使整个报告按既有失败合同
     收口，并清理本次电子表格上传产生的全部临时 Sheet 资源。
   - 模型若在报告内容中返回本次任务的内部上传名，服务端会在最终 HTML、任务结果和
     callback 落库前，将其替换为对应 `filePathList` URL path 的安全末段文件名；
     query/fragment 不参与展示。安全文件名只允许 Unicode 字母、数字、组合标记及
     `.`、`_`、`-`；文件名为空或包含其他字符时回退为“来源文件N”。内部审计 trace
     仍保留模型原始内容。
   - `templateOutline` 表示 Word 模板文件下载地址；服务端会下载 `.docx` 模板并提取其中的文字内容作为报告大纲要求，再进入原有报告生成流程。
   - `.doc` 不支持作为 `templateOutline`；本轮不会隐式转换报告模板。
   - 当前公开路由已使用 `SubmitReportTask`/`RunReportTask(task_id)` 和任务级 Report RAG、
     Audit、Callback、Artifact、Resource Adapter。RAG 空内容继续按已确认兼容契约生成空 HTML
     并成功回调，但会记录内部可观测标记；临时 RAG 资源和本地产物均进入持久恢复闭环。

3. `/llm/weaponry`
   - `params` 为对象（非数组）。
   - 提交时会校验 `analyseData` / `analyseDataSource` 必须清空。
   - `params.filePathList` 可选；缺省或空数组表示解析当前类别下的全部文件，非空时可选择任意已进入知识库类别的文件。列表元素兼容完整下载 URL 和裸哈希文件名；服务端从 URL 路径提取并解码文件名、按首次出现顺序去重，并要求每个文件名唯一对应一条本地文档记录。未解析文件返回 `404`；同名跨类别或多个选中文件对应同一外部文档位置时返回 `400`，不会随机选择文档。
   - 显式文件范围和类别全量范围都会在受理阶段冻结为不可变文档快照；后台执行不会重新按类别选文档，也不会修改任何永久来源 workspace。每个 execution 使用任务级临时检索范围，资源创建后立即登记，终态后由持久清理意图和独立恢复线程收敛。
   - 字段抽取使用“专用语义 Query → Candidate → 稳定 score-or-rank Selection → Provided-Evidence Extraction”链路。普通 `INPUT` 字段候选批次默认 `topN=8`，`TABLE` 字段默认 `topN=16`；这是供应商单次候选批次，不是 `rows` 内容截断配额。合法来源、正文和分数/排名协议通过后，Evidence 全文按稳定顺序进入抽取和回调 `rows`。引用型正文筛选版本也会冻结进 production profile；当前版本不会仅因结构化 Markdown 业务表包含较多英文名称和年份就将其丢弃，但显式参考文献标题、至少 4 个 URL，或年份密集且伴随 URL、引用标记或非结构化高英文占比的内容仍会触发过滤。
   - `TABLE` 字段不再按单元格逐个查询；请求中的 `tableFieldList` 作为列模板，后端会进行整表检索和 JSON 行抽取。只有成功解析出有效行时，回调才扩展为多行二维 `tableFieldList`；否则保留原始列模板。
   - `fieldName` 去除首尾空格后精确等于 `装备编号`、`一级分类`、`二级分类`、`三级分类` 或 `四级分类` 时，服务端强制执行保留字段空值合同。该合同覆盖顶层 `INPUT` 和 `TABLE.tableFieldList` 嵌套列：命中字段跳过术语辅助、目标证据检索、模型抽取和翻译，回调固定返回 `analyseData=""`，并保留一个标准空来源占位对象（`content/source/time/fileName/translate` 均为空字符串，`rows=[]`）。混合 TABLE 只抽取普通列并按原始列顺序组装，保留列仍强制为空；仅含保留列的 TABLE 不进入外部抽取链。近似名称（如 `装备编号说明`）及其他普通字段不受影响。
   - 术语规则辅助由 `WEAPONRY_TERMS_RULE_CONTEXT_ENABLED` 控制，默认关闭。关闭路径不读取术语目录/workspace 配置，也不产生术语文件、网络、workspace 或 embedding I/O；开启路径只读预先配置的独立术语 workspace，术语内容仅作为字段口径、别名和单位参考，术语规则卡正文不进入 `analyseDataSource`，也不得作为装备事实来源。新 execution 冻结 `terms-rules-column-compact-v2`：每个 `INPUT` 字段独立检索一次，每个普通 `TABLE` 列分别独立检索一次；只有标准中英文字段名、别名或字段说明语义能够明确对应的规则卡才会命中。命中卡先压缩为包含目标列、卡片 ID、标准字段、标准单位、输出格式和防止“只改单位名”的核心合同，所有命中列的核心合同均保留后，剩余总预算才按列公平分配定义、别名、特殊换算规则和样例，避免靠前规则卡耗尽预算而静默丢失后续列。旧 execution 中已冻结的 `terms-rules-v1` 继续按旧行为执行。
   - 仅当术语辅助已启用且字段命中“标准单位”非空的规则卡时，规则卡要求模型把字段值写成“数值表达 + 单个半角空格 + 规则标准单位”，例如 `70926 吨`，禁止只返回 `70926`；该格式同时用于顶层 `INPUT` 的 `analyseData`、对应各来源的 `analyseDataSource.content`，以及 `TABLE.tableFieldList` 的命中单元格。请求与回调的 JSON 字段、层级和类型均不改变；这是条件化的术语规则输出要求，不是关闭术语辅助时的全局格式保证，也不新增服务端单位拼接或换算后处理。
   - 武器谱只保留按来源文件聚合 Evidence 后抽取的 `file_aggregate_v1` 策略。每个来源 attempt 使用全新、无历史的临时 workspace/thread，并且只接收最终 `rows` 对应的 Evidence；禁止访问任务或类别文档 workspace 做二次 RAG，也不存在共享父 Thread 回退。
   - 回调 `analyseDataSource.source` 严格返回文件解析请求中 `originalFileName` 的原值，`fileName` 返回哈希文件名，`rows` 与实际进入该来源模型 Prompt 的完整 Evidence 逐项、同序一致。MHTML/OCR 等内部产物名不会写入回调；缺少来源谱系的数据必须重新解析，不做名称猜测。
   - 公开路由不创建线程；请求可靠受理后返回 HTTP 202 严格空响应体。相同 `architectureId` 存在活动任务，或 Callback Guard 处于发送/结果未知状态时返回既有 HTTP 409。

4. `/llm/check-task`
   - 支持 `file` / `report` / `weaponry`。
   - 支持批量检查（`params` 多项）；`params` 必须为非空对象数组，任一非对象元素会使整次请求按既有 HTTP 400 错误体失败。
   - 去重只影响内部检查与恢复次数；单项缺失返回 HTTP 404、批量含缺失项仍返回 HTTP 200 的判定始终以原始 `params` 项数为准。
   - 负责人于 2026-07-25 明确同意后，成功响应已统一为 HTTP 200 空响应体；内部仍执行必要的同步回调补发，批量缺失项不阻断其余存在项处理，400/404 错误体保持不变。
   - 成功响应不公开任务状态、进度、回调状态、恢复结果或内部执行标识；调用方以既有业务键、Progress 与最终回调跟踪结果。

5. `/llm/progress`（WebSocket）
   - 阶段 1B-2 已完成控制面切换：当前只接受不带 `action` 的订阅消息；只要出现显式 `action` 就返回既有 `error` 结构、保持连接且不发送 ack。
   - `params` 中任一元素不是对象或任一业务键无效时，整条消息失败，不建立部分订阅；后续合法消息仍可继续使用同一连接。
   - 单连接可管理多个任务订阅。每个连接拥有独立的有界合并缓冲，后台任务线程只入队，由该连接的路由线程唯一执行 WebSocket `send`，慢连接不会在 Hub 锁内阻塞其他发布者。
   - 当前 Progress Hub/Adapter 仍是单实例内存实现，不提供跨进程广播、断线重放或可靠事件日志；50 条真实长连接和跨实例通知仍须在后续 Redis/MySQL 与集成压测阶段验收。

6. `/llm/chat`（文件对话体系）
   - 基于 SSE（Server-Sent Events）实现流式文本返回打字机效果。
   - 底座上强制 1 对话 = 1 Workspace + 1 Thread 的隔离限制以避污染；历史以本地已提交消息为权威来源。
   - 新建文件对话的 AnythingLLM Workspace 名称固定为 `chat-id{chatId}`；名称只由持久化业务身份生成，主 Thread 仍使用内部 `thread-{conversation_id}`。已经保存远端引用的历史会话直接复用原资源，不批量重命名。
   - 公开文件对话只接受 `chatId + fileNames`；原来复用该路径的 `architectureId` 模式已经下线，知识谱系对话改用独立的 `/llm/weaponry-chat*`。
   - `fileNames` 表示本轮前端显式文件范围，不是增量追加列表：任意非空列表整体替换会话 Active Scope；后续空数组复用当前 Active Scope，从而保持最后一次成功受理的非空范围。
   - 首次创建 `chatId` 且显式传入 `fileNames=[]` 时，受理事务把当时全部可用已解析文件冻结为 `automatic_initial` Scope；空知识库会冻结空范围，后续知识库新增文件不自动吸收。首次自动范围和任意显式范围都受 `DOCSENSE_CHAT_MAX_FILES` 约束。
   - AnythingLLM Workspace 与本地 binding heads 继续累计历史绑定，但每轮模型只接收 Effective Scope 的文档引用；累计绑定不能替代或扩大 Active Scope。
   - `/llm/chat/history` 只为 user 消息返回该轮前端显式文件，空请求固定为 `files: []`；assistant 消息不返回范围或来源 Chunk 字段。
   - 不可变会话 Binding、Scope Revision/Head、run input 与 pending message 在同一 SQLite 事务中提交，执行器只凭 `run_id` 恢复冻结的 Effective Scope。同一 `chatId` 冲突先于全局容量检查；File 身份删除后保留永久墓碑，不会把旧 `chatId` 误建成新会话。
   - 同一 `chatId` 同时只有一个活跃流；`abort` 只持久化中断请求，由流在事件边界收敛为 `aborted`。SSE 的 `done`、`aborted`、`error` 都会结束本轮，其中 `aborted` 表示回答不完整，不等同于成功完成。
   - 客户端关闭 SSE 后不继续后台生成：执行尚未开始时丢弃该轮 user；执行已开始时保留 user、不保存不完整 assistant，并以失败状态收敛。
   - `GET /llm/chat/history` 以本地对话库中状态为 `committed` 的消息为权威数据源，不从 AnythingLLM Thread 读取历史接口结果；默认数据库为 `${DOCSENSE_RUNTIME_DIR}/chat_sessions.sqlite3`，可由 `DOCSENSE_CHAT_DB` 覆盖。
   - `POST /llm/chat/title` 仅使用本地已提交消息生成标题；`POST /llm/chat/abort` 向当前活跃 run 写入持久化中断标志，执行器在后续事件边界观察到标志后发送 `aborted` 终态。没有活跃 run 时接口仍返回 HTTP 200，但 `aborted=false`。
   - `POST /llm/chat/delete` 先清理远端 Thread、Workspace 及相关资源租约，全部成功后再把本地会话标记为 `deleted`；清理失败时会保留 `cleanup_failed` 恢复记录，将会话置为 `error` 并返回错误。

7. `/llm/weaponry-chat`（知识谱系对话体系）
   - 五个接口固定使用 `businessType="weaponryChat"`，由 DocSense 可信业务 `userId` 与 `architectureId` 共同标识对话；不接收或返回公开 `chatId`，也不通过 AnythingLLM 用户上下文/Header 传递身份。新建 Workspace 的展示名称固定为 `wChat-user{userId}-arch{architectureId}`，主 Thread 仍使用内部 `conversation_id` 隔离。
   - 两个 ID 的公开安全范围均为 `1..9007199254740991`。POST 中 `userId` 必须是 JSON number；`architectureId` 兼容 JSON number 和十进制字符串。History Query 的两个 ID 都允许前导零，未知字段和重复 Query 参数均严格拒绝。
   - 首次成功受理只冻结该类别当时的直接文件，不包含子类别；后续不因类别新增、删除、重新解析或重新分类而刷新。任一候选缺少非空 `originalFileName` 时整次范围失败，不回退到 `fileName`。
   - AnythingLLM v1.15.0 流必须越过最后一个 `textResponseChunk(close=true)` 继续读取最终来源事件；常规回答以唯一 Finalization 收敛，Query 明确拒答允许 `textResponse(close=true, sources=[])` 收敛。其他缺失、重复或冲突终态均失败关闭。
   - 成功 SSE 在全部 `textChunk` 后恰好发送一次 `sourceChunks`，再发送 `done`；每项包含已删除开头完整 AnythingLLM `<document_metadata>` 包装的 `content`、`fileName` 和 `originalFileName`，剩余正文保持原值。非字符串/畸形 Metadata/清洗后空正文、未知或歧义来源、缺失原文件名均失败，不保存不完整 assistant 或 Chunk。
   - History 保持裸消息数组；assistant 的 `chunks` 与当轮 SSE `sourceChunks` 在内容和顺序上完全一致，user 消息不含 `chunks`。正文、全部 Chunk、成功事件和 Run 终态在同一 SQLite 事务提交。
   - 同一复合身份只允许一条未删除对话。删除必须先确认远端资源清理成功，再物理清除消息和 Chunk，只保留不含正文的最小审计事实并释放 Weaponry 身份；随后再次进入会创建新会话和新文件快照。
   - 当本地没有远端引用却发现同名 Workspace 时失败关闭，不自动认领或删除未知资源；供应商返回名称与目标名称不一致时补偿本次新建资源。应用日志可记录规范化业务 ID、精确 Workspace 名称、内部关联 ID 及必要资源引用，但禁止记录凭据、消息/模型/Chunk/文件正文、Prompt、原始请求响应或 SSE 帧。
   - 当前不对 `sourceChunks` 数量、清洗后 Chunk 正文长度或 History 响应大小设置独立业务上限；既有每轮文件数、消息/模型输出和进程内流并发限制仍有效。完整公开字段、状态码、Header 与事件顺序以 `docs/接口文档/知识谱系类别文件对话.md` 为准。

8. `/llm/reassign`（分类节点变更）
   - 这是即时同步过程接口，不产生额外后台队列任务和 HTTP 进度回调。
   - 安全方面要求调用前必须传输且一致匹配底库中存证的 `oldArchitectureId`。
   - 2026-07-24 的 1E-6 已将公开执行链切换为 `Parser → DocumentReassignmentService → Presenter`。
     路由不再直接构造 AnythingLLM Client、执行 SQL 或编排本地/远端写；Container 只装配一次
     `ReassignApplicationServices`，每次需要远端操作时仍由请求级 Knowledge Factory 创建隔离的
     Transport 与 deadline。
   - 同步 Saga 先以短事务保存 Operation/Step 写意图和 lease/fencing 事实，再在 UoW 外执行旧绑定
     解绑、目标 workspace 准备/复用、新绑定、Pin 与本地条件 CAS。`false`、缺少必要目标引用、CAS
     冲突和结果未知都不会伪装成功；远端明确失败或 CAS 冲突后会在同一有限请求预算内优先执行
     “解绑目标、恢复来源”的同步补偿，可证明的路径按五类固定文案返回，无法证明一致性时保留
     `recovery_required` 现场并返回既有 HTTP 500 结构。
   - `doc_path` 为空仍保持历史 local-only 兼容分支：不创建远端客户端，只执行同一条件本地更新。
     请求原始 ID 比较、旧 ID `int(...)` 转换时点保持不变；新 ID 只保留已冻结的 JSON `false`、
     有符号 64 位整数和十进制整数字符串兼容，其他值在创建 Operation 前沿用未包装 HTTP 500 边界拒绝。
    - 恢复仍是显式、精确 Operation 的内部能力。2026-07-25 的 1E-7 已将 Observer、Checkpoint
      Reconciler、Compensator 和 Finalizer 的实际算法拆入独立文件：Facade 只做命令校验、过期 lease
      接管与流程选择，四个协作器分别执行观察、检查点收敛、有序补偿及终态收口/隔离；不启动后台
      恢复线程。诊断脚本默认只读，真正恢复需要显式 `--apply` 与完整审计/lease 参数。
   - 本次未增删请求参数、响应字段、状态码、Header 或同步语义，也不向前端暴露 operation、lease、
     fencing、步骤或恢复事实。真实 AnythingLLM 故障演练、生产预算校准、可靠任务队列和多实例容量
     验收仍未完成，因此不得据此标记 production ready。

## 6. 快速启动

1. 安装依赖

仓库根目录当前只提供 `requirements.txt`，未提供 `environment.yml` 或 `requirements-venv.txt`。macOS 建议使用根目录 `.venv`：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Windows 可使用：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

下文的裸 `python` 命令均假设当前终端已经激活根目录 `.venv`；未激活时，macOS 使用 `.venv/bin/python`，Windows 使用 `.\.venv\Scripts\python.exe`。

扫描 PDF 的内置 OCR 还依赖系统安装的 Tesseract 及所配置语言的数据包；`requirements.txt` 只安装 Python 依赖，不安装这些系统组件。默认 analysis 扫描件引擎为 MinerU，需要相应的本地运行环境，或通过 `DOCSENSE_MINERU_API_URL` 复用外部 MinerU API；MinerU 或内置 OCR 失败时，代码会按既有降级链路继续处理原 PDF。

Office 97–2003 支持依赖本机稳定版 LibreOffice 26.2.x。标准 `.env.example` 默认显式开启，
代码在环境变量缺失时仍保持安全关闭；DocSense 不会在启动或任务执行时自动下载/安装
LibreOffice。仓库中的
[Legacy Office 离线交付说明](scripts/legacy_office/README.md) 提供固定 26.2.5 的
官方下载、SHA-256 校验、离线打包、安装和 preflight 流程；实际安装包、smoke 样本和
最终包只写入 Git 已忽略的 `dist/legacy-office/`。

2. 配置环境变量（建议使用 `.env`）

启动代码当前直接读取以下变量且未在代码中提供兜底值，使用 `run.py` 前必须通过环境变量或 `.env` 配置：

- `ANYTHINGLLM_BASE_URL`
- `ANYTHINGLLM_API_KEY`
- `APP_HOST`
- `APP_PORT`
- `APP_DEBUG`

`.env.example` 提供的示例值包括：

- `ANYTHINGLLM_BASE_URL=http://localhost:3001/api/v1`
- `APP_HOST=0.0.0.0`
- `APP_PORT=5001`
- `APP_DEBUG=true`

其他常用配置：

- `CALLBACK_URL`（可选；不配置则不主动回调外部系统，并将已完成任务标记为 `callback_status=skipped`。`.env.example` 中的“必须填写”注释与当前代码不一致，以此处及代码行为为准）
- `DOCSENSE_REPORT_*`：报告单实例 Dispatcher 的扫描批量、故障冷却、资源恢复、停机和
  cleanup 超时/租约配置；这些都是后端内部容量参数，不属于公开接口。默认值及约束见
  `.env.example`，其中清理租约必须严格大于清理 HTTP 超时。
- `DOCSENSE_REASSIGN_HTTP_TIMEOUT_SECONDS`、`DOCSENSE_REASSIGN_TOTAL_TIMEOUT_SECONDS`、
  `DOCSENSE_REASSIGN_COMPENSATION_RESERVE_SECONDS`：分类节点变更同步 Saga 的内部预算配置。Container
  在启动装配时读取并拒绝空值、非有限值、非正数或不能预留补偿窗口的组合；它们不属于公开接口参数。
  当前值仅完成离线边界验证，真实环境秒数仍须通过隔离供应商故障演练与容量校准后冻结。
- `DOCSENSE_REASSIGN_RUNTIME_MODE`：分类节点变更运行模式，当前唯一允许值为 `single_instance`。
  SQLite lease 使用进程本地时钟，配置为其他值会在装配阶段 fail-fast；迁移到数据库权威时间并完成
  跨实例 fencing 验证前，不得把该模块作为多实例运行链启用。
- `DOCSENSE_LEGACY_OFFICE_ENABLED`：Legacy Office 转换总开关。标准 `.env.example` 的部署
  默认值为 `true`；环境变量完全缺失时的代码安全默认值为 `false`。关闭时不探测
  LibreOffice，但 `.doc/.ppt/.xls` 任务会明确失败，禁止原样上传。
- `DOCSENSE_LIBREOFFICE_EXECUTABLE`：可选的 `soffice` 绝对路径；为空时依次检查
  Windows/macOS 标准安装路径和 `PATH`。Windows 非标准安装必须指向控制台入口
  `soffice.com`，不要指向可能等待 GUI/管道退出的 `soffice.exe`；标准安装留空即可自动选择。
- `DOCSENSE_LIBREOFFICE_ALLOWED_VERSION_SERIES=26.2`：允许的稳定版本系列。
  功能开启后，应用在公开路由/后台线程启动前执行 `--version` 门禁；缺失、
  `LibreOfficeDev`、空值或非 26.2.x 均 fail-fast。
- `DOCSENSE_LEGACY_OFFICE_TIMEOUT_SECONDS=120` 与
  `DOCSENSE_LEGACY_OFFICE_MAX_CONCURRENCY=1`：单次转换超时和进程内并发上限。
- `DOCSENSE_LEGACY_OFFICE_MAX_INPUT_BYTES=536870912` 与
  `DOCSENSE_LEGACY_OFFICE_MAX_OUTPUT_BYTES=1073741824`：内部输入/输出安全上限，
  默认分别为 512 MiB 和 1 GiB。任务私有目录固定派生为
  `${DOCSENSE_RUNTIME_DIR}/office_conversion/jobs`，不使用持久转换缓存。

Legacy Office 转换只按甲方保证正确的后缀路由，并额外校验 OLE2 文件签名。零字节、
伪造后缀、密码保护、损坏文件、超时、非零退出、缺少/多个产物、超限或 OOXML ZIP
结构不完整均 fail-closed。每次任务使用固定短输入名、独立 LibreOffice profile 和独立
输出目录；宏安全设为最高级，加载时禁止更新外链，不配置可信目录，输出只允许非宏
OOXML。OOXML 完整性校验在隔离子进程内执行，并在读取中央目录前按 ZIP/ZIP64 尾部
元数据预检：固定最多 100,000 个成员、中央目录最多 64 MiB；后续校验固定最多
120 秒，且成员声明的解压总量不得超过 4 GiB。这些门禁均不可通过环境变量放宽。
宏、ActiveX、嵌入对象、动画和
媒体不会执行，也不承诺完整保留。

转换质量验收只要求与同内容直接上传 `.docx/.pptx/.xlsx` 的现有链路等价，不额外承诺
PPT notes/OCR、XLS 公式计算结果/隐藏内容、动画/媒体，或三种 legacy 格式的
`originalText` 全部非空。XLS/XLSX 只接受恰好一个可解析工作表，多工作表
fail-closed。本轮交付平台为 Windows x64 与 macOS Apple Silicon；Windows 尚未实机认证。
Docker、macOS Intel、Windows ARM64 和 `.doc` 报告模板不在范围。

XLSX Collector Folder 存储库存可使用
`python scripts/inspect_xlsx_folder_inventory.py --knowledge-db-path <knowledge.sqlite3>` 只读查看。
脚本仅调用 AnythingLLM GET 接口并以 SQLite 只读模式比对永久知识引用，输出哈希与计数；没有
apply/delete 参数。已提交永久知识的历史 Folder 只报告、不自动删除，无本地引用的 Folder 也只
进入人工所有权复核。

实现与交付依据为 LibreOffice 官方的
[启动参数](https://help.libreoffice.org/latest/en-US/text/shared/guide/start_parameters.html)、
[转换 filter](https://help.libreoffice.org/latest/en-US/text/shared/guide/convertfilters.html)、
[宏安全](https://help.libreoffice.org/latest/en-US/text/shared/optionen/macrosecurity_sl.html)、
[外链更新策略](https://help.libreoffice.org/latest/en-GB/text/shared/optionen/01040900.html)
和[官方下载](https://www.libreoffice.org/download/)说明。

在 macOS 上复制 `.env.example` 后，必须将其中启用的 Windows 路径 `DOCSENSE_RUNTIME_DIR=C:/DocSenseRuntime` 改为 macOS 绝对路径，或注释/删除该行以使用仓库根目录 `.runtime`；其他平台相关路径也应按实际环境调整。

Weaponry 可选配置：

- `WEAPONRY_ANALYSE_MODE`：已删除的迁移期模式选择器。新链固定为 `file_aggregate_v1`；不配置或遗留值 `2` 可启动，旧值 `1` 及其他值会在应用装配阶段明确拒绝，不会切回遗留流程。
- `WEAPONRY_TERMS_RULE_CONTEXT_ENABLED`：是否启用可拔除的术语规则辅助上下文，默认 `false`；关闭时不读取其余术语专属配置，也不产生术语 Provider I/O。
- `WEAPONRY_TERMS_WORKSPACE_NAME`：术语规则版本化 AnythingLLM workspace 的基础名称，默认 `weaponry-terms-rules`；实际名称会自动追加目录内容摘要前缀。
- `WEAPONRY_TERMS_DIR`：本地术语卡目录，默认 `terms`。启用术语辅助时，服务端严格读取其中的 `term_rule_*.md`，按排序后的相对路径、`card_id` 和 UTF-8 内容自动生成 `terms-manifest-v1:sha256:<digest>` 指纹；不再读取或要求人工 `WEAPONRY_TERMS_CATALOG_FINGERPRINT`。
- Weaponry 取得本地单实例锁后、启动 Worker 前，会幂等准备该指纹对应的独立版本化 workspace：只补齐缺失规则卡并精确验证完整性，不修改目标文档 workspace，也不自动删除旧版本。上传或绑定结果无法确认时会持久化隔离并阻止 Worker 启动，禁止盲目重放。旧 execution 继续按其冻结指纹读取旧版本；升级前的人工指纹只读回退到原 workspace。
- `DOCSENSE_WEAPONRY_TERMS_CANDIDATE_TOP_N`：每个 `INPUT` 字段或每个普通 `TABLE` 列独立检索的规则卡候选数，默认 `5`。
- `DOCSENSE_WEAPONRY_TERMS_MAX_CONTEXT_CHARS`：单个业务字段最终注入的全部压缩规则文本总预算，默认 `1200`。服务端先完整保留所有命中列的核心合同；若该预算连全部核心合同都无法容纳，则本次术语辅助整体降级为空并记录 `terms_rule_context_budget_insufficient`，不会只截掉靠后的列。
- `DOCSENSE_WEAPONRY_*`：武器谱单实例 Dispatcher、维护扫描、清理超时/租约、供应商能力指纹和固定 score/rank、引用型正文筛选、Extraction 策略。默认值及必填项见 `.env.example`；这些变量均为内部运行配置，不属于公开接口。
- 生产环境必须提供 provider、embedding、文档处理和 extraction model 四类真实能力指纹，配置
  `DOCSENSE_WEAPONRY_PRODUCTION_ATTESTATION_PATH`，并将
  `DOCSENSE_WEAPONRY_REQUIRE_PRODUCTION_GATE=true`。指纹或证明缺失、漂移、过期、被篡改时，
  应用在生产必需模式下会于公开路由和后台线程启动前失败；开发环境默认只把 readiness 保持为
  false。

3. 启动服务

```bash
python run.py
```

按 `.env.example` 的 `APP_HOST`、`APP_PORT` 示例值配置时，监听地址为 `http://0.0.0.0:5001`。

4. 本地调试页面（可选）

回调调试页可用性：

- 页面和数据接口始终可访问；没有回调历史时会展示空状态
- 如需由当前服务产生新的回调历史，需要配置 `CALLBACK_URL`，并触发一次文件解析、报告生成或武器装备知识谱系解析回调

回调调试页访问：

- 页面：`http://127.0.0.1:5001/debug/callback`
- 数据：`http://127.0.0.1:5001/debug/api/callback`

回调调试页说明：

- 新回调 JSON 历史记录统一保存在 `${DOCSENSE_RUNTIME_DIR}/callback/`（默认仓库根目录 `.runtime/callback/`）
- `/debug/callback` 默认展示回调历史目录下最新一条回调，并可在页面中选择最近历史记录
- `/debug/api/callback?record=<json文件名>` 可读取指定历史回调文件；不再兜底读取旧版 `${DOCSENSE_RUNTIME_DIR}/call_back.json`
- `file` 回调会结构化展示摘要信息、原文和翻译预览
- `report` 回调会结构化展示报告信息和 HTML 报告预览
- `weaponry` 回调会结构化展示字段抽取结果和溯源信息
- 若当前还没有新版回调历史文件，页面会显示空状态提示

文件对话调试页前提：

- `ANYTHINGLLM_API_KEY` 已配置
- 至少已有一个成功解析并入库的文件，供 `fileNames` 选择

文件对话调试页访问：

- 页面：`http://127.0.0.1:5001/debug/chat`
- 初始化数据：`http://127.0.0.1:5001/debug/api/chat/bootstrap`

文件对话调试页说明：

- `/debug/chat` 不写入也不依赖 `${DOCSENSE_RUNTIME_DIR}/call_back.json`
- 页面直接联调正式接口 `POST /llm/chat`、`GET /llm/chat/history`、`POST /llm/chat/delete`
- 页面左侧展示本地 `chat_sessions.sqlite3` 中的会话及 Active Scope 文件数，文件选择来自 `knowledge_base.sqlite3` 中已解析文件记录；累计 Workspace bindings 不作为当前模型范围
- 文件选择器以“已选标签 + 添加文件面板”展示，支持勾选与取消勾选
- SSE 主输出在聊天主区域实时显示，调试事件收纳于折叠详情中
- 该调试页仅用于本地联调文件对话模块，不参与甲方真实回调链路

安全说明：`/debug/*` 路由当前始终注册，`APP_DEBUG=false` 只关闭 Flask 调试模式，不会关闭这些路由。生产部署必须通过防火墙、监听地址或反向代理限制访问，不能把 `APP_DEBUG=false` 当作调试路由访问控制。

## 7. 运行时路径与持久化

未配置组件级兼容覆盖项时，运行时文件统一派生自绝对根目录 `DOCSENSE_RUNTIME_DIR`。Windows 推荐使用正斜杠，例如：

```env
DOCSENSE_RUNTIME_DIR=C:/.me/envs/DocSenseEnv
```

macOS 使用 POSIX 绝对路径，例如：

```env
DOCSENSE_RUNTIME_DIR=/Users/your-name/DocSenseRuntime
```

显式配置时必须使用当前平台可识别的绝对路径；`.env.example` 中启用的 `C:/DocSenseRuntime` 仅适用于 Windows，在 macOS 上必须修改或注释。未配置时为了向后兼容，默认使用仓库根目录 `.runtime`。目录结构如下：

- 任务库：`${DOCSENSE_RUNTIME_DIR}/llm_tasks.sqlite3`
- 知识库映射库：`${DOCSENSE_RUNTIME_DIR}/knowledge_base.sqlite3`
- 对话状态库：`${DOCSENSE_RUNTIME_DIR}/chat_sessions.sqlite3`
- 任务私有文件：`${DOCSENSE_RUNTIME_DIR}/tasks/`
- 共享文档 Artifact：`${DOCSENSE_RUNTIME_DIR}/document_processing/artifacts/`
- 文档转换物化文件：`${DOCSENSE_RUNTIME_DIR}/document_processing/materializations/`
- RAG 投影物化文件：`${DOCSENSE_RUNTIME_DIR}/document_processing/rag_projection_materializations/`
- Legacy Office 临时任务：`${DOCSENSE_RUNTIME_DIR}/office_conversion/jobs/`
- 回调历史：`${DOCSENSE_RUNTIME_DIR}/callback/`
- SQLite JSON 导出：`${DOCSENSE_RUNTIME_DIR}/sqlite/`
- 旧版回调预览：`${DOCSENSE_RUNTIME_DIR}/call_back.json`

旧的 `llm_downloads`、`ocr_markdown` 和 `mineru_markdown` 已退出当前生产组合根，应用加载配置时
不会再预创建这些目录。文件下载由任务私有目录承载，OCR/MinerU 结果由共享 DocumentProcessing
Artifact 与物化目录管理。Legacy Office 的 `jobs` 目录仍是有效的短生命周期工作目录；转换任务
结束后会清理其拥有的 `job-*` 子目录，启动时也会扫描可安全回收的遗留任务。

文件对话当前以 SQLite 单实例模式运行：同一个 `chat_sessions.sqlite3` 只能由一个应用副本使用，不能放在网络共享目录模拟多实例。`DOCSENSE_CHAT_RUNTIME_MODE` 必须为 `single_instance`（默认值）；配置 `cluster`、外部调度或其他未安装模式时，应用会在依赖装配阶段拒绝启动，而不会以共享 SQLite 文件伪装集群能力。

为保护该模式下的资源，`DOCSENSE_CHAT_MAX_FILES`、`DOCSENSE_CHAT_MAX_MESSAGE_CHARS`、`DOCSENSE_CHAT_MAX_OUTPUT_CHARS` 和 `DOCSENSE_CHAT_MAX_CONCURRENT_STREAMS` 分别限制单轮文件数、消息/输出长度和进程内同时流数。持久化能力、运行租约、取消通知和资源清理均通过内部可替换边界装配：当前实现只提供本地事务、同步执行、持久化取消轮询和同步清理。删除和标题临时资源会先写入持久化清理任务；当前内联执行器只同步处理本次新建任务，失败记录不会被伪装成已具备自动延迟重试能力。不提供事务 outbox、可靠队列、跨实例通知或 fencing。数据库迁移、可靠调度与多实例部署尚未启用；在选型、迁移和故障演练完成前，不得开放对应运行模式。

知识谱系对话当前不对 `sourceChunks` 数量、清洗后 Chunk 正文长度或 History 响应大小设置独立
业务上限，也不分页、截断、去重或执行 Metadata 清洗之外的正文改写；上述既有文件数和模型输出限制仍照常生效。部署前必须按实际
数据规模执行容量验证，并完成以下日志安全检查：

- Web Server、API Gateway、负载均衡和反向代理不得记录
  `/llm/weaponry-chat/history` 的原始 Query String；若平台不能关闭 Query 记录，至少必须在写入
  前删除或不可逆脱敏 `userId`，并验证错误日志和追踪系统没有保留原值；
- 五个知识谱系对话接口均不得启用请求体、SSE 帧或响应体采集，避免消息、Chunk、`fileName`、
  `originalFileName` 和内部来源信息进入访问日志、APM 或异常快照；
- 应用层日志捕获测试只能证明 DocSense Python 日志的已覆盖路径，不代表上游网关、Web Server、
  Sidecar 或日志采集器已经脱敏；发布验收必须逐层检查实际日志配置和脱敏样例。

报告 Dispatcher 当前同样只允许 `single_instance`，但会额外取得
`${DOCSENSE_RUNTIME_DIR}/locks/report-dispatcher.lock` 的操作系统文件锁；第二个进程在公开
应用创建完成前拒绝启动。当前模式禁止 preload/fork，`run.py` 已关闭 Werkzeug reloader。
报告执行固定为一条重型 Worker，资源恢复与队列诊断各有一条固定维护线程；accepted 积压
只保存在 SQLite，`Event` 不保存任务列表。领取前毒任务和坏资源记录使用持久冷却让出扫描
首页，但不对正常积压设置数量上限。该实现仍不是 RabbitMQ 可靠队列，也不支持多实例。

当前仍对外保留说明的数据库组件级变量可作为兼容覆盖项，一旦配置就会优先于统一根目录。
若希望持久数据库全部位于同一目录，应删除这些覆盖项：

- 任务库：`DOCSENSE_LLM_TASK_DB`；未覆盖时为 `${DOCSENSE_RUNTIME_DIR}/llm_tasks.sqlite3`
- 知识库映射库：`DOCSENSE_KNOWLEDGE_BASE_DB` 或兼容变量 `KNOWLEDGE_BASE_DB_PATH`；未覆盖时为 `${DOCSENSE_RUNTIME_DIR}/knowledge_base.sqlite3`
- 对话状态库：`DOCSENSE_CHAT_DB`；未覆盖时为 `${DOCSENSE_RUNTIME_DIR}/chat_sessions.sqlite3`
- 回调历史固定为 `${DOCSENSE_RUNTIME_DIR}/callback/`
- 旧版最近一次回调预览固定为 `${DOCSENSE_RUNTIME_DIR}/call_back.json`；这是历史兼容遗留文件，当前新回调和 debug 页不再更新或读取

## 8. 本地联调与测试

任务库 JSON 导出脚本：

```bash
python scripts/inspect_llm_tasks.py
```

`scripts/inspect_llm_tasks.py` 可在 Windows 和 macOS 上运行。脚本默认读取 `${DOCSENSE_RUNTIME_DIR}/llm_tasks.sqlite3`，也会遵循 `DOCSENSE_LLM_TASK_DB` 指定的兼容覆盖路径；导出时会自动创建 `${DOCSENSE_RUNTIME_DIR}/sqlite/`，并写入按时间戳命名的 JSON 文件。

导出的 JSON 顶层包含：

- `metadata`：导出时间、来源数据库路径、输出文件路径、SQLite 版本、表数量和总行数
- `tables`：每张表的表名、建表 SQL、列定义、行数和完整行数据

Weaponry 生产能力证明只能在已配置四类真实指纹后生成。验证器使用两个随机临时 workspace 和
一个临时 thread，复核真实 score/rank、来源身份、空 workspace Provided-Evidence 隔离与清理
基线；它只把一份既有全局文档绑定到临时 workspace，不修改既有 workspace 或文档：

```powershell
venv\Scripts\python.exe -B scripts\verify_weaponry_production_readiness.py `
  --environment production-local `
  --output C:\DocSenseRuntime\weaponry-production-attestation.json
venv\Scripts\python.exe -B scripts\check_weaponry_production_gate.py
```

证明默认 24 小时过期，最长不得超过 7 天；更新使用同目录原子替换。部署示例已把生产门禁设为
必需，不能用手写布尔值、Fake 测试或进程存活替代该证明。

Weaponry callback 投递结果未知或远端资源清理结果未知时，系统会保守冻结。人工对账使用内部
命令，不新增 HTTP 接口；改变状态的命令必须显式提供操作者、原因和确认标志，并写追加审计：

```powershell
# 先脱敏查看资源状态和历史处置审计
venv\Scripts\python.exe -B scripts\manage_weaponry_operations.py `
  inspect-resources --task-id <task-id>

# 已确认远端资源仍存在，允许恢复循环重试清理
venv\Scripts\python.exe -B scripts\manage_weaponry_operations.py `
  resolve-resources --task-id <task-id> --resolution retry_cleanup `
  --operator <operator> --reason <reason> --external-state-confirmed

# 已确认旧 Worker 停止或隔离，解除该 architectureId 的 callback unknown 提交门禁
venv\Scripts\python.exe -B scripts\manage_weaponry_operations.py `
  release-callback --architecture-id <id> --operator <operator> `
  --reason <reason> --worker-stopped-confirmed
```

全局 `--db-path` 如需覆盖必须写在子命令之前。禁止直接改 SQLite 状态绕过活跃 execution、CAS、
fencing 或审计检查。

其中 `llm_tasks.rows[]` 的每一项对应一条 LLM 任务记录，常用字段包括 `business_type`、`business_key`、`request_payload`、`status`、`progress`、`result_payload`、`callback_status`、`callback_attempts`、`created_at` 和 `updated_at`。脚本会把 `request_payload`、`result_payload` 这类 JSON 字符串自动展开为对象，便于直接查看原始请求和最终结果。可通过 `--db-path` 和 `--output-dir` 指定其他 SQLite 文件或输出目录。

### `/llm/analysis` 存量 `security` 字段迁移

升级到 `security` 字段后，必须先停止 DocSense，再迁移任务库、知识库 metadata 和回调历史中的旧 `secrets` 键。脚本默认只做全量预检，仅显式传入 `--apply` 时才会备份并改写数据：

```bash
# 1. DocSense 停服后预检命中数
.venv/bin/python scripts/migrate_analysis_security.py

# 2. 备份并执行迁移
.venv/bin/python scripts/migrate_analysis_security.py --apply

# 3. 再次预检，changedTargets 和 renamedKeys 应均为 0
.venv/bin/python scripts/migrate_analysis_security.py
```

迁移备份和包含源文件/备份文件哈希的 manifest 写入 `${DOCSENSE_RUNTIME_DIR}/migration_backups/analysis-security-<timestamp>/`。同值新旧双键会保留 `security` 并删除 `secrets`；异值双键或非法 JSON 会在改写前直接终止。脚本不改写 LLM 审计 Prompt、模型原始响应、attempt raw response 和 trace digest，也不扫描历史导出、E2E 快照或方案文档。自定义路径时可使用 `--runtime-dir`、`--task-db` 和 `--knowledge-db`。

本地联调脚本（PowerShell）：

```powershell
pwsh -NoLogo -Command "./scripts/start_test_file_server.ps1"
pwsh -NoLogo -Command "python scripts/mock_callback_server.py"
pwsh -NoLogo -Command "./scripts/test_llm_analysis.ps1"
pwsh -NoLogo -Command "./scripts/test_llm_report.ps1"
pwsh -NoLogo -Command "./scripts/test_llm_weaponry.ps1"
pwsh -NoLogo -Command "./scripts/test_llm_check_task.ps1"
pwsh -NoLogo -Command "./scripts/test_llm_progress.ps1"
```

本地联调脚本（macOS / zsh）：

```bash
zsh scripts/start_test_file_server.sh
python scripts/mock_callback_server.py
zsh scripts/test_llm_analysis.sh
zsh scripts/test_llm_report.sh
zsh scripts/test_llm_weaponry.sh
zsh scripts/test_llm_check_task.sh
zsh scripts/test_llm_progress.sh
```

HTTP/WS 请求 wrapper 的默认行为：

- `test_llm_analysis`、`test_llm_report`、`test_llm_weaponry`、`test_llm_check_task`、`test_llm_progress` 的 zsh/PowerShell wrapper 会读取仓库根目录 `.env`，不存在时回退 `.env.example`；`start_test_file_server` 不读取环境文件，`mock_callback_server.py` 只通过 `python-dotenv` 读取 `.env`
- `test_llm_analysis.sh` 默认使用本地 `tests/fixtures/llm/analysis_request.json` 请求 `POST /llm/analysis`
- `test_llm_report.sh` 默认使用本地 `tests/fixtures/llm/report_request.json` 请求 `POST /llm/generate-report`
- `test_llm_weaponry.sh` 默认使用本地 `tests/fixtures/llm/weaponry_request.json` 请求 `POST /llm/weaponry`
- `test_llm_check_task.sh` 默认使用本地 `tests/fixtures/llm/check_task_file_request.json` 请求 `POST /llm/check-task`
- `test_llm_progress.sh` 默认使用同一 check-task 请求文件连接 `WS /llm/progress`

`tests/fixtures/llm/*.json` 当前受 `.gitignore` 规则排除，仓库未跟踪上述默认请求文件；当前工作区可能存在个人联调产物，但不能假设干净克隆后可用。运行脚本前需要自行创建相应 JSON，或把请求文件路径作为第二个参数显式传入。

可选参数示例（先自行创建这些本地请求 JSON，或替换为实际 payload 路径）：

```bash
zsh scripts/start_test_file_server.sh 8000 tests/fixtures/files
zsh scripts/test_llm_analysis.sh http://127.0.0.1:5001 tests/fixtures/llm/analysis_request.json
zsh scripts/test_llm_report.sh http://127.0.0.1:5001 tests/fixtures/llm/report_request.json
zsh scripts/test_llm_weaponry.sh http://127.0.0.1:5001 tests/fixtures/llm/weaponry_request.json
zsh scripts/test_llm_check_task.sh http://127.0.0.1:5001 tests/fixtures/llm/check_task_file_request.json
zsh scripts/test_llm_progress.sh ws://127.0.0.1:5001/llm/progress tests/fixtures/llm/check_task_file_request.json 5 false
```

领域召回 benchmark 可直接读取包含 `params[0].architectureList` 的请求 JSON；输出只包含 tree fingerprint、query digest、候选数、Prompt 字符数和召回指标等有界统计，不输出正文。`finalCandidateRecall` 统计模型最终可见候选，能计入合同允许的可靠父节点，是发布门禁的主指标；`recallAt64` 只统计基础叶子 Top-64，保留为诊断指标：

```bash
.venv/bin/python scripts/benchmark_architecture_recall.py \
  --tree-json /path/to/analysis-request-with-full-tree.json \
  --cases-json /path/to/architecture-recall-cases.json \
  --min-final-candidate-recall 1.0
```

启用任一阈值后，每个 case 都必须提供 `goldIds` 或 `gold_ids`。如需同时约束基础叶子诊断指标，可增加 `--min-base-leaf-recall-at-64 0.95`。退出码 `0` 表示报告生成成功且门禁通过（或未启用门禁），`1` 表示指标未达门禁，`2` 表示输入或门禁配置非法；质量失败仍会输出有界 JSON 指标，便于定位排名，但不会回显正文。

三文件真实 E2E 必须直接读取验收提供的 `文件解析领域树.json` 中 `.params[0].architectureList` 的完整节点内容。当前固定验收树为 6,822 个节点；构造多文件请求时须逐项保持节点原始顺序与字段内容，不得改用默认树、合成树、截断树或二次生成的节点。外部 fixture 的本机绝对路径和原始运行产物不提交到仓库。

目录级武器装备字段抽取自动化脚本：

```bash
# macOS / zsh
APP_DEBUG=false python run.py
zsh scripts/test_llm_weaponry_directory.sh "测试文件-水面装备" --base-url http://127.0.0.1:5001

# Windows / PowerShell
$env:APP_DEBUG = "false"
python run.py
pwsh -NoLogo -Command "./scripts/test_llm_weaponry_directory.ps1 '测试文件-水面装备' --base-url http://127.0.0.1:5001"
```

`test_llm_weaponry_directory` 会自动为指定目录启动临时静态文件服务，按文件串行执行 `/llm/analysis -> /llm/weaponry -> /llm/check-task`，并输出：

- 主表：`qwen3-4b-new.csv`
- Markdown 主表：`qwen3-4b-new.md`
- 来源核验表：`qwen3-4b-new_source_audit.csv/json`
- 运行记录：`qwen3-4b-new_manifest.json`
- 每个文件的请求、响应、任务结果与知识库映射快照

常用参数：

```bash
zsh scripts/test_llm_weaponry_directory.sh "测试文件-水面装备" \
  --pattern "*.pdf" \
  --output-dir "/var/lib/docsense/runtime/weaponry_surface_extract_manual" \
  --architecture-base 993000000

pwsh -NoLogo -Command "./scripts/test_llm_weaponry_directory.ps1 '测试文件-水面装备' --pattern '*.pdf' --output-dir 'C:/.me/envs/DocSenseEnv/weaponry_surface_extract_manual' --architecture-base 993000000"
```

如需递归扫描子目录，加 `--recursive`；如只想确认会扫描哪些文件和使用哪些临时 `architectureId`，加 `--dry-run`。脚本会自动避开已存在的 `architectureId`，防止旧 workspace/document 记录污染本轮结果。

目录 runner 默认仍使用既有 75 字段模板。需要专门核验上述保留字段合同时，可增加默认关闭的
`--verify-forced-empty-contract`：

```bash
zsh scripts/test_llm_weaponry_directory.sh "测试文件-水面装备" \
  --base-url http://127.0.0.1:5001 \
  --output-dir "/var/lib/docsense/runtime/weaponry_forced_empty_contract" \
  --verify-forced-empty-contract
```

该模式使用最小探针：5 个顶层保留 `INPUT`、普通对照字段 `舷号`、同时含 5 个保留列和普通列的
混合 `TABLE`，以及仅含保留列的 `TABLE`。保留字段不计入 `extractable_field_count`、
`non_empty_count` 或 `missing_fields`，违规非空值单独记录；manifest 另行记录顶层、两类 TABLE、
普通字段对照、callback 状态和 interaction audit 的合同核验结果。

由于 `/llm/check-task` 成功时返回 HTTP 200 空响应体，runner 只用该请求触发必要的 callback
恢复，任务终态改从与 DocSense 服务相同的隔离任务 SQLite 轮询。服务与 runner 必须指向同一个
`DOCSENSE_LLM_TASK_DB`；HTTP 200 后找不到对应业务键的任务行时，runner 会立即报告数据库路径
不一致，而不是继续空转。真实合同核验应使用全新 `DOCSENSE_RUNTIME_DIR`/输出目录，并配置且
启动本地 mock callback 服务，避免历史任务、回调和审计记录混入本轮证据。

测试文件批量武器装备字段抽取复现流程：

1. 确认 AnythingLLM 可用并启动 DocSense，目录批跑建议使用 `APP_DEBUG=false python run.py`。Weaponry 新链固定采用 `file_aggregate_v1`，无需再设置模式变量。只有在 `.env` 配置了本地 mock 的 `CALLBACK_URL` 且需要核验真实回调时，才需要另行启动 `python scripts/mock_callback_server.py`。
2. 先执行带 `--dry-run` 的目录 wrapper，核对扫描文件、输出目录和临时 `architectureId`。runner 会读取知识库映射并自动避开已存在的 ID；如使用自定义 `--architecture-base`，仍应检查 dry-run manifest 是否符合预期。
3. 去掉 `--dry-run` 执行目录 wrapper。未提供 `--static-base` 时，runner 会自动为目标目录启动临时静态文件服务；提供该参数时才复用调用方已有的文件服务。
4. runner 对每个目标 PDF 串行提交 `/llm/analysis`、调用 `/llm/check-task` 触发必要的回调恢复并从同一任务 SQLite 轮询终态、核验当前临时分类的文档隔离，再提交 `/llm/weaponry` 并按相同方式轮询。默认请求模板中的 75 个字段由脚本内置，调用方无需手工逐字段构造；只有显式传入 `--verify-forced-empty-contract` 时才改用最小合同探针。
5. 术语规则辅助默认关闭。显式启用后只需配置本地 `WEAPONRY_TERMS_DIR` 和稳定的 `WEAPONRY_TERMS_WORKSPACE_NAME` 基础名称；服务端在启动期自动计算内容指纹、创建或复用对应的版本化 workspace、上传缺失规则卡并验证绑定完整性。可先执行 `venv\Scripts\python.exe -B scripts\sync_weaponry_terms_catalog.py --dry-run` 只读查看计划，再用 `--apply` 调用同一协调器显式同步；`--apply` 会取得与在线服务相同的 Weaponry 单实例锁，锁被占用时拒绝并发写入。运行链不会修改目标文档 workspace，也不会自动删除旧术语版本。验证日志中的 `policy_id` 应为 `terms-rules-column-compact-v2`，并应逐字段列出实际命中的 `card_ids`。历史任务的回调补发只会重发已持久化的 `result_payload`，不会重新执行模型抽取，也不会自动补齐新单位格式。
6. runner 持续写出 `qwen3-4b-new.csv`、`qwen3-4b-new.md`、来源核验表、manifest 和逐文件快照；来源核验表用于确认装备事实来源不是 `term_rule_*.md`。

Windows 与 macOS 可按各自环境选择对应脚本。

本地调试页联调建议：

1. 启动服务：`python run.py`
2. 若联调回调型业务，触发一次 `/llm/analysis`、`/llm/generate-report` 或 `/llm/weaponry`
3. 打开 `http://127.0.0.1:5001/debug/callback`
4. 若要比对原始回调报文，优先查看 `${DOCSENSE_RUNTIME_DIR}/callback/` 下按业务键和时间戳保存的历史 JSON
5. 若联调文件对话，先确保至少有一个已解析文件，再打开 `http://127.0.0.1:5001/debug/chat`
6. 在 `/debug/chat` 中可直接完成发送消息、查看历史、删除会话三类联调

单元测试（仓库默认 `unittest`）：

```bash
.venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

完整发现命令不是当前开发环境的安全默认命令：其中部分环境测试可能启动本地 `run.py`/Shell，
部分资产测试直接读取受 `.gitignore` 排除的 `tests/fixtures/llm/*.json`，且 Windows 对个别
POSIX 权限位断言没有等价表达。执行前应先核对 `tests/README.md` 中的排除清单和适用边界，
并如实记录本次发现、排除与执行结果；不得将历史分支的通过数量视为当前集成基线。

核对 Analysis 树索引、Top-K 召回、两阶段 Prompt、配置默认值、callback debug 路由和安全迁移时，
可运行不依赖本地请求文件的定向测试：

```bash
.venv/bin/python -m unittest \
  tests.test_architecture_tree \
  tests.test_architecture_recall_service \
  tests.test_architecture_recall_benchmark \
  tests.test_analysis_prompts \
  tests.test_analysis_classification_config \
  tests.test_analysis_service \
  tests.test_range_defaults \
  tests.test_callback_debug_routes \
  tests.test_migrate_analysis_security
```

## 9. 协议文档

- 文件处理与报告生成：`docs/接口文档/文件处理和报告生成.md`
- 知识谱系解析：`docs/接口文档/知识谱系解析.md`
- 文件对话：`docs/接口文档/文件对话.md`
- 文件对话新增接口：`docs/接口文档/文件对话新增接口.md`
- 知识谱系对话：`docs/接口文档/知识谱系类别文件对话.md`
- 节点分类与文档变更：`docs/接口文档/分类节点变更.md`

## 10. Git 规范

提交信息格式：`type: description`

- `feat`：新增功能
- `fix`：修复 bug
- `docs`：文档更新
- `refactor`：代码重构
- `test`：测试相关
- `chore`：其他变更（依赖、配置等）

分支规范：

- `main`：稳定版本，随时可部署
- `feature/xxx`：新功能开发
- `hotfix/xxx`：紧急修复
- `docs/xxx`：文档更新
- `test/xxx`：测试相关
