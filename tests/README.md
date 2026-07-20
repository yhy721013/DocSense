# 测试目录说明

本目录保存 DocSense 的离线单元测试和集成边界测试。文件对话改造相关测试均应使用临时 SQLite、替身端口和模拟传输，不依赖 AnythingLLM、模型服务、回调服务或 `run.py`。

## 文件对话测试文件

| 文件 | 覆盖内容 |
| --- | --- |
| `test_chat.py` | `/llm/chat` 路由受理、既有 SSE 事件、响应头、输入校验、并发拒绝和内部标识不泄露。 |
| `test_chat_port_contract.py` | `ChatConversationPort` 数据传输对象、异常与协议边界。 |
| `test_anythingllm_chat_gateway.py` | AnythingLLM 对话网关的离线传输、字段归一化、流关闭和删除行为。 |
| `test_chat_repositories.py` | SQLite 架构迁移、会话/运行/消息/文档绑定/清理任务的约束与幂等性。 |
| `test_chat_resource_ids.py` | 租约标识和不透明远端引用的编码/解码。 |
| `test_chat_run_state.py` | 同一会话互斥、执行租约、心跳、过期运行和删除准入。 |
| `test_chat_run_executor.py` | 输入冻结、运行领取、资源租约、文档选择、流事件记录和异常收敛。 |
| `test_chat_event_repository.py` | 内部事件账本的序号、终态唯一性和事务写入。 |
| `test_chat_dispatcher.py` | 仅以持久化 `run_id` 调度执行的协议和内联实现。 |
| `test_chat_history_service.py` | 本地权威历史、消息过滤和标题输入。 |
| `test_chat_title_service.py` | 标题生成、临时线程租约、清理失败与删除竞争。 |
| `test_chat_abort_service.py` | 活动运行中断、中断通知和终态语义。 |
| `test_chat_delete_service.py` | 删除状态机、同步清理要求和失败保留。 |
| `test_chat_stream_presenter.py` | 领域事件到冻结 SSE 文本的格式化与关闭回调。 |
| `test_chat_infrastructure.py` | 当前持久化/调度/租约能力边界，防止 SQLite 被误用为可靠队列。 |
| `test_chat_debug_preview.py`、`test_chat_debug_routes.py` | 本地调试预览和调试路由。 |
| `test_dependency_container.py` | 容器装配、工厂隔离、传输惰性创建和能力校验。 |

`fakes/` 子目录保存端口替身，具体见其 README。

## 阶段 0 离线资产

| 文件 | 覆盖内容 |
| --- | --- |
| `offline_application.py` | 为路由测试装配临时 SQLite 和 Fake AnythingLLM，避免 `create_app()` 隐式构造生产依赖。 |
| `contracts/stage0_contracts.json` | 已批准目标契约及其实施状态，以及继续冻结的 Callback/SSE 黄金样例；Progress 目标已标记为 1B-2 implemented，check-task 仍待阶段 6。 |
| `test_stage0_contract_assets.py` | 校验空成功响应、report 409、严格 params 元素校验、显式 action 错误后保持连接且无 ack、回调与 SSE 黄金样例。 |
| `test_stage0_baseline_tools.py` | 校验容量工具的回环地址、重型场景、有界 Future 窗口、成功/失败延迟拆分、WebSocket 持续存活探测与配置门禁，不发出网络请求。 |
| `test_stage0_sqlite_inventory.py` | 验证 SQLite 盘点器使用显式只读事务，识别 WAL/SHM，区分数据版本与物理文件变化且不输出业务正文。 |

## 阶段 1A-1 契约资产

| 文件 | 覆盖内容 |
| --- | --- |
| `test_stage1a1_check_task_contract.py` | `/llm/check-task` 当前 400/404/成功 JSON 迁移基线、批量顺序、缺失策略、回调恢复，以及波次 1B 的 HTTP 200 空响应与严格 params 元素目标。 |
| `test_stage1a1_progress_contract.py` | `/llm/progress` 阶段 1B-2 切换后的无 action 契约、严格整条消息校验、无 ack、批量顺序/重复位置、Hub 回退、实时通知单写入、多连接隔离、发送失败和断连清理。 |

check-task 文件仍保留切换前的成功 JSON/同步恢复基线；Progress 文件已经切换为批准后的
当前行为。目标与当前状态以 `contracts/stage0_contracts.json` 区分，对外字段仍以
`docs/接口文档/` 为准。

## 阶段 1A-2 架构资产

| 文件 | 覆盖内容 |
| --- | --- |
| `architecture/import_rules.py` | 使用 AST 解析绝对/相对导入，不加载生产模块；输出包含规则、文件、行号、目标与原因的稳定违规信息。 |
| `test_architecture_boundaries.py` | 校验 tasks/Flask Adapter 包骨架，扫描当前 domain/application/ports/tasks/presenters，用临时违规源码自证每条规则能够失败，并禁止路由测试裸调用生产 `create_app()`。 |

当前门禁采用正向白名单并包括：

- module domain 只能依赖批准的标准库和本模块 domain；
- module application 只能依赖批准的标准库及本模块 domain/ports/application；
- module ports 只能依赖批准的标准库及本模块 domain/ports；
- tasks 不得读取 chat persistence 或任何其他业务模块分层；
- presenters 只能依赖批准的标准库、领域类型或框架无关应用结果；
- 受保护分层和 tasks 模块均禁止 `__import__`/`importlib.import_module` 动态绕过，未列出的 httpx/redis/pika/minio/boto3 等客户端默认失败。

## 阶段 1A-3 内部契约资产

| 文件 | 覆盖内容 |
| --- | --- |
| `fakes/tasks.py` | Task Read、Callback Recovery、Progress Snapshot/Subscription 的可编程内存替身和调用记录。 |
| `test_task_check_application.py` | 不可变 Task DTO、有序批量缺失、回调恢复、同 TaskId 无条件重读、持久化状态一致性、skipped 收敛、异常传播和端口返回值门禁。 |
| `test_task_progress_application.py` | Progress/Task/missing 快照选择、初始顺序屏障、有界慢连接缓冲、连接归属、重复 key 去重订阅、可重试补偿/释放和类型化通知。 |

这两组测试不创建 Flask 应用、SQLite、Hub、WebSocket、网络 Session 或后台服务；
它们证明应用服务只依赖 domain/ports，并冻结连接缓冲的线程安全边界。生产路由、旧
Task Service 和旧 Progress Hub 在阶段 1A 均未切换。

## 阶段 1B-1 可靠恢复命令资产

| 文件 | 覆盖内容 |
| --- | --- |
| `test_task_callback_recovery_application.py` | 最小命令信封、四类 outcome、单次批量端口调用、重复活动命令 ID 复用、事务失败整批回滚、Task Read/Command 返回值门禁、追踪字段和旧请求 DTO 兼容别名。 |
| `test_task_status_presenter.py` | 单项/批量 200 零字节体、批量缺失空成功、单项 404、既有 400 `error` JSON，以及内部 TaskId/recovery request ID 不泄露。 |

这 20 项测试只使用 Fake 和框架无关 Presenter；其中 1 项以 50 个线程验证 Fake 所表达
的活动命令唯一/复用契约。测试不创建 MySQL、Outbox、RabbitMQ、Worker
或 Flask Response。它们证明未来可靠登记链路的形状，不代表生产 `/llm/check-task`
已异步化；旧 Blueprint 和同步回调恢复仍保留到阶段 6。

## 阶段 1B-2 Progress 迁移资产

| 文件 | 覆盖内容 |
| --- | --- |
| `test_progress_request_adapter.py` | 无 action 请求解析、reportId 整数/整数字符串及 128 位边界规范化、显式 action 拒绝，以及混合/非法 params 整条消息失败。 |
| `test_task_progress_presenter.py` | file/report/weaponry 字段类型、缺失快照、错误结构、严格 JSON 及内部字段不泄露。 |
| `test_legacy_task_read_adapter.py` | 遗留 Task Service 到不可变 Task DTO 的只读转换、顺序/缺失保留和 execution ID 读取。 |
| `test_in_memory_progress_adapter.py` | 50 个 Barrier 同步线程并发订阅/发布、同键多订阅者、锁外通知、异常隔离、发布/释放竞争、深拷贝隔离、TaskId/序号，以及慢持久化 Guard 不阻塞其他业务键。 |
| `test_progress_connection_registry.py` | 连接归属、先登记后发送、幂等释放、失败令牌保留和有限重试。 |

`test_task_progress_application.py` 另覆盖初始快照屏障期间的新旧 TaskId 交错、连接已
接受 sequence 水位，以及通知出队后才完成的旧回调：屏障前旧执行通知会被当前快照
淘汰，读取当前快照后到达的新执行通知必须保留，同 TaskId 的迟到旧序号不得使进度
倒退。上述测试只证明单实例内存并发边界，不代替 50 条真实 WebSocket、Redis 跨实例
和稳态容量测试。

## 阶段 1C 报告资产

| 文件 | 覆盖内容 |
| --- | --- |
| `test_report_request_adapter.py` | 无 Flask 的报告 HTTP 入站解析：顶层对象、严格 params、filePathList 元素索引错误、reportId 128 位边界、可选文本兼容转换、深复制隔离及既有 400 文本。 |
| `test_report_contract.py` | 1C-6/1C-7 报告路由契约：202 严格空体、活动/回调 Guard 409、三类 400、单次持久化受理与唤醒、报告路由不再调用遗留 Worker，以及生产源码不得重新导入遗留报告执行链；旧覆盖缺陷只保留为隔离的兼容证据。 |
| `test_report_domain.py` | 不受 32/64 位限制且最多 128 位的 `ReportId`、不可变 submission/input/result/callback DTO、输入顺序与重复语义、HTML/空 RAG 成功、稳定错误和执行级名称。 |
| `test_report_service.py` | 遗留服务对新纯规则的兼容转发，以及 Progress、MHTML、Word、空 RAG 日志和成功/失败路径。 |
| `test_report_ports.py` | Task Command/Progress 与 File/Artifact/RAG/Audit/Callback/Dispatcher Port DTO、运行期 Protocol、身份/Artifact 类别隔离和严格序列约束。 |
| `test_report_application.py` | Submit/Run 无框架编排、全局调用顺序、失败注入、stale、审计硬门禁、任务事实写入结果不确定、回调故障不二次写终态、清理和非法端口返回值。 |
| `test_report_task_adapter.py` | SQLite Schema、追加 execution、report Codec、原子受理/领取、expected TaskId 条件写、触发器回滚、旧写拒绝，以及 50 同键/领取/不同键 Barrier 并发。 |
| `test_report_callback_guard.py` | callback acquire 的事务内 latest 复核、50 并发唯一发送权、租约过期 unknown 冻结、明确终局不自动重试、HTTP outcome 分类、fencing CAS、50 并发人工解除及追加式解除审计。 |
| `test_report_callback_recovery.py` | 甲方 check-task 报告同步恢复与正常 Worker 共用 Guard、50 并发唯一外发、明确失败显式重试及新任务提交后旧候选 stale。 |
| `test_report_io_adapters.py` | 50 个并发任务目录隔离、scratch/final 生命周期、伪造引用拒绝，以及下载/规范化/Word 遗留工具输出重新收口到任务命名空间。 |
| `test_report_rag_adapter.py` | 多文档保序上传/绑定/查询、任务级 Client 租约、来源验证、完整 trace/call/lifecycle、部分失败、空结果和清理失败证据。 |
| `test_report_interaction_audit_adapter.py` | 审计 Schema v3 的 trace/call 无损持久化、主记录/attempt/lifecycle 原子提交、cleanup 幂等追加及旧 execution 拒绝。 |
| `test_report_runtime_adapters.py` | 真实 SQLite Task/File/Artifact/RAG/Audit Adapter 的离线组合；审计事务故障禁止成功终态/callback 并保留本地与外部现场。 |
| `test_report_resource_recovery.py` | 终态权威 final 所有权、50 线程 CAS、审计事件精确重放、逐步骤心跳/检查点、过期幂等恢复、到期边界续心竞态、有界扫描、坏首页持久冷却、同任务 Artifact 副作用串行、待清理重试及 stale final 删除。 |
| `test_report_dispatcher.py` | 50 个 SQLite accepted 任务使用一条执行 Worker、零内存积压项、事务序号 FIFO、领取前毒任务冷却、无唤醒重启恢复、独立资源/诊断线程、共享许可停机取消、running 只观测、关闭超时真实性，以及同进程和真实子进程文件锁。 |
| `test_report_submission_presenter.py` | 报告成功 202 零字节、400/409 精确 JSON、Content-Type 语义和内部 TaskId/通知结果不泄漏。 |
| `contracts/stage0_contracts.json` | `reportGenerationBaseline.current/target` 双基线、固定 400 文本、Progress 值、成功/失败/空 RAG 回调及目标 202 空体/409。 |

1C-2 已证明 Application 可以只按 `TaskId` 编排抽象 Port，并且不需要 Flask context、真实
后台服务或 callback。1C-3 已证明兼容 SQLite Adapter 的追加 execution、内部原子冲突
分类、领取、expected TaskId 条件写和事务回滚。1C-4 已证明任务级 File/Artifact、多文档
AnythingLLM RAG 与原子 Audit Adapter 可以在离线组合中驱动 `RunReportTask(task_id)`，且
50 个并发任务的目录互不串扰。1C-5 已证明 Callback Guard 人工解除审计、终态权威
Artifact 所有权和 cleanup/quarantine 持久恢复闭环。1C-6 又证明当前开发分支的
`/llm/generate-report` 已切换 202/409 薄路由，SQLite accepted 积压可由一条执行 Worker
持续排空，资源恢复/队列诊断独立运行，毒任务和坏资源不会长期占住扫描首页；等待许可可
停机取消，关闭状态不再早于线程退出，操作系统文件锁拒绝第二进程。持久化 latest owner
复核与 Hub 写入在同业务键原子区间完成，不阻塞其他业务键。1C-7 以 358 项合并定向、
74 个模块/872 项安全全仓回归关闭阶段；阶段关闭后全面审查进一步以 75 个模块/889 项
安全全仓回归验证同步恢复、Guard 维护、
下载/审计/资源/Dispatcher 补强，并继续保留生产代码不得重新导入遗留报告 Worker 的
永久 AST 门禁。上述测试不代表代码已部署生产，也不代表 RabbitMQ 可靠队列、多实例或
阶段 10 的真实 50+ 容量验收已经完成。

## 阶段 1D-0～1D-7 武器谱契约、领域、任务输入、I/O Adapter、Application 与关闭验收资产

| 文件 | 覆盖内容 |
| --- | --- |
| `contracts/stage1d_weaponry_contracts.json` | D01～D05、202/400/404/409、architectureId 规范化、严格字段/TABLE 错误、仅模式 2、INPUT/TABLE 回调、文档范围、术语双路径、fieldDescription 双阶段、A/B/Thread/上下文隔离、供应商能力、故障矩阵、离线 Selection 夹具及脱敏真实校准结果。 |
| `test_stage1d_weaponry_contract_assets.py` | 原 1D-0 严格离线资产测试，并增加地面真值纠正断言；包含接口文档错误契约同步和测试侧 Evidence Selection Oracle。 |
| `contracts/stage1d0r_retrieval_quality.json` | 1D-0R 根因审计、MHTML 去噪、专用 Query、只读/隔离复校准、临时资源清理证据和生产停止门禁。 |
| `test_weaponry_retrieval_quality.py` | 专用 Query、无隐藏长度/最小语义词门禁、Chunk 质量门禁、score/rank 整批协议、稳定排序、来源身份、精确去重和完整 Evidence 保留纯规则测试。 |
| `test_stage1d0r_retrieval_quality_assets.py` | 严格 JSON、校准地面真值、工具只读/脱敏/fail-fast、候选 profile 和退出门禁测试。 |
| `test_stage1d0r_isolated_reindex.py` | 隔离重建工具的随机资源所有权、成功清理、嵌入失败补偿、路径越界、既有位置碰撞和源文件只读测试。 |
| `test_weaponry_domain.py` | ArchitectureId 纯规范化、深冻结字段/列/文档/结果 DTO、来源映射、Evidence 完整保留、仅 Selected Evidence Prompt、INPUT/TABLE 字段说明、TABLE 强行身份/合并/来源去重/组装及可变容器隔离。 |
| `test_weaponry_contract.py` | INPUT/TABLE/失败 Callback 黄金投影、单一文件聚合策略、模式 1 删除及遗留服务/Prompt 源码静态删除证据。 |
| `test_weaponry_service.py` | 遗留模式 2 对新 Domain 的兼容转发、专用 Retrieval Query、实际 Prompt rows、来源原名/哈希名、术语开关，以及显式模式 1 在检索/模型副作用前拒绝。 |
| `test_weaponry_request_adapter.py` | 1D-2 框架无关请求 Parser：D01～D03 精确错误、64 位 architectureId、URL 文件名、字段/TABLE 严格结构、未知键保留、深冻结和非标准 JSON 拒绝。 |
| `test_weaponry_submission_presenter.py` | 1D-2 Presenter：202 零字节成功，以及只含既有 `error` 字段的 400/404/409；内部 TaskId 和运行字段不泄露。 |
| `test_weaponry_document_scope.py` | explicit/category 文档范围、跨分类请求顺序、稳定类别排序、完整位置规范化碰撞、空类别、404/歧义/完整性、单次只读查询、旧选文表零写入和严格 Fake。 |
| `test_weaponry_task_adapter.py` | 唯一 Schema v2 严格 Codec、v1 明确拒绝、公开投影与 Worker 输入隔离、成功结果/终态完整性、原子受理/回滚、50 同键 1/49 冲突和 50 不同键全隔离。 |
| `test_weaponry_ports.py` | 1D-3A 稳定 call/attempt 身份、Retrieval/Extraction 类型隔离、Evidence/rows、辅助/翻译、审计计数和全部运行期 Protocol。 |
| `test_weaponry_strict_fakes.py` | 审计/资源登记前置、A/B 来源隔离、独立来源会话、明确失败/结果未知、shared 禁止清理、cleanup lease/fencing、Callback latest、术语关闭零 I/O、Dispatcher 生命周期和 50 线程 scope/session/call/resource 隔离。 |
| `test_weaponry_production_gate.py` | Schema v2 production attestation 的证据摘要、篡改/过期/指纹漂移、严格指纹类型、发布检查脚本、生产 fail-fast、容器 readiness 汇总和开发启动不伪装生产就绪。 |
| `test_weaponry_readiness_verifier.py` | 隔离临时 workspace/thread、真实响应形状、score/rank、URL/入库文件强身份、Provided-Evidence 空 workspace、基线恢复与证明可回读。 |
| `test_weaponry_operations_script.py` | 资源隔离人工处置、显式确认标志、脱敏查看和追加式审计；Callback 解除的底层 50 并发唯一写由 Report/Weaponry Guard 测试共同覆盖。 |
| `contracts/stage1d3b_weaponry_multi_document.json` | Schema v2 最小多文档结构黄金样例；只冻结来源/rows/跨文档隔离，不声明精度指标。 |
| `test_weaponry_production_adapters.py` | 1D-3B production profile、任务级 AnythingLLM Retrieval、Provided-Evidence Extraction、可拔除术语、Translation、SQLite Audit/Resource、Audit reserve 三态、409 unknown、终态 tracking 恢复、创建后登记补偿、同任务并发和 50 任务持久化隔离。全部通过 Fake Transport，不访问真实服务。 |
| `contracts/stage1d4_weaponry_application.json` | 1D-4 INPUT、TABLE 和失败 Callback 完整黄金载荷，并明确公开参数未改变、生产路由未切换。 |
| `test_weaponry_application.py` | 1D-4 原子 Submit、只按 TaskId 的 Run、字段/来源串行编排、精确“未找到”哨兵、零 Evidence、score/rank、Audit 三态防重放、单终态、Callback、清理意图先行、cleanup/quarantine、重复派发、50 个共享 Port 并发任务隔离，以及慢检索不持有 SQLite 写事务或全局任务锁；1D-5 又验证供应商容量/输入契约诊断可穿过字段降级路径但不会进入公开结果。 |
| `test_weaponry_dispatcher.py` | 1D-5 严格基础设施配置、实际能力指纹 fail-fast、固定运行策略、通用持久扫描 Dispatcher、稳定 FIFO、常量空间唤醒、毒任务冷却、共享 limiter、跨进程锁、三条隔离维护线程、运行中只观察、stop/close/readiness/fatal、供应商容量/业务零结果/输入契约分项指标，以及不启动线程的离线组合根和容器生命周期。 |
| `test_weaponry_stage1d7.py` | 1D-7 永久关闭门禁：生产代码不回流遗留 Worker，公开路由保持无线程薄适配，活动模式选择配置清零，固定策略兼容边界，Domain/Application 的 Terms 解耦，Query → Retrieval → Selection → Extraction 顺序，Provided-Evidence 禁止二次 RAG，以及遗留 Worker 测试引用白名单。 |
| `test_architecture_boundaries.py` | weaponry 四层包和 README 完整性、domain/application/ports 正向导入白名单；测试通过 AST 解析，不导入生产组合根。 |

1D-0 没有修改 `app/`；1D-0R 新增的生产源码尚未接管 `/llm/weaponry`。只读校准确认当前 LanceDB/native
`MintplexLabs/multilingual-e5-small` 分数方向稳定，但强相关与自然语言负例分数区间重叠，无法
冻结生产阈值。因此 JSON 中真实 profile 的 `minimumRelevanceScore` 必须保持 `null`；夹具
`0.82` 明确不是生产配置。1D-0R 已纠正误标的导弹负例、分离 Retrieval Query/Extraction Prompt、
增加 MHTML/Chunk 质量门禁。当时设计的“锚点或独立 reranker”后来已被批准的 Schema v2
score-or-rank 稳定选择取代，不再构成当前生产门禁。
获批的临时隔离清洗副本已真实嵌入并复校准，四次执行都恢复既有资源快照；真实
`passage: <document_metadata>` 包装也已从证据正文中移除。但原始 MHTML 已
不存在，当前语料只有一份业务文档，且生产入库/独立信号未完成。因此隔离结果不替代代表性
生产精度校准；该历史事实不能证明准确率，但也不再阻止 1D-3B Adapter 实现。真实供应商协议
与上下文边界仍在 1D-6 切流前验证。

1D-1 当时只完成纯领域迁移，没有接入 production profile 或切换公开路由。遗留模式 2
映射出的 Prompt Evidence 固定标记为 `legacy-mode2-unprofiled`，不能被解释为已通过生产
Selection。模式 1 执行、来源组装和旧 Prompt 已删除；显式配置值 `1` 会在外部副作用前失败。

1D-2 只实现并验证离线受理边界：Parser/Presenter 尚未绑定 Blueprint；Document Scope 在受理
事务前一次性解析并冻结，Codec 解码后 Worker 不再接触原始公开请求、文档 Repository 或环境
变量。其开发期 Schema v1 已在 1D-3B 按“无历史数据、无旧 Worker”口径直接删除。

1D-3A 只完成供应商无关 Port/DTO 和严格 Fake：检索只能返回 Candidate，抽取只能接收 Selected
Evidence；每次外部调用使用稳定 call_id 和独立 attempt，审计 reserve 必须先于调用。资源事实
区分 owned/shared，并使用 CAS、清理 lease 和 fencing token。20 项新增测试包含 50 线程结构隔离，
但没有创建真实 workspace/thread、调用模型或验证生产吞吐，因此不代表 1D-3B 已完成。本波次
1D 合并定向 190 项、动态安全全仓 87 个模块/1034 项测试均通过。

1D-3B 已实现唯一 Schema v2 production profile 和生产 I/O Adapter 代码。Retrieval 为每个
execution 使用独立 Transport/workspace，完整位置/供应商 ID 映射到 `document_key`；Extraction
为每个来源 attempt 创建空 workspace/thread，只发送已校验 Evidence/rows。SQLite Audit/Resource
使用短事务、CAS 与 lease/fencing，资源记录缺失会在外部创建前拒绝；创建成功但登记失败会补偿，
补偿无法确认时按 `outcome_unknown` 隔离。相关测试全部使用 Fake Transport 和临时数据库，未访问
真实 AnythingLLM、模型或 Callback，也没有切换公开路由。末次验收中，production Adapter 与
AnythingLLM Source 协议 33 项、weaponry 模块发现 146 项、stage1d 资产发现 40 项、架构门禁
17 项均通过；动态排除 4 个既有环境型模块后，88 个安全模块共 1050 项测试全部通过。

1D-4 已在上述边界上实现框架无关 Submit/Run Application。INPUT、TABLE 与失败 Callback 均按
完整黄金载荷逐字段比对；零 Selected Evidence 的 Extraction/Translation 调用数为零；每个来源
只接收当前文档最终 Selected Evidence。终态 CAS 失权、任务事实提交结果未知、Callback 投影/
发送异常、外部创建结果未知、审计完成失败、清理中断和崩溃遗留非空资源均有确定性测试。
严格 Task Fake 已与真实 Repository 的 active/terminal/stale 分类对齐。1D 合并定向 198 项、
排除会启动 `run.py` 的本地脚本、依赖 `.gitignore` 本地样例的资产测试和 Windows 不支持的单个
POSIX 权限位断言后，1086 项安全全仓回归全部通过。这里的 50 并发只证明单进程 Port/Application
状态隔离，不代表真实模型吞吐或生产容量。

2026-07-19 的 1D-4 全面审查补强后，`test_weaponry*.py` 183 项和 `test_stage1d*.py` 40 项通过；
安全全仓动态发现 1108 项，排除相同的 13 项环境型测试后，其余 1095 项全部通过。新增用例覆盖
超长 Query、单字/无辅助语义词、精确 INPUT 哨兵、TABLE 行保留、Audit pending/completed、
清理意图持久化失败、终态 tracking 恢复、AnythingLLM 409 unknown 和规范化文档位置碰撞。

1D-5 把 Report 已验证的单机持久扫描生命周期抽为业务无关内核，并让 Report 以薄包装继续复用；
Weaponry 新链完成严格配置、固定策略、单执行 Worker、独立资源/Callback 维护、共享 limiter、进程锁和
离线组合根，但生产 `ApplicationServices` 仍不构造该组合，公开路由也仍走遗留线程，留待 1D-6
一次性切换。最终验收中，`test_weaponry*.py` 203 项、`test_stage1d*.py` 40 项，以及架构、Report
Dispatcher 与容器组合 52 项均通过。安全全仓动态发现 1129 项，逐项排除 7 个会启动本地
`run.py`/Shell 的测试、5 个依赖 `.gitignore` 本地样例的资产测试，以及 Windows 无法表达的 1 个
POSIX `0640` 权限位断言后，其余 1116 项全部通过；未访问真实 AnythingLLM、模型或 Callback。

1D-6 已把 Weaponry Callback Guard、同步 check-task 恢复、资源有界恢复、生产组合根和公开薄路由
接入同一条实例链。`test_weaponry_stage1d6.py` 覆盖严格 2xx（禁止跟随 3xx）、timeout unknown、
人工解除、stale 零网络、50 并发同步恢复唯一发送、公开投影无损重建、资源清理冷却/隔离、生产组合
唯一性和路由静态边界；`test_routes.py` 又覆盖 50 个不同业务键全部 202，以及同一业务键恰好
1 个 202、49 个 409。最终 `test_weaponry*.py` 215 项、`test_stage1d*.py` 40 项、Report Callback/
Dispatcher/容器/架构关联回归 69 项均通过。全仓原始发现共 1143 项；逐项排除 7 个会启动本地
`run.py`/Shell 的环境测试、5 个依赖 `.gitignore` 本地样例的资产测试和 Windows 无法表达的 1 个
POSIX `0640` 权限位断言后，1130 项安全测试全部通过。当次真实 AnythingLLM 只读探测因
`localhost:3001` 拒绝连接且四类生产指纹未配置而未完成；该历史结论已由下述关闭后审查的
只读 8/8 结果更新，但完整 Provided-Evidence 证明仍待四类生产指纹。

1D-7 在 1D-6 运行链基础上增加永久 AST/配置门禁，并形成遗留 Worker 的生产、测试、配置引用
清单与 TermsRuleGuidance 未来删除清单。开发分支代码和离线关闭验收已经完成；当次
`localhost:3001` 不可达，且四类生产指纹与 production attestation 未配置，因此 readiness 保持
false。服务现已恢复但完整证明仍未生成；该事实不阻塞后续阶段代码开发，在真实供应商证明
生成并通过机器校验前不得发布为
production ready，也不得把离线测试解释为真实容量结论。末次验收中，8 项 1D-7 永久门禁、
232 项 `test_weaponry*.py`、40 项 `test_stage1d*.py` 和 131 项关联回归通过；安全全仓动态发现
1160 项，逐项排除下述 13 项环境/平台测试后，其余 1147 项全部通过，0 失败、0 错误、0 跳过。

2026-07-20 的关闭后全面审查又修复真实 Source URL 身份、共享重型许可 FIFO、公信力不足的
production attestation v1 和人工处置缺口。localhost 只读探测已确认 8 条 Candidate 全部携带
合法 score，并能通过结构化 URL 末段与唯一冻结 `ingested_file_name` 解析为 8/8；完整临时资源
证明仍等待四类能力指纹冻结。新增后 `test_weaponry*.py` 251 项、`test_stage1d*.py` 40 项通过；
全仓动态发现 1181 项，排除同一 13 项后 1168 项分成两个等量批次全部通过。

## 推荐验证流程

1. 先运行文件对话范围测试：`venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_chat*.py" -q`。
2. 再运行网关与容器边界测试：`venv\Scripts\python.exe -B -m unittest tests.test_anythingllm_chat_gateway tests.test_dependency_container -q`。
3. 运行阶段 0 资产测试：`venv\Scripts\python.exe -B -m unittest tests.test_stage0_contract_assets tests.test_stage0_baseline_tools tests.test_stage0_sqlite_inventory -q`。
4. 运行阶段 1A-1 契约测试：`venv\Scripts\python.exe -B -m unittest tests.test_stage1a1_check_task_contract tests.test_stage1a1_progress_contract tests.test_stage0_contract_assets -q`。
5. 运行阶段 1A-2 架构测试：`venv\Scripts\python.exe -B -m unittest tests.test_architecture_boundaries -q`。
6. 运行阶段 1A-3 内部契约测试：`venv\Scripts\python.exe -B -m unittest tests.test_task_check_application tests.test_task_progress_application tests.test_architecture_boundaries -q`。
7. 运行阶段 1B-1 可靠命令与 Presenter 测试：`venv\Scripts\python.exe -B -m unittest tests.test_task_callback_recovery_application tests.test_task_status_presenter tests.test_architecture_boundaries -q`。
8. 运行阶段 1B-2 Progress 迁移测试：`venv\Scripts\python.exe -B -m unittest tests.test_progress_request_adapter tests.test_task_progress_presenter tests.test_legacy_task_read_adapter tests.test_in_memory_progress_adapter tests.test_progress_connection_registry tests.test_stage1a1_progress_contract tests.test_progress_and_check_task tests.test_task_progress_application tests.test_dependency_container tests.test_architecture_boundaries -q`。
9. 运行阶段 1C 报告测试：`venv\Scripts\python.exe -B -m unittest tests.test_report_task_adapter tests.test_report_ports tests.test_report_application tests.test_report_request_adapter tests.test_report_domain tests.test_report_contract tests.test_report_service tests.test_report_io_adapters tests.test_report_rag_adapter tests.test_report_interaction_audit_adapter tests.test_report_runtime_adapters tests.test_report_callback_guard tests.test_report_resource_recovery tests.test_report_dispatcher tests.test_report_submission_presenter tests.test_stage0_contract_assets tests.test_dependency_container tests.test_architecture_boundaries -q`。
10. 运行共享审计与 Knowledge Gateway 回归：`venv\Scripts\python.exe -B -m unittest tests.test_task_service tests.test_anythingllm_knowledge_gateway -q`。
11. 运行阶段 1D-0 契约资产：`venv\Scripts\python.exe -B -m unittest tests.test_stage1d_weaponry_contract_assets -q`。
12. 运行阶段 1D-0R 检索质量资产：`venv\Scripts\python.exe -B -m unittest tests.test_weaponry_retrieval_quality tests.test_stage1d0r_retrieval_quality_assets tests.test_stage1d0r_isolated_reindex tests.test_mhtml_normalizer tests.test_anythingllm_workspaces tests.test_anythingllm_client tests.test_architecture_boundaries -q`。
13. 运行阶段 1D-1 领域与兼容测试：`venv\Scripts\python.exe -B -m unittest tests.test_weaponry_domain tests.test_weaponry_contract tests.test_weaponry_retrieval_quality tests.test_stage1d0r_retrieval_quality_assets tests.test_weaponry_service tests.test_stage1d_weaponry_contract_assets tests.test_architecture_boundaries -q`。
14. 运行阶段 1D-2 请求、文档范围与任务 Codec 测试：`venv\Scripts\python.exe -B -m unittest tests.test_weaponry_request_adapter tests.test_weaponry_submission_presenter tests.test_weaponry_document_scope tests.test_weaponry_task_adapter tests.test_architecture_boundaries -q`。
15. 运行阶段 1D-3A Port 与严格 Fake：`venv\Scripts\python.exe -B -m unittest tests.test_weaponry_ports tests.test_weaponry_strict_fakes tests.test_architecture_boundaries -q`。
16. 运行阶段 1D-3B Schema v2 与生产 Adapter 离线测试：`venv\Scripts\python.exe -B -m unittest tests.test_weaponry_retrieval_quality tests.test_weaponry_task_adapter tests.test_weaponry_production_adapters tests.test_anythingllm_workspaces tests.test_architecture_boundaries -q`。
17. 运行阶段 1D-4 Application：`venv\Scripts\python.exe -B -m unittest tests.test_weaponry_application tests.test_weaponry_ports tests.test_weaponry_production_adapters tests.test_weaponry_task_adapter tests.test_architecture_boundaries -q`。
18. 运行阶段 1D-5 Dispatcher、配置与离线组合：`venv\Scripts\python.exe -B -m unittest tests.test_weaponry_dispatcher tests.test_dependency_container tests.test_report_dispatcher tests.test_architecture_boundaries -q`。
19. 运行阶段 1D-6 Callback、资源恢复、生产组合和公开路由：`venv\Scripts\python.exe -B -m unittest tests.test_weaponry_stage1d6 tests.test_routes tests.test_dependency_container tests.test_architecture_boundaries -q`。
20. 运行阶段 1D-7 永久关闭门禁：`venv\Scripts\python.exe -B -m unittest tests.test_weaponry_stage1d7 -q`。
21. 最后执行 `git diff --check`，并检查没有将内部运行标识暴露给目标响应。

## 执行限制

- 不要直接使用原始全量发现命令替代上述定向测试；当前原始发现会包含 7 个可能触发本地
  `run.py`/Shell 的环境测试、5 个依赖被 `.gitignore` 排除样例的资产测试和 1 个 Windows 不支持的
  POSIX 权限位断言。安全全仓口径必须逐项排除这 13 项并报告名称和理由，禁止笼统写成“全量通过”。
- 新文件对话测试必须显式构造临时目录和临时数据库，不能读取开发机 `.runtime` 数据。
- 新测试不得为了方便而放宽 `/llm/chat*` 既有请求字段或 SSE 协议断言。
- 架构测试必须静态解析源码，不能通过 import 生产组合根来收集依赖；规则调整应与模块边界设计一起评审。
