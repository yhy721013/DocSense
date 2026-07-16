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
| `test_architecture_boundaries.py` | 校验 tasks/Flask Adapter 包骨架，扫描当前 domain/application/ports/tasks/presenters，并用临时违规源码自证每条规则能够失败。 |

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
| `test_progress_request_adapter.py` | 无 action 请求解析、reportId 整数/整数字符串无范围规范化、显式 action 拒绝，以及混合/非法 params 整条消息失败。 |
| `test_task_progress_presenter.py` | file/report/weaponry 字段类型、缺失快照、错误结构、严格 JSON 及内部字段不泄露。 |
| `test_legacy_task_read_adapter.py` | 遗留 Task Service 到不可变 Task DTO 的只读转换、顺序/缺失保留和 execution ID 读取。 |
| `test_in_memory_progress_adapter.py` | 50 个 Barrier 同步线程并发订阅/发布、同键多订阅者、锁外通知、异常隔离、发布/释放竞争、深拷贝隔离和 TaskId/序号。 |
| `test_progress_connection_registry.py` | 连接归属、先登记后发送、幂等释放、失败令牌保留和有限重试。 |

`test_task_progress_application.py` 另覆盖初始快照屏障期间的新旧 TaskId 交错、连接已
接受 sequence 水位，以及通知出队后才完成的旧回调：屏障前旧执行通知会被当前快照
淘汰，读取当前快照后到达的新执行通知必须保留，同 TaskId 的迟到旧序号不得使进度
倒退。上述测试只证明单实例内存并发边界，不代替 50 条真实 WebSocket、Redis 跨实例
和稳态容量测试。

## 推荐验证流程

1. 先运行文件对话范围测试：`venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_chat*.py" -q`。
2. 再运行网关与容器边界测试：`venv\Scripts\python.exe -B -m unittest tests.test_anythingllm_chat_gateway tests.test_dependency_container -q`。
3. 运行阶段 0 资产测试：`venv\Scripts\python.exe -B -m unittest tests.test_stage0_contract_assets tests.test_stage0_baseline_tools tests.test_stage0_sqlite_inventory -q`。
4. 运行阶段 1A-1 契约测试：`venv\Scripts\python.exe -B -m unittest tests.test_stage1a1_check_task_contract tests.test_stage1a1_progress_contract tests.test_stage0_contract_assets -q`。
5. 运行阶段 1A-2 架构测试：`venv\Scripts\python.exe -B -m unittest tests.test_architecture_boundaries -q`。
6. 运行阶段 1A-3 内部契约测试：`venv\Scripts\python.exe -B -m unittest tests.test_task_check_application tests.test_task_progress_application tests.test_architecture_boundaries -q`。
7. 运行阶段 1B-1 可靠命令与 Presenter 测试：`venv\Scripts\python.exe -B -m unittest tests.test_task_callback_recovery_application tests.test_task_status_presenter tests.test_architecture_boundaries -q`。
8. 运行阶段 1B-2 Progress 迁移测试：`venv\Scripts\python.exe -B -m unittest tests.test_progress_request_adapter tests.test_task_progress_presenter tests.test_legacy_task_read_adapter tests.test_in_memory_progress_adapter tests.test_progress_connection_registry tests.test_stage1a1_progress_contract tests.test_progress_and_check_task tests.test_task_progress_application tests.test_dependency_container tests.test_architecture_boundaries -q`。
9. 最后执行 `git diff --check`，并检查没有将内部运行标识暴露给目标响应。

## 执行限制

- 不要使用会启动后台服务的全量测试方式替代上述定向测试；`test_local_scripts.py` 等文件可能触发 `run.py`。
- 新文件对话测试必须显式构造临时目录和临时数据库，不能读取开发机 `.runtime` 数据。
- 新测试不得为了方便而放宽 `/llm/chat*` 既有请求字段或 SSE 协议断言。
- 架构测试必须静态解析源码，不能通过 import 生产组合根来收集依赖；规则调整应与模块边界设计一起评审。
