# Weaponry 模块

该模块承载 `/llm/weaponry` 的新垂直切片。阶段 1D-1 已完成纯领域内核；1D-2 已完成
请求/响应边界、文档范围冻结和 execution 输入 Codec；1D-3A 已完成供应商无关 I/O Ports、
严格 Fake、资源清理租约和 50 线程隔离证明；1D-3B 已完成唯一 Schema v2 与生产 I/O Adapter
离线实现；1D-4 已完成 Submit/Run/字段 Application、单终态/资源现场保护、黄金回调、50 个
在途任务隔离和慢 I/O 不持有 SQLite 写事务验证；1D-5 已完成通用持久化 Dispatcher 的
Weaponry 薄适配、严格基础设施配置、共享 limiter、进程锁、离线组合根与容器生命周期验证；
1D-6 已完成真实 Callback Guard、同步 check-task 恢复、资源有界恢复、生产组合根绑定，以及
公开路由的 Parser → Document Scope → Submit → Presenter 切换。2026-07-20 又关闭了 1D-7 前的
代码型阻塞：外部 create 前持久化 Creation Intent、崩溃后只查回/隔离不重放、HTTP 租约覆盖
连接与响应头读取、缺失 execution 的资源隔离、内部降级诊断和机器可判定生产门禁；随后完成
1D-7 永久静态门禁、I01～I07 证据、遗留引用清单和完整离线关闭验收。

当前边界：

- `domain/`：不可变身份/字段/文档/结果/回调 DTO，专用 Retrieval Query、Evidence Selection、
  来源映射、Evidence 完整正文规则、INPUT/TABLE Extraction Prompt、文档范围和 execution 输入快照，
  以及 TABLE 解析/合并/组装纯规则；
- `application/`：已实现 `SubmitWeaponryTask`、`RunWeaponryTask(task_id)`、字段级执行器和稳定
  错误分类；只依赖 Domain、Ports 与通用 Task 控制面；
- `ports/`：Document Scope、Retrieval、Extraction、Auxiliary Guidance、Translation、Audit、
  Callback、Resource、Creation Intent 与 Dispatcher 契约均已完成；Candidate/Selected Evidence/Prompt 在类型层
  不可互换，外部调用必须使用稳定 call/attempt 身份；
- `adapters/`：已实现 DatabaseService/SQLite 文档范围、唯一 Schema v2 Codec、任务级
  AnythingLLM Retrieval、Provided-Evidence Extraction、NoAuxiliary/Terms、Translation、SQLite
  Interaction Audit/Resource/Creation Intent Store、严格运行配置、本地 Dispatcher、SQLite Callback Guard/
  Recovery Source、AnythingLLM 单项幂等清理、Creation Intent 崩溃恢复及 production attestation
  校验；
- `composition.py`：显式注入所有 Port、固定策略、Adapter 能力、共享 limiter 和进程锁；构造
  不启动线程，并校验 Submit/Run/Dispatcher/Callback/TaskCommand 必须属于同一实例链；
- `app/adapters/web/flask/weaponry_requests.py` 与 `app/presenters/weaponry_submission.py`：
  已绑定公开路由的 Parser/Presenter；成功响应为 202 严格零字节；
- 新模块不得导入 Flask、SQLite、HTTP Client、环境变量或遗留 Service；
- 旧模式 1 已从执行链和旧 Prompt 工具中删除；迁移期配置值 `1` 会在外部副作用前明确拒绝；
- 当前开发分支已接管 `/llm/weaponry` 源码运行绑定；阶段 1D-7 的最终静态证据、完整离线关闭
  和遗留引用清单已经完成。有效 production attestation 尚未在真实受控环境生成，readiness 必须
  保持 false，代码也尚未部署生产环境。

2026-07-29 起，weaponry 类型 `/llm/check-task` 在既有 `pending/failed` 恢复之外，允许新的
请求显式授权一次 `outcome_unknown` 的 at-least-once 补发。路由在任何回调副作用前完整校验
全部 `params`，并按规范化 `architectureId` 稳定去重；同一请求的每个唯一键最多竞争一次，
同一 attempt 的并发请求仍由 SQLite CAS/lease/fencing 保证单 owner。正常 Worker 与维护线程
不会自动重发 unknown，维护线程只冻结过期 Guard；接收端必须按规范化 `architectureId` 和
业务结果幂等。该改造没有增加任何公开字段，也没有改变 200/400/404/409/202 契约。

2026-08-02 起，Weaponry Worker 在业务终态提交、资源收口状态可由持久事实恢复后，只向独立
资源维护线程发送一个可合并的常量空间唤醒提示；正常路径已提交 `cleanup_pending`，清理意图
提交异常则由终态 execution 与 `tracking` 记录保守收敛。业务终态与 Callback 均不等待远端
DELETE。资源恢复批次按“逐项资源恢复尝试数”而不是“唯一任务数”计量，并在任务之间轮转
推进，避免一个含数百个
extraction workspace/thread 的任务每个固定周期只清理一项。批次有可证明进展且仍可能有积压时
会在批次边界继续唤醒，进程停止信号则在两项远端副作用之间生效；定时扫描和启动扫描仍是
提示丢失、进程重启及历史积压的持久兜底。所有权不明、pending Interaction Audit、DELETE 结果
未知和检查点不可靠的资源仍只隔离，禁止按名称前缀或记录年龄盲删。公开接口与数据库 Schema
均未改变。

1D-3B 的“生产 Adapter”表示可以在组合根注入的真实基础设施实现；1D-6 已完成源码装配，但不
表示已经部署或完成真实模型/回调端联调。当前 1D-6 自动测试仍使用受控 Fake Transport、Mock HTTP
和临时 SQLite，不修改现有 AnythingLLM workspace/文档。1D-5 的 50 个 SQLite accepted 任务只形成持久行、一条
执行 Worker 和零内存队列项；它证明单实例调度形状，不代表 50 个重模型任务并行。50+ 实际
容量仍由首次完整集成环境和阶段 10 验收。
