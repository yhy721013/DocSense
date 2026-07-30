# Analysis Adapters

阶段 1F-3/1F-4/1F-6 已提供以下未接线 Adapter：

- `task_commands.py`：以 `LLMTaskService.create_analysis_batch_if_allowed` 受理一整个冻结批次。
  它在事务外完成 TaskId、batchId、Codec 往返和输入身份准备，在 SQLite `BEGIN IMMEDIATE` 内由
  Service 重检活动投影与 Callback Guard，并写入带 `batch_id`、`batch_sequence` 和全局
  `dispatch_sequence` 的 execution。它只扫描新批次 execution，拒绝将历史无批次 file execution
  交给新 `RunAnalysisTask`，也不创建线程或进程内队列；

- `task_codec.py`：严格编码/解码 `AnalysisTaskInputV1`，对 schema、字段和持久化身份不一致
  fail closed，并拒绝任意层级重复 JSON 键、损坏的固定范围和无法重放的批内序号；
- `translation.py`：以注入的遗留翻译服务和共享协调器实现任务级 Translation Port，不保存任何
  任务 callback，并在单进程内串行化调用。
- `legacy_files.py`：在每个 execution 的专属目录内调用既有下载、规范化和 OCR；OCR/MinerU 缓存
  也被限制在任务目录，任何越界产物立即失败。
- `legacy_rag.py`：每个 execution 建立独立的旧 `DocumentRagFactory` 租约，显式映射
  `open → execute → close` 生命周期，不维护进程全局 Session 映射；阶段 Conversation 取真实活动
  引用，每次 execute 只消费本次新增 attempt，close 重复调用保持第一次三态结果。
- `legacy_knowledge.py`：把既有永久知识库写入结果映射为 confirmed、known-not-applied 或
  outcome-unknown，结果未知时不擅自清理文档。
- `legacy_audit.py`：复用现有 SQLite 召回和交互审计接口，保存 reserve/finalize、attempt、来源及
  close 生命周期证据；打开失败没有完整 Session 时仍保存 lifecycle 中已确认的部分资源引用，并按
  execution 与审计幂等键精确查回。
- `resource_store.py`：只为带批次身份的新 file execution 写入 `analysis_resource_records`；每次推进
  都比较 state+version，外部引用、所有权、清理意图和延期均为显式 JSON 事实，Adapter 本身不执行
  文件、HTTP 或 AnythingLLM 删除。扫描遇到无法解码的毒记录时，使用仅含 identity/state/version
  的控制面 CAS 隔离并保留原始 payload，单条坏记录不会终止或饿死后续扫描。
- `callback_guard.py` 与 `callback_recovery.py`：复用通用 Callback Guard 的 latest owner、lease/fencing、
  过期 unknown 冻结和严格 2xx 规则；HTTP 仍在 SQLite 事务外，空 URL 也会完成为 `skipped`，同步恢复
  只读取当前终态的新 execution，且以首次读取的 `callback_attempts` 快照原子授权；同一并发波次即使
  明确失败也不会滚动成多次 HTTP，绝不重跑模型或 RAG。恢复查询只解码最终结果，不读取请求和执行输入。

Translation Adapter 对异常、错误类型和空字符串分别给出内部错误码；空结果不得标记为成功。

阶段 1F-5B 后它们已由生产组合根与公开文件分析路由接线；离线验收仍只使用临时 SQLite
与替身 Transport，未连接真实 AnythingLLM、
模型、OCR 或 callback 服务。`task_commands.py` 的提交后扫描入口只是未来 Dispatcher 的持久发现基础，
尚未启动 Dispatcher 或构成可靠任务队列。真实 Dispatcher、生产组合根与公开路由切换仍属于后续阶段；
进程内翻译锁和 SQLite 写事务均不能替代多实例协调。
