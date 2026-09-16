# 报告应用层

本目录提供两个正式、框架无关的内部用例：

- `SubmitReportTask`：调用 Tasks 原子受理端口；活动任务、旧 callback 正在发送或结果
  未知时立即返回内部冲突，不在 Web 请求线程等待或重试；成功后发布初始 Progress 和
  可丢的常量空间 Dispatcher 唤醒，通知失败不撤销持久化受理事实。
- `RunReportTask`：只接收 `TaskId`，恢复不可变输入，条件领取后依次编排文件、模板、
  多文档 RAG、完整交互审计、expected-task-id 终态、Progress、Callback Guard 和清理。
  任务进度或终态写入抛出异常时按“提交结果不确定”保留现场并向上抛出，不补写第二个
  失败终态，也不继续 Progress、Callback 或清理；终态已经明确提交后，latest/Guard/
  投递故障只进入回调错误维度，不反向改写业务终态。

应用层禁止依赖 Flask/FastAPI、SQLite/SQLAlchemy、requests、Celery、真实路径、环境变量
和 AnythingLLM Client。完整 RAG trace 未通过原子审计时，成功终态和成功回调均被阻断，
同时保留外部资源与 Artifact 现场。

阶段 1C-3 已用真实兼容 SQLite Task Command Adapter 驱动 `SubmitReportTask` 的受理边界，
并验证 execution、领取和 expected TaskId 条件写。阶段 1C-4 又以真实本地 File/Artifact、
多文档 RAG 和 SQLite Audit Adapter 离线驱动 `RunReportTask(task_id)`，验证了成功链路及
审计事务失败时禁止成功终态/callback、保留现场。阶段 1C-5 新增
`ReportResourceRecoveryService`，以终态权威所有权、CAS 资源事实、精确事件重放和
逻辑隔离完成 Guard/资源恢复闭环。阶段 1C-6 已由组合根把 Submit/Run、一条报告执行
Worker、隔离的恢复/诊断线程和持久扫描接入当前 Flask 报告路由；全面审查又补齐毒任务与
坏资源冷却、真实停机语义和跨进程单实例门禁。应用层本身仍不感知 Flask、线程、SQLite
或 AnythingLLM 实现。代码尚未部署生产，可靠队列替换仍属于阶段 3～6。
