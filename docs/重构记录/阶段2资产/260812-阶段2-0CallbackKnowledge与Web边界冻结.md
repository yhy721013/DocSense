# 阶段 2-0 Callback History、Knowledge Database 与 Web Adapter 边界冻结

> 冻结日期：2026-08-12  
> 性质：内部所有权与依赖方向，不改变 Debug 或任何公开接口  
> 机器资产：`tests/contracts/stage2_boundary_contract.json`

## 1. Callback 本地 JSON 只作诊断副本

当前三个业务 Callback Guard 在发送前调用
`app/services/utils/callback_client.py::save_callback_history_payload`，Mock Callback Server 也复用该
Writer；Debug 则通过独立的 `FileCallbackHistoryReadAdapter` 只读展示 `${DOCSENSE_RUNTIME_DIR}/callback/`。

该文件的冻结语义是：best-effort、追加创建、同名不覆盖。写入失败只记录不含 payload/路径的警告，
不能改变 Callback HTTP、Guard、业务终态或 Task 状态。它不是以下任何事实的权威来源：

- Callback 是否成功或是否可能已经送达；
- 是否允许 `/llm/check-task` 显式补发；
- latest-wins/受理冲突；
- Task Step、Reaper 或 Recovery Observation/Decision。

阶段 2 把 Writer 迁到 `app/infrastructure/observability/callback_history.py`，保留原目录和 Debug Reader。
当前无生产调用方的通用 `post_callback_payload` 经引用门禁后删除；发送语义继续归三个业务 Callback
Adapter，不建立新的通用 HTTP Facade。未完成真实数据保留期决策前，自动 TTL/清理保持关闭。

## 2. Knowledge `DatabaseService` 是阶段 3A 输入

`app/services/core/database.py::DatabaseService` 当前仍是 `workspaces/documents` 的唯一物理 Writer。
阶段 2 不把它搬进 Task Control DB、不复制表、不先做一次 SQLite 路径迁移；现有七个直接依赖固定为
Container、AnythingLLM Factory/Gateway、Chat/Weaponry Knowledge Adapter 和 Debug 装配/快照 Adapter。
阶段 2 新代码不得新增 `DatabaseService` 直接依赖，必须依赖已有或新建的窄 Knowledge Port。

阶段 3A 必须同时承接：

- `workspaces(id, architecture_id, workspace_slug)` 的双唯一约束；
- `documents` 的代理主键、原始/实际上传文件名、外部文档 ID、路径和 metadata；
- `(architecture_id, file_name)` 唯一约束以及 Reassign 冻结的稳定 `documents.id`；
- Knowledge Index operation/collection 与 RAG resource lease 的恢复事实；
- 外键开启、跨 Repository 事务顺序、迁移/回滚和并发验证。

因此，阶段 2 只清除巨型 `LLMTaskService` 对 Knowledge 窄 Store 的转发，不宣称 Knowledge 已迁入
共享数据库，也不以 Task UoW 包围 AnythingLLM/文件网络 I/O。

## 3. `app/adapters/web` 是目标协议边界

当前目录共 16 个文件，继续承担 Flask 请求解析、连接生命周期、Application Command 构造，以及
框架无关的公开 ID/Chat Scope 规范化。它不是遗留目录，阶段 2 禁止整体搬迁或为未来 FastAPI 复制
业务编排。

本目录禁止导入/持有：SQLite、后台 Thread/Executor、`LLMTaskService`、`DatabaseService`、
AnythingLLM 具体实现、Task 状态机或后台 Executor。连接级 Registry 可以使用 `RLock` 保护同一连接
的订阅令牌；这不授予它创建后台执行线程的职责。公开路径、参数、状态码、Callback、SSE/WebSocket 语义始终由
`docs/接口文档/` 决定；本次哈希门禁证明没有发生契约修改。

## 4. 本步检查结论

- Callback History 当前消费者与目标 Writer 已冻结，通用 POST 没有生产消费者；
- Knowledge 当前七个直接依赖和两张表的关键 Schema 已冻结，阶段 2 不新增直依赖；
- Web 目录文件集完整，AST 检查未发现禁止的基础设施导入或后台线程构造；现有 `RLock` 仅保护
  Progress 单连接 Registry。

未发现需要修改公开接口或调整阶段 3A 边界的事项。
