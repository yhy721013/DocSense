# 报告适配层

阶段 1C-6 当前包含以下可独立装配的生产形态 Adapter；构造或导入它们不会启动主进程或连接
真实后台服务：

- 把 `ReportSubmission` 编码为包含 TaskId、reportId 双表示、源 URL 顺序、模板、需求、
  accepted time 和 trace 的完整 execution JSON；
- 把同一输入生成兼容旧 `llm_tasks.request_payload` 的最新投影 JSON；
- 从 SQLite 严格恢复 `ReportInputSnapshot`，拒绝未知 Schema、缺失/额外字段和身份漂移；
- 把 `ReportTaskCompletion` 分别编码为最小内部 execution 结果和精确公开 callback 投影，
  避免 check-task 恢复误发内部 wrapper；
- 使用短 SQLite 事务完成 callback latest 权威复核、lease/fencing CAS、过期 unknown 冻结，
  在事务外区分 HTTP 明确成功、拒绝、明确未送达和结果未知；人工解除以追加式审计保存，
  不会被下一次 acquire 覆盖；
- `LocalReportArtifactAdapter` 使用 TaskId 哈希隔离任务目录，以临时文件和原子替换发布
  scratch/最终 HTML，并返回不透明引用、大小和 SHA-256；
- `LegacyReportFileAdapter` 在 Worker 执行时下载源文件/模板，包装现有 MHTML、OCR 和
  Word 提取工具，并把任何工具输出重新收口到当前任务目录；
- `AnythingLLMReportRagAdapter` 按请求顺序在同一临时 Workspace 中上传多文档，使用每次
  调用独占的 Transport/Client，保存 trace/call、来源验证和完整资源生命周期；
- `SQLiteReportInteractionAuditAdapter` 通过一个原子入口保存主交互、全部 attempts 和
  初始 lifecycle，只有成功凭据返回后才允许业务成功或外部资源清理；
- `SQLiteReportResourceStoreAdapter` 持久化任务级 RAG/Audit/Artifact 恢复事实，以
  execution 终态权威决定最终报告所有权，并用 version CAS 隔离并发恢复者；独立恢复
  调度列可在 JSON payload 损坏时仍把坏记录移出扫描首页。
- `LocalReportTaskDispatcher` 以 SQLite accepted 为积压事实、单个 Event 为常量空间唤醒，
  固定一条报告执行 Worker 按事务序号有界扫描并持续排空；领取前毒任务持久冷却，等待
  共享执行许可可响应停机；资源恢复和队列诊断各使用一条隔离维护线程，running 只读
  观测且告警节流；只有线程真实退出后才标记关闭。
- `FileProcessSingletonGuard` 使用操作系统文件锁拒绝第二个进程取得本地 Dispatcher 所有权；
  当前模式禁止 preload/fork，不能把该门禁描述成多实例调度能力。

以上对象已在当前开发分支的应用组合根和报告路由中完成装配，并通过无真实网络的离线
执行链验证；代码尚未部署生产。当前 Dispatcher 是 SQLite 单实例兼容实现；文件锁只
负责拒绝重复本地 Worker，不提供 RabbitMQ late ACK、跨进程任务重试、DLQ 或多实例租约。
