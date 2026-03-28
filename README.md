# DocSense - 甲方协议 LLM 接口后端

DocSense 当前为纯后端接口服务，聚焦甲方协议定义的 LLM 任务处理能力，不包含前端页面与前端调试路由。

## 1. 核心能力

- 文件解析：`POST /llm/analysis`
- 报告生成：`POST /llm/generate-report`
- 武器装备知识谱系解析：`POST /llm/weaponry`
- 任务查询与回调补发：`POST /llm/check-task`
- 任务进度推送：`WS /llm/progress`
- 结果回调：服务端主动 `POST` 到 `CALLBACK_URL`

## 2. 分层架构与调用关系

### 2.1 分层职责

| 层级 | 目录 | 职责 | 代表文件 |
| --- | --- | --- | --- |
| 接口层 | `app/blueprints/` | HTTP/WS 入参校验、任务受理、线程派发、返回协议响应 | `llm.py` |
| 业务层 | `app/services/llm_service/` | 文件解析、报告生成、谱系提取、任务状态管理、翻译编排 | `analysis_service.py` `report_service.py` `weaponry_service.py` `task_service.py` |
| 核心基础层 | `app/services/core/` | 全局配置、路径常量、日志、任务/知识库数据库、进度中枢、Prompt 构建 | `config.py` `settings.py` `database.py` `progress_hub.py` `prompts.py` |
| 工具与外部边界层 | `app/services/utils/` | AnythingLLM 客户端、回调发送、文件下载、OCR 预处理、mhtml 归一化、RAG 流程 | `anythingllm_client.py` `callback_client.py` `file_downloader.py` `ocr_preprocessor.py` `mhtml_normalizer.py` `rag_pipeline.py` |
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
  __init__.py                       # Flask App 工厂，仅注册 llm 蓝图
  blueprints/
    llm.py                          # /llm/* 路由 + WebSocket 进度通道
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
    utils/
      anythingllm_client.py         # AnythingLLM HTTP 客户端
      callback_client.py            # 回调发送
      file_downloader.py            # 下载到临时文件
      mhtml_normalizer.py           # mhtml/mht 归一化
      ocr_preprocessor.py           # 扫描件 OCR 预处理
      rag_pipeline.py               # 文件上传 + RAG 调用流水线
    translator/                     # 翻译底层能力

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
- `APP_HOST`（默认 `127.0.0.1`）
- `APP_PORT`（默认 `5001`）
- `APP_DEBUG`（默认 `true`）

3. 启动服务

```bash
python run.py
```

默认监听：`http://127.0.0.1:5001`

## 7. 运行时路径与持久化

- 任务库：`.runtime/llm_tasks.sqlite3`（`DOCSENSE_LLM_TASK_DB`）
- 知识库映射库：`.runtime/knowledge_base.sqlite3`（`DOCSENSE_KNOWLEDGE_BASE_DB`）
- 下载缓存目录：`FILE_DOWNLOAD_DIR`（用于任务下载源文件）

## 8. 本地联调与测试

本地联调脚本（PowerShell）：

```powershell
pwsh -NoLogo -Command "./scripts/start_test_file_server.ps1"
pwsh -NoLogo -Command "python scripts/mock_callback_server.py"
pwsh -NoLogo -Command "./scripts/test_llm_analysis.ps1"
pwsh -NoLogo -Command "./scripts/test_llm_report.ps1"
pwsh -NoLogo -Command "./scripts/test_llm_check_task.ps1"
pwsh -NoLogo -Command "./scripts/test_llm_progress.ps1"
```

本地联调脚本（macOS / zsh）：

```bash
zsh scripts/start_test_file_server.sh
python scripts/mock_callback_server.py
zsh scripts/test_llm_analysis.sh
zsh scripts/test_llm_report.sh
zsh scripts/test_llm_check_task.sh
zsh scripts/test_llm_progress.sh
```

脚本默认行为：

- 自动读取仓库根目录 `.env`，不存在时回退 `.env.example`
- `test_llm_analysis.sh` 默认请求 `POST /llm/analysis`
- `test_llm_report.sh` 默认请求 `POST /llm/generate-report`
- `test_llm_check_task.sh` 默认请求 `POST /llm/check-task`
- `test_llm_progress.sh` 默认连接 `WS /llm/progress`

可选参数示例：

```bash
zsh scripts/start_test_file_server.sh 8000 tests/fixtures/files
zsh scripts/test_llm_analysis.sh http://127.0.0.1:5001 tests/fixtures/llm/analysis_request.json
zsh scripts/test_llm_report.sh http://127.0.0.1:5001 tests/fixtures/llm/report_request.json
zsh scripts/test_llm_check_task.sh http://127.0.0.1:5001 tests/fixtures/llm/check_task_file_request.json
zsh scripts/test_llm_progress.sh ws://127.0.0.1:5001/llm/progress tests/fixtures/llm/check_task_file_request.json 5 false
```

Windows 与 macOS 可按各自环境选择对应脚本。

单元测试（仓库默认 `unittest`）：

```bash
.venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

## 9. 协议文档

- 文件处理与报告生成：`docs/接口文档/文件处理和报告生成.md`
- 知识谱系解析：`docs/接口文档/知识谱系解析.md`

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
