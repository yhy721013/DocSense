# 阶段 1C-6：Dispatcher、组合根与报告路由切换执行记录

## 1. 执行结论

阶段 1C-6 已于 2026-07-17 在 `refactor/concurrency` 开发分支完成。报告生成当前代码已从
“每请求一条 daemon 线程 + 遗留 Service”切换为：

```text
Flask Parser
  → SubmitReportTask
      → SQLite 原子受理并保存不可变 execution
      → 原子 latest Progress 通知
      → Event 常量空间唤醒
  → HTTP Presenter（202 空体 / 409 既有错误体）

LocalReportTaskDispatcher
  → 一条执行 Worker：有界扫描 SQLite accepted
      → UploadTaskLimiter → RunReportTask(task_id)
      → File / Artifact / RAG / Audit / Callback / Resource Adapter
  → 独立资源恢复线程
  → 独立队列诊断线程
```

本轮没有启动 `run.py`，没有连接真实 AnythingLLM、文件服务器、模型服务或甲方回调服务，
也没有部署到生产服务器。当前实现仍是 SQLite 单实例兼容执行链，不是 RabbitMQ 可靠队列，
阶段 1C 还需由 1C-7 完成最终关闭验收。

## 2. 公开契约

本轮只实施此前已经批准的报告契约，没有新增、删除、重命名或泄漏任何请求、响应、
Callback 或 Progress 参数：

- 首次可靠受理：HTTP `202`，严格零字节响应体且不设置实体 `Content-Type`；
- 活动任务、callback 正在发送或 `delivery_outcome_unknown`：HTTP `409`，响应仍为
  `{"error":"任务正在处理中"}`；
- 顶层非对象、混合非对象 `params`、非法 `filePathList`、reportId 及其他既有 400 文本不变；
- 内部 TaskId、trace、通知结果、Dispatcher 状态和资源租约均不进入公开响应；
- 报告 Callback 载荷、Progress 公开字段和空 RAG 成功语义均未改变。

`docs/接口文档/文件处理和报告生成.md` 仅把已批准行为的实施状态更新为当前代码已实现，
没有调整参数表或消息 Schema。

## 3. 持久化积压与单执行 Worker Dispatcher

新增 `LocalReportTaskDispatcher`，其关键边界如下：

1. `llm_task_executions.execution_state='accepted'` 是积压的唯一事实；进程内不保存任务
   Queue、list、Future 或 Executor backlog。
2. `dispatch(task_id)` 只校验 TaskId 并设置一个 `threading.Event`。多个同时唤醒会合并，
   即使任务持续积压，内存等待空间仍为常量。
3. 固定一条名为 `docsense-report-worker` 的 daemon 执行 Worker，按受理事务内生成的
   `dispatch_sequence` 有界扫描；满批时立即继续，未满批时等待唤醒或固定周期。
4. 资源恢复和队列诊断各使用一条独立维护线程，重型报告执行、cleanup 与诊断不会互相
   阻塞；单项可预期异常在各自循环内隔离。
5. Dispatcher 只自动领取 `accepted`。`running` 可能仍在执行，也可能是崩溃遗留；阶段 2
   Attempt/Step/Checkpoint/Worker lease 就绪前只记录数量、最老年龄和有界 TaskId 样本，
   绝不重置为 accepted。只读队列聚合和相同 running 告警最多每 30 秒执行/输出一次，
   避免随终态历史增长反复扫描和刷屏；清空后重新出现会立即告警。
6. 构造对象不启动线程；`start()` 幂等，`stop()` 使用一个总超时，`close()` 幂等。停机
   超时会记录当前/等待 TaskId 并保持 `stopping`，只有后台线程真实退出后才允许进入
   `closed`，不能制造关闭成功假象或回退 running。
7. 生产组合根装配跨进程内核文件锁；同一运行目录的第二个 Dispatcher 拒绝启动，且
   当前 `single_instance` 模式明确禁止 preload/fork。

容量测试预先持久化 50 个不同报告任务，在启动前连续发送 50 次唤醒：结果为一条报告执行
Worker、零内存积压项、49 次合并唤醒，全部任务按稳定事务序号被领取并持续排空。两条
维护线程是固定运行成本，不随业务积压数量增长。

## 4. 启动恢复和两个必需超时

Dispatcher 在启动时立即调用一次 `ReportResourceRecoveryService.sweep(limit)`，之后按固定
周期继续调用。当前内部默认配置为：

| 配置 | 默认值 | 作用 |
| --- | ---: | --- |
| accepted 扫描周期 | 1 秒 | 无唤醒或重启后发现持久化 accepted |
| accepted 单批上限 | 50 | 限制单次数据库读取，满批后继续排空 |
| 领取前故障冷却 | 30 秒 | 毒任务暂时让出 FIFO 首页，不限制正常积压总数 |
| 资源恢复周期 | 30 秒 | 周期恢复 cleanup/audit 事实 |
| 资源恢复单批上限 | 50 | 防止一次 sweep 无界读取 |
| running TaskId 样本 | 20 | 只读诊断，不用于重排 |
| Dispatcher 停机等待 | 5 秒 | 确保关闭调用有限返回 |
| 清理 HTTP 超时 | **60 秒** | 只用于幂等 DELETE 等资源清理请求 |
| 清理租约 | **90 秒** | 覆盖一次 60 秒外部请求及状态提交余量 |

清理 HTTP 超时和清理租约是安全恢复所必需的两个独立值。若清理请求可以无限阻塞，或
租约不长于请求超时，其他恢复者无法安全判断原恢复者是否仍可能提交结果。配置加载器
因此要求所有数值为正有限值，并强制 `cleanup_lease_seconds > cleanup_http_timeout_seconds`；
误配时应用拒绝启动，不静默回退。

60/90 秒只作用于资源清理。报告生成、模型查询和 AnythingLLM 主流程继续使用现有
`ANYTHINGLLM_TIMEOUT`，其为空时仍保持原有不限时语义。本轮没有擅自改变重型模型任务的
业务超时。

## 5. 组合根与生命周期

`app/container.py` 现在显式装配同一对象图：

- `ReportTaskCommandCodec` + `LegacyTaskCommandAdapter`；
- 持久化 latest Guard 的 Progress Publisher；
- Task 级 File/Artifact 与生成专用 AnythingLLM RAG Factory；
- 使用有限 60 秒超时的独立 cleanup RAG Factory；
- SQLite Audit、Callback、Resource Store 与 `ReportResourceRecoveryService`；
- `RunReportTask`、共享 `UploadTaskLimiter`、`LocalReportTaskDispatcher` 与
  `SubmitReportTask`。

容器启动时验证 Submit 和生命周期管理使用同一 Dispatcher 实例，避免“受理唤醒 A、
实际启动 B”的静默错配。应用工厂只为自行构建的生产形态容器启动后台服务并登记退出
关闭；显式注入的离线测试容器不会自动启动线程。生产 Local Dispatcher 若未装配跨进程
文件锁，组合根会拒绝启动；`run.py` 保留 debug 行为但禁用 Werkzeug 自动 reloader，并
明确禁止 preload/fork，防止当前单实例 Dispatcher 被父子进程继承或重复启动。

## 6. Progress latest 竞态补强

全面复核发现，若先在 SQLite 外做 `is_latest` 预检查、随后再写 Hub，新任务可能恰好在
两步之间提交，使迟到的旧 `accepted` 短暂覆盖新快照。为此新增
`GuardedProgressPublisherPort`：持久化 owner 的只读判断由 Hub 在同一业务键的 latest
发布锁内执行、全局 Hub 状态锁外运行，只有判断仍为当前 TaskId 时才写入快照。同键判断
与投影更新保持原子，不同键的数据库查询和发布不再互相阻塞；旧 accepted、running 和
terminal 都不能越过新 owner。

该 Guard 只影响内部通知投影，不改变 WebSocket 消息字段。调用顺序明确要求先结束数据库
写事务，再进入 Hub Guard，避免形成反向锁序。

## 7. 路由切换与旧链隔离

`POST /llm/generate-report` 当前只执行：请求解析、不可变 `ReportSubmission` 映射、
`SubmitReportTask.execute()` 和 `ReportSubmissionResponsePresenter`。路由不再导入或调用
`run_report_task`，也不再创建报告线程。遗留 `report_service.py` 暂时保留兼容测试和后续
静态清理证据，但新路由不会对它双写、双执行或双 callback。

本轮开发环境没有运行主进程，因此不存在可实际排空的在线旧 daemon 线程。真正部署切流
时仍需停止旧版本新受理、盘点旧进程和 accepted/running/Callback Guard、确认旧 Worker
退出或隔离后再启动新版本；该生产 Runbook 门禁保留到阶段 11。

## 8. 测试与静态检查

全部命令均使用项目解释器 `venv\Scripts\python.exe`，没有运行 `run.py`。

### 8.1 定向与并发验收

- 阶段 1C、容器、Progress、路由和架构合并：**164 项通过**；
- Dispatcher 专项：**13 项通过**，包括 50 任务单执行 Worker/零内存积压、稳定 FIFO、
  毒任务冷却、独立维护线程、许可等待停机取消、关闭超时真实性及真实子进程文件锁。

### 8.2 全量安全回归

在最终代码上执行 74 个安全测试模块：

```text
Ran 871 tests
OK
```

与既有安全基线一致，未运行以下四个环境敏感/外部集成模块：

- `tests.test_local_scripts`
- `tests.test_multilingual_translation_integration`
- `tests.test_migrate_analysis_security`
- `tests.test_test_assets`

这些模块不是本轮新增失败；排除它们是为了遵守“不启动主进程、不擅自连接或操作外部
环境”的开发约束。

### 8.3 静态检查

- 240 个 `app/`、`tests/` Python 文件通过编译检查；
- `tests/contracts/stage0_contracts.json` 通过严格 JSON 解析；
- 架构 AST 门禁包含在全量回归中并通过；
- `git diff --check` 无空白错误，仅有 Windows 工作区既有 LF/CRLF 提示；
- 静态搜索只在 analysis/weaponry 路由保留两处既有 `threading.Thread`，报告路由为零；
- 未启动 `run.py`，未创建真实后台网络会话。

## 9. 剩余边界与下一步

1. **尚未进入生产**：当前修改只存在于开发分支工作区，未提交、未发布、未部署。
2. **尚非可靠队列**：SQLite accepted + Event 可恢复受理积压，但不具备 RabbitMQ 消息
   持久化、publisher confirm、late ACK、重试/DLQ、跨进程 Worker 或多实例租约。
3. **running 崩溃恢复延期到阶段 2**：在 Attempt/Step/Checkpoint 与 Worker lease 之前，
   不允许自动重跑可能已经产生外部副作用的 running。
4. **真实容量结论仍延期**：50 任务离线并发证明结构不会生成 50 条等待线程，但不能替代
   阶段 10 的 50+ 在途任务、50 长连接、短请求稳态及真实资源限流压测。
5. **本记录完成时的下一子波次为 1C-7**：完成阶段 1C 最终契约/故障/架构验收、旧引用清单和阶段 2 输入
   清单；之后再按滚动计划进入 1D。

> 后续状态（2026-07-17）：上述 1C-7 已完成，阶段 1C 已关闭。最终验收、遗留引用证据和
> 阶段 2～6 输入见 `260717-阶段1C-7阶段关闭验收执行记录.md`。

本记录在完成后的全面审查修复、并发语义和最终验证细节见
`260717-阶段1C-6全面审查风险修复执行记录.md`。

## 10. 回滚说明

当前无发布动作，因此不需要生产回滚。未来切流若失败，应先停止报告新受理并关闭新
Dispatcher，列出 accepted/running/Guard/资源事实，确认不会重复执行或重复回调后再切换
绑定。增量 SQLite 表和任务现场不得破坏性删除；即使内部实现回滚，已批准的公开 202
空体与 409 契约也不得恢复成旧成功 JSON。
