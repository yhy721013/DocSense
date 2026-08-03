# 测试目录说明

本目录保存 DocSense 的离线单元测试和集成边界测试。文件对话改造相关测试均应使用临时 SQLite、替身端口和模拟传输，不依赖 AnythingLLM、模型服务、回调服务或 `run.py`。

## 文件对话测试文件

| 文件 | 覆盖内容 |
| --- | --- |
| `test_chat.py` | `/llm/chat` 路由受理、Requested/Active/Effective 范围矩阵、history requested 投影、既有 SSE 事件、响应头、输入校验、同会话并发拒绝、50 个不同会话完整隔离和内部标识不泄露。 |
| `test_chat_port_contract.py` | `ChatConversationPort` 数据传输对象、异常与协议边界。 |
| `test_anythingllm_chat_gateway.py` | AnythingLLM 对话网关的精确名称创建、同名未知资源失败关闭、供应商名称漂移补偿、补偿失败资源证据、日志、流关闭和删除行为。 |
| `test_chat_workspace_naming.py` | File/Weaponry Workspace 业务身份命名、边界整数、缺失字段和未知身份失败关闭。 |
| `test_knowledge_workspace_naming.py` | 永久知识谱系 Workspace 的 `archId-{id}` 精确命名、有符号 64 位边界、非法输入失败关闭，以及 50 个并发分类 ID 的唯一性。 |
| `test_chat_repositories.py` | SQLite 架构迁移、统一有界 busy timeout、会话/运行/消息/文档绑定/清理任务的约束与幂等性。 |
| `test_chat_resource_ids.py` | 租约标识和不透明远端引用的编码/解码。 |
| `test_chat_run_state.py` | 同一会话互斥、Scope Head 原子受理、首次/显式 50 并发唯一更新、全事实回滚、1,000 文件历史压力、执行租约、心跳、过期运行和删除准入。 |
| `test_chat_run_executor.py` | Requested/Effective 输入冻结与 run-id 重启式恢复、从持久化 Identity Binding 生成 Workspace 名称、旧引用零创建、Workspace 累计绑定和模型范围隔离、Weaponry 来源 Metadata 提交前清洗、资源租约、流事件记录及异常收敛。 |
| `test_chat_event_repository.py` | 内部事件账本的序号、终态唯一性和事务写入。 |
| `test_chat_dispatcher.py` | 仅以持久化 `run_id` 调度执行的协议和内联实现。 |
| `test_chat_history_service.py` | 本地权威历史、消息过滤和标题输入。 |
| `test_chat_title_service.py` | 标题生成、临时线程租约、清理失败与删除竞争。 |
| `test_chat_abort_service.py` | 活动运行中断、中断通知和终态语义。 |
| `test_chat_delete_service.py` | 删除状态机、同步清理要求、失败保留，以及 Weaponry 清理失败时旧世代身份继续占用。 |
| `test_weaponry_chat_routes.py` | 知识谱系公开路由、SSE/History 共用已清洗来源快照、删除后同业务身份新世代/新范围快照、相同 Workspace 名称与不同内部 Thread，以及日志正文与 Metadata 防泄漏。 |
| `test_chat_stream_presenter.py` | 领域事件到冻结 SSE 文本的格式化与关闭回调。 |
| `test_chat_infrastructure.py` | 当前持久化/调度/租约能力边界，防止 SQLite 被误用为可靠队列。 |
| `test_debug_application.py`、`test_debug_adapters.py`、`test_debug_presenter.py`、`test_chat_debug_routes.py` | 本地调试 `fileNames` 对齐 Active Scope、Workspace bindings 独立脱敏计数、公开 history requested 语义，以及 Query/Adapter/Presenter/路由分层。 |
| `test_dependency_container.py` | 容器装配、工厂隔离、传输惰性创建和能力校验。 |
| `test_chat_scope_contract_assets.py` | Requested/Active/Effective Scope 分离的阶段 0 黄金资产；冻结最后显式范围、禁止自动吸收、history 仅展示显式请求及公开字段零增删。 |
| `test_chat_document_scope.py` | Scope Revision/Head/Decision 不可变 DTO、严格内部 Schema、重复身份拒绝及 Requested→Active→Effective 纯状态机。 |
| `test_chat_scope_repositories.py` | SQLite Schema v4、Scope Revision/Member/Head、CAS、run/chat 完整性、requested/effective run input Codec、源 run 清理隔离及 50 会话持久化隔离。 |

`fakes/` 子目录保存端口替身，具体见其 README。

## 阶段 0 离线资产

| 文件 | 覆盖内容 |
| --- | --- |
| `offline_application.py` | 为路由测试装配临时 SQLite 和 Fake AnythingLLM，避免 `create_app()` 隐式构造生产依赖。 |
| `contracts/stage0_contracts.json` | 已批准且已在当前集成分支实现的任务受理/check-task/Progress 契约，以及继续冻结的 Callback/SSE 黄金样例。 |
| `test_stage0_contract_assets.py` | 校验空成功响应、report 409、严格 params 元素校验、显式 action 错误后保持连接且无 ack、回调与 SSE 黄金样例。 |
| `test_stage0_baseline_tools.py` | 校验容量工具的回环地址、重型场景、有界 Future 窗口、成功/失败延迟拆分、WebSocket 持续存活探测与配置门禁，不发出网络请求。 |
| `test_stage0_sqlite_inventory.py` | 验证 SQLite 盘点器使用显式只读事务，识别 WAL/SHM，区分数据版本与物理文件变化且不输出业务正文。 |

## 阶段 1A-1 契约资产

| 文件 | 覆盖内容 |
| --- | --- |
| `test_stage1a1_check_task_contract.py` | `/llm/check-task` 的 400/404、严格对象数组、三类业务键、HTTP 200 空成功体、批量缺失策略与同步回调恢复副作用。 |
| `test_stage1a1_progress_contract.py` | `/llm/progress` 阶段 1B-2 切换后的无 action 契约、严格整条消息校验、无 ack、批量顺序/重复位置、Hub 回退、实时通知单写入、多连接隔离、发送失败和断连清理。 |

check-task 与 Progress 文件均验证批准后的当前行为：成功体不泄露内部任务或回调细节，必要的
同步恢复副作用仍由持久化状态验证。目标与当前状态以 `contracts/stage0_contracts.json` 区分，
对外字段仍以 `docs/接口文档/` 为准。

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

## 阶段 1E-0 分类节点变更契约与故障资产

| 文件 | 覆盖内容 |
| --- | --- |
| `contracts/stage1e0_reassign_contracts.json` | 1E-0 历史遗留基线与 1E-6 已接线同步 Saga 的目标基线；400/500/200 黄金 JSON、原始 ID 比较、旧 ID 未包装转换失败、新 ID 冻结兼容白名单、空 `doc_path`、缺失旧 workspace 映射、`false`/缺 slug/CAS 0 行/补偿失败矩阵，以及仅供离线注入的有限预算。 |
| `test_stage1e0_reassign_contract_assets.py` | 显式注入离线容器，验证切换后的 Parser → Application → Presenter 路由字节级契约、内部字段隐藏和实际组合根 local-only 兼容路径；不启动 `run.py`、不连接真实 AnythingLLM。 |

1E-0 自身只建立后续重构的可执行验收边界，当时未创建 `app/modules/reassign/`、未切换 Flask 路由，也不修改
`docs/接口文档/分类节点变更.md`。1E-6 已将同一资产升级为切换后路由的黑盒回归；两个 `null` ID 的精确 400 文案仍被记录为代码观测，
但接口文档未逐字列出它们；因此资产明确标注其非权威状态，不能反向把代码细节升级为接口文档契约。
目标中的远端 `false`、缺 slug、CAS 0 行和补偿失败均要求既有 500 结构，但具体新增错误文案未被
本波次擅自冻结。

## 阶段 1E-1 分类节点变更领域模型与纯规则

| 文件 | 覆盖内容 |
| --- | --- |
| `test_reassign_domain.py` | 原始 ArchitectureId 深冻结、旧 ID 查询值保留、空 `doc_path` 兼容、Operation/Step/Result 不变量、全部普通合法/非法状态转换、受控恢复出边、64 组补偿事实决策、步骤幂等键与稳定错误类别。 |
| `test_architecture_boundaries.py` | `reassign` 四层目录必须同时具备包标识和职责 README，并静态验证 Domain/Application/Ports 的正向依赖白名单。 |

1E-1 只创建无 I/O 的领域层和分层边界：不定义 Repository/Knowledge Port、严格 Fake、SQLite
Operation/Step/Event 表或 AnythingLLM Adapter，不启动 `run.py`，也不切换 `/llm/reassign` 路由。
领域代码不把 `1`、`"1"` 或 `false` 重新解释成同一 ID；未来 Web Parser 仍须保持接口文档锁定的
原始值比较、旧 ID `int(...)` 时点，以及新 ID 已冻结的 `false`、整数、整数字符串兼容行为。

## 阶段 1E-1R 分类节点变更领域一致性修正

| 文件 | 新增门禁 |
| --- | --- |
| `test_reassign_domain.py` | 释放保护的终态证据、目标绑定与本地 CAS 一致性、本地-only路径、独立补偿 Step、已知失败受控重试、UTC lease、原始/查询 ID 一致性和诊断字段长度上限。 |
| `test_stage1e0_reassign_contract_assets.py` | 五类稳定失败 message、远端异常脱敏、正常远端迁移精确 200、JSON Content-Type、扩充后的 8 类目标故障和公开字段禁止清单。 |

1E-1R 经确认修改了 `docs/接口文档/分类节点变更.md` 中的稳定 `data.message` 对照表，但没有
增删接口参数、JSON 字段、状态码或同步语义。当前遗留路由的远端异常分支不再透传 `str(e)`；
在 1E-1R 完成时，其余同步 Saga、真实外部写、补偿恢复和路由切换仍等待 1E-3～1E-6。

## 阶段 1E-2 分类节点变更 Port、严格 Fake 与 SQLite 事实

| 文件 | 覆盖内容 |
| --- | --- |
| `test_reassign_ports.py` | 端口 DTO 不变量、运行时 Protocol、Port 依赖束，以及 SQLite Adapter 不导入网络/AnythingLLM Client 的静态门禁。 |
| `test_reassign_fake_repository.py` | Repository Fake 的重复 operation ID 拒绝、旧 ID 原始字符串到整数 source workspace 查询，以及 `"12"`/`false` 新 ID 的 SQLite 存储兼容。 |
| `test_reassign_strict_fakes.py` | 未声明调用、事务内外部调用、错误顺序、重复副作用，以及 workspace 创建结果未知后的盲重放 fail-fast。 |
| `test_reassign_sqlite_adapter.py` | 三表初始化、append-only 审计、50 同文档唯一 owner、50 不同文档保留、跨分类同名隔离、workspace 创建归属、lease/fencing、CAS 成功/冲突/回滚和原始新 ID 兼容。 |

1E-2 只新增尚未接线的内部 Port、Fake 与 SQLite Adapter。每个 UoW 使用独立短事务，Adapter 不创建
HTTP Client、不调用 Knowledge Port；测试全部使用临时数据库和离线替身，不启动 `run.py`、不连接
AnythingLLM 或其他后台服务。当前 `/llm/reassign` 仍是遗留同步路由，接口参数、JSON 字段、HTTP
状态码、同步语义和 `docs/接口文档/` 均未在本波次修改。

## 阶段 1E-2R 分类节点变更持久化一致性修正

1E-2R 在不接线生产路由的前提下补齐以下回归门禁：

- 恢复隔离必须由更大 fencing 的过期接管者解除；同一 lease/fencing 不能重试已知失败步骤；
- 终态 Operation 禁止续租或启动新步骤，通用状态转换禁止直接进入任一释放保护的终态；
- 本地成功提交必须验证目标准备、源解绑和目标挂载等必要事实；本地-only 文档不得伪造远端步骤；
- Step 状态与探测结论严格匹配，重试清除旧探测结果；SQLite 与 Fake 对重复 ID、原始目标值、
  事务上下文和失败类型保持一致；
- 恢复扫描使用只读 UoW、显式上限和稳定游标；审计保存 fencing、尝试次数、探测结论、
  脱敏操作者和原因码；
- 早期 1E-2 Schema 使用加列回填升级，活动文档部分唯一索引的谓词会被核验并按需重建；
  存在 `reassign_*` 事实时禁止 `DatabaseService` 单独重建 `documents` 并改变冻结行 ID；
- Knowledge Port 增加无副作用目标 workspace 查回，为 1E-3 的“调用完成但检查点未提交”窗口提供
  探测能力。

所有新增测试仍只使用临时 SQLite 与严格 Fake，不启动 `run.py`、不连接真实 AnythingLLM。
本波次没有修改 `docs/接口文档/`，也没有增删接口参数或响应字段。

## 阶段 1E-3 分类节点变更 AnythingLLM 适配与目标准备

| 文件 | 覆盖内容 |
| --- | --- |
| `test_reassign_anythingllm_adapter.py` | 内部预算拒绝与环境加载、单调 deadline 裁剪、请求级 Transport 正常/异常关闭、workspace 精确复用/创建/多重冲突/缺 slug/超时查回、完整 doc_path 成员探测、解绑/挂载写后探测、false、4xx、断连、协议异常、Pin best-effort 与 Adapter Factory 隔离。 |

测试只替换原子 Workspace Client 和 Transport，不访问真实 AnythingLLM。它确认每个原子调用使用独立
Transport，任何写后未知状态只查回而不盲目重发，并确认读到的 workspace 不能在缺少可验证创建归属时
标记为当前 Operation 的可删除资源。1E-3 尚未创建 Application Saga、写意图/步骤持久化接线、
本地 workspace 映射提交、Container 或公开路由；接口参数、JSON 字段、HTTP 状态码、同步语义和
docs/接口文档均未修改。

## 阶段 1E-4 分类节点变更 Application 成功路径

| 文件 | 覆盖内容 |
| --- | --- |
| `test_reassign_application.py` | `DocumentReassignmentService` 的远端完整成功、Factory 故障下 local-only、按既有 slug 复用、reserve 异常稳定收口、非法 Step DTO fail-closed、未知 prepare claim 保留、mapping 冲突恢复事实、Pin 审计、lease 预算门禁，以及真实 SQLite 组合提交。 |
| `test_reassign_sqlite_adapter.py` | 新 mapping preparation claim、同目标竞争/过期接管、步骤续租同步延长 claim，以及 mapping 冲突后的准确远端准备事实持久化。 |

1E-4/1E-4R 的 Application 只依赖 Domain、Port、显式执行设置和请求级 Knowledge Factory。测试验证所有
AnythingLLM 调用均发生在 UoW 外，mapping、prepare Step 与 claim 释放原子提交，成功结果不携带
Operation/lease/fencing；不确定目标准备保留 claim，mapping 写失败保留准确远端事实，步骤边界
续租同时延长活动 claim。该执行记录当时尚未在当前请求内补偿；1E-6 全面审查修正已对远端明确
失败和 CAS 冲突增加有界同步补偿，未知结果仍保持 `recovery_required` 且不盲重放。当前记录所述
波次当时未接入 Container、Flask 路由或真实 AnythingLLM；接口参数、JSON
字段、HTTP 状态码、同步语义和 `docs/接口文档/` 均未修改。

## 阶段 1E-5 分类节点变更补偿、恢复与诊断

| 文件 | 覆盖内容 |
| --- | --- |
| `test_reassign_recovery.py` | 过期 lease 接管、local-only 无网络恢复、前向未知写的探测与固定补偿顺序、目标解绑/来源恢复两个写后检查点、workspace 创建后 mapping 丢失、写意图但 HTTP 未执行、成功持久事实门禁、补偿失败隔离、同 Operation fencing 竞争，以及错误 workspace 引用、非法接管/续租 DTO 和数据库读取异常的 fail-closed 回归。 |
| `test_reassign_recovery_sqlite.py` | `reassign_recovery_observations` 追加事实、接管时 claim 转移、最新观测和 claim 原子释放、SQLite 本地状态探测、运行中 Operation 先进入 `compensating` 再补偿、恢复 prepare 事实的新 fencing 门禁，以及成功终态拒绝缺失前向事实。 |
| `test_reassign_diagnostic_script.py` | 默认 dry-run 不追加事件、不修改 Operation、不初始化缺失 Schema；`--apply` 在缺少 operation ID、预期 fencing、操作者、原因或 lease 参数时提前拒绝；已收口、未找到、未接管和仍待恢复具有稳定进程退出码。 |

恢复测试只使用严格 Fake 和临时 SQLite。严格 Fake 会拒绝 UoW 内的外部调用，因此同时证明探测、
补偿与写后复核均发生在短事务外。脚本默认不创建 HTTP Client、不读取运行配置，也不对数据库执行
DDL；真正恢复必须由人工显式指定单个 Operation。当前仍未启动 `run.py`、未连接真实 AnythingLLM、
未接入 Container 或 Flask 路由，接口参数、JSON 字段、HTTP 状态码、同步语义和
`docs/接口文档/` 均未修改。

1E-5R 进一步要求恢复 Reference Probe 的返回 slug 与持久化引用一致，并对接管、续租、
Operation 重读及 preparation claim 执行完整身份复核。读取异常不得伪装成 Operation 不存在；
诊断脚本只有三个已收口结果返回退出码 0。

## 阶段 1E-6/1E-7 分类节点变更组合根、公开路由与恢复实现下沉

| 文件 | 覆盖内容 |
| --- | --- |
| `test_stage1e0_reassign_contract_assets.py` | 真实 Flask 路由的 400/500/200 字节级黄金响应、原始 ID 兼容、稳定公开 message、内部字段禁止清单、蓝图薄路由，以及实际组合根下 `doc_path=""` 不创建远端 Port 的 local-only 回归。 |
| `test_reassign_recovery.py`、`test_reassign_recovery_sqlite.py` | 拆分前后的恢复状态机、固定补偿顺序、fencing、观测和 SQLite 事实仍由既有故障矩阵覆盖。 |
| `test_reassign_recovery_collaborators.py` | 不经恢复 Facade 直接验证 Observer 的续租/远端观察/观察事实、Checkpoint Reconciler 的探测事实写入、Compensator 的补偿阶段转换、Finalizer 的隔离收口。 |
| `test_architecture_boundaries.py` | 长期 AST 门禁：路由不得构造 AnythingLLM/SQLite/线程，Container 必须经唯一组合根接线且不得绕过 Application；四个恢复协作器必须直接调用最小 Port，禁止 callback-wrapper，并锁定 Facade 行数/圈复杂度基线。 |
| `test_reassign_application.py`、`test_reassign_recovery_collaborators.py` | 模拟“事务已经提交，但提交确认异常”的故障，验证 Application 与 Finalizer 会先重读终态并返回已经持久化的真实结果，避免错误补偿或覆盖终态。 |
| `test_reassign_anythingllm_adapter.py`、`test_reassign_sqlite_adapter.py` | 验证同步远端预算从 Application 命令入口累计扣减，以及领域文档保护状态与 SQLite 删除保护状态持续一致。 |

1E-6/1E-7 只使用离线容器、临时 SQLite 和严格 Fake，不启动 `run.py`、不连接真实 AnythingLLM。公开请求/响应
参数、JSON 字段、HTTP 状态码、Header 与同步语义均未改变；接口文档只同步非契约的实现状态。真实供应商
故障演练、预算校准和多实例容量验证仍是 production ready 前置条件。

阶段 1E 整体审查进一步冻结以下边界：远端调用总预算从 Application 命令入口开始计算，进入
Repository/UoW 前的等待会占用后续远端预算；lease 过期时间必须在取得写事务后计算；事务提交确认
丢失时必须先以持久化终态为准，只有无法证明预期终态时才进入补偿或隔离。当前 SQLite 仍无法中断
已经进入的本地锁等待，因此“同步硬截止”和数据库权威时间属于后续多实例阶段，不能由本阶段离线
测试宣称完成。

## 阶段 1F-0 文件分析契约、黄金与现状资产

| 文件 | 覆盖内容 |
| --- | --- |
| contracts/stage1f0_analysis_contracts.json | 1F-0 的接口文档摘要、Analysis 202/400/409/413/503/500 兼容矩阵、file check-task/Progress/callback、范围默认值、领域树、召回、Prompt、身份重选、字段映射、阶段顺序、副作用和遗留引用清单。 |
| test_analysis_contract_assets.py | 显式离线容器下的字节级空成功体、既有错误结构、SQLite 忙、未捕获 500、callback、Progress 和全部算法黄金；不启动 run.py，不连接真实 AnythingLLM、模型或回调服务。 |

1F-0 只建立后续垂直切片迁移的可执行基线，不创建 Analysis 新模块、不改生产路由、
任务 Schema、后台线程或任何公开接口。接口文档仍是唯一公开契约权威；资产 SHA-256
只用于让后续文档变更进入显式评审。

## 阶段 1F-1 Analysis Domain 与纯规则迁移

| 文件 | 覆盖内容 |
| --- | --- |
| test_analysis_domain.py | 静态验证 Analysis Domain 不依赖 Flask、SQLite、HTTP、文件解析器、旧服务或集成层；验证旧领域树/召回模块与新 Domain 是同一模块对象，并验证旧服务导出委托到 Domain。 |
| test_architecture_boundaries.py | 验证 Analysis 四层包骨架、README 与既有架构边界规则持续存在。 |
| test_analysis_contract_assets.py 及 test_analysis*.py | 继续验证 1F-0 公开契约、算法黄金及文件分析相关离线回归，确保纯规则迁移不改变既有输出。 |

1F-1 只迁移确定性规则和既有进程内只读缓存。旧 `analysis_service` 仍负责任务 I/O、资源、
回调和兼容导出；为保持历史可观测性，结果映射的既有诊断日志仍保留在该旧服务边界，Domain
本身不写日志、不访问外部资源。没有修改路由、任务 Schema、请求/响应字段、状态码、callback、
Progress 或接口文档。

## 阶段 1F-2 Analysis Port、任务 Codec 与初始未接线 Web Adapter

| 文件 | 覆盖内容 |
| --- | --- |
| test_analysis_task_adapter.py | `AnalysisSubmissionSnapshot`/`AnalysisTaskInputV1` 的深冻结、原始文件名语义、策略快照、严格 V1 Codec、重复 JSON 键/损坏范围/未知 schema/身份拒绝及 50 个并发输入隔离。 |
| test_analysis_ports.py、tests/fakes/analysis.py | 九类 Analysis Port 的显式 RAG 生命周期、两阶段/追加审计、Resource CAS、Callback Guard、运行时 Protocol、按 execution 并发脚本和严格身份关联。 |
| test_analysis_web_adapters.py | Parser/Presenter 对 1F-0 400/413/202/409/503 黄金映射、内部身份不外泄、冻结快照复用、意外异常不误报 400，以及 1F-5B 当前 Blueprint 的唯一新链路 AST 门禁。 |
| test_analysis_translation_isolation.py | 移除全局任务 callback、全文翻译临界区、注入式协调器串行化、旧字段映射兼容及空结果失败分类。 |

1F-2 当时只准备后续组合所需的内部边界。后续 1F-5B 已将 Parser、Presenter、Codec 和 Submit 用例接入
`/llm/analysis`，但翻译锁仍只保护当前单进程的共享 Translator/MinerU 输出目录，不能解释为分布式锁、
多实例部署或可靠任务队列。

## 阶段 1F-3 Analysis Application 与任务级 I/O

| 文件 | 覆盖内容 |
| --- | --- |
| `test_analysis_application.py` | 只接收 `TaskId` 的 Application：领取/stale、受理快照、既有 Progress、召回审计前置、阶段顺序、单终态、交互审计硬门禁、知识库三态、翻译降级、RAG close 和 Factory 退出故障。 |
| `test_analysis_production_adapters.py` | 任务目录/OCR 缓存边界、越界下载拒绝、遗留知识库三态、任务级 RAG 文档绑定和 Transport 释放，以及临时 SQLite 的召回/交互/close 审计落库。 |

这些测试只使用临时目录、临时 SQLite、严格 Fake 和替身 Transport。它们证明 Application 的单任务
编排与 fail-closed 收口，不代表新链路已经接入 Worker、Dispatcher、资源恢复、Callback、可靠队列或
多实例部署；`/llm/analysis` 仍执行遗留链路。

## 阶段 1F-3S Analysis Application 等价拆分

| 文件 | 覆盖内容 |
| --- | --- |
| `fixtures/analysis_application_1f3s_happy_trace.json` | 拆分前冻结的单候选成功路径：31 个 Port 调用顺序、RAG Prompt digest、recall payload 稳定字段、交互 attempt、知识库幂等键和文档身份；不冻结单调时钟耗时。 |
| `test_analysis_application.py` | 继续覆盖既有成功/故障矩阵，并新增公开 `__all__`、`RunAnalysisTask` 构造签名和结构化成功轨迹差分，确保拆分不改变公开 Application 入口或副作用。 |
| `test_architecture_boundaries.py` | 新增 1F-3S AST 门禁：473 行 Facade 的 700 行上限、四协作器装配、禁止 Prompt/分类/结果映射回流、协作器反向依赖禁止及各职责真实 Port 算法。 |

1F-3S 只拆分 `app/modules/analysis/application/` 内部实现。五个协作器没有跨 execution 可变状态，
没有新增线程、锁、缓存、重试或队列；离线轨迹证明的是行为等价，不代表新链路已经接入生产路由、
Dispatcher、可靠队列或多实例运行时。

## 阶段 1F-4 Analysis 批量原子受理与顺序协调

| 文件 | 覆盖内容 |
| --- | --- |
| `test_analysis_batch.py` | 追加式 `batch_id`/`batch_sequence` Schema 与部分唯一索引、32 项整批提交/任一投影失败回滚、首项 `1` 后续 `0` 的旧公开投影、Codec/领取/持久扫描、活动任务与 Callback Guard 冲突、已过期旧回调租约清理、SQLite busy、50 同键/不同键并发、跨批连续全局调度序号、提交后单次唤醒及唤醒失败后的持久发现。 |
| `test_task_service.py`、`test_analysis_task_adapter.py`、`test_analysis_ports.py`、`test_analysis_application.py`、`test_analysis_web_adapters.py`、`test_architecture_boundaries.py` | 共同回归旧任务控制面、Analysis Port/Codec/Application、路由唯一新链路 AST 门禁和架构边界，防止新批量事实破坏公开 file 契约。 |

1F-4 的测试只使用临时 SQLite、严格 Fake 和受控 SQLite 故障注入。它证明单实例 SQLite 写事务中的
批量原子性与持久发现基础；不证明真实 Dispatcher 已运行、Callback/资源恢复已闭环、可靠队列、多实例
一致性或生产吞吐已经完成。1F-4 当时的路由仍走遗留线程；后续 1F-5B 已切到新受理链，公开接口文档、
请求/响应参数、状态码、Progress 与 Callback 均未修改。

## 阶段 1F-6 Analysis 资源与 Callback 闭环

| 文件 | 覆盖内容 |
| --- | --- |
| `test_analysis_resource_recovery.py` | 新 execution 的资源记录、同状态 CAS、审计确认后的收口、未知 close 结果隔离，以及恢复审计追加的有限退避/最终隔离。 |
| `test_analysis_callback_guard.py` | 仅 latest 终态 execution 可恢复、空 URL 显式 skipped、未知 HTTP 结果冻结、以及 50 个并发恢复调用至多一次 HTTP。 |
| `test_analysis_application.py` | Resource/Callback 可选依赖在终态提交后的调用顺序、资源 close 的 intent/running/result/audit 事实，以及未注入时的既有兼容编排行为。 |
| `test_task_service.py`、`test_analysis_ports.py`、`test_analysis_production_adapters.py`、`test_architecture_boundaries.py` | SQLite 资源事实、Port 契约、Adapter 边界和 Facade 规模/依赖方向的联合回归。 |

1F-6 的离线测试只用临时 SQLite、严格 Fake 和替身 HTTP Transport。资源恢复仅补有证明的审计追加，
不能自动重放 RAG close/delete；Callback 恢复只复用 Guard 并重读 latest 候选一次，不能重跑模型、RAG
或知识库。1F-6 当时的组件尚未接入 Worker、Container、Dispatcher 或公开路由；后续 1F-5B 已完成
唯一新链路接线。SQLite 验证仅覆盖当前 `single_instance` 事实，不代表可靠队列或多实例部署已完成；公开
接口文档、请求/响应、状态码、Progress 与 Callback 载荷均未修改。

## 阶段 1F-7A Analysis 隔离与存量库只读门禁

| 文件 | 覆盖内容 |
| --- | --- |
| `test_analysis_stage1f7a.py` | 一个共享 `RunAnalysisTask` 实例下 50 个不同 TaskId 并发执行；冻结输入、任务目录、Progress、RAG、翻译、Callback Guard、资源 CAS/close 审计全部按任务隔离，并以 AST 禁止新 Analysis 模块导入旧 `analysis_service` / Worker。 |
| `test_analysis_cutover_preflight.py` | `inspect_analysis_cutover.py` 的 ready/阻断/缺 Schema 三种退出路径、稳定有界且脱敏的 JSON、只读 SQLite 边界、以及绝不初始化缺失 Schema。 |
| 既有 `test_analysis_application.py`、`test_analysis_production_adapters.py`、`test_analysis_resource_recovery.py`、`test_analysis_callback_guard.py`、`test_analysis_batch.py`、`test_analysis_translation_isolation.py` | 文件、模型、审计、知识库、翻译、Callback、资源恢复、SQLite 事务/唤醒/并发的故障矩阵复核。 |

1F-7A 提供离线验收和保留存量数据库时的只读门禁。脚本没有 `--apply`，阻断项必须人工处置；它不启动
`run.py`，不连接真实 AnythingLLM、模型、OCR、知识库或 Callback。当前发布制度每次更新均停服并由
`clean.py` 清库重建，日常发布无需对空库重复运行预检；保留存量库、备份恢复或清理结果存疑时，仍须确认
五类硬阻断均为零并人工核对 `newAcceptedExecutions`。离线验证不等于 production ready。

## 阶段 1F-5B 唯一生产链路由接线

| 文件 | 覆盖内容 |
| --- | --- |
| `test_analysis_web_adapters.py`、`test_analysis_contract_assets.py` | 当前 `/llm/analysis` 的 Parser → Submit → Presenter AST、严格空 202、400/409/413/503/500 映射、内部身份不外泄和新 batch 持久化。 |
| `test_stage1a1_check_task_contract.py` | file `check-task` 的严格空 200、同一新 Callback Guard 恢复、空 URL/失败/成功收口、同请求重复 fileName 稳定去重，以及历史无 batch 身份终态绝不回退到旧恢复器。 |
| `test_routes.py`、`test_dependency_container.py` | 受理后只唤醒 Dispatcher、不创建路由线程、策略快照持久化、RAG/知识库租约不进入请求线程。 |

本阶段验证的是代码接线和临时 SQLite/Fake 行为。当前清库发布必须先停止 DocSense，确认 `clean.py`
成功退出后再启动新版本；`test_clean_runtime.py` 验证运行时目录外的兼容数据库覆盖项也会删除，且文件
占用不会被伪装成成功。保留存量库时才运行 `inspect_analysis_cutover.py` 并关闭全部五类阻断项。
禁止启动 `run.py` 代替发布编排，禁止在线双跑。

## 阶段 1G 结构关闭资产

| 文件 | 覆盖内容 |
| --- | --- |
| `contracts/stage1g_debug_contract.json`、`test_stage1g_contract_assets.py` | 冻结公开路由集合和四条 Debug 路由的内部响应、查询参数及页面依赖，不扩大接口参数。 |
| `test_stage1g_bootstrap_boundaries.py`、`test_architecture_boundaries.py` | 禁止生产组合根依赖 Flask，并限制正式/Debug 蓝图只承担 Parser、Application 调用和 Presenter。 |
| `test_stage1g_reference_inspector.py` | 验证遗留引用检查器的分类、动态引用识别、候选定义和负例能力。 |
| `contracts/stage1g_legacy_test_migration.json`、`test_stage1g_legacy_test_migration.py` | 记录十个旧测试文件共 200 条断言的现行语义归属，并永久禁止旧模块重新进入测试执行路径。 |
| `test_stage1g_closeout.py` | 1G-6 关闭资产门禁：全部静态数据库表均有所有权、八个现行模块均有说明、1G-5 已删除运行路径不得回流。 |
| `docs/重构记录/阶段0资产/260801-阶段1关闭模块所有权与遗留适配矩阵.md` | 固化阶段 1 关闭时的模块/表所有权、依赖方向、保留适配、回滚点、阶段 2 输入和生产未验证边界。 |

1G-4 删除的是已由现行分层测试承接的重复或实现耦合测试；1G-5 在负责人确认仓库外条件后才逐候选
物理删除生产兼容源码。旧测试文件、断言数、目标测试和语义边界以迁移清单为准；Task Service 的
历史投影造数已限制在 `task_service_fixtures.py`，并发受理和 Callback 恢复测试必须走现行
Analysis Application/Adapter/Guard 链。

1G-6 的矩阵测试只证明资产与当前源码一致，不证明 SQLite 之外的数据库迁移、多实例 lease/fencing、
RabbitMQ ACK/DLQ、生产容量或 exactly-once。新增固定表或模块时必须先明确所有者和迁移阶段，再更新
矩阵；不得通过放宽扫描规则绕过所有权评审。

## 推荐验证流程

1. 先运行文件对话范围测试：`venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_chat*.py" -q`。
2. 再运行网关与容器边界测试：`venv\Scripts\python.exe -B -m unittest tests.test_anythingllm_chat_gateway tests.test_dependency_container -q`。
3. 运行阶段 0 资产测试：`venv\Scripts\python.exe -B -m unittest tests.test_stage0_contract_assets tests.test_stage0_baseline_tools tests.test_stage0_sqlite_inventory -q`。
4. 运行阶段 1A-1 契约测试：`venv\Scripts\python.exe -B -m unittest tests.test_stage1a1_check_task_contract tests.test_stage1a1_progress_contract tests.test_stage0_contract_assets -q`。
5. 运行阶段 1A-2 架构测试：`venv\Scripts\python.exe -B -m unittest tests.test_architecture_boundaries -q`。
6. 运行阶段 1A-3 内部契约测试：`venv\Scripts\python.exe -B -m unittest tests.test_task_check_application tests.test_task_progress_application tests.test_architecture_boundaries -q`。
7. 运行阶段 1B-1 可靠命令与 Presenter 测试：`venv\Scripts\python.exe -B -m unittest tests.test_task_callback_recovery_application tests.test_task_status_presenter tests.test_architecture_boundaries -q`。
8. 运行阶段 1B-2 Progress 迁移测试：`venv\Scripts\python.exe -B -m unittest tests.test_progress_request_adapter tests.test_task_progress_presenter tests.test_legacy_task_read_adapter tests.test_in_memory_progress_adapter tests.test_progress_connection_registry tests.test_stage1a1_progress_contract tests.test_progress_and_check_task tests.test_task_progress_application tests.test_dependency_container tests.test_architecture_boundaries -q`。
9. 运行阶段 1C 报告测试：`venv\Scripts\python.exe -B -m unittest tests.test_report_task_adapter tests.test_report_ports tests.test_report_application tests.test_report_request_adapter tests.test_report_domain tests.test_report_contract tests.test_report_io_adapters tests.test_report_rag_adapter tests.test_report_interaction_audit_adapter tests.test_report_runtime_adapters tests.test_report_callback_guard tests.test_report_resource_recovery tests.test_report_dispatcher tests.test_report_submission_presenter tests.test_stage0_contract_assets tests.test_dependency_container tests.test_architecture_boundaries -q`。
10. 运行共享审计与 Knowledge Gateway 回归：`venv\Scripts\python.exe -B -m unittest tests.test_task_service tests.test_anythingllm_knowledge_gateway -q`。
11. 运行阶段 1D-0 契约资产：`venv\Scripts\python.exe -B -m unittest tests.test_stage1d_weaponry_contract_assets -q`。
12. 运行阶段 1D-0R 检索质量资产：`venv\Scripts\python.exe -B -m unittest tests.test_weaponry_retrieval_quality tests.test_stage1d0r_retrieval_quality_assets tests.test_stage1d0r_isolated_reindex tests.test_document_processing_mhtml tests.test_anythingllm_workspaces tests.test_anythingllm_transport tests.test_anythingllm_documents tests.test_architecture_boundaries -q`。
13. 运行阶段 1D-1 领域测试：`venv\Scripts\python.exe -B -m unittest tests.test_weaponry_domain tests.test_weaponry_contract tests.test_weaponry_retrieval_quality tests.test_stage1d0r_retrieval_quality_assets tests.test_weaponry_application tests.test_stage1d_weaponry_contract_assets tests.test_architecture_boundaries -q`。
14. 运行阶段 1D-2 请求、文档范围与任务 Codec 测试：`venv\Scripts\python.exe -B -m unittest tests.test_weaponry_request_adapter tests.test_weaponry_submission_presenter tests.test_weaponry_document_scope tests.test_weaponry_task_adapter tests.test_architecture_boundaries -q`。
15. 运行阶段 1D-3A Port 与严格 Fake：`venv\Scripts\python.exe -B -m unittest tests.test_weaponry_ports tests.test_weaponry_strict_fakes tests.test_architecture_boundaries -q`。
16. 运行阶段 1D-3B Schema v2 与生产 Adapter 离线测试：`venv\Scripts\python.exe -B -m unittest tests.test_weaponry_retrieval_quality tests.test_weaponry_task_adapter tests.test_weaponry_production_adapters tests.test_anythingllm_workspaces tests.test_architecture_boundaries -q`。
17. 运行阶段 1D-4 Application：`venv\Scripts\python.exe -B -m unittest tests.test_weaponry_application tests.test_weaponry_ports tests.test_weaponry_production_adapters tests.test_weaponry_task_adapter tests.test_architecture_boundaries -q`。
18. 运行阶段 1D-5 Dispatcher、配置与离线组合：`venv\Scripts\python.exe -B -m unittest tests.test_weaponry_dispatcher tests.test_dependency_container tests.test_report_dispatcher tests.test_architecture_boundaries -q`。
19. 运行阶段 1D-6 Callback、资源恢复、生产组合和公开路由：`venv\Scripts\python.exe -B -m unittest tests.test_weaponry_stage1d6 tests.test_routes tests.test_dependency_container tests.test_architecture_boundaries -q`。
20. 运行阶段 1D-7 永久关闭门禁：`venv\Scripts\python.exe -B -m unittest tests.test_weaponry_stage1d7 -q`。
21. 运行阶段 1E-0 契约与故障资产：`venv\Scripts\python.exe -B -m unittest tests.test_stage1e0_reassign_contract_assets -q`。
22. 运行阶段 1E-1 领域模型与状态机：`venv\Scripts\python.exe -B -m unittest tests.test_reassign_domain tests.test_architecture_boundaries -q`。
23. 运行阶段 1E-1R 契约与领域修正：`venv\Scripts\python.exe -B -m unittest tests.test_reassign_domain tests.test_stage1e0_reassign_contract_assets tests.test_routes tests.test_architecture_boundaries -q`。
24. 运行阶段 1E-2 Port、严格 Fake 与 SQLite 事实：`venv\Scripts\python.exe -B -m unittest tests.test_reassign_ports tests.test_reassign_fake_repository tests.test_reassign_strict_fakes tests.test_reassign_sqlite_adapter tests.test_architecture_boundaries -q`。
25. 运行阶段 1E-2R 联合回归：`venv\Scripts\python.exe -B -m unittest -b tests.test_reassign_domain tests.test_stage1e0_reassign_contract_assets tests.test_reassign_ports tests.test_reassign_fake_repository tests.test_reassign_strict_fakes tests.test_reassign_sqlite_adapter tests.test_routes tests.test_architecture_boundaries tests.test_dependency_container tests.test_database_service -q`。
26. 运行阶段 1E-3 Adapter 联合回归：`venv\Scripts\python.exe -B -m unittest -b tests.test_reassign_domain tests.test_stage1e0_reassign_contract_assets tests.test_reassign_ports tests.test_reassign_fake_repository tests.test_reassign_strict_fakes tests.test_reassign_sqlite_adapter tests.test_reassign_anythingllm_adapter tests.test_routes tests.test_architecture_boundaries tests.test_dependency_container tests.test_database_service -q`。
27. 运行阶段 1E-4 Application 联合回归：`venv\Scripts\python.exe -B -m unittest tests.test_stage1e0_reassign_contract_assets tests.test_reassign_domain tests.test_reassign_ports tests.test_reassign_fake_repository tests.test_reassign_strict_fakes tests.test_reassign_sqlite_adapter tests.test_reassign_anythingllm_adapter tests.test_reassign_application tests.test_architecture_boundaries -q`。
28. 运行阶段 1E-5 恢复与诊断联合回归：`venv\Scripts\python.exe -B -m unittest tests.test_stage1e0_reassign_contract_assets tests.test_reassign_domain tests.test_reassign_ports tests.test_reassign_fake_repository tests.test_reassign_strict_fakes tests.test_reassign_sqlite_adapter tests.test_reassign_anythingllm_adapter tests.test_reassign_application tests.test_reassign_recovery tests.test_reassign_recovery_sqlite tests.test_reassign_diagnostic_script tests.test_architecture_boundaries -q`。
29. 运行阶段 1E-6 组合根、公开路由与关闭联合回归：`venv\Scripts\python.exe -B -m unittest tests.test_stage1e0_reassign_contract_assets tests.test_reassign_domain tests.test_reassign_ports tests.test_reassign_fake_repository tests.test_reassign_strict_fakes tests.test_reassign_sqlite_adapter tests.test_reassign_anythingllm_adapter tests.test_reassign_application tests.test_reassign_recovery tests.test_reassign_recovery_sqlite tests.test_reassign_diagnostic_script tests.test_dependency_container tests.test_routes tests.test_architecture_boundaries tests.test_database_service -q`。
30. 运行阶段 1E-7 恢复实现下沉联合回归：`venv\Scripts\python.exe -B -m unittest tests.test_stage1e0_reassign_contract_assets tests.test_reassign_recovery tests.test_reassign_recovery_sqlite tests.test_reassign_recovery_collaborators tests.test_reassign_diagnostic_script tests.test_routes tests.test_architecture_boundaries -q`。
31. 运行阶段 1E 整体审查修复回归：`venv\Scripts\python.exe -B -m unittest tests.test_stage1e0_reassign_contract_assets tests.test_reassign_strict_fakes tests.test_reassign_sqlite_adapter tests.test_reassign_recovery_sqlite tests.test_reassign_recovery_collaborators tests.test_reassign_recovery tests.test_reassign_ports tests.test_reassign_fake_repository tests.test_reassign_domain tests.test_reassign_diagnostic_script tests.test_reassign_application tests.test_reassign_anythingllm_adapter tests.test_database_service tests.test_architecture_boundaries -q`。
32. 运行阶段 1F-0 契约与黄金资产：`venv\Scripts\python.exe -B -m unittest tests.test_analysis_contract_assets -q`。
33. 运行阶段 1F-1 Domain、架构与兼容回归：`venv\Scripts\python.exe -B -m unittest tests.test_analysis_domain tests.test_architecture_boundaries tests.test_analysis_contract_assets tests.test_analysis_prompts tests.test_architecture_tree tests.test_architecture_recall_service tests.test_range_defaults tests.test_analysis_scope_guard tests.test_analysis_identity_reselect -q`。
34. 运行阶段 1F-2 Port、Codec、Web Adapter 与翻译隔离回归：`venv\Scripts\python.exe -B -m unittest tests.test_analysis_task_adapter tests.test_analysis_ports tests.test_analysis_web_adapters tests.test_analysis_translation_isolation tests.test_architecture_boundaries tests.test_analysis_contract_assets -q`。
35. 运行阶段 1F-3 Application、任务级 I/O 与架构回归：`venv\Scripts\python.exe -B -m unittest tests.test_analysis_application tests.test_analysis_production_adapters tests.test_analysis_task_adapter tests.test_analysis_ports tests.test_analysis_web_adapters tests.test_analysis_translation_isolation tests.test_analysis_domain tests.test_analysis_contract_assets tests.test_architecture_boundaries -q`。
36. 运行阶段 1F-3R 审查修复回归：`venv\Scripts\python.exe -B -m unittest tests.test_analysis_application tests.test_analysis_production_adapters tests.test_analysis_ports tests.test_task_service tests.test_anythingllm_rag_gateway tests.test_rag_port_contract tests.test_architecture_boundaries -q`。
37. 运行阶段 1F-3S 等价拆分回归：`venv\Scripts\python.exe -B -m unittest tests.test_analysis_application tests.test_analysis_production_adapters tests.test_analysis_ports tests.test_task_service tests.test_anythingllm_rag_gateway tests.test_rag_port_contract tests.test_architecture_boundaries -q`；该命令同时覆盖结构化轨迹和 AST 门禁。
38. 运行阶段 1F-4 批量受理联合回归：`venv\Scripts\python.exe -B -m unittest tests.test_analysis_batch tests.test_analysis_task_adapter tests.test_analysis_ports tests.test_analysis_application tests.test_analysis_web_adapters tests.test_task_service tests.test_report_task_adapter tests.test_weaponry_task_adapter tests.test_architecture_boundaries -q`。
39. 运行文件分析定向发现回归：`venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_analysis*.py" -q`；安全全仓回归应动态发现后精确排除 7 个 `test_local_scripts.LocalScriptTests.*`、5 个 `test_test_assets.LLMTestAssetsTests.*` 和 `test_migrate_analysis_security.AnalysisSecurityMigrationTests.test_apply_is_idempotent_and_preserves_callback_metadata_and_audit`，再覆盖所有其余用例。
40. 运行阶段 1F-6 资源与 Callback 联合回归：`venv\Scripts\python.exe -B -m unittest tests.test_task_service tests.test_analysis_batch tests.test_analysis_resource_recovery tests.test_analysis_callback_guard tests.test_analysis_application tests.test_analysis_ports tests.test_analysis_production_adapters tests.test_architecture_boundaries -q`。
41. 运行阶段 1F-7A 隔离与门禁回归：`venv\Scripts\python.exe -B -m unittest tests.test_analysis_cutover_preflight tests.test_analysis_stage1f7a tests.test_analysis_application tests.test_analysis_resource_recovery tests.test_analysis_callback_guard tests.test_analysis_batch tests.test_analysis_production_adapters tests.test_analysis_translation_isolation tests.test_architecture_boundaries -q`；再运行 `venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_analysis*.py" -q`。
42. 运行 1F-6/1F-7A 全面复核修复回归：`venv\Scripts\python.exe -B -m unittest tests.test_analysis_callback_guard tests.test_analysis_resource_recovery tests.test_analysis_cutover_preflight tests.test_analysis_ports tests.test_task_service tests.test_report_callback_guard tests.test_weaponry_stage1d6 tests.test_architecture_boundaries -q`；重点验证 Callback attempt fencing、资源不可逆状态机、毒记录隔离和只读预检句柄释放。
43. 运行阶段 1F-5B 路由接线回归：`venv\Scripts\python.exe -B -m unittest tests.test_analysis_web_adapters tests.test_analysis_contract_assets tests.test_stage1a1_check_task_contract tests.test_routes tests.test_dependency_container -q`。
44. 运行阶段 1F-7B 关闭验收：先执行上述 1F 定向矩阵和 `test_analysis*.py` 发现回归；再动态发现
    `test*.py`，逐项排除本文件“执行限制”中的 13 个测试 ID，并报告发现、排除、执行、成功、失败、错误和
    跳过数量。2026-07-27 的结果为发现 1,752 项、排除 13 项、执行/成功 1,739 项、失败 0 项、错误 0 项、
    跳过 0 项；完整排除清单、分组和环境边界见
    `docs/更新记录/260727-阶段1F-7B关闭验收执行记录.md`。
45. 运行阶段 1F 关闭后审查修复回归：重点覆盖 `test_analysis_callback_guard.py`、
    `test_analysis_resource_recovery.py`、`test_analysis_dispatcher.py`、
    `test_stage1a1_check_task_contract.py` 和 `test_clean_runtime.py`；2026-07-27 的安全全仓结果为
    发现 1,758 项、排除 13 项、执行/成功 1,745 项、失败 0 项、错误 0 项、跳过 0 项。
46. 最后执行 `git diff --check`，并检查没有将内部运行标识暴露给目标响应。
47. 运行 report/weaponry 显式 unknown 重试关闭回归：`venv\Scripts\python.exe -B -m unittest tests.test_task_service tests.test_callback_attempt_audit tests.test_analysis_callback_guard tests.test_analysis_ports tests.test_report_ports tests.test_report_callback_guard tests.test_report_callback_recovery tests.test_weaponry_stage1d6 tests.test_weaponry_strict_fakes tests.test_stage1a1_check_task_contract tests.test_progress_and_check_task tests.test_dependency_container tests.test_architecture_boundaries tests.test_stage0_contract_assets tests.test_stage1d_weaponry_contract_assets tests.test_analysis_contract_assets`；2026-07-29 结果为 262 项通过。安全全仓动态发现 2,055 项，精确排除既有 13 项后执行 2,042 项，成功 2,040、失败 0、错误 0、跳过 2。该证据只覆盖临时 SQLite/Fake 的单实例离线边界。
48. 运行阶段 1H-7 关闭门禁：`venv\Scripts\python.exe -B -m unittest tests.test_document_processing_architecture tests.test_stage1h_closeout tests.test_stage1h_consumer_cutover tests.test_translation_module -q`；再按本文件既有 13 项精确排除清单执行安全全仓。2026-07-29 动态发现 2,128 项，排除 13 项，执行 2,115 项，成功 2,112、失败 0、错误 0、跳过 3；跳过项为两个仅 macOS 真实进程组用例和当前 Windows 无符号链接权限用例。
49. 运行阶段 1H-R 全面审查修复门禁：先执行各 R 波次的
    `test_document_processing_{artifacts,records,formats,mhtml}.py`、`test_stage1h_consumer_cutover.py`、
    `test_legacy_office_conversion.py`、`test_translation_module.py` 和架构门禁；再动态发现
    `test*.py` 并只排除既有 13 项。2026-07-29 发现 2,145 项、排除 13 项、执行 2,132 项，
    成功 2,129、失败 0、错误 0、跳过 3；未运行 `run.py` 或真实后台服务。
50. 运行知识谱系类别文件对话全面审查修复门禁：执行 `test_chat*.py` 动态发现，2026-07-29
    结果为 261 项通过；随后新增的容量适配器异常释放 Admission Guard 用例与全部 architecture
    合同资产共 9 项通过。再执行 Database、AnythingLLM Chat Gateway、Dependency Container、
    Architecture Boundary 和通用 Routes 相邻门禁，共 115 项通过；`compileall app tests` 与
    `git diff --check` 通过。安全全仓后台尝试因无可观测进度被人工终止，不计入通过统计。
51. 运行文件分析 RAG 投影与业务原名上传命名门禁：先执行
    `venv\Scripts\python.exe -B -m unittest tests.test_analysis_rag_naming tests.test_document_processing_rag_projection tests.test_analysis_rag_upload_pipeline tests.test_anythingllm_documents tests.test_anythingllm_rag_gateway tests.test_analysis_web_adapters tests.test_analysis_task_adapter tests.test_analysis_production_adapters tests.test_analysis_application tests.test_analysis_contract_assets tests.test_stage1h_consumer_cutover tests.test_document_processing_architecture tests.test_architecture_boundaries`；
    再动态发现 `test*.py` 并精确排除既有 13 项。2026-07-30 发现 2,189 项、排除 13 项、
    执行 2,176 项，失败 0、错误 0、跳过 3。AnythingLLM Desktop `1.15.0-r2` 的
    `lancedb`/Ollama Embedder 真实单实例验收应作为独立受控证据执行，不得通过自动化测试
    读取开发机 Provider 数据，也不得把该结果解释为生产、多实例或可靠队列能力。
52. 运行文件分析资源活跃权与 close 恢复竞态门禁：
    `venv\Scripts\python.exe -B -m unittest tests.test_analysis_resource_recovery
    tests.test_analysis_application tests.test_analysis_dispatcher
    tests.test_analysis_composition tests.test_analysis_deployment_config
    tests.test_analysis_ports tests.test_analysis_production_adapters
    tests.test_analysis_callback_guard tests.test_analysis_batch
    tests.test_task_service tests.test_dependency_container
    tests.test_architecture_boundaries tests.test_document_processing_architecture -q`。
    2026-07-30 联合回归 198 项通过；安全全仓动态发现 2,191 项，精确排除既有 13 项后
    执行 2,178 项，失败 0、错误 0、跳过 3。门禁必须覆盖终态 Callback 等待期间仍为
    `tracking` 的记录、`session_close=running`、close 后审计窗口、活跃 Worker 零版本
    写入，以及进程失活且保护期超时后只隔离不重放远端 close/delete。
53. 运行阶段 1G-5 条件物理删除门禁：先执行各 A～E 小批次专项测试、
    `tests.test_stage1g_reference_inspector` 和 `tests.test_architecture_boundaries`，再运行引用检查器；
    2026-08-01 最终扫描 554 个 Python/313 个文本文件，11/11 候选删除就绪。安全全仓动态发现
    2,115 项、精确排除既有 13 项、执行 2,102 项，失败 0、错误 0、跳过 3；未运行 `run.py`
    或真实后台服务，且 `docs/接口文档/` 零修改。
54. 运行阶段 1G-6 关闭验收：依次执行 Debug/Container/Route/Architecture、Analysis、
    Report/Tasks/Callback、Weaponry、Reassign、Chat/Progress、DocumentProcessing/Translation、
    AnythingLLM Integration 和安全全仓九组门禁。2026-08-01 安全全仓动态发现 2,118 项，
    精确排除 13 项，执行 2,105 项，失败 0、错误 0、跳过 3；同时运行
    `tests.test_stage1g_closeout` 固化数据库表与模块所有权、已删除路径不回流和阶段 2 交接边界。
55. 运行对话模块迁移与知识谱系独立接口阶段 9 关闭验收：定向联合门禁执行 286 项，Analysis
    定向发现执行 269 项，Stage 1G/DocumentProcessing 资产门禁执行 16 项，均为 failure/error 0；
    再动态发现 `test*.py` 并严格按“执行限制”中的 13 个完整测试 ID 排除。2026-08-02 最终发现
    2,158 项、排除 13 项、执行 2,145 项，失败 0、错误 0、跳过 3。该结果只证明临时 SQLite、
    Fake/Mock 和 Flask Test Client 的 Windows 单实例离线行为；没有运行 `run.py`、真实
    AnythingLLM、停服清库或前端联调。
56. 运行 Translation 严格表格 HTML 恢复门禁：执行
    `venv\Scripts\python.exe -B -m unittest tests.test_translation_module
    tests.test_stage1h_consumer_cutover tests.test_document_processing_formats
    tests.test_analysis_translation_isolation -q`，并补跑 `test_analysis*.py` 定向发现及
    Architecture/DocumentProcessing Architecture 门禁。必须覆盖合法表格恢复、危险/畸形/资源
    超限候选整段转义、属性白名单、Text 不恢复、MinerU PPTX 当前输出、Analysis 两个结果字段、
    Engine 只接收文本节点和共享 Renderer 并发确定性；不得把纯文本 MHTML 降级解释为可恢复表格。
57. 运行 RAG 图片占位文本移除门禁：执行
    `venv\Scripts\python.exe -B -m unittest tests.test_document_processing_rag_projection
    tests.test_analysis_rag_upload_pipeline tests.test_stage1h_consumer_cutover
    tests.test_document_processing_architecture tests.test_analysis_production_adapters
    tests.test_dependency_container tests.test_analysis_application -q`。必须证明投影中不存在图片移除
    提示、alt、媒体类型、摘要、payload 长度和 Base64，行内图片只留下 Token 分隔空格，
    canonical Artifact、代码示例、外部图片、流式内存、并发隔离与 Analysis 上传边界保持不变。
58. 运行 Weaponry 临时资源即时持久清理门禁：执行
    `venv\Scripts\python.exe -B -m unittest tests.test_weaponry_dispatcher
    tests.test_weaponry_stage1d6 tests.test_dependency_container -q`，再执行
    `venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_weaponry*.py" -q` 和
    `venv\Scripts\python.exe -B -m unittest tests.test_report_dispatcher
    tests.test_analysis_dispatcher -q`。必须证明业务终态只唤醒资源维护而不等待清理、不连带唤醒
    Callback Guard；批次上限按逐项资源恢复尝试数计量、多个大任务轮转推进、停止信号在单项
    之间生效；
    明确失败保留持久退避，Interaction Audit/DELETE/检查点结果未知继续隔离且绝不盲删。
59. 运行对话 Workspace 业务身份命名关闭门禁：先执行命名、Chat 执行器、AnythingLLM Chat
    Gateway、删除、公开路由、合同、Container 和架构定向组合，再动态发现 `test_chat*.py`；
    最后动态发现 `test*.py` 并只排除“执行限制”中的 13 个完整测试 ID。2026-08-02 最终定向
    组合 182 项、Chat 281 项通过；安全全仓发现 2,183、排除 13、执行 2,170，失败 0、错误 0、
    跳过 3。经授权的阶段 6 另以任务级生产 Chat Gateway 在本机回环 AnythingLLM 完成两类精确
    命名、文档绑定、最小 Query 和全资源清理；该真实证据仍不替代 Flask/浏览器、多实例或容量验收。
60. 运行永久知识谱系 Workspace 命名关闭门禁：先执行共享命名、Analysis 入库、Knowledge
    Gateway、Reassign 前向/恢复/AnythingLLM Adapter、Weaponry 合同、公开路由与架构组合；必须
    覆盖 50 个不同分类 ID 的并发唯一性、数据库权威 ID 规范化、旧前缀生产引用清零和恢复链同名。
    2026-08-03 阶段 0～6 定向组合依次通过 154、290、67 和最终 38 项；安全全仓发现 2,193、
    精确排除 13、执行 2,180 项，失败 0、错误 0、跳过 3。该证据仅覆盖 Windows 临时
    SQLite/Fake 单实例。阶段 7 随后以本机回环 AnythingLLM 完成永久 Knowledge Gateway、
    Weaponry 任务级 Client、Reassign Adapter 的隔离创建、精确核名和全量 Workspace 基线恢复；
    该证据仍不代表浏览器、完整模型抽取质量、多实例、可靠队列或容量验收。
61. 运行知识谱系对话来源 Metadata 清洗门禁：先执行 Source Mapper、Chat Executor、Weaponry
    Route/History、合同资产、AnythingLLM Thread/Chat Gateway、持久化、Presenter、策略、日志与
    架构定向组合。必须证明只删除一个完整前置 `document_metadata` 包装，剩余正文码点保持，SSE
    与 SQLite/History 快照一致；畸形包装失败关闭且无部分 assistant/chunks；File Chat 不暴露来源，
    通用供应商 DTO 保留原值。2026-08-03 最终定向组合 186 项通过；安全全仓发现 2,203、精确排除
    13、执行 2,190 项，失败 0、错误 0、跳过 3。经单独授权的阶段 7 随后使用本机
    AnythingLLM 和 Flask 协议客户端证明：原始 Finalization 的 1 个来源真实含前置 Metadata，
    公开 SSE/History 的 1 个来源均已清洗且快照一致；Workspace 基线从 4 恢复为 4，目标
    Workspace、Thread、全局文档和临时本地数据残留为 0。该证据仍不代表浏览器、多实例、可靠
    队列、共享数据库、容量或其他 AnythingLLM 版本。
62. 运行 Core 旧目录配置与 Report/Analysis 旧路径式 OCR/MinerU 兼容链完整删除门禁：依次执行
    Runtime/Container、DocumentProcessing、Report/Analysis、Stage 1G/1H 资产门禁，再动态发现
    `test*.py` 并严格排除“执行限制”中的 13 个完整测试 ID。2026-08-03 最终发现 2192 项、排除
    13 项、执行 2179 项，失败 0、错误 0、跳过 3；三个旧路径常量、Config 字段、环境变量解析、
    路径式自由函数和业务 fallback 均已删除，公开接口文档哈希保持不变。该证据仅覆盖 Windows
    临时 SQLite、Fake/Mock 与 Flask Test Client，不代表真实 OCR/MinerU/LibreOffice、生产容量、
    多实例、可靠队列或跨数据库一致性。

## 执行限制

- 不要直接使用原始全量发现命令替代上述定向测试；当前原始发现会包含 7 个可能触发本地
  `run.py`/Shell 的环境测试、5 个依赖被 `.gitignore` 排除样例的资产测试和 1 个 Windows 不支持的
  POSIX 权限位断言。安全全仓口径必须逐项排除这 13 项并报告名称和理由，禁止笼统写成“全量通过”。
- 新文件对话测试必须显式构造临时目录和临时数据库，不能读取开发机 `.runtime` 数据。
- 新测试不得为了方便而放宽 `/llm/chat*` 既有请求字段或 SSE 协议断言。
- 架构测试必须静态解析源码，不能通过 import 生产组合根来收集依赖；规则调整应与模块边界设计一起评审。
