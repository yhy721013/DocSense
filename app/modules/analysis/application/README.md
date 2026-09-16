# Analysis Application

本层在 1F-3 提供 `RunAnalysisTask`：它只接收内部 `TaskId`，领取受理时冻结的输入，并用 expected
TaskId 条件写推进既有任务状态和 Progress。它不导入 Flask、SQLite、HTTP Client、旧
`analysis_service` 或文件系统实现。

用例的关键收口顺序是：文件准备 → 召回审计 reserve → 资源登记 → 任务级 RAG（每个引用即时 CAS）→
分类/抽取 → 交互审计 → 永久知识库 → 可降级翻译 → 终态 → Callback Guard → RAG close。交互审计未确认
时不得永久入库；知识库或关闭结果未知时保留现场，不自动重放；Factory 退出异常不得覆盖已经提交的终态。

1F-6 新增的 `AnalysisResourceLifecycle` 与 `RecoverAnalysisResources` 只接受 Port：前者在外部 close 前
依次持久化 `planned/running` 意图、外部三态结果和审计追加结果，后者只补可证明幂等的
`append_lifecycle_events`。`RecoverAnalysisCallbackSynchronously` 同样复用正常 Worker 的 Callback Guard，
等待已有 lease 有界结束后仍以首次读取的 callback attempt 快照做原子复核，防止同一批并发恢复在
明确失败后连续重发。资源恢复预算耗尽或 payload 结构无效时通过不解码 payload 的控制面入口隔离；
`outcome_unknown`、审计身份不完整和所有权未知都进入隔离，绝不自动删除或重发。

1F-3R 将首次 expected TaskId 门禁前移到工作目录创建之前，并保证 Context-only 等打开失败只要存在
生命周期证据就会先写失败交互审计；完整 Session 只继续作为成功审计和可执行 close 的必要条件。

1F-3S 已将此前 2,162 行的单文件实现做等价拆分：

- `run_analysis.py` 保留公开导出、依赖装配、`TaskId` 顶层顺序、RAG Factory 作用域和兼容的知识库
  幂等键入口；
- `workflow_models.py` 承载公开结果/异常类型及每次 execution 独享的计划、RAG 生命周期状态；
- `model_workflow.py` 承载冻结范围、Prompt、分类、repair、RAG 调用和结果映射；
- `audit_lifecycle.py` 承载召回/交互/关闭审计；`knowledge_handoff.py` 承载知识库转交与翻译降级；
  `failure_convergence.py` 承载 expected TaskId、Progress、终态和 fail-closed 现场保留。

1F-3S 的拆分本身没有接入新路由、Container、Dispatcher 或后台线程；随后 1F-6 仅在既有 Facade
加入可选的 Resource/Callback 内部依赖和恢复用例，仍未装配生产组合根。`run_analysis.py` 现为 511 行，
永久 AST 门禁限制其不超过 700 行，并禁止它重新直接导入 Prompt、分类规则内部函数或结果映射算法；
所有协作器仍使用原 `run_analysis` logger 名称，避免日志分类随源码文件移动而变化。

1F-4 新增 `SubmitAnalysisBatch` 与 `AnalysisBatchOrderCoordinator`。1F-6 为 `RunAnalysisTask` 追加可选
`resources`、`callbacks` 和 `callback_url` 内部依赖；未注入时保持旧的离线编排行为，注入时不会将内部
TaskId、batchId、lease 或资源状态投影到公开响应。前者只调用一次 `AnalysisBatchCommandPort.create_batch_if_allowed`，成功后逐项核对
fileName、共同 batchId 和连续 batchSequence，再调用一次 `Dispatcher.wake_up()`；唤醒抛错只记日志，
绝不撤销已经提交的受理事实。它不做 `get_task`/latest 事务外预查、不创建线程、不保存内存队列，也不会
向 HTTP、Progress 或 Callback 投影 TaskId/batchId。真实 Dispatcher、生产组合根和公开路由切换仍在后续
阶段，当前 SQLite 离线验证不代表多实例或可靠队列就绪。
