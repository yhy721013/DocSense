# 文件解析临时 Workspace 清理与交互持久化

## 1. 背景

`/llm/analysis` 文件解析任务会为每个文件创建名称以 `llm-file-` 开头的临时 AnythingLLM Workspace，并在其中创建 Thread、上传解析文件、执行结构化抽取。

原流程在任务结束后没有删除该 Workspace，长期运行会在 AnythingLLM 中积累大量一次性 Workspace。同时，直接删除 Workspace 会连同 Thread 历史一起删除，而项目原有 SQLite 只保存：

- `llm_tasks.request_payload`：前端提交的原始任务请求。
- `llm_tasks.result_payload`：最终结构化回调结果。

实际提交给模型的提示词、模型原始回答、RAG sources 和临时资源标识此前没有在本地持久化。

## 2. 改造目标

- 每次 `/llm/analysis` 任务结束后删除本次创建的 `llm-file-*` Workspace。
- 删除前将模型交互完整保存到 `llm_tasks.sqlite3`。
- 同一文件重复解析时保留每次调用记录，不被 `llm_tasks` 的任务更新覆盖。
- 清理行为不影响分类知识库使用的 `architectureId-*` Workspace。
- 交互落库失败时优先保护数据，保留 AnythingLLM Workspace 供人工恢复。
- 不改变 `/llm/analysis` 请求、回调及 `/llm/check-task` 协议。

## 3. 整体流程

```text
创建 llm-file-* Workspace
  -> 创建 analysis-* Thread
  -> 上传文件并绑定 Embedding
  -> 提交结构化抽取 Prompt
  -> 获取清洗后的解析文本、模型原始回答和 sources
  -> 继续分类入库、翻译、任务结果落库和回调
  -> finally 保存 llm_interactions
  -> 保存成功后删除本次 llm-file-* Workspace
  -> 更新 Workspace 清理状态
```

临时 Workspace 的删除发生在单个文件任务的 `finally` 阶段，因此正常完成、模型返回失败或后续业务处理异常都会进入清理流程。

## 4. LLM 交互数据表

在现有 `${DOCSENSE_RUNTIME_DIR}/llm_tasks.sqlite3` 中新增 `llm_interactions` 表：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | INTEGER | 自增主键；每次模型调用生成一条新记录 |
| `business_type` | TEXT | 当前固定为 `file` |
| `business_key` | TEXT | 文件任务键，即请求中的哈希文件名 `fileName` |
| `workspace_name` | TEXT | 临时 Workspace 名称 |
| `workspace_slug` | TEXT | AnythingLLM Workspace slug |
| `thread_slug` | TEXT | AnythingLLM Thread slug |
| `prompt` | TEXT | 实际提交给模型的完整提示词 |
| `response` | TEXT | AnythingLLM 返回的原始模型文本 |
| `sources_json` | TEXT | RAG sources 的 JSON 数组 |
| `status` | TEXT | 本次模型交互状态：`succeeded` 或 `failed` |
| `error_message` | TEXT | 模型无有效返回时的错误原因 |
| `workspace_cleanup_status` | TEXT | `pending`、`deleted` 或 `failed` |
| `workspace_cleanup_error` | TEXT | Workspace 清理失败原因 |
| `created_at` | TEXT | 交互记录创建时间，UTC ISO 8601 |
| `completed_at` | TEXT | 交互完成时间，UTC ISO 8601 |

同时创建 `(business_type, business_key, created_at)` 索引，用于按任务查询交互历史。

该表使用 `CREATE TABLE IF NOT EXISTS` 自动初始化，旧版 `llm_tasks.sqlite3` 不需要手工迁移或删除。服务启动并初始化 `LLMTaskService` 后即可补建新表。

## 5. 持久化接口

`LLMTaskService` 新增三个方法：

### 5.1 `create_llm_interaction()`

插入模型调用记录并返回自增 `interaction_id`。初始清理状态固定为 `pending`。

### 5.2 `update_llm_interaction_cleanup()`

Workspace 删除完成后，将清理状态更新为：

- `deleted`：AnythingLLM 确认删除成功。
- `failed`：删除 API 返回失败或调用异常，同时保存错误信息。

### 5.3 `get_llm_interactions()`

按 `business_type` 和 `business_key` 查询全部交互，并按自增 ID 升序返回。读取时自动把 `sources_json` 还原为 `sources` 列表。

该方法目前作为服务内部查询能力，不新增外部 HTTP 接口。现有 SQLite 导出脚本会按表导出数据库内容，因此也可以通过 `scripts/inspect_llm_tasks.py` 检查交互记录。

## 6. RAG 执行信息采集

`app/services/utils/rag_pipeline.py` 新增 `RAGExecutionDetails`，记录：

- `workspace_name`
- `workspace_slug`
- `thread_slug`
- `workspace_created`
- `text_response`
- `raw_response`
- `sources`

`run_anythingllm_rag()` 新增可选参数 `execution_details`。参数缺省时保持原有返回值和调用方式，因此报告生成等现有调用方不受影响。

执行过程中一旦 Workspace 或 Thread 创建成功，对应标识会立即写入该对象。即使后续上传、Embedding 或模型调用失败，文件解析服务仍能在 `finally` 中定位已经创建的临时 Workspace。

`AnythingLLMClient.send_prompt_to_thread()` 同时返回：

- `textResponse`：去除 `<think>` 和 Markdown JSON 代码块后，供现有解析逻辑使用的文本。
- `rawTextResponse`：清洗前的模型原始文本，供审计和问题复现。
- `sources`：AnythingLLM 返回的 RAG 来源信息。

## 7. Workspace 删除边界

只有同时满足以下条件才会执行删除：

1. 本次 RAG 调用实际创建了 Workspace，即 `workspace_created=True`。
2. 已取得有效 `workspace_slug`。
3. `workspace_name` 以 `llm-file-` 开头。
4. 交互记录已经成功写入 SQLite。

这组条件用于避免误删以下持久资源：

- 文件分类完成后使用的 `architectureId-*` 知识库 Workspace。
- `/llm/chat` 使用的对话 Workspace。
- `/llm/weaponry` 使用的类别或任务级 Workspace。
- `/llm/report` 创建的 `llm-report-*` Workspace。

本次改造只处理 `/llm/analysis` 产生的 `llm-file-*` Workspace。

## 8. 失败处理

### 8.1 交互落库失败

如果 SQLite 写入失败：

- 记录异常日志。
- 不删除 AnythingLLM Workspace。
- 保留 Thread 和上游对话，避免本地记录与上游记录同时丢失。

该情况需要修复数据库权限、磁盘空间或 SQLite 锁问题后人工处理残留 Workspace。

### 8.2 Workspace 删除失败

如果 AnythingLLM 删除 API 返回失败或抛出异常：

- 不覆盖已经生成的任务结果和回调状态。
- 将 `workspace_cleanup_status` 记录为 `failed`。
- 将失败原因写入 `workspace_cleanup_error`。
- 保留 Workspace，便于后续重试清理。

### 8.3 清理状态更新失败

如果 Workspace 已删除，但 SQLite 清理状态更新失败，只记录异常日志。此时数据库记录可能仍显示 `pending`，需要结合 AnythingLLM 实际 Workspace 列表核对。

### 8.4 进程强制终止

`finally` 只能处理 Python 进程仍能正常展开异常栈的场景。如果进程被强制结束、机器掉电或容器被立即终止，本次 Workspace 仍可能残留。后续可以增加启动时或定时补偿任务：

1. 查询超过保留时间的 `llm-file-*` Workspace。
2. 根据 `llm_interactions` 判断是否已经持久化。
3. 删除确认可清理的 Workspace。
4. 更新清理状态。

## 9. 与 `chat_sessions.sqlite3` 的关系

本次不修改 `chat_sessions.sqlite3`。

该数据库用于 `/llm/chat` 会话索引，当前只保存 `chat_id`、关联文件、回合时间以及 AnythingLLM Workspace/Thread 标识，消息正文仍从 AnythingLLM 实时读取。文件解析属于后台任务审计，不属于用户连续对话，因此交互记录放入 `llm_tasks.sqlite3` 更符合职责边界。

如果未来也要删除 `/llm/chat` 的 Workspace，需要另行增加 `chat_messages` 等本地消息表，不能直接复用本次文件解析清理逻辑。

## 10. 验证范围

新增离线测试覆盖：

- 提示词、模型回答和 sources 写入 `llm_interactions`。
- 清理状态由 `pending` 更新为 `deleted`。
- 文件任务先持久化交互，再删除临时 Workspace。
- 删除调用只使用本次捕获的 `llm-file-*` slug。
- AnythingLLM 原始回答和清洗后回答能够同时返回。

实施阶段执行了：

- 全项目 69 个 Python 文件 AST 语法检查。
- 直接 `print()` 回归扫描。
- `git diff --check`。

未启动 `run.py`，也未执行依赖 AnythingLLM、模型或 OCR/MinerU 后台服务的测试套件。

## 11. 相关代码

- `app/services/llm_service/analysis_service.py`
- `app/services/llm_service/task_service.py`
- `app/services/utils/rag_pipeline.py`
- `app/services/utils/anythingllm_client.py`
- `tests/test_analysis_service.py`
- `tests/test_task_service.py`
- `tests/test_anythingllm_client.py`

