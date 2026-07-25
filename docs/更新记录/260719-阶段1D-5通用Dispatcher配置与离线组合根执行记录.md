# 阶段 1D-5 通用 Dispatcher、配置与离线组合根执行记录

## 1. 执行结论

阶段 1D-5 已于 2026-07-19 完成代码实现、故障补强、定向回归、安全全仓回归、静态检查和文档
同步，可以进入 1D-6。

本波次只完成新 Weaponry 执行链的**离线组合**，没有切换 `/llm/weaponry` 公开路由。生产
`create_application_services()` 有意保持 `weaponry_services=None`，因此实际运行仍由遗留
`run_weaponry_task` 线程链处理。真实 Callback Guard、资源恢复 I/O、供应商协议联调、公开路由
202 空体切换与同步 check-task 恢复均留在 1D-6 一次性完成。

本波次没有增删任何前后端接口参数，没有改变请求、响应或 Callback 字段，也没有修改
`docs/接口文档/`。没有启动 `run.py`，没有连接真实 AnythingLLM、模型、Callback、MySQL、
RabbitMQ、Redis 或 MinIO。

## 2. 实际改造

### 2.1 抽取业务无关的单机持久扫描内核

新增：

- `app/modules/tasks/adapters/local_persistent_dispatcher.py`
- `app/modules/tasks/adapters/process_guard.py`

`LocalPersistentTaskDispatcher` 只认识 TaskId、持久查询、执行函数、共享 limiter、进程锁和有界维护
任务，不导入 Report 或 Weaponry 业务模块。它统一提供：

1. 单执行 Worker 和稳定 FIFO 分页扫描；
2. `Event` 常量空间唤醒，accepted 数量增长不会增加内存队列项；
3. 领取前毒任务的持久冷却，坏任务不会长期占住扫描首页；
4. running execution 只读诊断，禁止按年龄猜测并自动重置；
5. 多个独立维护线程，重型模型调用不会阻塞资源/Guard 维护；
6. 可中断 limiter 等待、真实 stop/close、readiness、fatal error 和 fork 后保护；
7. OS 文件锁单实例门禁，第二个本地 Dispatcher 明确拒绝启动；
8. 进程锁释放失败转为 fatal，不伪装成干净关闭。

`buffered_task_count` 固定为 `0`：50 个 accepted 任务只增加数据库持久行，不创建 50 个业务线程，
也不复制到进程内队列。`accepted_batch_size` 只是每次扫描的页大小，不是积压上限。

### 2.2 Report 迁移为通用内核薄包装

`app/modules/report/adapters/local_dispatcher.py` 与
`app/modules/report/adapters/process_guard.py` 已改为通用内核的兼容薄包装，同时保留原有 Report
日志、快照字段和公开类型。完整 Report Dispatcher 回归证明：稳定 FIFO、冷却、资源维护、停止
语义、跨进程门禁和 50 个持久任务行为没有退化。

这次复用只减少生命周期代码重复，没有把 Report 和 Weaponry 业务逻辑互相导入；后续可靠队列
仍可在 tasks 边界替换本地执行内核。

### 2.3 严格 Weaponry 基础设施配置与固定策略

新增 `app/modules/weaponry/adapters/infrastructure_config.py`，并补齐 `.env.example`。新组合根只允许
`single_instance`，把下列行为冻结为 execution 可审计的固定策略：

- Retrieval Query：`field-semantic-v2`；
- Evidence：`explicit-unit-score-or-stable-rank-v2` 与稳定 score-desc/rank-asc 排序；
- Extraction：`file_aggregate_v1` + `provided_evidence_model_v1`；
- 模式 1 永久拒绝；迁移期显式模式 2 只作为兼容输入，不再选择算法；
- 不设置 Query 字符上限、最小语义词规则或 Evidence 字符/总量/单文档配额；
- 术语辅助关闭时，不读取五个术语专属配置，也不访问术语目录/workspace。

装配前必须分别提供并核对供应商、Embedding、文档处理和抽取模型四类实际能力指纹。期望配置与
Adapter 的实际声明不一致时 fail-fast，禁止在某次调用失败后静默切换 Selection 或 Extraction
策略。生产容器尚未装配新链，所以这些生产现场指纹将在 1D-6 接入真实 Adapter 时填写和验收。

### 2.4 Weaponry Dispatcher 与隔离维护线程

新增 `app/modules/weaponry/adapters/local_dispatcher.py`，复用通用内核并装配：

- 1 条串行任务执行 Worker；
- 1 条资源恢复维护线程；
- 1 条 Callback Guard 维护线程；
- 1 条队列/运行中任务诊断线程；
- 与 Report/上传控制面共享的重型任务 limiter；
- Weaponry 独立进程锁。

资源维护与 Callback 维护通过有界 Port 注入。单次维护失败只记录独立计数和日志，不会杀死执行
Worker；Worker 的 fatal、维护失败和业务执行结果在快照中分开表达。

### 2.5 内部失败分类不再被成功空结果掩盖

审查发现：字段级 Retrieval/Extraction 在保持既有“成功空结果”契约时，供应商 413/429 或
Selection 协议错误可能丢失原因，并被 Dispatcher 误记为正常业务零结果。现已补强：

- HTTP 413 规范化为 `provider_payload_too_large`；
- HTTP 429 规范化为 `provider_rate_limited`；
- Retrieval、Selection、Extraction 的内部诊断沿字段执行结果汇总到任务结果；
- Dispatcher 将供应商容量、输入/策略契约、纯业务零结果和其他失败分项记录；
- 内部诊断字段不进入持久公开结果、HTTP 响应或 Callback。

这既保留既有成功回调口径，也避免生产监控把供应商容量故障误判为“确实没有信息”。

### 2.6 离线组合根和容器生命周期

新增 `app/modules/weaponry/composition.py`。组合时显式校验 Submit、Runner、Dispatcher、Task
Command、Progress、Callback、Retrieval、共享 limiter、进程锁与固定策略必须属于同一实例链；
构造函数本身不会启动后台线程。

`ApplicationServices` 已支持可选 `weaponry_services`，并把它纳入 start、失败回滚、stop 和 close
生命周期。离线容器测试装入完整 Weaponry bundle，证明成功启动/关闭和锁释放；正常生产工厂仍
保持未绑定，以避免 1D-5 中间态进入实际运行链。

## 3. 关键不变量与并发结论

1. 同一进程只有一个 Weaponry 执行 Worker；同一锁路径的第二个进程 Dispatcher 被拒绝。
2. accepted 积压没有业务数量上限；扫描批次只控制单轮数据库读取量。
3. 50 个 accepted 任务只形成持久行、一条执行 Worker 和零内存业务队列项，按稳定顺序持续处理。
4. 重型任务受共享 limiter 限制；资源、Callback 和诊断线程不占用该重型许可。
5. stop 超时时仍如实报告线程存活，running 任务保持 running，不回退为 accepted。
6. 领取前异常只对对应 accepted execution 写入持久冷却，不阻塞后续任务。
7. 这些测试证明单机控制面隔离，不代表真实模型吞吐、50 条长连接/短请求容量、RabbitMQ 可靠
   投递或多实例 execution 所有权。

## 4. 测试与静态检查

### 4.1 定向测试

最终结果：

- `test_weaponry*.py`：203 项通过；
- `test_stage1d*.py`：40 项通过；
- `test_architecture_boundaries.py`、`test_report_dispatcher.py`、
  `test_dependency_container.py`：52 项通过；
- 1D-5 Dispatcher + 上述三组的最终合并复跑：70 项通过。

覆盖稳定 FIFO、50 个持久 accepted、零内存积压、毒任务冷却、running 只观察、维护隔离、
limiter 停机取消、跨进程锁、重复 start/stop/close、fork/释放失败、能力指纹不匹配、术语关闭零
配置访问、模式 1 拒绝、错误分项指标、离线组合实例一致性和容器生命周期。

测试输出中的预期 `WARNING`/`ERROR` 来自故障注入用例，例如永久领取前异常、锁竞争、stop 超时、
供应商容量错误和 running 只观察；测试最终状态均为 `OK`。

### 4.2 安全全仓回归

动态发现 1129 项测试，明确排除以下 13 项：

- `tests.test_local_scripts.LocalScriptTests` 7 项：会启动本地 `run.py` 或 Shell；
- `tests.test_test_assets.LLMTestAssetsTests` 5 项：依赖被 `.gitignore` 排除、当前环境不存在的本地
  测试资产；
- `tests.test_migrate_analysis_security.AnalysisSecurityMigrationTests.`
  `test_apply_is_idempotent_and_preserves_callback_metadata_and_audit` 1 项：Windows 无法验证 POSIX
  `0640` 权限位。

其余 1116 项全部通过，0 failure、0 error、0 skip。排除项均为环境/平台原因，不是隐藏业务失败。

### 4.3 静态门禁

- `venv\Scripts\python.exe -B -m compileall -q app`：通过；
- 关键 1D-5/Report/容器/测试文件 `py_compile`：通过；
- 四份 stage1d 契约 JSON 严格解析：通过；
- AST 架构边界：通过；
- `git diff --check`：通过，仅有仓库既有的 LF/CRLF 转换提示；
- 静态路由检查确认 `app/blueprints/llm.py` 仍导入并在线程中调用遗留
  `run_weaponry_task`；这正是 1D-5 明确保留、1D-6 才删除的生产未切换证据；
- 生产容器日志和分支明确记录 `weaponry_services is None` / `production_route_switched=false`。

## 5. 数据、部署与回滚边界

- 本波次只使用临时 SQLite/Fake/文件锁；历史测试数据无需迁移。
- 新配置和组合根尚未接入生产工厂，不会因缺少四类能力指纹影响当前遗留启动链。
- Report 已切到通用内核薄包装；如需定位回归，可通过其完整测试与兼容快照确认，不需要修改
  数据库 Schema 或公开接口。
- 尚未实现 RabbitMQ ack/redelivery、Outbox、跨实例 lease/fencing 或 Redis 通知；不能把本地
  Dispatcher 称为最终可靠队列。

## 6. 1D-6 输入

下一波次必须在不增删公开参数的前提下完成：

1. 真实 Weaponry Callback Guard、同步 check-task 恢复和过期任务 stale 判定；
2. 真实资源恢复、确定性查回、cleanup pending/终态 tracking 有界对账；
3. 真实供应商能力声明与四类指纹、score/rank、来源身份、空 workspace/Provided Evidence 模型验证；
4. 生产组合根装配与 `/llm/weaponry` Parser → Submit → 202 空体切换，删除路由线程；
5. 50 个并发 Callback 唯一发送、3xx 失败、unknown 冻结和旧任务过期验收；
6. 若实现产生任何超出既有批准范围的接口差异，必须先停止并确认，再同步接口文档。
