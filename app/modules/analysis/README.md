# Analysis 模块

该模块承载 `/llm/analysis` 文件分析链路的渐进式垂直切片。阶段 1F-1 已完成领域树、
有限候选召回、分类保护、结果映射、回调 DTO 与 Analysis Prompt 的无副作用迁移；阶段 1F-2
已补齐不可变任务输入、九类 Port、严格 Fake、fail-closed Codec 和 Web Adapter；阶段 1F-3
已实现只接收内部 `TaskId` 的运行用例，以及任务目录、RAG、知识库、审计与翻译的遗留适配器；阶段
1F-4 已补齐 SQLite 批量原子受理、批次/全局调度顺序和提交后有界唤醒的内部边界；阶段
1F-6 已补齐新 execution 的资源事实、清理意图/CAS/隔离、统一 Callback Guard 与同步恢复用例；1F-7A
已完成共享 Application 的 50 任务隔离验收、旧 Worker 静态隔离门禁和切换前只读预检工具；1F-5A
已完成严格运行配置、Analysis Dispatcher 包装、生产组合根与统一生命周期接线，以及同步 Callback
恢复的本进程活跃请求合并门禁；1F-5B 已将公开路由接入新 Submit/Recovery 用例；1F-7B 已完成代码/离线
关闭验收。

阶段 1G 已在现行运行路径、测试、配置和当前说明引用清零后，物理删除旧 Analysis 执行器及其兼容执行
API。公开路由、请求参数、响应/回调格式、状态码和既有公开任务投影 Schema 均未变更；历史文档中的
旧路径仅作为迁移证据保留，不得重新接回生产组合根。

当前边界：

- 永久知识入库根据数据库权威 `architecture_id` 使用共享领域规则生成
  `archId-{architecture_id}`；本模块不兼容或迁移旧 `architectureId-*` Workspace，供应商 Gateway
  仍只消费业务层传入的 `CollectionSpec`，不拥有业务命名规则；
- `domain/`：领域树校验与内存索引、文档信号召回、分类/范围保护、结果映射、Prompt、回调
  载荷和受理期不可变输入；不导入 Flask、SQLite、HTTP Client、文件解析库、旧 Service 或
  Integrations。仅允许为既有并发安全缓存使用标准库内存同步原语；
- `ports/`：批量命令、文件、RAG、知识、审计、翻译、资源、回调和 Dispatcher 的抽象 DTO/Protocol；
  RAG 生命周期、两阶段召回审计、完整交互轨迹、资源 state+version CAS 和 Callback Guard 均为显式
  内部契约；
- `application/`：`RunAnalysisTask` 只按 `TaskId` 领取冻结输入，使用 expected 条件写收敛进度和
  单终态；注入 Resource Port 时按“资源登记 → 引用即时 CAS → close 意图/running → close 结果 → 审计
  追加”收口，注入 Callback Port 时只在终态提交后取得 Guard 并至多发送一次；`SubmitAnalysisBatch` 只执行
  一次批量 Command、复核 Port 回显的请求内顺序，并在成功提交后最多发送一次 Dispatcher 唤醒；
- `adapters/`：已包含严格任务输入 Codec、任务目录/文件准备、任务级 RAG Factory、知识库、审计、
  注入式翻译串行适配器、SQLite Resource Store、Callback Guard 与 Callback 恢复源。新 Adapter 只处理带
  `batch_id`/`batch_sequence` 的 execution，不维护内存队列；资源恢复只补可证明幂等的审计追加，绝不自动
  重放 RAG close/delete；毒化资源记录通过不解析 payload 的控制面 CAS 隔离，且
  `cleaned/quarantined` 不可复活；1F-5A 已将它们装配进生产组合根的内部 Dispatcher，1F-5B 再将公开
  路由接到新提交用例；
- `composition.py`：唯一的 Analysis 组合入口。它复用容器的 `task_service`、latest-wins Progress
  Publisher、共享 `UploadTaskLimiter`、Document RAG/Knowledge Factory 与 Callback Guard 基础设施；
  不创建 Flask、网络连接或后台线程，线程只由容器生命周期显式启动；
- `app/adapters/web/flask/analysis_*.py`：Parser/Presenter 已接入当前 Blueprint；它们只处理 HTTP 边界，
  不创建线程、不直接受理任务，也不访问 RAG、知识库或 Progress Hub。

阶段 1F-5A 已把组件接入 `ApplicationServices` 的启动、readiness、停止和失败逆序回滚；Dispatcher 使用
独立 `analysis-dispatcher.lock`，只扫描带 `batch_id`/`batch_sequence` 的新 file execution，绝不领取旧
兼容链任务。调度领取前的基础设施错误在 SQLite 中以 `dispatch_failure_count/next_dispatch_at` 做有界
指数退避；确定性损坏快照收敛为稳定失败，资源/Callback 维护错误仅记录日志并保留下一轮扫描机会。显式
Fake 不会偷偷启动生产线程。同一容器内同一 file 的同步 Callback 恢复在 owner 活跃时只允许一次执行，
活跃键在 `finally` 释放，后续独立恢复仍可按既有显式重试语义发起；SQLite Callback Guard 仍是跨进程
发送权的唯一权威。阶段 1F-5B 后，file `/llm/analysis` 与 `/llm/check-task` 分别只调用新
Submit/Recovery 用例，不会回退到旧执行器；提交后唤醒失败只保留错误日志和持久化 accepted execution，
等待 Dispatcher 的扫描恢复发现；这不是可靠队列已经完成的声明。当前运行时的任务执行边界与 SQLite
`single_instance` 限制保持不变；翻译串行锁只防止单进程共享对象交错。后续迁移必须以任务隔离、可恢复
队列、数据库一致性和多实例可判定性为前提，不能把基础设施依赖反向带回 Domain。真实发布前必须使用
`scripts/inspect_analysis_cutover.py` 对经确认的目标库进行只读预检；旧活跃任务、sending/unknown
Callback Guard、开放旧 RAG 租约、历史终态待恢复回调或新 `running` execution 任一非零都必须
停止切换；新 `accepted` execution 只做显式观测，禁止自动清理或并行双跑。
