# 阶段 1F-5A Dispatcher、配置与离线组合根执行记录

> - 实施日期：2026-07-27
> - 对应设计：[阶段 1F 文件分析高内聚收口文件级实施设计](../重构记录/260726-阶段1F文件分析高内聚收口文件级实施设计.md)
> - 范围：文件分析新链路的内部 Dispatcher、严格运行配置、组合根和生命周期；不切换公开路由
> - 公开契约：未修改 `docs/接口文档/`，未增删任何接口参数、字段、Header、状态码、Progress 或 Callback 格式

## 1. 本次结论

1F-5A 已将已经完成的 Analysis Domain/Application/Adapter 接入容器的内部运行链：新 Dispatcher
使用 SQLite 持久 accepted 扫描、独立进程锁、一个 Worker 和一条有界维护线程；配置、readiness、启动失败
回滚和停止顺序已统一进入 `ApplicationServices`。

联合离线回归曾发现 Callback Guard 的同轮并发恢复竞态：部分线程会在首个 owner 明确失败后才读取新的
`callback_attempts`，从而误形成第二次发送。本次已在同步恢复 Application 入口做有界的本进程活跃请求
合并，并用确定性与 50 并发回归验证其释放后的独立重试语义；1F-5A 现已满足关闭口径。

本次**没有**把公开 `POST /llm/analysis` 或 file 类型 `/llm/check-task` 接到新链路。它们仍由遗留兼容
实现处理，因此不存在同一公开请求的新旧双跑。新 Dispatcher 只扫描同时具有 `batch_id` 和
`batch_sequence` 的新 file execution，旧 file execution 不会被它领取。

这仍是 SQLite `single_instance` 过渡实现：不是可靠任务队列、不是多实例 Worker 竞争机制，也没有
`running` execution 自动接管或分布式 lease/fencing 证明。

## 2. 实现内容

### 2.1 严格内部配置

`app/services/core/config.py` 新增 `AnalysisInfrastructureConfig` 和
`load_analysis_infrastructure_config()`，显式读取以下内部环境变量：

```text
DOCSENSE_ANALYSIS_RUNTIME_MODE
DOCSENSE_ANALYSIS_DISPATCH_SCAN_INTERVAL_SECONDS
DOCSENSE_ANALYSIS_DISPATCH_BATCH_SIZE
DOCSENSE_ANALYSIS_DISPATCH_RETRY_BASE_SECONDS
DOCSENSE_ANALYSIS_DISPATCH_RETRY_MAX_SECONDS
DOCSENSE_ANALYSIS_RESOURCE_SWEEP_INTERVAL_SECONDS
DOCSENSE_ANALYSIS_RESOURCE_SWEEP_BATCH_SIZE
DOCSENSE_ANALYSIS_RUNNING_ALERT_SECONDS
DOCSENSE_ANALYSIS_STOP_TIMEOUT_SECONDS
DOCSENSE_ANALYSIS_CALLBACK_HTTP_TIMEOUT_SECONDS
DOCSENSE_ANALYSIS_CALLBACK_LEASE_SECONDS
```

- 仅允许 `single_instance`；未知、`distributed` 或 `multi_instance` 在组合根发生任何 I/O 前失败。
- 浮点时间参数必须为有限正数且有上限；批量参数必须为 `1..1000` 的整数；退避上界不能小于下界。
- Callback lease 必须严格大于 HTTP timeout 加连接、响应读取和安全余量，避免 lease 恰好在请求尚未完成时
  失效。
- `.env.example`、`docker/.env.docker` 与离线部署说明同时固定默认值，并由部署配置测试防止两个入口漂移。

### 2.2 Dispatcher 与故障收敛

新增 `LocalAnalysisTaskDispatcher`，它是对通用
`LocalPersistentTaskDispatcher` 的 Analysis 薄包装，而不是第二套内存队列：

- 使用运行目录下的 `analysis-dispatcher.lock` 作为跨进程单实例门禁。
- 任务队列观测和领取仅包含新 batch file execution；旧兼容链的 execution 保持原样、不会被新 Worker
  意外消费。
- 普通领取前基础设施错误调用 `LLMTaskService` 的 SQLite 短事务，在同一条件更新中递增
  `dispatch_failure_count`、计算 capped exponential backoff 并写入 `next_dispatch_at`。坏任务让出扫描首页，
  后续 accepted 任务仍可继续被发现。
- 可确定的损坏任务快照调用既有 Analysis 毒快照控制面终态，记录稳定错误并收敛失败；若收敛本身失败，
  才退回上述持久化退避，绝不无限快速重试。
- Resource 恢复和 Callback Guard 维护各自有异常隔离与中文日志；任一维护动作失败不会结束 Dispatcher
  线程，也不会对外部未知副作用作盲目补偿。

为支持 Submit 用例在提交后只做常量空间唤醒，通用 Dispatcher 新增无 `TaskId` 的 `wake_up()` 信号入口；
既有 report/weaponry 的 `dispatch(task_id)` 行为保持兼容。

### 2.3 组合根、进度与生命周期

新增 `app/modules/analysis/composition.py`。组合时复用容器已有的：

- `task_service` 与 Analysis Task Command Adapter；
- `LatestTaskProgressPublisherAdapter` 和共享 `UploadTaskLimiter`；
- Document RAG Factory、Knowledge Factory、任务目录/文件准备、审计、翻译串行协调器；
- SQLite Callback Guard、Callback Recovery、Resource Store 与恢复用例。

`LatestTaskProgressPublisherAdapter` 补充 guarded 发布入口：它把 Application 提供的当前任务守卫与已有的
latest-wins 条件发布合并，使 Analysis 不需要另建 Progress 内存状态。

`ApplicationServices` 增加 `analysis_submit`、`analysis_callback_recovery`、`analysis_dispatcher`、
`analysis_runtime_config`。后台组件按 `report → weaponry → analysis` 启动；任一启动失败只逆序停止**本轮新
启动**的组件，避免误停已经运行的组件；停止和关闭按反向顺序执行，不会把 `running` execution 重置为
`accepted`。readiness 检查 Analysis 运行模式、Dispatcher 锁/线程快照、共享 limiter 身份和 lease 预算，
不发起真实模型或 HTTP 探测。

显式离线 Fake 可以完成组合和 identity 校验，但不会偷偷启动生产后台线程。

### 2.4 同 file 活跃 Callback 恢复合并

`RecoverAnalysisCallbackSynchronously` 新增只保存活跃 `fileName` 的进程内集合：

- 一个 owner 正在读取 candidate、取得 Guard 或执行 HTTP 时，同一容器内相同文件名的跟随者立即返回
  `False`，不会在 owner 明确失败后读取新的 attempt 快照并滚入下一轮发送。
- 活跃键始终在 `finally` 中删除；owner 完成后，下一次独立 check-task 仍可按照原有的显式失败恢复语义
  再次取得发送权。
- 集合不保存结果、任务快照或历史键，大小只与当前活跃恢复数有关。SQLite Callback Guard 仍是跨进程的
  唯一授权与 fencing 事实；该合并仅收敛当前 `single_instance` 容器内的并发入口，不是分布式锁。

## 3. 接口、数据与发布影响

### 3.1 接口影响

无。以下内容均未修改：

- `POST /llm/analysis` 的路由、请求参数、响应体、错误文本与状态码；
- file 类型 `/llm/check-task` 的公开投影与同步恢复接线；
- Progress、Callback 的字段、Header、传输格式和状态码；
- `docs/接口文档/` 中的任何文件。

因此本次不需要接口文档协商。若 1F-5B 要切换上述公开接线，必须先按项目规则确认接口文档影响；不得在
切换时增删前后端参数。

### 3.2 数据与回滚

本次未新增或删除数据库表、列、索引，也未清理历史任务。退避复用已有
`llm_task_executions.dispatch_failure_count/next_dispatch_at`，新 Worker 的筛选只读取 1F-4 已建立的
`batch_id/batch_sequence` 新任务事实。

回滚本次代码时，应在进程完全停止后回退应用版本；不要在新旧 Dispatcher 或旧线程之间在线并行切换。
出现 Callback、资源清理或外部副作用结果未知时，继续保留现有 `outcome_unknown` / `recovery_required`
现场并人工处理，不盲目重放。

## 4. 离线验证

验证均使用 `venv/Scripts/python.exe`，未运行 `run.py`，未连接真实 Callback、模型、AnythingLLM 或其他后台服务。

- 静态编译与导入检查覆盖新配置、Dispatcher、组合根和容器引用。
- `tests/test_analysis_dispatcher.py`：构造不启动线程、毒快照稳定失败、普通错误持久退避、维护异常隔离。
- `tests/test_analysis_batch.py`：旧 file execution 排除、新 batch execution 发现以及 5/10/12 秒 capped backoff
  的 SQLite 原子持久化。
- `tests/test_analysis_composition.py`：严格配置、非法模式/租约关系、无网络 Fake 组合与 shared identity。
- `tests/test_dependency_container.py`：Analysis 启动失败时 Report 回滚，不重置任务状态。
- `tests/test_in_memory_progress_adapter.py`：Application guarded 发布与已有 latest-wins 条件写共同生效。
- `tests/test_analysis_deployment_config.py`：开发样例、Docker 样例与离线部署说明中的全部 Dispatcher 默认值一致。

- `python -m py_compile`：新配置、Dispatcher、组合根、容器和 Callback 恢复模块通过。
- `tests.test_analysis_callback_guard`：6 项通过；新增确定性用例覆盖 owner 活跃时合并、owner 退出后的
  独立失败重试，以及原 50 并发 rejected recovery 用例。
- 原失败用例连续独立执行 10 次，10 次均通过。
- `unittest discover -s tests -p "test_analysis*.py" -q`：305 项通过。
- Dispatcher、配置、Container、Progress、TaskService、Report/Weaponry Dispatcher 与架构关联回归：175 项通过。

上述验证仅使用临时 SQLite 和严格 Fake；未把结果表述为生产、多实例或真实供应商验证。

## 5. 后续边界

1. 1F-5B 前必须先运行 `scripts/inspect_analysis_cutover.py` 对已确认目标库执行只读预检。旧活跃任务、
   sending/unknown Callback Guard、开放旧 RAG 租约、历史终态待恢复 Callback 或新 `running` execution
   任一非零都必须停止切换。
2. 切换发布只能存在一个 production execution owner；禁止双写、双跑、在线影子外部副作用或用环境变量
   启动第二条旧线程链。
3. 后续可靠队列、多实例、数据库权威时间、Task Attempt/heartbeat/reaper、分布式 lease/fencing 与高并发
   吞吐，需要独立阶段及真实基础设施演练；不能由本次本地锁和离线回归推导出来。
