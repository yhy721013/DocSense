# DocSense - 甲方协议接口后端服务

DocSense 当前已精简为“接口后端模式”，仅保留甲方协议相关实现，不再包含前端页面与前端调试路由。

## 保留能力

- 文件解析接口：`POST /llm/analysis`
- 报告生成接口：`POST /llm/generate-report`
- 武器装备知识谱系解析接口：`POST /llm/weaponry`
- 任务查询接口：`POST /llm/check-task`
- 任务进度推送：`WS /llm/progress`
- 结果回调：由后端主动 `POST` 到 `DOCSENSE_LLM_CALLBACK_URL`

## 已移除内容

- 页面路由与模板：`/`、`/classify`、`/chat`
- 前端调试 API：`/api/classify/*`、`/api/chat/*`
- 对应静态资源与前端服务层代码

## 核心目录

```text
app/
  __init__.py                       # Flask App 工厂（仅注册 llm 蓝图）
  blueprints/
    llm.py                          # /llm/* 路由定义与 WebSocket 进度通道
  services/
    llm_analysis_service.py         # 文件解析任务执行
    llm_report_service.py           # 报告生成任务执行
    llm_weaponry_service.py         # 知识谱系解析任务执行
    llm_task_service.py             # 任务持久化与回调状态管理
    llm_progress_hub.py             # 进度发布/订阅中枢
    llm_callback_service.py         # 回调发送
    llm_download_service.py         # 文件下载
    mhtml_normalizer.py             # mhtml/mht 文本提取
    knowledge_base/
      database_service.py           # 知识库映射与查询

web_ui.py                           # 服务启动入口
config.py                           # AnythingLLM 与协议配置
pipeline.py                         # 文档预处理 + RAG 调用
anythingllm_client.py               # AnythingLLM API 客户端
docs/接口文档/文件处理和报告生成.md
docs/接口文档/知识谱系解析.md
```

## 快速启动

1. 安装依赖

```bash
pip install -r requirements.txt
```

2. 配置环境变量

- `ANYTHINGLLM_BASE_URL`
- `ANYTHINGLLM_API_KEY`
- `DOCSENSE_LLM_CALLBACK_URL`

可选参数见 `config.py` 与 `app/settings.py`。

3. 启动服务

```bash
python web_ui.py
```

默认监听：`http://127.0.0.1:5001`

## 接口清单

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/llm/analysis` | 文件解析（支持单文件和多文件串行处理） |
| POST | `/llm/generate-report` | 报告生成 |
| POST | `/llm/weaponry` | 武器装备知识谱系字段提取 |
| POST | `/llm/check-task` | 查询任务状态并按需补发回调 |
| WS | `/llm/progress` | 任务进度推送（subscribe/query/unsubscribe） |

## 协议文档

- 文件处理与报告生成：`docs/接口文档/文件处理和报告生成.md`
- 知识谱系解析：`docs/接口文档/知识谱系解析.md`

## 本地联调建议

1. 启动本地文件服务：

```powershell
pwsh -NoLogo -Command "./scripts/start_test_file_server.ps1"
```

2. 启动本地回调服务：

```powershell
pwsh -NoLogo -Command "python scripts/mock_callback_server.py"
```

3. 运行接口测试脚本：

```powershell
pwsh -NoLogo -Command "./scripts/test_llm_analysis.ps1"
pwsh -NoLogo -Command "./scripts/test_llm_report.ps1"
pwsh -NoLogo -Command "./scripts/test_llm_check_task.ps1"
pwsh -NoLogo -Command "./scripts/test_llm_progress.ps1"
```

## 说明

- `/llm/analysis` 与 `/llm/generate-report` 支持 `mhtml`/`mht`，服务端会先提取正文文本再进入既有流程。
- 多文件 `analysis` 在同一请求内按顺序串行执行。
- 回调失败可通过 `/llm/check-task` 触发补发。

## Git 提交规范
格式：`type: description`

- `feat`: 新增功能
- `fix`: 修复 bug
- `docs`: 文档更新
- `refactor`: 代码重构
- `test`: 测试相关
- `chore`: 其他变更（如依赖更新、配置修改等） 

## Git 分支规范
- `main`: 稳定版本分支，随时可部署
- `feature/xxx`: 新功能开发分支，完成后合并到 `main`
- `hotfix/xxx`: 紧急修复分支，完成后合并到 `main`，必要时合并到 `feature/xxx`
- `docs/xxx`: 文档更新分支，完成后合并到 `main`
- `test/xxx`: 测试相关分支，完成后合并到 `main`
- 分支命名应简洁明了，反映主要内容或目的
- 合并前应确保代码质量，必要时进行代码审查和测试验证
