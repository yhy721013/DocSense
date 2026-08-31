# Chat 业务模块目录说明

本目录是文件对话与知识谱系对话共用的底层业务模块。它以本地权威状态为中心，分离业务用例、领域模型、抽象端口、基础设施适配器和生产装配，使当前 SQLite 单实例实现能安全运行，并为未来可靠队列、多实例和共享数据库保留替换边界。

## 目录结构

| 路径 | 作用 |
| --- | --- |
| `__init__.py` | 文件对话公共导出门面；组合根和路由从这里导入稳定类型。 |
| `composition.py` | Chat 唯一生产组合根；集中装配 Store、协调器、AnythingLLM 工厂及各应用服务。 |
| `application/` | 用例编排：受理运行、流执行、历史、标题、中断、删除和清理调度。 |
| `domain/` | 与供应商无关的实体、状态常量、流事件和资源标识规则。 |
| `ports/` | 对话供应商、持久化和运行协调的抽象契约。 |
| `adapters/sqlite/` | SQLite 本地权威数据、仓储、资源租约、运行协调、清理任务与事件账本。 |
| `adapters/anythingllm_*.py` | AnythingLLM 对话网关及任务级网络生命周期工厂。 |
| `adapters/knowledge_documents.py` | DocSense 知识记录到不可变 Chat 文档快照的只读适配器。 |

## 主工作流

```text
POST /llm/chat
  -> RunExecutor.prepare_chat_run()
     -> 持久化会话、运行、输入快照和待处理用户消息
  -> Dispatcher.dispatch(run_id)
  -> RunExecutor.execute_chat_run()
     -> 从持久化 Identity Binding 生成 Workspace 名称
     -> 领取运行归属、创建或复用已保存的远端会话、绑定文档、调用 Chat Port
  -> EventRecorder
     -> 事件先入账，再展示；终态原子收敛消息和运行
  -> Presenter
     -> 输出冻结的 SSE 协议
```

## 辅助工作流

- 历史：`HistoryService` 只读取本地已提交消息，不读取 AnythingLLM 历史作为权威来源。
- Weaponry 来源：`ChatSourceMapper` 在成功提交前只删除 Chunk 开头完整的 AnythingLLM
  `<document_metadata>` 包装；清洗后的同一快照同时用于 SSE 与 History。通用供应商 Client、
  Gateway、Presenter 和 History 读取路径不复制该规则，畸形包装失败关闭。
- 标题：`TitleService` 基于本地历史创建带租约的临时线程；清理失败会留下持久化任务。
- 中断：`AbortService` 持久化中断标记，执行器在消费上游事件时观察该标记并收敛本轮消息。
- 删除：`DeleteService` 先使会话进入删除中，再创建清理任务；当前内联调度器同步等待清理完成，以保持已有接口语义。
- 恢复：`ChatCleanupJobExecutor` 与运行锁服务可基于持久化 `job_id`/`run_id` 重试或收敛，不依赖请求闭包。

## 远端资源命名与归属

- File 新 Workspace 使用 `chat-id{chatId}`；Weaponry 新 Workspace 使用 `wChat-user{userId}-arch{architectureId}`。
- 名称只由持久化、规范化后的 `ConversationIdentityBinding` 生成，不从 Flask 请求重新拼接，也不写入 SQLite Schema。
- 主 Thread 保持 `thread-{conversation_id}`，因此 Weaponry 删除后以相同业务名称重建的新世代仍由不同内部 Thread 隔离。
- 已经持久化 `workspace_ref + thread_ref` 的会话直接复用引用，不查找或重命名历史 Workspace。
- 没有本地引用时，任何同名 Workspace 都视为归属不明并失败关闭；不得按名称自动认领、创建 Thread 或删除未知资源。

## 重要边界

- `run_id`、租约、清理任务和事件账本仅供内部使用，绝不能泄露到 HTTP 响应、SSE `data`、响应头或新增接口参数中。
- Application/Domain/Ports 不得导入 Flask、SQLite、AnythingLLM 或知识库实现；具体产品依赖只能位于 Adapters/Composition。
- SQLite 当前明确只支持单实例；不要将其误用为跨实例锁、可靠队列或事务发件箱。
- 新后台工作进程应只接收持久化标识并重新加载数据，不应捕获 Flask 请求、网络会话或内存回调。
- 日志可记录规范化业务 ID、精确 Workspace 名称、内部关联 ID 和排障必需的资源引用；不得记录凭据、正文、Prompt、原始请求响应或 SSE 帧。
