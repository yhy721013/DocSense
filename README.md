# DocSense - 甲方协议 LLM 接口后端

DocSense 当前以甲方协议后端接口服务为主，聚焦 LLM 任务处理能力；同时提供调试路由，用于查看已落盘的回调结果及联调文件对话模块。`/debug/*` 不属于甲方协议或生产回调链路，当前也不会随 `APP_DEBUG=false` 自动关闭，部署时必须在网络或反向代理层限制访问。

## 1. 核心能力

- 文件解析：`POST /llm/analysis`
- 报告生成：`POST /llm/generate-report`
- 武器装备知识谱系解析：`POST /llm/weaponry`
- 文件内容对话：`POST /llm/chat`（附加标题生成 `POST /llm/chat/title`、历史查询 `GET /llm/chat/history`、生成中断 `POST /llm/chat/abort` 及删除 `POST /llm/chat/delete`）
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
| 接口层 | `app/blueprints/` | HTTP/WS/SSE 入参校验、任务受理、线程派发、协议流式及常态响应、本地调试入口 | `llm.py` `debug.py` |
| 应用端口层 | `app/ports/` | 定义供应商无关的 RAG、长期知识库、文件对话及任务级 Factory 契约 | `rag.py` `knowledge_index.py` `chat.py` |
| 业务层 | `app/services/llm_service/`、`app/services/chat/` | 文件解析、报告生成、谱系提取、任务状态管理、翻译编排，以及文件对话应用服务与本地持久化/锁实现 | `analysis_service.py` `report_service.py` `weaponry_service.py` `task_service.py` `chat/application/` `chat/persistence/` |
| 应用装配层 | `app/container.py` | 组装应用服务、供应商 Factory、配置和上传并发限制器 | `ApplicationServices` `create_application_services()` |
| 核心基础层 | `app/services/core/` | 配置、路径、日志、数据库、进度中枢和 Prompt 构建 | `config.py` `settings.py` `database.py` `progress_hub.py` `prompts.py` |
| 外部集成层 | `app/integrations/anythingllm/` | AnythingLLM Transport、执行策略、原子 Client、纯方案 B Gateway 与任务级 Factory | `transport.py` `policies.py` `documents.py` `workspaces.py` `threads.py` `rag_gateway.py` `factory.py` |
| 迁移期工具层 | `app/services/utils/` | 回调、文件/OCR 预处理，以及尚未迁移完成的 legacy AnythingLLM Facade/RAG 流程 | `anythingllm_client.py` `rag_pipeline.py` `callback_client.py` `file_downloader.py` `ocr_preprocessor.py` |
| 翻译能力层 | `app/services/translator/` | 文档/文本翻译底层实现，被业务翻译服务封装调用 | `core.py` `document_handler.py` `mhtml_handler.py` `txt_handler.py` |

### 2.2 主要调用方向

1. `blueprints -> llm_service/chat application`：大部分后台长流程已下沉至服务层；`/llm/reassign` 的同步编排及部分文件对话协议桥接仍位于蓝图中。
2. `blueprints -> app.container`：从 Flask 应用扩展读取服务与无状态 Factory，不创建模块级服务单例。
3. `llm_service -> ports`：新链路只依赖供应商无关 Port/Factory；旧链路在迁移期仍使用 legacy Facade。
4. `integrations.anythingllm -> ports`：Gateway 实现端口；新链路每次进入 Factory 租约时创建独立 Transport，legacy report/weaponry 链路不经过该 Factory。
5. `llm_service -> core/utils`：写任务状态、发布进度、下载和规范化文件、发送回调。
6. `llm_service.translation_service -> translator`：翻译能力由 `translation_service.py` 统一编排。
7. `check-task -> task_service.replay_callback_if_needed`：用于成功/失败任务的回调补发。

### 2.3 analysis/report/weaponry 请求到回调的链路

```text
Client Request
  -> app/blueprints/llm.py
    -> LLMTaskService 创建/更新任务
    -> 后台线程执行 llm_service 任务
      -> ports/integrations 或迁移期 utils 下载、预处理并调用 AnythingLLM
      -> core.progress_hub 推送 WS 进度
      -> 组装业务结果并写入任务库
      -> utils.callback_client 回调业务系统
```

## 3. 当前目录（关键部分）

```text
app/
  __init__.py                       # Flask App 工厂，安装依赖容器并注册蓝图
  container.py                      # 应用装配根、ApplicationServices 与 analysis/report 任务限制器
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
    llm_service/
      analysis_service.py           # 文件解析主流程（含 mhtml/OCR/翻译编排）
      report_service.py             # 报告生成主流程
      weaponry_service.py           # 知识谱系字段提取主流程
      task_service.py               # 任务状态、结果、回调状态持久化
      translation_service.py        # 翻译服务编排层
    chat/
      application/                  # 对话命令、历史、标题、中断、删除与运行执行器
      persistence/                  # 本地消息、会话及资源租约持久化
      locking/                      # 会话级锁服务
    utils/
      anythingllm_client.py         # AnythingLLM HTTP 客户端
      callback_client.py            # 回调发送
      callback_preview.py           # 本地回调预览读取
      chat_debug_preview.py         # 本地文件对话调试页初始化数据聚合
      file_downloader.py            # 下载到临时文件
      mhtml_normalizer.py           # mhtml/mht 归一化
      ocr_preprocessor.py           # 扫描件 OCR 预处理
      rag_pipeline.py               # 文件上传 + RAG 调用流水线
    translator/                     # 翻译底层能力
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
- `skipped`：任务已完成，但当前部署未配置 `CALLBACK_URL`，因此无需向外部系统回调

`/llm/check-task` 只会在当前已配置 `CALLBACK_URL` 时补发 `pending` 或 `failed` 的终态结果。`skipped` 不增加回调尝试次数且不可重放；任务进入该状态后再配置 URL，也不会通过 check-task 自动补发。

## 5. 接口行为说明（按当前代码核对）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/llm/analysis` | 文件解析，支持 `params` 多文件顺序串行处理 |
| POST | `/llm/generate-report` | 报告生成，使用 `params[0]` |
| POST | `/llm/weaponry` | 武器装备知识谱系字段提取 |
| POST | `/llm/check-task` | 查询任务状态，必要时补发回调 |
| WS | `/llm/progress` | 进度订阅/查询/取消订阅 |
| POST | `/llm/chat` | 基于指定文件内容发起对话请求（SSE 流式响应下发） |
| POST | `/llm/chat/title` | 根据指定会话的本地已提交消息生成标题 |
| GET | `/llm/chat/history` | 查询指定会话在本地持久化的已提交消息 |
| POST | `/llm/chat/abort` | 请求中断指定会话当前活跃的生成任务 |
| POST | `/llm/chat/delete` | 清理远端资源，全部成功后将本地会话标记为 `deleted` |
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
   - 同请求可提交多个文件，服务端按数组顺序串行执行。
   - 支持 `mhtml/mht`，会先归一化正文再进入解析。
   - 扫描件 PDF 在 `/llm/analysis` 中默认先经 MinerU 解析为 Markdown，再上传到 AnythingLLM；MinerU 失败时降级为既有 OCR Markdown，再失败才直传原 PDF。
   - `params[].originalFileName` 表示原文件名，当前作为请求上下文进入文件解析提示词，后续可继续用于业务链路。
   - `params[].channel` 表示资料来源机构候选范围（字典编码），服务端只使用请求中提供的候选，不再注入“装发、军情、科技、训练”等默认值；未传、传空数组或没有有效候选对象时，Prompt 要求模型输出空字符串，回调 `data.channel` 也返回空字符串。
   - `params[].security` 表示密级候选范围（字典编码），回调 `data.security` 返回密级解析结果；解析时根据文档开头内容判断，未见密级相关说明时，候选包含“公开”则返回“公开”，否则返回密级候选中的第一个 `value`。
   - `architectureList` 使用甲方最新节点结构：`id` 为节点唯一标识，`name` 为节点名称，`parentId` 为父节点 id，`path` 为 id 路径链，`pathName` 为名称路径链，`remark` 为节点名词概述。
   - `architectureStandardList` 表示数据标准额外解析范围；当最终 `architectureId` 命中该范围或其子孙节点时，`fileDataItem` 会额外返回 `militaryName`、`num`、`startTime`、`implTime`、`approvalDept`。
   - 若 `architectureList` 只有一个节点，解析结果直接返回该节点 `id`，不再执行领域分类判断；其他信息提取仍正常执行。
   - 主 Prompt 的多节点分类合同是：`architectureId` 必须返回有唯一文档证据支持的候选叶子节点 ID；叶子证据不足、无法区分或无法匹配时保持为空，不得返回父节点、公共父节点或任意默认值。
   - 当前服务端尚未验证候选是否为叶子：单候选会直接返回该节点 ID；多候选成功路径要求非空 `architectureId`，并只校验它是请求 `architectureList` 中的整数候选。因而主 Prompt 输出的空值会先被视为合同不合规，非叶子候选却可能通过校验；这两点都是当前实现限制，不代表主 Prompt 的目标口径。首次结果不合规时，服务端会先尝试下述 GJB 确定性兜底，未命中再发起一次 architecture repair，repair 仍不合规才将任务置为失败。
   - GJB、国军标、国家军用标准资料应归类到候选中“数据标准”下有证据支持的叶子节点，禁止返回“数据标准”父节点。当前服务端的 GJB 兜底会按候选顺序选择名称或路径包含“数据标准”的首个节点，尚未区分父节点与叶子节点；调用方应保证数据标准叶子结构完整，并避免泛化父节点排在可命中的叶子节点之前，但最终仍应以主 Prompt 合同作为验收口径。
   - 当最终分类名称严格符合 `<武器装备名称>-基础数据`、`<武器装备名称>-战技指标`、`<武器装备名称>-运用数据` 或 `<武器装备名称>-效能数据` 时，回调 `data.architectureId` 返回具体子分类 ID；本地知识库关系和 AnythingLLM workspace 按解析出的装备级父节点 ID 归并。业务 metadata 以 DocSense 本地数据库为准，AnythingLLM 上传 metadata 当前仅写入用于来源追踪的 `docSource`，不写入分类 ID。
   - 主 Prompt 要求 `fileDataItem.score` 必填且只能是 `95`、`85`、`75`、`65`、`55`，分别对应闭源渠道或权威机构公开发布，专业科研单位/知名智库/装备研制单位，专业信息网站，普通信息网站，未明确数据来源资料；服务端映射会将缺失、无法转为数值、数值不是整数值或候选外的评分归一化为 `55`，可转换为整值的 `95.0`、`"95"` 等输入会保留为对应整数档位。
   - 解析后可进入翻译流程（由 `translation_service` 编排）。

2. `/llm/generate-report`
   - `filePathList` 支持多文件，统一汇总后生成 HTML 报告。
   - `mhtml/mht` 文件会先归一化再参与报告生成。
   - `templateOutline` 表示 Word 模板文件下载地址；服务端会下载 `.docx` 模板并提取其中的文字内容作为报告大纲要求，再进入原有报告生成流程。
   - 当前仍使用 legacy RAG 链路：若底层返回 `None`，服务会把空内容包装为 HTML 并仍按 `status=1` 落库/回调，因此验收必须检查报告正文，不能只看任务状态；该链路当前也未清理临时 Workspace/Thread 或显式关闭 AnythingLLM Client。

3. `/llm/weaponry`
   - `params` 为对象（非数组）。
   - 提交时会校验 `analyseData` / `analyseDataSource` 必须清空。
   - `params.filePathList` 可选；缺省或空数组表示解析当前类别下的全部文件，非空时只解析列表选中的文件。列表元素兼容完整下载 URL 和裸哈希文件名；服务端从 URL 路径提取并解码文件名、按首次出现顺序去重，并严格校验文件已解析且属于当前 `architectureId`。
   - 指定文件范围时会创建任务级临时 workspace，仅引用选中文档执行向量检索；任务结束时会在 `finally` 中尝试删除，失败只记录 warning，可能残留临时 workspace；原类别 workspace 不做选中文档增删。
   - 通过 `architectureId` 从知识库映射中定位 workspace 后执行字段提取。
   - 字段抽取默认采用“目标证据 + 术语规则”分池检索：普通 `INPUT` 字段的目标证据默认 `topN=8`，`TABLE` 字段默认 `topN=16`；术语规则 workspace 单独检索 `term_rule_*.md`，默认 `topN=3`。
   - `TABLE` 字段不再按单元格逐个查询；请求中的 `tableFieldList` 作为列模板，后端会进行整表检索和 JSON 行抽取。只有成功解析出有效行时，回调才扩展为多行二维 `tableFieldList`；否则保留原始列模板。
   - 当目标 workspace 中混入 `term_rule_*.md` 术语文档时，任务开始会先把这些术语移入/复用术语规则 workspace，并从目标 workspace 临时移除；任务结束时会尝试恢复，失败只记录 warning，不能视为强恢复保证。
   - 术语规则辅助上下文由 `WEAPONRY_TERMS_RULE_CONTEXT_ENABLED` 控制；关闭时跳过字段阶段的术语向量检索和 Prompt 注入，但准备阶段仍可能上传或复用术语 workspace，并处理目标 workspace 中混入的术语文档。
   - 开启时，术语规则只会作为 Prompt 中的字段口径、别名和单位参考，不进入 `analyseData` / `analyseDataSource`，也不得作为装备事实来源。
   - `WEAPONRY_ANALYSE_MODE=2` 按文件聚合抽取时，回调 `analyseDataSource.source` 优先返回 `documents.original_name` 文件原名，`fileName` 返回 `documents.file_name` 哈希文件名，`rows` 返回经过上下文限制后实际提交给模型的 Chunk 列表；文件映射缺失时文件名字段回退为 AnythingLLM 返回的内部来源名。
   - 每次字段问答优先使用独立临时 Thread，并对空响应做一次重试，避免字段间历史污染和本地模型/嵌入服务短时无响应导致漏抽。

4. `/llm/check-task`
   - 支持 `file` / `report` / `weaponry`。
   - 支持批量查询（`params` 多项）；单项与批量返回结构略有差异。

5. `/llm/progress`（WebSocket）
   - 支持动作：`subscribe`、`query`、`unsubscribe`。
   - 未显式传 `action` 时默认按订阅处理。
   - 单连接可管理多个任务订阅。

6. `/llm/chat`（文件对话体系）
   - 基于 SSE（Server-Sent Events）实现流式文本返回打字机效果。
   - 底座上强制 1 对话 = 1 Workspace + 1 Thread 的隔离限制以避污染；历史以本地已提交消息为权威来源。
   - 通过增量 update-embeddings (adds) 追加引用文件，fileNames 仅含本次新增文件；本地保留不可变的文件绑定 revision，并以最新 revision 作为后续对话默认引用。
   - 同一 `chatId` 同时只有一个活跃流；`abort` 只持久化中断请求，由流在事件边界收敛为 `aborted`。
   - 客户端关闭 SSE 后不继续后台生成：执行尚未开始时丢弃该轮 user；执行已开始时保留 user、不保存不完整 assistant，并以失败状态收敛。
   - `GET /llm/chat/history` 以本地对话库中状态为 `committed` 的消息为权威数据源，不从 AnythingLLM Thread 读取历史接口结果；默认数据库为 `${DOCSENSE_RUNTIME_DIR}/chat_sessions.sqlite3`，可由 `DOCSENSE_CHAT_DB` 覆盖。
   - `POST /llm/chat/title` 仅使用本地已提交消息生成标题；`POST /llm/chat/abort` 向当前活跃 run 写入持久化中断标志，执行器在后续事件边界观察到标志后发送 `aborted` 终态。没有活跃 run 时接口仍返回 HTTP 200，但 `aborted=false`。
   - `POST /llm/chat/delete` 先清理远端 Thread、Workspace 及相关资源租约，全部成功后再把本地会话标记为 `deleted`；清理失败时会保留 `cleanup_failed` 恢复记录，将会话置为 `error` 并返回错误。

7. `/llm/reassign`（分类节点变更）
   - 这是即时同步过程接口，不产生额外后台队列任务和 HTTP 进度回调。
   - 安全方面要求调用前必须传输且一致匹配底库中存证的 `oldArchitectureId`。
   - 当前实现会尝试从旧 workspace 删除文档、加入新 workspace，再更新本地分类；但尚未校验 AnythingLLM 删除/添加操作返回的布尔结果，`doc_path` 为空时也会跳过远端迁移。因此接口返回成功不等于远端 embedding 必然迁移成功，验收时还需核对 workspace 与本地映射。

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

在 macOS 上复制 `.env.example` 后，必须将其中启用的 Windows 路径 `DOCSENSE_RUNTIME_DIR=C:/DocSenseRuntime` 改为 macOS 绝对路径，或注释/删除该行以使用仓库根目录 `.runtime`；其他平台相关路径也应按实际环境调整。

Weaponry 可选配置：

- `WEAPONRY_ANALYSE_MODE`：`/llm/weaponry` 字段抽取模式，`2` 表示按文件聚合多 Chunk 后抽取。
- `WEAPONRY_TERMS_RULE_CONTEXT_ENABLED`：是否启用术语规则辅助上下文，默认 `true`；设为 `false` 时不检索术语 workspace，也不向 Prompt 加入术语规则辅助信息。
- `WEAPONRY_TERMS_WORKSPACE_NAME`：术语规则专用 AnythingLLM workspace 名称，默认 `weaponry-terms-rules`。
- `WEAPONRY_TERMS_DIR`：本地术语规则 Markdown 目录，默认 `terms`；当目标 workspace 没有术语文档时，会从该目录上传 `*.md` 作为术语规则参考。

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
- 页面左侧展示本地 `chat_sessions.sqlite3` 中的会话，文件选择来自 `knowledge_base.sqlite3` 中已解析文件记录
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
- 下载缓存：`${DOCSENSE_RUNTIME_DIR}/llm_downloads/`
- OCR Markdown 缓存：`${DOCSENSE_RUNTIME_DIR}/ocr_markdown/`
- MinerU Markdown 缓存：`${DOCSENSE_RUNTIME_DIR}/mineru_markdown/`
- 回调历史：`${DOCSENSE_RUNTIME_DIR}/callback/`
- SQLite JSON 导出：`${DOCSENSE_RUNTIME_DIR}/sqlite/`
- 旧版回调预览：`${DOCSENSE_RUNTIME_DIR}/call_back.json`

文件对话当前以 SQLite 单实例模式运行：同一个 `chat_sessions.sqlite3` 只能由一个应用副本使用，不能放在网络共享目录模拟多实例。`DOCSENSE_CHAT_RUNTIME_MODE` 必须为 `single_instance`（默认值）；配置 `cluster`、外部调度或其他未安装模式时，应用会在依赖装配阶段拒绝启动，而不会以共享 SQLite 文件伪装集群能力。

为保护该模式下的资源，`DOCSENSE_CHAT_MAX_FILES`、`DOCSENSE_CHAT_MAX_MESSAGE_CHARS`、`DOCSENSE_CHAT_MAX_OUTPUT_CHARS` 和 `DOCSENSE_CHAT_MAX_CONCURRENT_STREAMS` 分别限制单轮文件数、消息/输出长度和进程内同时流数。持久化能力、运行租约、取消通知和资源清理均通过内部可替换边界装配：当前实现只提供本地事务、同步执行、持久化取消轮询和同步清理。删除和标题临时资源会先写入持久化清理任务；当前内联执行器只同步处理本次新建任务，失败记录不会被伪装成已具备自动延迟重试能力。不提供事务 outbox、可靠队列、跨实例通知或 fencing。数据库迁移、可靠调度与多实例部署尚未启用；在选型、迁移和故障演练完成前，不得开放对应运行模式。

旧的组件级变量仍可作为兼容覆盖项，一旦配置就会优先于统一根目录。若希望全部内容位于同一目录，应删除这些覆盖项：

- 任务库：`DOCSENSE_LLM_TASK_DB`；未覆盖时为 `${DOCSENSE_RUNTIME_DIR}/llm_tasks.sqlite3`
- 知识库映射库：`DOCSENSE_KNOWLEDGE_BASE_DB` 或兼容变量 `KNOWLEDGE_BASE_DB_PATH`；未覆盖时为 `${DOCSENSE_RUNTIME_DIR}/knowledge_base.sqlite3`
- 对话状态库：`DOCSENSE_CHAT_DB`；未覆盖时为 `${DOCSENSE_RUNTIME_DIR}/chat_sessions.sqlite3`
- 下载缓存：`FILE_DOWNLOAD_DIR`；未覆盖时为 `${DOCSENSE_RUNTIME_DIR}/llm_downloads/`
- OCR Markdown 缓存：`DOCSENSE_OCR_CACHE_DIR`；未覆盖时为 `${DOCSENSE_RUNTIME_DIR}/ocr_markdown/`
- MinerU Markdown 缓存：`DOCSENSE_MINERU_CACHE_DIR`；未覆盖时为 `${DOCSENSE_RUNTIME_DIR}/mineru_markdown/`
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

目录级武器装备字段抽取自动化脚本：

```bash
# macOS / zsh
APP_DEBUG=false WEAPONRY_ANALYSE_MODE=2 python run.py
zsh scripts/test_llm_weaponry_directory.sh "测试文件-水面装备" --base-url http://127.0.0.1:5001

# Windows / PowerShell
$env:APP_DEBUG = "false"
$env:WEAPONRY_ANALYSE_MODE = "2"
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

测试文件批量武器装备字段抽取复现流程：

1. 确认 AnythingLLM 可用并启动 DocSense，目录批跑建议使用 `APP_DEBUG=false WEAPONRY_ANALYSE_MODE=2 python run.py`。只有在 `.env` 配置了本地 mock 的 `CALLBACK_URL` 且需要核验真实回调时，才需要另行启动 `python scripts/mock_callback_server.py`。
2. 先执行带 `--dry-run` 的目录 wrapper，核对扫描文件、输出目录和临时 `architectureId`。runner 会读取知识库映射并自动避开已存在的 ID；如使用自定义 `--architecture-base`，仍应检查 dry-run manifest 是否符合预期。
3. 去掉 `--dry-run` 执行目录 wrapper。未提供 `--static-base` 时，runner 会自动为目标目录启动临时静态文件服务；提供该参数时才复用调用方已有的文件服务。
4. runner 对每个目标 PDF 串行提交 `/llm/analysis`、轮询 `/llm/check-task`、核验当前临时分类的文档隔离，再提交 `/llm/weaponry` 并轮询终态。请求模板中的 75 个字段由脚本内置，调用方无需手工逐字段构造。
5. 术语规则由 `/llm/weaponry` 服务自动处理：目标 workspace 没有术语文档时，会从 `WEAPONRY_TERMS_DIR` 指向的本地目录上传或复用规则，并加入独立的 `WEAPONRY_TERMS_WORKSPACE_NAME` workspace；如果目标 workspace 已混入术语文档，则任务期间临时移除并在结束后恢复。不要手工把术语规则加入目标 workspace。
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

当前完整测试集中有用例直接读取受 `.gitignore` 排除的 `tests/fixtures/llm/*.json`；未准备这些本地请求文件时，完整发现命令会因 fixture 缺失失败。除此之外，当前分支的完整测试仍有其他既有失败，因此不能把上述命令当作全绿基线；应按实际运行结果区分通过项和失败项。核对 analysis service 合同、候选默认值、callback debug 路由和 security 迁移时，可运行不依赖这些本地请求文件的定向测试：

```bash
.venv/bin/python -m unittest tests.test_analysis_service tests.test_range_defaults tests.test_callback_debug_routes tests.test_migrate_analysis_security
```

## 9. 协议文档

- 文件处理与报告生成：`docs/接口文档/文件处理和报告生成.md`
- 知识谱系解析：`docs/接口文档/知识谱系解析.md`
- 文件对话：`docs/接口文档/文件对话.md`
- 文件对话新增接口：`docs/接口文档/文件对话新增接口.md`
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
