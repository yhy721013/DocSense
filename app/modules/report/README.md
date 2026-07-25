# 报告生成模块说明

`report` 模块拥有 `/llm/generate-report` 对应的报告输入规则、结果规则和执行用例边界。
通用任务身份、执行事实、Progress 与可靠回调投递事实仍由 `tasks` 模块拥有；报告模块
不得另建一套只服务报告的通用任务内核。

## 目标依赖方向

```text
Flask/FastAPI/Celery Adapter
  -> report application
      -> report domain
      -> report ports
      -> tasks public ports
```

## 数据所有权

- report：报告业务键、源文件顺序、模板/需求、报告 HTML 与回调载荷规则；
- tasks：task ID、执行状态、输入持久化、Outbox、Progress 与 callback delivery；
- Artifact Adapter：本地任务目录及未来 MinIO 对象引用，不由领域对象保存真实路径；
- AnythingLLM Adapter：外部 Context/Conversation/Document 引用和完整 RAG trace。

## 当前实施状态（阶段 1C 已关闭）

现状/目标黄金契约和 `domain/` 已完成。阶段 1C-2 又建立：

- `application/SubmitReportTask`：原子受理后发布初始 Progress 和可丢 Dispatcher 唤醒；
- `application/RunReportTask`：只按 `TaskId` 恢复并编排文件、模板、RAG、审计、条件终态、
  Callback Guard 和清理；任务事实写入异常按结果不确定处理，且终态后的回调控制面
  故障不得触发二次终态覆盖；
- `ports/`：File、Artifact、Report RAG、Interaction Audit、Callback 和 Dispatcher；
- tasks 公共 `TaskCommandPort`、`ProgressPublisherPort` 及携带输入的执行快照；
- 严格 Fake 的全局调用顺序、故障注入、Artifact 类别/顺序、stale、终态唯一写入和
  “失败后不得继续”测试。

阶段 1C-3 又增加通用 `LegacyTaskCommandAdapter` 与 `ReportTaskCommandCodec`：兼容
SQLite 已可追加 execution、原子分类活动/Callback Guard 冲突、按 TaskId 领取，并以
expected TaskId 原子更新 execution 与最新公开投影。50 同键受理、50 并发领取、50 不同键
受理及双表回滚均已通过离线测试。

阶段 1C-4 已增加可独立装配的任务级本地 Artifact、执行时下载/规范化/Word、多文档
AnythingLLM RAG 和 SQLite Interaction Audit Adapter。RAG 的完整 trace/call/source/resource
lifecycle 可在一个事务中落库；审计失败会阻止成功终态和成功 callback，并保留本地及
外部资源现场。50 个并发 TaskId 的目录隔离和真实 SQLite 离线组合均已验证。

阶段 1C-5 已把 SQLite Callback Guard 装配进新的 `RunReportTask` 离线执行链，并完成
人工解除的追加式审计、精确 HTTP outcome、latest/lease/fencing CAS。任务级资源记录
现在持久化 RAG/Audit/Artifact 引用、CAS 版本、待追加事件和外部调用占用；成功终态才
拥有最终报告，stale/failed 的未提交 final 会被删除。审计追加失败只重放既有事件，
明确清理失败可接续序号恢复，调用结果未知或审计证据不完整则逻辑隔离且不自动重试。

阶段 1C-6 已把上述对象装入当前开发分支的应用组合根：`LocalReportTaskDispatcher` 只用
SQLite accepted 事实和单个 Event 唤醒一条报告执行 Worker，资源恢复与队列诊断各使用
一条固定维护线程；毒任务/坏资源通过持久冷却让出扫描首页，生产组合根以操作系统文件锁
拒绝第二个进程。Flask 路由已改为 Parser → Submit → Presenter，成功返回 202 空体，活动
任务及 Callback Guard 占用返回既有 409，不再创建报告 daemon 线程。Progress 的持久化
latest 复核与 Hub 写入在同业务键原子发布区间完成，不同业务键可并发，旧 accepted 不会
在竞态中覆盖新任务。

阶段 1C-7 已完成契约、并发、故障、架构、静态旧引用和扩大回归验收，并将阶段 2～6
后置责任落档。该链路仍未部署生产，也不等同于可靠队列：当前只支持 SQLite 单实例，
进程崩溃遗留的 `running` 只能告警和人工处置。RabbitMQ、跨进程 ACK/重试、DLQ、多实例
租约与 Redis 通知仍属于阶段 2～7；真实 50+ 容量仍由阶段 10 验收。

2026-07-17 阶段关闭后的审查补强新增 `RecoverReportCallbackSynchronously`：报告类型
`/llm/check-task` 按甲方要求在请求内尝试必要恢复，但候选读取不构成发送权，必须再次通过
SQLite Callback Guard 的 latest/lease/fencing 原子校验。显式 check-task 可以重新授权
`failed/rejected` 的明确结果，`delivery_outcome_unknown` 仍冻结且不得自动重发；50 个并发
恢复请求最多一方外发。Guard 过期扫描已进入隔离维护线程，过期 `sending` 只冻结为 unknown，
绝不因扫描而重发。未来 callback Worker 是后台兜底触发源，必须复用同一应用服务和 Guard，
不能替换甲方同步入口或形成第二套发送权。

## 公开入口与禁止依赖

- 正式内部入口为不可变领域类型、纯规则以及 `SubmitReportTask`/`RunReportTask`/
  `RecoverReportCallbackSynchronously`；
- Application 只能依赖本模块 Domain/Ports 与 tasks 的公开 Domain/Port；
- Domain 禁止依赖 Web、数据库、队列、文件系统、网络、日志设施和供应商 Client；
- 外部接口字段始终以 `docs/接口文档/` 为准，本模块不得自行增删参数。
