# README.md

本说明用于快速理解项目内各个 `.py` 文件的职责与关系，以及给出运行与调试的建议。

## 文件职责概览

- `config.py`  
  读取 AnythingLLM 的基础配置（Base URL、API Key、Timeout），通过环境变量覆盖默认值。

- `anythingllm_client.py`  
  对 AnythingLLM REST API 做统一封装：workspace/thread 创建、文档上传、embedding 更新、流式响应解析等。

- `document_utils.py`  
  文档类型判断工具函数。

- `pipeline.py`  
  核心处理流水线：所有文件直接上传到 AnythingLLM -> 更新 embedding -> 发送 Prompt -> 返回结构化结果。

- `rag_with_ocr.py`  
  CLI 入口：加载配置并调用 `pipeline.process_file_with_rag` 完成整体处理。

- `web_ui.py`  
  Web UI 入口：仅负责创建 Flask App 并启动服务（业务逻辑已拆分到 `app/`）。

- `app/`  
  Web UI 业务层：采用 Flask Blueprint 解耦“分类抽取”与“对话”模块，降低多人协作冲突。

- `templates/home.html`  
  Web UI 主页面：两个功能入口。

- `templates/classify.html`  
  分类与信息抽取页面。

- `templates/chat.html`  
  对话页面（占位）。

- `static/styles.css`  
  Web UI 样式表。

- `static/app.js`  
  分类与信息抽取模块前端交互逻辑（上传、进度轮询、结果渲染、分类确认等）。

- `static/chat.js`  
  对话模块前端（占位）。

- `ssh.py`  
  SSH 端口转发工具，用于连接远程 Ollama/模型服务。


## 主要调用关系

```
浏览器
  └─> web_ui.py (启动)
        └─> app/ (Flask Blueprint)
              ├─> templates/home.html
              ├─> templates/classify.html + static/app.js
              └─> templates/chat.html + static/chat.js

web_ui.py
  └─> rag_with_ocr.py (process_file_with_rag)
        └─> pipeline.py
              └─> anythingllm_client.py
                    └─> config.py
```

## 数据流说明（简化）

1. 浏览器访问 `/`，渲染 `templates/home.html`。
2. 进入 `/classify` 后，渲染 `templates/classify.html` 并加载 `static/styles.css`、`static/app.js`。
3. `static/app.js` 选择文件并调用 `/api/classify/upload` 或 `/api/classify/upload_folder` 上传。
3. `web_ui.py` 启动后台线程调用 `rag_with_ocr.process_file_with_rag`。
4. `pipeline.prepare_upload_files` 直接将所有类型文件添加到上传列表（OCR 由 AnythingLLM 处理）。
5. `pipeline.run_anythingllm_rag`：
   - 创建 workspace/thread
   - 上传文档、等待处理
   - 更新 embedding
   - 发送 Prompt，获取 JSON 结果
6. `static/app.js` 轮询 `/api/classify/status/<task_id>` 获取结果并渲染页面。
7. 若需人工确认分类，前端调用 `/api/classify/select_category` 或 `/api/classify/select_category_batch`，后端完成分类并移动文件。

## 运行与调试建议
> 注意：首次部署需要在`config.py`中配置Anything LLM的个人API Key，LLM，和嵌入模型。

- CLI 运行：`python rag_with_ocr.py <file>`  
- Web UI：`python web_ui.py` -> 访问 `http://127.0.0.1:5001`（可通过 `WEB_UI_PORT` 指定端口）
  - 首页：`/`
  - 分类与抽取：`/classify`
  - 对话（占位）：`/chat`
- 远程模型：先运行 `ssh.py` 建立端口转发

## 前端开发/调试说明

- 页面模板：`templates/home.html`、`templates/classify.html`、`templates/chat.html`。
- 样式与脚本：`static/styles.css`、`static/app.js`。
- 修改静态文件无需重启服务，刷新浏览器即可；修改 `web_ui.py` 时建议重启（debug 模式会自动重载）。
- 调试接口：浏览器开发者工具查看 `/api/classify/upload`、`/api/classify/upload_folder`、`/api/classify/status/<task_id>`、`/api/classify/select_category`、`/api/classify/select_category_batch`。

## 前端目录结构

```
templates/
  home.html           # 主页面（入口）
  classify.html        # 分类与信息抽取页面
  chat.html            # 对话页面（占位）
static/
  styles.css          # 页面样式
  app.js              # 分类与抽取模块前端交互
  chat.js             # 对话模块前端（占位）
```

## 接口清单

| 方法 | 路径 | 说明 | 请求 | 返回 | 
| --- | --- | --- | --- | --- |
| GET | `/` | 主页面（入口选择） | 无 | HTML 页面 | 
| GET | `/classify` | 分类与信息抽取页面 | 无 | HTML 页面 | 
| GET | `/chat` | 对话页面（占位） | 无 | HTML 页面 | 
| POST | `/api/classify/upload` | 单文件上传并启动处理 | `multipart/form-data`：`file`，可选 `thread` | `{task_id, message}` 或错误 | 
| POST | `/api/classify/upload_folder` | 文件夹批量上传并启动处理 | `multipart/form-data`：`files[]`，可选 `workspace_prefix`、`thread_name` | `{task_id, message}` 或错误 | 
| GET | `/api/classify/status/<task_id>` | 轮询任务状态 | 无 | 任务状态 JSON | 
| POST | `/api/classify/select_category` | 单文件人工确认分类 | JSON：`task_id`、`category`、`sub_category` | `{message, category}` 或错误 | 
| POST | `/api/classify/select_category_batch` | 批量任务人工确认分类 | JSON：`task_id`、`file_index`、`category`、`sub_category` | `{message, category}` 或错误 | 
| POST | `/api/chat/upload` | 对话上传（占位） | - | 501 | 
| POST | `/api/chat/message` | 对话消息（占位） | - | 501 | 

## Git Commit 规范

格式：`type: description`

- **feat**: ✨ 新功能
- **fix**: 🐛 修 Bug
- **docs**: 📝 仅文档/注释改动
- **style**: 🎨 代码格式改动
- **refactor**: ♻️ 代码重构
- **chore**: 🔧 构建/依赖/杂项

