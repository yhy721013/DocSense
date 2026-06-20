# DocSense - 甲方协议 LLM 接口后端

DocSense 当前以甲方协议后端接口服务为主，聚焦 LLM 任务处理能力；同时提供本地调试页，用于查看最近一次已落盘的回调结果，以及联调文件对话模块。

## 1. 核心能力

- 文件解析：`POST /llm/analysis`
- 报告生成：`POST /llm/generate-report`
- 武器装备知识谱系解析：`POST /llm/weaponry`
- 文件内容对话：`POST /llm/chat`（附加历史查询 `GET /llm/chat/history` 及删除 `POST /llm/chat/delete`）
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
| 业务层 | `app/services/llm_service/` | 文件解析、报告生成、谱系提取、任务状态管理、翻译编排、对话记录及文档动态联动 | `analysis_service.py` `report_service.py` `weaponry_service.py` `chat_service.py` `task_service.py` |
| 核心基础层 | `app/services/core/` | 全局配置、路径常量、日志、任务/知识库及独立对话数据库、进度中枢、Prompt 构建 | `config.py` `settings.py` `database.py` `progress_hub.py` `prompts.py` |
| 工具与外部边界层 | `app/services/utils/` | AnythingLLM 客户端、回调发送、回调预览读取、文件下载、OCR 预处理、mhtml 归一化、RAG 流程 | `anythingllm_client.py` `callback_client.py` `callback_preview.py` `file_downloader.py` `ocr_preprocessor.py` `mhtml_normalizer.py` `rag_pipeline.py` |
| 翻译能力层 | `app/services/translator/` | 文档/文本翻译底层实现，被业务翻译服务封装调用 | `core.py` `document_handler.py` `pdf_handler.py` |

### 2.2 主要调用方向

1. `blueprints -> llm_service`：蓝图只负责协议入口，不承载长流程业务。
2. `llm_service -> core`：读取配置、写任务状态、发布进度、构建 Prompt。
3. `llm_service -> utils`：下载文件、规范化文本、调用 AnythingLLM、发送回调。
4. `llm_service.translation_service -> translator`：翻译能力由 `translation_service.py` 统一编排。
5. `check-task -> task_service.replay_callback_if_needed`：用于成功/失败任务的回调补发。

### 2.3 请求到回调的链路

```text
Client Request
  -> app/blueprints/llm.py
    -> LLMTaskService 创建/更新任务
    -> 后台线程执行 llm_service 任务
      -> utils 下载/预处理/调用 AnythingLLM
      -> core.progress_hub 推送 WS 进度
      -> 组装业务结果并写入任务库
      -> utils.callback_client 回调业务系统
```

## 3. 当前目录（关键部分）

```text
app/
  __init__.py                       # Flask App 工厂，注册 llm/debug 蓝图
  blueprints/
    llm.py                          # /llm/* 路由 + WebSocket 进度通道
    debug.py                        # /debug/* 本地调试路由
  services/
    core/
      config.py                     # 环境变量与配置加载
      settings.py                   # 路径常量与限制（上传目录、DB 路径等）
      logging.py                    # 日志初始化
      database.py                   # 知识库映射及对话记录持久化（architecture_id <-> workspace_slug, chats）
      progress_hub.py               # 进度发布/订阅中枢
      prompts.py                    # 统一 Prompt 构建
    llm_service/
      analysis_service.py           # 文件解析主流程（含 mhtml/OCR/翻译编排）
      report_service.py             # 报告生成主流程
      weaponry_service.py           # 知识谱系字段提取主流程
      chat_service.py               # 文件对话主流程（含 SSE 生成、跨工作区引用）
      task_service.py               # 任务状态、结果、回调状态持久化
      translation_service.py        # 翻译服务编排层
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
scripts/                            # 本地联调脚本
tests/                              # unittest 测试用例
clean.py                            # 清理测试数据
environment.yml                     # Conda环境依赖（Conda安装）
requirements.txt                    # Conda环境依赖（Pip安装）
requirements-venv.txt               # Venv环境依赖（Pip安装）
```

## 4. 任务模型与状态

所有任务统一持久化到任务库（默认 `.runtime/llm_tasks.sqlite3`），查询键如下：

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

- `pending`：未回调或未配置回调地址
- `success`：回调成功
- `failed`：回调失败（可通过 `/llm/check-task` 触发补发）

## 5. 接口行为说明（与代码一致）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/llm/analysis` | 文件解析，支持 `params` 多文件顺序串行处理 |
| POST | `/llm/generate-report` | 报告生成，使用 `params[0]` |
| POST | `/llm/weaponry` | 武器装备知识谱系字段提取 |
| POST | `/llm/check-task` | 查询任务状态，必要时补发回调 |
| WS | `/llm/progress` | 进度订阅/查询/取消订阅 |
| POST | `/llm/chat` | 基于指定文件内容发起对话请求（SSE 流式响应下发） |
| GET | `/llm/chat/history` | 查询指定会话的完整聊天历史消息记录 |
| POST | `/llm/chat/delete` | 对应彻底释放删除聊天的底座资源（工作区与 Thread 隔离模型） |
| POST | `/llm/reassign` | 调整和修改文档分类节点，实时重定向嵌入其 RAG 工作区数据位置 |

本地调试路由（非甲方协议接口）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/debug/callback` | 本地回调结果调试页，面向人工阅读 |
| GET | `/debug/api/callback` | 读取最近一次落盘的 `.runtime/call_back.json` |
| GET | `/debug/chat` | 本地文件对话调试页，联调 `/llm/chat*` 三个接口 |
| GET | `/debug/api/chat/bootstrap` | 读取本地会话列表与已解析文件列表，供 `/debug/chat` 初始化使用 |

关键补充：

1. `/llm/analysis`
   - 同请求可提交多个文件，服务端按数组顺序串行执行。
   - 支持 `mhtml/mht`，会先归一化正文再进入解析。
   - `params[].originalFileName` 表示原文件名，当前作为请求上下文进入文件解析提示词，后续可继续用于业务链路。
   - `architectureList` 使用甲方最新节点结构：`id` 为节点唯一标识，`name` 为节点名称，`parentId` 为父节点 id，`path` 为 id 路径链，`pathName` 为名称路径链，`remark` 为节点名词概述。
   - `architectureStandardList` 表示数据标准额外解析范围；当最终 `architectureId` 命中该范围或其子孙节点时，`fileDataItem` 会额外返回 `militaryName`、`num`、`startTime`、`implTime`、`approvalDept`。
   - 若 `architectureList` 只有一个节点，解析结果直接返回该节点 `id`，不再执行领域分类判断；其他信息提取仍正常执行。
   - 多节点分类时优先返回最具体叶子节点；叶子证据不足时返回最深可靠父节点；多个兄弟节点都可能时返回最近公共父节点。
   - 当最终分类名称严格符合 `*-基础数据`、`*-战技指标`、`*-运用数据` 或 `*-效能数据` 时，回调 `data.architectureId` 仍返回该具体子分类 ID；知识库关系表、AnythingLLM workspace 和向量 metadata 则统一按对应的武器装备父节点 ID 存储，便于 `/llm/weaponry` 检索该装备的全部文档。
   - 多节点分类时优先返回最具体叶子节点；命中非叶子节点时需回退到该分支下的“其他”叶子节点（节点名称或路径包含“其他”），仍无对应“其他”时回退到 1。
   - 文档内容明确为 GJB、国军标、国家军用标准相关资料，且候选中存在 `数据标准` 节点时，优先返回该节点 `id`。
   - `fileDataItem.score` 为必填离散评分，只返回 `95`、`85`、`75`、`65`、`55`，分别对应闭源渠道或权威机构公开发布，专业科研单位/知名智库/装备研制单位，专业信息网站，普通信息网站，未明确数据来源资料。
   - 解析后可进入翻译流程（由 `translation_service` 编排）。

2. `/llm/generate-report`
   - `filePathList` 支持多文件，统一汇总后生成 HTML 报告。
   - `mhtml/mht` 文件会先归一化再参与报告生成。

3. `/llm/weaponry`
   - `params` 为对象（非数组）。
   - 提交时会校验 `analyseData` / `analyseDataSource` 必须清空。
   - 通过 `architectureId` 从知识库映射中定位 workspace 后执行字段提取。
   - 字段抽取默认采用“目标证据 + 术语规则”分池检索：目标 workspace 检索目标 PDF 证据，默认 `topN=8`；术语规则 workspace 单独检索 `term_rule_*.md`，默认 `topN=3`。
   - 当目标 workspace 中混入 `term_rule_*.md` 术语文档时，任务开始会先把这些术语临时移入/复用术语规则 workspace，并从目标 workspace 临时移除；任务结束后再恢复目标 workspace，避免术语文档占满目标证据检索结果。
   - 术语规则辅助上下文由 `WEAPONRY_TERMS_RULE_CONTEXT_ENABLED` 控制；关闭时不检索术语 workspace，也不向 Prompt 加入术语规则辅助信息，但仍保留目标证据过滤和术语文档临时清理/恢复。
   - 开启时，术语规则只会作为 Prompt 中的字段口径、别名和单位参考，不进入 `analyseData` / `analyseDataSource`，也不得作为装备事实来源。
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
   - 底座上强制 1 对话 = 1 Workspace + 1 Thread 的隔离限制以避污染，历史数据在 `AnythingLLM` 保留。
   - 通过增量 update-embeddings (adds) 追加引用文件，fileNames 仅含本次新增文件。

7. `/llm/reassign`（分类节点变更）
   - 这是即时同步过程接口，不产生额外后台队列任务和 HTTP 进度回调。
   - 安全方面要求调用前必须传输且一致匹配底库中存证的 `oldArchitectureId`。

## 6. 快速启动

1. 安装依赖

Conda环境使用：

```bash
conda env create -f environment.yml
conda activate DocSense
uv pip install -r requirements.txt
```

Venv环境可使用：

```bash
pip install -r requirements-venv.txt
```

2. 配置环境变量（建议使用 `.env`）

必填（最小可用）：

- `ANYTHINGLLM_API_KEY`

常用：

- `ANYTHINGLLM_BASE_URL`（默认 `http://localhost:3001/api/v1`）
- `CALLBACK_URL`（不配置则不主动回调外部系统）
- `APP_HOST`（默认 `0.0.0.0`）
- `APP_PORT`（默认 `5001`）
- `APP_DEBUG`（默认 `true`）

Weaponry 可选配置：

- `WEAPONRY_ANALYSE_MODE`：`/llm/weaponry` 字段抽取模式，`2` 表示按文件聚合多 Chunk 后抽取。
- `WEAPONRY_TERMS_RULE_CONTEXT_ENABLED`：是否启用术语规则辅助上下文，默认 `true`；设为 `false` 时不检索术语 workspace，也不向 Prompt 加入术语规则辅助信息。
- `WEAPONRY_TERMS_WORKSPACE_NAME`：术语规则专用 AnythingLLM workspace 名称，默认 `weaponry-terms-rules`。
- `WEAPONRY_TERMS_DIR`：本地术语规则 Markdown 目录，默认 `terms`；当目标 workspace 没有术语文档时，会从该目录上传 `*.md` 作为术语规则参考。

3. 启动服务

```bash
python run.py
```

默认监听：`http://0.0.0.0:5001`

4. 本地调试页面（可选）

回调调试页前提：

- 已配置 `CALLBACK_URL`
- 至少发生过一次文件解析或报告生成回调

回调调试页访问：

- 页面：`http://127.0.0.1:5001/debug/callback`
- 数据：`http://127.0.0.1:5001/debug/api/callback`

回调调试页说明：

- 页面展示的数据来自仓库根目录 `.runtime/call_back.json`
- `file` 回调会结构化展示摘要信息、原文和翻译预览
- `report` 回调会结构化展示报告信息和 HTML 报告预览
- 若当前还没有回调文件，页面会显示空状态提示

文件对话调试页前提：

- `ANYTHINGLLM_API_KEY` 已配置
- 至少已有一个成功解析并入库的文件，供 `fileNames` 选择

文件对话调试页访问：

- 页面：`http://127.0.0.1:5001/debug/chat`
- 初始化数据：`http://127.0.0.1:5001/debug/api/chat/bootstrap`

文件对话调试页说明：

- `/debug/chat` 不写入也不依赖 `.runtime/call_back.json`
- 页面直接联调正式接口 `POST /llm/chat`、`GET /llm/chat/history`、`POST /llm/chat/delete`
- 页面左侧展示本地 `chat_sessions.sqlite3` 中的会话，文件选择来自 `knowledge_base.sqlite3` 中已解析文件记录
- 文件选择器以“已选标签 + 添加文件面板”展示，支持勾选与取消勾选
- SSE 主输出在聊天主区域实时显示，调试事件收纳于折叠详情中
- 该调试页仅用于本地联调文件对话模块，不参与甲方真实回调链路

## 7. 运行时路径与持久化

- 任务库：`.runtime/llm_tasks.sqlite3`（`DOCSENSE_LLM_TASK_DB`）
- 知识库映射库：`.runtime/knowledge_base.sqlite3`（`DOCSENSE_KNOWLEDGE_BASE_DB`）
- 对话状态库：`.runtime/chat_sessions.sqlite3`（`DOCSENSE_CHAT_DB`）
- 下载缓存目录：`FILE_DOWNLOAD_DIR`（用于任务下载源文件）
- 最近一次回调预览：`.runtime/call_back.json`

## 8. 本地联调与测试

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

脚本默认行为：

- 自动读取仓库根目录 `.env`，不存在时回退 `.env.example`
- `test_llm_analysis.sh` 默认请求 `POST /llm/analysis`
- `test_llm_report.sh` 默认请求 `POST /llm/generate-report`
- `test_llm_weaponry.sh` 默认请求 `POST /llm/weaponry`
- `test_llm_check_task.sh` 默认请求 `POST /llm/check-task`
- `test_llm_progress.sh` 默认连接 `WS /llm/progress`

可选参数示例：

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
- 来源核验表：`qwen3-4b-new_source_audit.csv/json`
- 运行记录：`qwen3-4b-new_manifest.json`
- 每个文件的请求、响应、任务结果与知识库映射快照

常用参数：

```bash
zsh scripts/test_llm_weaponry_directory.sh "测试文件-水面装备" \
  --pattern "*.pdf" \
  --output-dir ".runtime/weaponry_surface_extract_manual" \
  --architecture-base 993000000

pwsh -NoLogo -Command "./scripts/test_llm_weaponry_directory.ps1 '测试文件-水面装备' --pattern '*.pdf' --output-dir '.runtime/weaponry_surface_extract_manual' --architecture-base 993000000"
```

如需递归扫描子目录，加 `--recursive`；如只想确认会扫描哪些文件和使用哪些临时 `architectureId`，加 `--dry-run`。脚本会自动避开已存在的 `architectureId`，防止旧 workspace/document 记录污染本轮结果。

测试文件批量武器装备字段抽取复现流程：

1. 启动依赖服务：确认 AnythingLLM `3001` 可用，启动 DocSense 时建议使用 `APP_DEBUG=false WEAPONRY_ANALYSE_MODE=2 python run.py`，同时启动 `python scripts/mock_callback_server.py` 和指向 `测试文件-水面装备/` 的静态文件服务。
2. 批量 runner 启动前先检查 `.runtime/knowledge_base.sqlite3` 和 `.runtime/llm_tasks.sqlite3`：本轮要使用的临时 `architectureId` 不应已有 workspace/document 记录；若已有记录，应停止并更换一组未占用的临时 ID，避免旧文档污染。
3. 扫描 `terms/` 下全部 `.md` 文件，上传到 AnythingLLM 并记录每个术语文件的 `doc_path`。术语文件只用于字段口径、别名和单位参考，不作为装备事实来源。
4. 对 `测试文件-水面装备/` 下每个目标 PDF 串行执行：
   - 构造 `/llm/analysis` 请求，`filePath` 使用静态文件 URL，`originalFileName` 使用原始 PDF 文件名，`architectureList` 只传一个临时节点，使该 PDF 固定入库到对应 `architectureId`。
   - 轮询 `/llm/check-task`，直到 `businessType=file` 的 `status=2`；随后核验知识库映射中该 `architectureId` 只关联当前 PDF。
   - 将第 3 步记录的全部术语 `doc_path` 临时加入当前 PDF 的 workspace。`/llm/weaponry` 内部会把 `term_rule_*.md` 与目标证据分池处理，任务结束后应从目标 workspace 移除术语。
   - 构造 `/llm/weaponry` 请求，75 个字段均使用 `fieldType="INPUT"`、`templateClassifyId=1772442376645740`，`analyseData` 和 `analyseDataSource` 保持空。`fieldDescription` 必须写明唯一目标 PDF 文件名，并声明 `terms/` 只作术语参考，找不到目标 PDF 明确依据时返回“未找到”。
   - 轮询 `/llm/check-task`，直到 `businessType=weaponry` 的 `status=2`；从 `.runtime/llm_tasks.sqlite3` 的任务结果中读取 `weaponryTemplateFieldList`。
   - 抽取每个字段的 `analyseData` 写入汇总表；同步保留 `analyseDataSource` 到核验表。若接口、模型或内容安全检查导致字段未返回，汇总表填“未找到明确依据”，并在 manifest 或日志中记录失败字段与错误原因。
5. 汇总产物建议固定为三类文件：主表 `qwen3-4b-new.csv`（列为 `文件名` 加 75 个字段）、来源核验表 `qwen3-4b-new_source_audit.csv/json`、运行记录 `qwen3-4b-new_manifest.json`。主表必须覆盖 `PDF 数量 x 75` 个字段槽位；来源核验表用于抽样确认来源文件不是 `term_rule_*.md`。

Windows 与 macOS 可按各自环境选择对应脚本。

本地调试页联调建议：

1. 启动服务：`python run.py`
2. 若联调回调型业务，触发一次 `/llm/analysis`、`/llm/generate-report` 或 `/llm/weaponry`
3. 打开 `http://127.0.0.1:5001/debug/callback`
4. 若要比对原始报文，可同时查看 `.runtime/call_back.json`
5. 若联调文件对话，先确保至少有一个已解析文件，再打开 `http://127.0.0.1:5001/debug/chat`
6. 在 `/debug/chat` 中可直接完成发送消息、查看历史、删除会话三类联调

单元测试（仓库默认 `unittest`）：

```bash
.venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

## 9. 协议文档

- 文件处理与报告生成：`docs/接口文档/文件处理和报告生成.md`
- 知识谱系解析：`docs/接口文档/知识谱系解析.md`
- 文件对话：`docs/接口文档/文件对话.md`
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
