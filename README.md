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
   - 解析后可进入翻译流程（由 `translation_service` 编排）。

2. `/llm/generate-report`
   - `filePathList` 支持多文件，统一汇总后生成 HTML 报告。
   - `mhtml/mht` 文件会先归一化再参与报告生成。

3. `/llm/weaponry`
   - `params` 为对象（非数组）。
   - 提交时会校验 `analyseData` / `analyseDataSource` 必须清空。
   - 通过 `architectureId` 从知识库映射中定位 workspace 后执行字段提取。

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
   - 通过增量 update-embeddings (adds/deletes) 维护引用文件列表动态变迁。

7. `/llm/reassign`（分类节点变更）
   - 这是即时同步过程接口，不产生额外后台队列任务和 HTTP 进度回调。
   - 安全方面要求调用前必须传输且一致匹配底库中存证的 `oldArchitectureId`。

## 6. 快速启动

1. 安装依赖

```bash
pip install -r requirements.txt
```

离线环境可使用：

```bash
pip install -r requirements-offline.txt
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
