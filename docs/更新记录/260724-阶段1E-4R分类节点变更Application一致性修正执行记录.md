# 阶段 1E-4R：分类节点变更 Application 一致性修正执行记录

## 1. 结论

阶段 1E-4 全面审查发现的恢复保护、资源身份、lease 和审计问题已经完成修正。1E-4R 没有
切换 `/llm/reassign` 生产路由，没有增删任何前后端接口参数、响应字段或 HTTP 状态码，也没有
修改 `docs/接口文档/分类节点变更.md`。

本轮仍属于未接线的内部 Application/Port/Adapter 修正。补偿执行、过期 Operation 接管后的恢复
用例、人工恢复工具、生产组合根和公开路由切换继续分别属于 1E-5、1E-6。

## 2. 已修正问题

### 2.1 目标 workspace 准备现场

1. `prepare_target_workspace` 返回未知、抛异常或违反 Port 返回契约时，不再由外层 `finally`
   立即释放 preparation claim；claim 保持活动直到过期或由 1E-5 处置。
2. 远端 workspace 已确认成功，但 mapping 因冲突、陈旧 lease 或事务异常未能提交时，
   Repository 通过独立内部请求保存：
   - 准确 workspace slug；
   - `created_by_operation / preexisting / unknown` 三态归属；
   - prepare Step 外部引用；
   - fencing、尝试次数、探测结论和脱敏原因事件。
3. 上述现场写入不创建或覆盖 `workspaces` mapping，不完成 prepare Step，也不释放 claim，
   防止把待恢复状态伪装成前向成功。
4. 正常成功路径仍保持 mapping、prepare Step 成功事实和 claim 释放同一短事务提交。

### 2.2 local-only 与既有 mapping

1. 空 `doc_path` 在创建 Knowledge Port 前进入 local-only 分支；AnythingLLM 配置或 Factory
   故障不再阻止纯本地条件 CAS。
2. 既有 mapping 使用新的供应商无关“按 workspace reference 查回”Port，严格按本地已保存
   slug 查询，不重新应用当前版本的确定性名称规则。
3. Adapter 查回、Application 身份校验以及 Repository mapping 唯一性判断统一使用
   `casefold()`；远端展示名被管理员修改、历史 slug 或大小写差异不会被误判为另一个
   workspace，也不能用大小写变体绕过本地占用检查。

### 2.3 本地契约与异常边界

1. `_begin_step_mutation` 和 `_complete_step` 现在只接受 `ReassignmentStepRecord` 作为成功；
   `None`、错误 DTO 或 `ReassignmentWriteOutcome` 均 fail closed。
2. reserve 阶段的 ID 工厂、时钟、UoW、SQLite 或提交异常被收口为既有
   `RECOVERY_PENDING` 公开结果，不泄露异常正文；事务结果可能未知时不伪装为确定本地失败。
3. 所有新增日志仅保存 operation ID、枚举、错误类型和内部原因码，不记录供应商响应正文、
   API Key、数据库路径或公开请求内容。

### 2.4 lease 与 preparation claim

1. `ReassignmentExecutionSettings` 显式接收远端总预算和 lease 安全余量，并要求：

   ```text
   lease_duration_seconds
   >= remote_total_timeout_seconds + lease_safety_margin_seconds
   ```

2. Application 在远端步骤边界续租；续租失败或返回契约错误时停止后续外部动作并进入恢复隔离。
3. SQLite Repository 与严格 Fake 在 Operation 续租的同一事务/状态快照中同步延长当前活动
   preparation claim；已经过期的 claim 不会被续租复活。
4. 续租结果返回新的不可变 lease 和可选更新后 claim，Application 校验 operation、owner、
   token、fencing、目标分类和过期时间的一致性。

### 2.5 Pin best-effort 审计

Pin 继续保持遗留 best-effort 语义，不参与关键 Saga 成败判定，但不再是不可见外部写：

1. Pin 前必须先追加 `best_effort_pin_attempted` 审计意图；意图写入失败时跳过 Pin。
2. Pin 后追加 `best_effort_pin_completed`，只保存确定副作用、确定无副作用或结果未知分类及
   有界内部错误码。
3. Pin 异常、错误返回类型或失败仍只记录告警，不推翻已经确认的目标成员关系。

## 3. 主要修改文件

| 文件 | 修改 |
| --- | --- |
| `app/modules/reassign/application/service.py` | local-only 提前分流、reserve 异常边界、步骤续租、claim 保留、远端准备事实、严格 Step DTO 和 Pin 审计。 |
| `app/modules/reassign/ports/knowledge.py` | 增加按已持久化 workspace reference 查回的内部 DTO/Port。 |
| `app/modules/reassign/ports/repository.py` | 增加准备现场、Pin 审计、续租后 claim 的强类型内部契约。 |
| `app/modules/reassign/adapters/anythingllm_knowledge.py` | 按 slug 大小写无关地精确查回既有 workspace。 |
| `app/modules/reassign/adapters/sqlite_repository.py` | 原子续租 claim、持久化恢复现场和追加 Pin 审计事件。 |
| `tests/fakes/reassign.py` | 与生产 Adapter 保持相同的续租、恢复事实、Pin 审计和按引用探测语义。 |
| `tests/test_reassign_*.py` | 增加异常、竞态、身份、预算、审计和 SQLite/Fake 一致性回归。 |

## 4. 验证结果

所有 Python 验证均使用项目 `venv\Scripts\python.exe -B`；没有执行 `run.py`，没有访问真实
AnythingLLM 或其他后台服务。

| 验证项 | 结果 |
| --- | --- |
| 新增模块和测试 `compileall` | 通过。 |
| 1E-0～1E-4R、AnythingLLM Adapter、Application 与完整架构边界联合回归 | 156 项通过。 |
| 安全全仓动态发现 | 发现 1,327 项；明确排除 13 项环境/平台测试后，1,314 项通过，0 failure、0 error、0 skip。 |
| `git diff --check` | 通过；只有工作区既有 LF/CRLF 转换提示。 |
| 公开接口文档 SHA-256 | `70BE30F1E768E7B980114793CFA8908AC83E77DBE233EF185C3B4591201361A0`，与 1E-3/1E-4 记录一致。 |
| Application/生产路由接线 | `app/blueprints/llm.py`、`app/container.py` 对新 Application/SQLite Adapter 的引用仍为 0。 |

安全全仓排除口径保持不变：

1. `tests.test_local_scripts.LocalScriptTests.*` 7 项：会启动本地 Shell、静态服务或 `run.py`；
2. `tests.test_test_assets.LLMTestAssetsTests.*` 5 项：依赖被 `.gitignore` 排除的本地样例；
3. `tests.test_migrate_analysis_security.AnalysisSecurityMigrationTests.`
   `test_apply_is_idempotent_and_preserves_callback_metadata_and_audit` 1 项：Windows 无法可靠表达
   POSIX `0640` 权限断言。

## 5. 对 1E-5/1E-6 的约束

1. 1E-5 必须优先读取 `target_workspace_slug`、`target_workspace_ownership`、prepare Step
   `external_reference` 和 `workspace_preparation_fact_recorded` 事件，不能重新猜测已确认的远端身份。
2. 进入恢复隔离且仍持有活动 claim 的 Operation，只能由 fencing 合法的恢复流程在探测、补偿
   或人工确认后释放；不得在普通请求清理器中无条件释放。
3. 1E-6 组合根必须把同一份 `ReassignmentInfrastructureConfig.total_timeout_seconds` 传给
   `ReassignmentExecutionSettings.remote_total_timeout_seconds`，并显式配置非零 lease 安全余量。
4. Presenter 继续只使用既有 `ReassignmentResult` 和冻结 message，不得暴露新增的 claim、
   续租、准备现场或 Pin 审计事实。
