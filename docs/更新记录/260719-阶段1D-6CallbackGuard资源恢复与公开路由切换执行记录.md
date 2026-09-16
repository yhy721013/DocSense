# 阶段 1D-6 Callback Guard、资源恢复与公开路由切换执行记录

## 1. 执行结论

阶段 1D-6 已于 2026-07-19 完成源码实现、安全离线验收、并发回归、静态检查和文档同步。
本波次将 Weaponry 的真实 Callback Guard、甲方要求的同步 check-task 恢复、资源有界恢复、
生产组合根和 `/llm/weaponry` 公开薄路由接入同一条 execution 实例链。

本结论分为两个层次：

- **代码与安全离线门禁已完成**：215 项 Weaponry、40 项 Stage 1D、69 项关联回归和排除
  13 项明确环境/平台测试后的 1130 项安全全仓回归全部通过；编译、JSON、架构、路由 AST 和
  差异检查通过。
- **真实 AnythingLLM 运行门禁待环境**：当前 `.env` 指向的 `localhost:3001` 拒绝连接，且
  四类生产能力指纹未配置，所以尚未验证真实 score/rank、来源身份、空 workspace 和
  Provided-Evidence 模型行为。只读探测没有创建、修改或删除任何 workspace、文档或 Thread。

因此，1D-6 的开发分支代码已经实现，但不能写成“真实供应商联调已通过”，也不代表代码已经
部署生产。此处是 1D-6 完成时的顺序判断；2026-07-20 后续已完成 1D-7 的代码与离线关闭验收，
真实供应商证明仍保留为生产启用硬门禁，机器校验通过前 readiness 必须为 false。

## 2. 范围与契约影响

| 项目 | 本次结论 |
| --- | --- |
| 实施分支 | `refactor/concurrency` 当前未提交工作区 |
| 公开接口 | `/llm/weaponry`、`/llm/check-task` 的 weaponry 分支 |
| 受理成功 | `/llm/weaponry` 使用已批准的 HTTP 202 严格空响应体 |
| 受理失败 | 保留既有 HTTP 400/404/409 单字段 JSON 错误体 |
| check-task | 保留甲方规定的请求内同步恢复副作用；本波次不改变当前 HTTP 200 JSON 响应，已批准的 200 空响应体目标仍留待总计划阶段 6 |
| 请求参数 | 未增加、删除或改名任何参数；ArchitectureId 继续执行已冻结规范 |
| Callback | 未增加、删除或改名任何字段；成功/失败公开投影保持既有结构 |
| 历史数据 | 当前仅测试数据，无迁移、双读、Schema v1 或旧 Worker 兼容要求 |
| 生产状态 | 源码路由已切换，尚未启动正式 `run.py`，尚未部署生产 |

接口实施状态已经同步到 `docs/接口文档/知识谱系解析.md` 和接口文档索引；本次只落实此前批准
的 Weaponry 202 空响应体和同步恢复内部 Guard，没有提前切换 `/llm/check-task` 的 200 空响应体
目标，也没有夹带新的接口参数或消息字段。

## 3. Callback Guard 与同步恢复

### 3.1 持久化 Guard

新增 Weaponry SQLite Callback Adapter，以业务键和 execution TaskId 管理 latest-wins 投递事实：

1. 正常 Worker 和 `/llm/check-task` 同步恢复必须竞争同一个 Guard；不存在第二条绕过 Guard 的
   直接 HTTP 发送路径。
2. 外发前再次复核 latest TaskId 和 fencing token；新任务已经受理时，旧回调标记 stale 并跳过，
   不产生网络请求。
3. 只有 HTTP 2xx 算作明确成功；请求显式设置为不跟随重定向，3xx 不会被下游 2xx 伪装成功。
4. 明确非 2xx 记录为失败并允许按既有策略恢复；连接中断、读取超时等无法确认接收方是否处理的
   结果记录为 `delivery_outcome_unknown`，冻结同业务键的新任务受理，禁止自动盲重试。
5. 人工解除必须记录操作者和原因；解除后若新任务已经受理，旧任务仍会在发送前被 stale 门禁拦截。
6. 维护扫描是有界的，只处理符合恢复条件的终态事实，不会无界串行遍历全部历史任务。

同时修正 Report Callback Adapter 的重定向行为，使其继续严格符合此前冻结的“仅 2xx 成功”口径。
该修改不改变 Report 请求或 Callback 字段。

### 3.2 公开结果无损恢复

新增 Weaponry Callback Recovery Source，从持久化 execution 结果精确重建公开 Callback 投影：

- INPUT 字段、TABLE 字段、空 TABLE 列表和来源明细按已持久化结果无损往返；
- 内部 task_id、attempt、lease、fencing、审计状态和资源标识不会进入公开载荷；
- 持久化结果不完整或无法形成合法公开载荷时拒绝发送，不以猜测值补齐；
- `/llm/check-task` 只负责调用恢复 Application，不再调用遗留 Weaponry 直接重放逻辑。

## 4. 资源恢复与保守隔离

新增 Weaponry 资源恢复 Application、AnythingLLM 单资源清理 Adapter，以及 SQLite 资源 lease/
fencing/cooldown 能力：

1. 有界扫描终态 `tracking` 和 `cleanup_pending` 资源；正常 `accepted`/`running` execution 只观察，
   不按运行时长自动重置。
2. 每次只对一个获得 lease 和 fencing token 的资源执行一次外部删除，避免一个 execution 的资源
   数量放大单轮扫描时间或形成重复副作用。
3. AnythingLLM 404 视为资源已不存在，按幂等成功处理；409 等明确失败写入持久冷却时间；timeout、
   断连等结果未知进入 quarantine，不自动盲删。
4. 外部调用前发现 Audit 仍为 pending 时直接隔离，防止“调用结果未知”期间误删仍可能被使用的资源。
5. 清理意图、lease、fencing、失败次数、下次尝试时间和 quarantine 原因均持久化；进程重启不会
   因内存状态丢失立即重复请求。
6. 清理成功后立即推进同 execution 的下一项资源；明确失败遵守持久冷却，不会在 30 秒维护周期中
   反复打击同一坏资源。

实现检查中还修复了两个真实问题：首次准备清理时误引用未定义变量，以及明确失败完成后没有真正
持久化重试延迟。两者均有回归测试覆盖。

## 5. 生产组合根与公开路由

### 5.1 单一实例链

`create_application_services()` 现在构造并持有唯一 Weaponry 组合：

- 同一个 Task Repository 同时服务 Submit、Run、Dispatcher、同步恢复和资源恢复；
- 同一个 Callback Guard 同时服务正常 Worker 与 check-task；
- 同一个资源 Store 同时服务业务执行和维护恢复；
- 执行 Worker、Callback 维护和资源维护使用业务无关的本地持久扫描内核；
- 重型 Weaponry 执行与 Report 共用既有限流器，但维护线程不占用重型模型许可；
- OS 文件锁拒绝同一运行目录下第二个 Weaponry Dispatcher 进程。

组合检查发现原实现误把 Report 专属进程锁薄包装当作通用锁并传入不支持的参数；现已改为 tasks
模块的通用 `FileProcessSingletonGuard`。生产工厂测试在不建立网络 Session 的条件下验证只构造
一条真实实例链。

### 5.2 Flask 薄路由

`/llm/weaponry` 已切换为以下调用链：

```text
Flask Blueprint
  -> WeaponryRequestParser
  -> DocumentScopeResolver
  -> SubmitWeaponryTask
  -> WeaponrySubmissionPresenter
```

路由不再创建 daemon thread、不再调用遗留 `run_weaponry_task`、不直接写任务数据库，也不持有
AnythingLLM Client。受理成功返回 HTTP 202 且响应体严格为零字节；同一 ArchitectureId 已有活动
execution 或 Callback outcome unknown 时返回既有 409。Dispatcher 从 SQLite accepted 事实持续
取任务，正常积压不会变成请求线程、等待线程或无界 Python Queue 元素。

## 6. 并发与故障验证

新增 `tests/test_weaponry_stage1d6.py` 12 项集成测试：

1. 3xx 被拒绝且不跟随重定向，显式恢复只接受 2xx；
2. read timeout 进入 unknown 并阻止同业务键新提交；
3. 人工解除后新任务先受理，旧回调 stale 且网络调用为零；
4. 50 个并发同步恢复最多产生一次 HTTP 调用；
5. INPUT 和空 TABLE 公开结果无损重建；
6. check-task 使用新 Guard，不调用遗留重放；
7. 终态 tracking 被发现并清理，活动 execution 不受影响；
8. 明确失败使用持久冷却，结果未知进入 quarantine；
9. pending Audit 在外部删除前隔离；
10. AnythingLLM 404/409/timeout 分别映射为成功/失败/未知；
11. 公开路由静态证明只保留 Adapter/Application/Presenter 链；
12. 生产工厂静态/动态证明只绑定一条真实链且构造期不打开网络会话。

`tests/test_routes.py` 另新增两项 50 并发受理测试：

- 50 个不同 ArchitectureId 均返回 202，并持久化为 50 条 accepted execution；
- 50 个相同 ArchitectureId 恰好 1 个返回 202、49 个返回 409；不存在“先查后写”竞态。

这些测试证明单进程控制面、持久化 Guard 和路由受理隔离，不等同于真实模型同时执行 50 个重型
任务。重型 AnythingLLM/模型调用仍按既定资源限制排队，真实 50+ 在途、长连接和短请求容量在
首次完整集成环境与阶段 10 验收。

## 7. 测试与静态检查结果

### 7.1 定向与关联回归

| 测试范围 | 结果 |
| --- | --- |
| 契约资产、领域契约、1D-6 与公开路由 | 65 项通过 |
| `test_weaponry*.py` | 215 项通过 |
| `test_stage1d*.py` | 40 项通过 |
| Report Callback/资源/Dispatcher、容器与架构 | 69 项通过 |

### 7.2 安全全仓回归

原始发现共 1143 项。为遵守“不启动主进程”和平台边界，明确排除以下 13 项：

- `tests.test_local_scripts.LocalScriptTests` 7 项：会启动本地 `run.py` 或 Shell；
- `tests.test_test_assets.LLMTestAssetsTests` 5 项：依赖被 `.gitignore` 排除且当前环境不存在的
  本地 LLM 请求资产；
- `tests.test_migrate_analysis_security.AnalysisSecurityMigrationTests.`
  `test_apply_is_idempotent_and_preserves_callback_metadata_and_audit` 1 项：Windows 无法验证 POSIX
  `0640` 权限位。

其余 1130 项全部通过，0 failure、0 error、0 skip。原始探测曾触发一个本地脚本测试，但
`run.py` 因四类 Weaponry 生产指纹缺失立即退出；随后确认没有残留 `run.py` 进程，并使用上述
安全口径重新完整运行。另一次测试因临时设置 `PYTHONIOENCODING=utf8` 改变 Windows 子进程的
GBK 解码行为而出现一项环境污染失败；清除该变量后重新执行，1130 项全绿。

### 7.3 静态门禁

- `compileall app scripts` 与关键测试编译通过；
- 四份 Stage 1D JSON 资产均通过 JSON 解析；
- Weaponry 路由 AST 不含遗留 Worker、旧 Client、直接数据库调用或线程创建；
- 业务模块导入边界和组合根架构测试通过；
- `git diff --check` 通过，仅有 Git 的 CRLF 转换提示；
- 没有残留 `run.py` 进程。

## 8. 真实 AnythingLLM 门禁

生产启动前必须明确以下四项不可为空的能力指纹：

- `DOCSENSE_WEAPONRY_PROVIDER_FINGERPRINT`；
- `DOCSENSE_WEAPONRY_EMBEDDING_FINGERPRINT`；
- `DOCSENSE_WEAPONRY_DOCUMENT_PROCESSING_FINGERPRINT`；
- `DOCSENSE_WEAPONRY_EXTRACTION_MODEL_FINGERPRINT`。

服务可用后使用隔离临时资源补测以下事实：真实 Candidate 的 score/rank 协议、完整来源身份、
空 workspace 是否不会访问目标文档、Provided-Evidence 抽取是否只消费当前 rows。仍须遵守既有
批准：临时资源使用随机名称并在结束时补偿清理，不得修改现有 workspace 和文档。若供应商协议
不满足冻结 profile，应停止切入实际环境并报告，禁止回退共享父 Thread、目标 workspace 二次 RAG
或遗留模式 1。

## 9. 发布、回滚与后续阶段

当前没有生产发布或数据迁移动作。开发分支源码已经完成新旧执行二选一；正式发布时不得同时启动
遗留 Weaponry 线程链和新 Dispatcher。若上线验证失败，应以版本级回滚恢复上一稳定发布，并先
停止新 Dispatcher、确认没有 running execution/unknown Callback/待清理 owned 资源，再切换版本；
不得只把 Flask 路由临时指回遗留线程而保留新 Worker，以免双执行或双 Callback。

后续顺序为：

1. 在受控 AnythingLLM 环境补齐本记录第 8 节运行门禁；
2. 执行 1D-7 全量关闭验收和遗留引用清单；
3. 原始文件处理、忠实 MHTML 重建和 Translator 解耦继续由平级阶段 1H 实施；
4. MySQL/Outbox/RabbitMQ/Redis 的可靠队列、多实例 lease/fencing 和后台 Callback 兜底继续按
   阶段 2～7 计划实施，不能把当前 SQLite 单实例 Dispatcher 描述为可靠队列已经完成。
