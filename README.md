# DocSense - 智能文档处理系统

DocSense 是一个基于 AnythingLLM 的智能文档处理平台，提供文档分类、信息抽取和 AI 对话等功能。

## 🚀 主要功能

### 📂 文档智能分类
- **自动分类**：利用 RAG（检索增强生成）技术，自动分析文档内容并进行分类
- **批量处理**：支持单文件上传和文件夹批量上传，高效处理大量文档
- **人工确认**：提供分类结果确认机制，支持人工调整和确认分类
- **自动归档**：确认分类后自动将文件移动到对应的分类目录

### 🔍 信息抽取
- **结构化输出**：从非结构化文档中自动提取关键信息，返回结构化 JSON 结果
- **多格式支持**：支持 PDF、Word、Excel、图片等多种文档格式
- **前置 OCR**：扫描件 PDF 在上传前自动 OCR 为 Markdown，再交给 AnythingLLM 处理
- **实时进度**：任务处理过程中实时显示进度，支持前端轮询状态

### 🔌 甲方协议接入
- **正式接口**：支持 `/llm/analysis`、`/llm/generate-report`、`/llm/check-task`、`/llm/progress`
- **主动回调**：任务完成后按甲方协议主动回调业务系统
- **开发联调**：提供样例 JSON、PowerShell 7 测试脚本、本地文件服务和本地假回调服务

### 💬 AI 对话
- **文件选择**：从已分类归档的文件中，通过模态框勾选多个文档建立对话上下文
- **工作区创建**：选定文件后自动在 AnythingLLM 中创建 Workspace/Thread 并完成文档 Embedding
- **多轮问答**：基于选定文档与 AI 进行多轮对话，每次请求携带文档 ID 确保回答基于文件内容
- **对话管理**：支持重新选择文件、自动清除旧会话、对话历史记录保留

### 🛠️ 技术特性
- **AnythingLLM 集成**：深度集成 AnythingLLM，利用其强大的文档处理和 RAG 能力
- **模块化架构**：采用 Flask Blueprint 架构，业务逻辑清晰分离，便于扩展和维护
- **异步处理**：后台线程处理文档，避免阻塞主服务
- **Web UI**：提供简洁直观的 Web 界面，开箱即用

---

以下说明用于快速理解项目内各个 `.py` 文件的职责与关系，以及给出运行与调试的建议。

## 文件职责概览

- `config.py`  
  读取 AnythingLLM 与 OCR 配置（Base URL、API Key、Timeout、Storage Root、OCR参数），通过环境变量覆盖默认值。

- `anythingllm_client.py`  
  对 AnythingLLM REST API 做统一封装：workspace/thread 创建、文档上传、embedding 更新、流式响应解析等。

- `document_utils.py`  
  文档类型判断工具函数。

- `ocr_preprocessor.py`  
  扫描件 PDF 前置处理：扫描检测、OCR 转 Markdown、缓存复用、失败降级。

- `pipeline.py`  
  核心处理流水线：文件预处理（扫描 PDF OCR 转 Markdown）-> 上传 AnythingLLM -> 更新 embedding -> 发送 Prompt -> 返回结构化结果。

- `rag_with_ocr.py`  
  CLI 入口：加载配置并调用 `pipeline.process_file_with_rag` 完成整体处理。

- `web_ui.py`  
  Web UI 入口：仅负责创建 Flask App 并启动服务（业务逻辑已拆分到 `app/`）。

- `app/`
  Web UI 业务层：采用 Flask Blueprint 解耦"分类抽取"与"对话"模块，降低多人协作冲突。

  - `app/__init__.py`
    Flask App 工厂函数，注册 Blueprint 和全局配置。

  - `app/settings.py`
    应用级配置：文件存储目录、暂存目录、上传大小限制等。

  - `app/blueprints/main.py`
    首页路由（`/`）。

  - `app/blueprints/classify.py`
    分类与信息抽取相关 API 路由。

  - `app/blueprints/chat.py`
    对话相关 API 路由：文件列表、工作区创建、消息收发。

  - `app/services/task_store.py`
    线程安全的内存任务状态存储，可替换为 Redis/DB。

  - `app/services/classify_worker.py`
    分类后台任务处理：调用 RAG 流水线、解析结果、自动/手动归档。

  - `app/services/file_ops.py`
    文件移动与分类路径规范化。

  - `app/services/category_rules.py`
    军事分类体系配置：一级分类与子分类的文件夹映射。

  - `app/services/chat_service.py`
    对话服务层：文件列表获取、AnythingLLM Workspace/Thread 创建、消息发送。

- `templates/home.html`  
  Web UI 主页面：两个功能入口。

- `templates/classify.html`
  分类与信息抽取页面。

- `templates/chat.html`
  对话页面：文件选择模态框、已选文件展示、多轮对话界面。

- `static/styles.css`
  Web UI 公共样式表。

- `static/chat_styles.css`
  对话模块专用样式表。

- `static/app.js`
  分类与信息抽取模块前端交互逻辑（上传、进度轮询、结果渲染、分类确认等）。

- `static/chat.js`
  对话模块前端：文件选择模态框、Workspace/Thread 创建、消息收发、对话历史管理。

- `ssh.py`  
  SSH 端口转发工具，用于连接远程 Ollama/模型服务。


## 主要调用关系

```
浏览器
  └─> web_ui.py (启动)
        └─> app/ (Flask Blueprint)
              ├─> templates/home.html
              ├─> templates/classify.html + static/app.js
              └─> templates/chat.html + static/chat.js + static/chat_styles.css

分类模块:
web_ui.py
  └─> app/blueprints/classify.py
        └─> app/services/classify_worker.py
              ├─> rag_with_ocr.py (process_file_with_rag)
              │     └─> pipeline.py
              │           └─> anythingllm_client.py
              │                 └─> config.py
              └─> app/services/file_ops.py
                    └─> app/services/category_rules.py

对话模块:
web_ui.py
  └─> app/blueprints/chat.py
        └─> app/services/chat_service.py
              ├─> pipeline.py (prepare_upload_files，OCR 预处理)
              └─> anythingllm_client.py (Workspace/Thread/Embedding/Chat)
                    └─> config.py
```

## 数据流说明（简化）

### 分类与信息抽取

1. 浏览器访问 `/`，渲染 `templates/home.html`。
2. 进入 `/classify` 后，渲染 `templates/classify.html` 并加载 `static/styles.css`、`static/app.js`。
3. `static/app.js` 选择文件并调用 `/api/classify/upload` 或 `/api/classify/upload_folder` 上传。
4. 后端启动后台线程调用 `rag_with_ocr.process_file_with_rag`。
5. `pipeline.prepare_upload_files` 执行前置预处理：
   - 非 PDF 或可提取文本 PDF：原文件直传；
   - 扫描件 PDF：OCR 转 Markdown 并上传 Markdown；
   - OCR 失败：自动降级直传原 PDF。
6. `pipeline.run_anythingllm_rag`：
   - 创建 workspace/thread
   - 上传文档、等待处理
   - 更新 embedding
   - 发送 Prompt，获取 JSON 结果
7. `static/app.js` 轮询 `/api/classify/status/<task_id>` 获取结果并渲染页面。
8. 若需人工确认分类，前端调用 `/api/classify/select_category` 或 `/api/classify/select_category_batch`，后端完成分类并移动文件。

### 对话问答

1. 进入 `/chat` 后，渲染 `templates/chat.html` 并加载 `static/chat_styles.css`、`static/chat.js`。
2. 用户点击"选择对话文件"，前端调用 `GET /api/chat/files` 获取已归档文件列表，在模态框中展示。
3. 用户勾选文件并确认后，点击"开始对话"，前端调用 `POST /api/chat/setup`：
   - 后端对选定文件进行 OCR 预处理（如需要）
   - 创建 AnythingLLM Workspace 和 Thread
   - 上传文档并生成 Embedding
   - 返回 `workspace_slug`、`thread_slug`、`document_ids`
4. 用户输入问题，前端调用 `POST /api/chat/message`（携带 workspace_slug、thread_slug、message、document_ids）。
5. 后端通过 AnythingLLM 发送消息并返回 AI 回复，前端渲染到对话界面。
6. 用户可重新选择文件，此时清除当前会话并重新建立 Workspace。

## 运行与调试建议
> 注意：首次部署需要在`config.py`中配置Anything LLM的个人API Key，LLM，和嵌入模型。

- CLI 运行：`python rag_with_ocr.py <file>`  
- Web UI：`python web_ui.py` -> 访问 `http://127.0.0.1:5001`（可通过 `WEB_UI_PORT` 指定端口）
  - 首页：`/`
  - 分类与抽取：`/classify`
  - 对话问答：`/chat`
  - 甲方协议：`/llm/analysis`、`/llm/generate-report`、`/llm/check-task`、`/llm/progress`
- 远程模型：先运行 `ssh.py` 建立端口转发

## OCR 部署说明（Windows / Linux）

### 1) 安装 Tesseract

- Windows：
  - 推荐安装 [UB Mannheim 构建版](https://github.com/UB-Mannheim/tesseract/wiki)
  - 安装后将 `tesseract.exe` 所在目录加入 `PATH`
  - 如语言包不在默认路径，可设置 `TESSDATA_PREFIX`

- Linux：
  - Ubuntu/Debian 示例：
    - `sudo apt-get update`
    - `sudo apt-get install -y tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng`
  - 若语言包路径非默认，设置 `TESSDATA_PREFIX`

> OCR 采用 CPU 本地离线方案

### 2) 环境变量配置

#### AnythingLLM

- `ANYTHINGLLM_BASE_URL`：AnythingLLM API 地址，默认 `http://localhost:3001/api/v1`
- `ANYTHINGLLM_API_KEY`：API Key
- `ANYTHINGLLM_TIMEOUT`：请求超时（秒），可设 `none` 关闭超时
- `ANYTHINGLLM_STORAGE_ROOT`：AnythingLLM storage 根目录（可选）
  - 未设置时：
    - Windows 默认 `%APPDATA%/anythingllm-desktop/storage`
    - Linux 默认 `~/.anythingllm/storage`
  - 若该目录不可用，将跳过“本地文件生成等待”并继续后续 embedding 逻辑

#### OCR

- `DOCSENSE_OCR_ENABLED`：是否启用前置 OCR，默认 `true`
- `DOCSENSE_OCR_LANGUAGES`：OCR 语言，默认 `chi_sim+eng`
- `DOCSENSE_OCR_DPI`：OCR DPI，默认 `300`
- `DOCSENSE_OCR_SAMPLE_PAGES`：扫描件判定抽样页数，默认 `3`
- `DOCSENSE_OCR_TEXT_THRESHOLD`：扫描件判定文本阈值，默认 `50`
- `DOCSENSE_OCR_CACHE_DIR`：OCR Markdown 缓存目录，默认 `.runtime/ocr_markdown`
- `TESSDATA_PREFIX`：Tesseract 语言包目录（可选，沿用 Tesseract 官方环境变量名）

#### 文件落盘目录

- `DOCSENSE_FILE_STORE_DIR`：分类后文件存放目录，默认 `uploads`
- `DOCSENSE_TEMP_UPLOAD_DIR`：上传暂存目录，默认 `.runtime/inbox`

#### 甲方协议集成

- `DOCSENSE_LLM_CALLBACK_URL`：甲方业务系统回调地址
- `DOCSENSE_LLM_CALLBACK_TIMEOUT`：回调超时秒数，默认 `10`
- `DOCSENSE_LLM_TASK_DB`：正式 `/llm/*` 任务 SQLite 路径，默认 `.runtime/llm_tasks.sqlite3`
- `DOCSENSE_LLM_DOWNLOAD_TIMEOUT`：文件下载超时秒数，默认 `60`
- `DOCSENSE_LLM_DOWNLOAD_DIR`：甲方文件下载暂存目录，默认 `.runtime/llm_downloads`

#### 文件解析范围约束

- `/llm/analysis` 中 `channel`、`country`、`format`、`maturity`、`architectureList` 若由甲方请求显式传入，则以后端收到的请求范围为准。
- 若这些字段缺失或为空，后端会自动注入默认测试范围，仅用于当前测试联调。
- 默认 `format` 测试范围为：`音频类`、`文档类`、`图片类`。
- 默认 `architectureList` 测试范围按 `rag_with_ocr.py` 中的分类体系生成。
- 最终回调时：
  - 命中范围内值才返回
  - 未命中或越界则留空
  - `architectureId` 未命中返回 `0`

### 3) OCR 缓存与降级策略

- 扫描件 PDF OCR 产物会缓存为：
  - `<fingerprint>.md`
  - `<fingerprint>.meta.json`
- 同一文件未变更时复用缓存，不重复 OCR。
- OCR 失败时不会中断任务，会自动降级为上传原 PDF。

## 前端开发/调试说明

- 页面模板：`templates/home.html`、`templates/classify.html`、`templates/chat.html`。
- 样式与脚本：`static/styles.css`、`static/chat_styles.css`、`static/app.js`、`static/chat.js`。
- 修改静态文件无需重启服务，刷新浏览器即可；修改 `web_ui.py` 时建议重启（debug 模式会自动重载）。
- 调试接口：浏览器开发者工具查看：
  - 分类模块：`/api/classify/upload`、`/api/classify/upload_folder`、`/api/classify/status/<task_id>`、`/api/classify/select_category`、`/api/classify/select_category_batch`
  - 对话模块：`/api/chat/files`、`/api/chat/setup`、`/api/chat/message`

## 开发期接口测试方案

推荐按以下顺序进行本地联调：

1. 启动本地文件服务：
   - `pwsh -NoLogo -Command "./scripts/start_test_file_server.ps1"`
2. 启动本地假回调服务：
   - `pwsh -NoLogo -Command "python scripts/mock_callback_server.py"`
3. 配置回调地址并启动应用：
   - PowerShell 7 示例：
   - `$env:DOCSENSE_LLM_CALLBACK_URL='http://127.0.0.1:9000/llm/callback'`
   - `python web_ui.py`
4. 运行正式接口测试脚本：
   - `pwsh -NoLogo -Command "./scripts/test_llm_analysis.ps1"`
   - `pwsh -NoLogo -Command "./scripts/test_llm_report.ps1"`
   - `pwsh -NoLogo -Command "./scripts/test_llm_check_task.ps1"`
   - `pwsh -NoLogo -Command "./scripts/test_llm_progress.ps1"`

仓库内已提供：

- 样例请求：`tests/fixtures/llm/*.json`
- 本地下载素材：`tests/fixtures/files/sample.txt`
- PowerShell 7 脚本：`scripts/test_llm_*.ps1`
- 本地假回调服务：`scripts/mock_callback_server.py`

这些脚本的目标是让你在甲方前后端完全接通前，先独立验证协议、任务流、回调和 WebSocket 进度推送。

## 前端目录结构

```
templates/
  home.html           # 主页面（入口）
  classify.html        # 分类与信息抽取页面
  chat.html            # 对话页面（文件选择 + 多轮对话）
static/
  styles.css          # 公共页面样式
  chat_styles.css     # 对话模块专用样式
  app.js              # 分类与抽取模块前端交互
  chat.js             # 对话模块前端（文件选择、工作区创建、消息收发）
```

## 接口清单

| 方法 | 路径 | 说明 | 请求 | 返回 |
| --- | --- | --- | --- | --- |
| GET | `/` | 主页面（入口选择） | 无 | HTML 页面 |
| GET | `/classify` | 分类与信息抽取页面 | 无 | HTML 页面 |
| GET | `/chat` | 对话页面 | 无 | HTML 页面 |
| POST | `/api/classify/upload` | 单文件上传并启动处理 | `multipart/form-data`：`file`，可选 `thread` | `{task_id, message}` 或错误 |
| POST | `/api/classify/upload_folder` | 文件夹批量上传并启动处理 | `multipart/form-data`：`files[]`，可选 `workspace_prefix`、`thread_name` | `{task_id, message}` 或错误 |
| GET | `/api/classify/status/<task_id>` | 轮询任务状态 | 无 | 任务状态 JSON |
| POST | `/api/classify/select_category` | 单文件人工确认分类 | JSON：`task_id`、`category`、`sub_category` | `{message, category}` 或错误 |
| POST | `/api/classify/select_category_batch` | 批量任务人工确认分类 | JSON：`task_id`、`file_index`、`category`、`sub_category` | `{message, category}` 或错误 |
| GET | `/api/chat/files` | 获取已归档文件列表 | 无 | `{files: [{path, name, size, modified}]}` |
| POST | `/api/chat/setup` | 创建对话工作区（上传文件 + Embedding） | JSON：`file_paths[]` | `{workspace_slug, thread_slug, document_ids, message}` 或错误 |
| POST | `/api/chat/message` | 发送对话消息 | JSON：`workspace_slug`、`thread_slug`、`message`、`document_ids[]` | `{response}` 或错误 |
| POST | `/api/chat/upload` | 对话上传（未实现） | - | 501 | 
| POST | `/llm/analysis` | 甲方文件解析正式接口 | JSON：`businessType=file` + `params[0]` | 受理结果 JSON |
| POST | `/llm/generate-report` | 甲方报告生成正式接口 | JSON：`businessType=report` + `params[0]` | 受理结果 JSON |
| POST | `/llm/check-task` | 按业务主键查询任务并补发回调 | JSON：`businessType` + `params[0]` | `{businessType, data, callbackReplayed}` |
| WS | `/llm/progress` | 甲方任务进度推送接口 | 首条消息发送订阅 JSON | 进度 JSON |

## Git Commit 规范

格式：`type: description`

- **feat**: ✨ 新功能
- **fix**: 🐛 修 Bug
- **docs**: 📝 仅文档/注释改动
- **style**: 🎨 代码格式改动
- **refactor**: ♻️ 代码重构
- **chore**: 🔧 构建/依赖/杂项

