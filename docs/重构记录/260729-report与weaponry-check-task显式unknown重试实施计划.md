# Report 与 Weaponry `check-task` 显式 unknown 重试实施计划

## 0. 文档信息

| 项目 | 内容 |
| --- | --- |
| 制订日期 | 2026-07-29 |
| 文档状态 | **已完成并离线关闭**；负责人确认本次只完成代码与离线验收，不执行真实环境发布门禁；目标库统计、实例版本/旧 Worker 核对留待未来实际发布前执行 |
| 观察基线 | `feat/weaponry-chat@539b046e3143`，工作区在制订计划时干净 |
| 建议实施基线 | 待当前功能分支合入后，从最新 `main` 创建独立开发分支，并重新核对本文基线 |
| 公开契约权威 | `docs/接口文档/` |
| 接口影响 | 不新增、删除或改名任何请求参数、响应字段、Header、状态码或 Callback 字段；但会改变 report/weaponry 的回调补发副作用与 at-least-once 语义，必须先取得明确确认 |
| 运行限制 | 不启动 `run.py`；默认只使用 `venv\Scripts\python.exe -B`、严格 Fake 和临时 SQLite 离线验证 |
| 当前能力边界 | file/report/weaponry 均已完成由新一次 `/llm/check-task` 显式重试 `outcome_unknown` 的代码与离线契约门禁；U7 关闭回归及真实发布前置检查尚未完成 |

> **开工硬门禁**：report 和 weaponry 必须分别确认是否采用“每个新的 `/llm/check-task`
> 请求可对每个规范化业务键显式授权至多一轮 at-least-once unknown 补发”的语义。
> 未确认的业务类型继续保持 unknown 冻结和人工处置；不得因为共享 Callback Guard 已具备
> 底层能力而顺带放开。

### 0.1 U0 契约确认与基线冻结执行记录

2026-07-29，用户明确确认本文 U0 的全部四项决策：

1. report 和 weaponry 均采用“每个新的 `/llm/check-task` 请求对每个规范化业务键至多显式
   授权一轮 at-least-once unknown 补发”；
2. report/weaponry 接收方能够幂等处理相同业务结果的重复到达；
3. `params` 必须完整校验通过后才允许产生任何 Callback 副作用；
4. 单项 404/批量 200 继续按原始 `params` 数量判断，不按去重后的唯一键数量判断。

U0 冻结证据：

| 项目 | 冻结值 |
| --- | --- |
| 分支/提交 | `feat/weaponry-chat@539b046e3143a7c88cc46ff5fc9f2e7558f7071b` |
| 本地 `main` | `e0b0daa9404ef1992846f70eefc484fb5ff56a39`；当前分支领先 2 个提交 |
| 工作区 | 除本文计划文件外无其他修改 |
| `文件处理和报告生成.md` | SHA-256 `063E331055C220FB2EAA5A17DE1A26DBD1907681CA76B0C30D3A11012D1FCD99` |
| `知识谱系解析.md` | SHA-256 `2513C3FC3AAD251091DA570276C3934B61EE6F4AF781638B8BD48F507765E180` |
| `stage0_contracts.json` | SHA-256 `26904EDDF4657AFC7E27A21E1EB7FAB072CEB07CBC8A8FAEE0D2FC9C36241D66` |
| `stage1f0_analysis_contracts.json` | SHA-256 `F41A4839FC3061E733A138DB1B0B4F4EBE9F9C4D03330433AE0E66B3E84E78F6` |
| `stage1d_weaponry_contracts.json` | SHA-256 `1F3A9768910E9BA111A167B7DF8B6A62BD629DA13EBA6D1E21C7701C3E4A70B1` |

U0 验收结论：**通过**。确认只授权按本文阶段实施，不允许提前修改公开字段，也不构成对
SQLite 多实例、可靠队列或真实接收方生产能力的证明。

### 0.2 U1 黄金契约与失败测试执行记录

U1 新增六个目标用例，使用项目 `venv`、临时 SQLite 和离线 Transport 执行：

- report 等价 ID 请求内去重：按预期失败，当前实际调用 Recovery 2 次；
- weaponry 等价 ID 请求内去重：按预期失败，当前实际调用 Recovery 2 次；
- report 后置非法项完整预校验：按预期失败，当前在返回 400 前已调用 Recovery 1 次；
- 原始多项、去重后单键的批量缺失继续返回 200：通过；
- report 新请求显式重试 unknown：按预期失败，第二次请求未发送；
- weaponry 新请求显式重试 unknown：按预期失败，第二次请求未发送。

执行结果：`Ran 6 tests`，其中 5 个目标失败、1 个兼容语义通过；没有导入、装配、临时数据库
或无关功能错误。失败位置与 U2、U4、U5 尚未实现的能力逐项对应，满足“先建立可执行目标、
再修改生产实现”的 U1 门禁。

U1 验收结论：**通过**。未发现新增待确认项，允许进入 U2；U1 的预期失败不作为最终关闭结果，
必须在对应阶段实现后全部转为通过。

### 0.3 U2 路由两阶段解析与三类型稳定去重执行记录

U2 已将 `/llm/check-task` 改为“先全量校验和规范化、再执行查询与恢复”的两阶段流程；按
`(business_type, normalized_business_key)` 对 file、report、weaponry 三类请求项进行稳定去重，
重复项只由第一次出现的位置执行一次查询或恢复。返回 404 还是批量 200 仍严格依据原始
`params` 数量判断，没有改变公开请求字段、响应字段、状态码规则或任务数据结构。

使用项目 `venv`、临时 SQLite 与离线替身执行 U2 门禁：

`venv\\Scripts\\python.exe -B -m unittest -v tests.test_stage1a1_check_task_contract tests.test_progress_and_check_task`

执行结果：`Ran 36 tests in 3.990s`，`OK`；失败 0、错误 0。U1 中与路由相关的三个目标已转绿：
report/weaponry 重复规范化标识只触发一次恢复、后置非法项不会在返回 400 前产生前置回调副作用，
重复缺失项仍保持原始批量 200 语义。既有 file 显式 unknown 语义、接口契约快照以及进度查询兼容
测试均通过。

U2 验收结论：**通过**。未发现新增待确认项，允许进入 U3；report/weaponry 的 unknown 重试仍未
开放，须等待 U3 原子授权审计能力及 U4、U5 各业务接入分别通过门禁。

### 0.4 U3 共享 Guard attempt CAS 与审计基础执行记录

U3 新增内部追加式 `callback_delivery_attempt_events` 表及按业务键、trace 的索引；授权事件与
Guard fencing CAS、execution/投影状态更新、`callback_attempts + 1` 位于同一个
`BEGIN IMMEDIATE` 事务。完成、租约过期冻结和 Guard 不一致冻结会追加对应收敛事件；升级前
已存在且没有授权事件的历史 sending Guard 仍可保守收敛，不伪造 trigger。人工解除继续只写
原有 `callback_guard_release_audits`，两类审计没有合并或相互覆盖。

共享 Guard 现仅在 `delivery_trigger=explicit_check_task_recovery`、业务类型位于 file/report/
weaponry 白名单、启用 failed/unknown 且携带首次读取的 attempt 快照时，才可能授权 unknown
补发。普通 Worker trigger 即使错误传入 unknown 标志也会在事务前失败。U3 同时把 file
check-task 的请求 trace 传入内部授权审计；没有新增或修改任何公开字段。

U3 门禁命令：

`venv\\Scripts\\python.exe -B -m unittest -v tests.test_task_service tests.test_analysis_ports tests.test_analysis_callback_guard tests.test_callback_attempt_audit`

执行结果：`Ran 75 tests in 12.307s`，`OK`；失败 0、错误 0。覆盖三业务初始 Worker 兼容、
普通 trigger 拒绝、三业务 unknown 一致性和 attempt CAS、审计插入故障整体回滚、50 线程同键
单 owner、50 个异键互不串扰、完成/过期/不一致事件以及 release audit 独立性。

U3 验收结论：**通过**。未发现新增待确认项，允许进入 U4；report/weaponry Recovery Source
尚未读取 unknown，U3 没有提前改变它们的 `/llm/check-task` 路由行为。

### 0.5 U4 Report 显式 unknown 恢复执行记录

U4 已为 Report Recovery Candidate 和 Acquire Command 增加内部 `callback_attempts` 快照、
request trace 及严格构造约束；Recovery Source 只对终态 latest report 加载 pending、failed、
outcome_unknown，并继续从已持久化公开结果重建原 Callback payload。显式恢复 Adapter 同时传递
failed/unknown 授权、attempt 快照、固定 trigger 和 trace；正常 Worker 的 initial delivery 不携带
快照，行为不变。

`RecoverReportCallbackSynchronously` 新增进程内活跃 report 键合并，避免同一轮并发线程在 owner
明确失败后重新读取新 attempt 并连续补发；该集合不保存任务历史或结果，跨进程裁决仍完全依赖
SQLite Guard CAS。路由复用 U2 生成的请求 trace，没有增加公开参数。

U4 门禁命令：

`venv\\Scripts\\python.exe -B -m unittest tests.test_report_ports tests.test_report_callback_guard tests.test_report_callback_recovery tests.test_stage1a1_check_task_contract tests.test_callback_attempt_audit`

执行结果：`Ran 68 tests in 13.867s`，`OK`；失败 0、错误 0。覆盖 unknown→success、
unknown→rejected→success 多轮独立请求、50 并发成功和明确失败、旧 candidate stale 零网络调用、
Guard 精确 HTTP outcome、追加审计 trace、公开 payload 及 check-task 契约兼容。测试日志中的回调
历史文件权限告警来自既有非权威调试副本写入；相关测试明确验证权威 Guard 状态，未影响门禁。

U4 验收结论：**通过**。未发现新增待确认项，允许进入 U5；weaponry Recovery Source 此时仍未
读取 unknown，没有被 Report 接入顺带开放。

### 0.6 U5 Weaponry 显式 unknown 恢复执行记录

U5 为 Weaponry Candidate/Acquire 增加与 Report 对等的 attempt 快照、trace 和严格构造约束；
Recovery Source 读取终态 latest weaponry 的 pending、failed、outcome_unknown 及
`callback_attempts`，并继续执行公开 payload 的无损往返校验。显式 Adapter 只在固定 check-task
reason 下开启 failed/unknown，正常 Worker、维护扫描和资源清理链均未获得该授权。

`RecoverWeaponryCallbackSynchronously` 使用进程内活跃 architectureId 合并当前请求竞争者，
持久化 Guard 继续作为跨线程/跨服务实例的唯一权威裁决。严格 Fake 同步实现 attempt 快照与
unknown 显式重试语义，防止后续单元测试用旧 Fake 掩盖生产 Port 约束。

U5 门禁命令：

`venv\\Scripts\\python.exe -B -m unittest tests.test_weaponry_stage1d6 tests.test_weaponry_strict_fakes tests.test_stage1a1_check_task_contract tests.test_callback_attempt_audit`

执行结果：`Ran 68 tests in 15.105s`，`OK`；失败 0、错误 0。覆盖 unknown→success、
unknown→rejected→success、同键 50 并发明确失败单 attempt、50 个异 architectureId 的 payload/
Guard/attempt/trace/transport 隔离、ID 规范化去重、INPUT/TABLE/空结果/历史证据 payload 无损、
stale 零网络调用及 Production Attestation 保持未就绪。

U5 验收结论：**通过**。未发现新增待确认项，允许进入 U6；本阶段没有执行 AnythingLLM
Retrieval、字段抽取、Provided-Evidence 或资源清理，也不构成真实供应商生产验收。

### 0.7 U6 组合根、接口契约与文档同步执行记录

U6 复核并冻结三类发送权边界：Weaponry 组合根原有的 `__post_init__` 已强制 Runner、同步
check-task 和 Guard 维护共用同一 Callback Adapter；Report 实际装配原本也复用同一实例，本阶段
补充 `RunReportTask.callbacks`、`LocalReportTaskDispatcher.callbacks` 只读身份以及容器 fail-fast
断言，防止未来重构形成第二套发送权。Report/Weaponry 维护器仍只调用 `freeze_expired`，没有
获得显式恢复 reason、attempt 快照或网络补发入口。

依据 U0 已取得的接口语义确认，同步更新 `docs/接口文档/README.md`、
`文件处理和报告生成.md` 与 `知识谱系解析.md`：三类业务均采用新的 check-task 显式
at-least-once unknown 补发，整批在副作用前完整校验、按规范化业务键去重，单项 404/批量 200
继续按原始 `params` 项数判定，接收方须按业务键和业务结果幂等。文档没有增删请求参数、响应
字段、Callback 字段、Header 或状态码。Stage 0、Stage 1D、Stage 1F 三套黄金资产同步记录本次
已批准语义；`文件处理和报告生成.md` 的跨平台规范化摘要更新为
`A565F7ED512CDC81CD5ECCECA7AD58C082AB0D672937F6514A895106C1451F84`。

U6 门禁首次执行因 `tests/test_analysis_contract_assets.py` 仍冻结旧批准日期而出现 1 个预期维护
失败；同步将断言更新为 `2026-07-29` 并校验批准说明后，原命令复跑结果为：

`venv\\Scripts\\python.exe -B -m unittest tests.test_dependency_container tests.test_architecture_boundaries tests.test_stage0_contract_assets tests.test_stage1d_weaponry_contract_assets tests.test_analysis_contract_assets`

执行结果：`Ran 85 tests in 4.990s`，`OK`；失败 0、错误 0。关键词审计未发现现行接口说明仍声称
“report/weaponry 只能人工解除”；命中旧 1F 设计文档的是有日期的历史事实，不回写历史记录。
`git diff --check` 无空白错误，仅有 Windows `core.autocrlf` 的预期换行提示。

U6 验收结论：**通过**。未发现新增待确认项，允许进入 U7；Production Attestation 缺失仍是
既有真实供应商发布阻断，不影响本次离线组合根门禁，也不会被本阶段描述为已就绪。

### 0.8 U7 关闭验收与发布门禁执行记录

U7 在临时 SQLite 中新增并通过两项专项演练：从已初始化测试库移除本阶段新增表以模拟旧
Schema，保留既有任务与 sentinel 表后连续初始化两次，验证审计表被加法恢复且旧表/旧数据不被
删除；对已冻结 unknown 的 report 人为制造 Guard owner 不一致，新的显式请求仍返回内部 unknown，
attempt 保持 1、事件集合不增加、网络发送权不产生。结合 U4/U5 的一致 unknown 恢复用例，完成
“一致事实可恢复、损坏事实 fail-closed”的双向演练。

定向与相邻回归命令覆盖 tasks、三类 Callback、路由、Progress、Container、架构和三套黄金资产，
结果为 `Ran 262 tests in 36.817s`，`OK`；失败 0、错误 0。安全全仓按 `tests/README.md` 动态发现
并打印 13 个精确排除 ID：实际发现 2,055 项、排除 13 项、执行 2,042 项、成功 2,040 项、失败
0 项、错误 0 项、跳过 2 项。测试输出中的 ERROR/CRITICAL 均来自断言覆盖的故障注入路径，不是
unittest failure/error。

`venv\\Scripts\\python.exe -B -m compileall -q app tests` 通过；三套 JSON 黄金资产严格解析通过；
接口权威文档摘要复核仍为
`A565F7ED512CDC81CD5ECCECA7AD58C082AB0D672937F6514A895106C1451F84`；
`git diff --check` 无空白错误，仅有 Windows `core.autocrlf` 换行提示。增量日志审计未发现新增日志
输出完整 Callback payload、request payload、lease token、Authorization 或 API key；公开响应也未
增加 owner、attempt、lease、fencing、trace 等内部身份。

U7 离线关闭验收结论：**通过**。负责人随后明确确认本次范围只包含代码与离线验收，不执行
真实环境发布门禁，因此 U0～U7 按批准范围全部完成并关闭。当前没有读取任何真实任务数据库，
也没有用开发机 `.runtime` 统计替代生产证据；report/weaponry 的 `sending/outcome_unknown`、
过期 lease、人工解除记录，以及部署实例版本一致性和旧 Worker 停止情况，均留待未来实际发布
前重新核验。代码与测试只确认 `single_instance` 策略，Weaponry Production Attestation 仍未
就绪；本文的“已完成”仅表示实现和离线关闭完成，不表示生产发布完成。

---

## 1. 问题说明：为什么 request 内去重与 attempt 快照缺一不可

### 1.1 当前路由只对 file 去重

当前 `/llm/check-task` 路由在进入逐项循环后：

- file 会把去除首尾空白后的 `fileName` 放入 `processed_file_keys`，同一 HTTP 请求只处理首次出现项；
- report 会把 JSON 整数或十进制整数字符串规范化为同一个 `reportId.business_key`，但不去重；
- weaponry 会把 JSON 正整数或只含 ASCII 数字的字符串规范化为同一个
  `architectureId.business_key`，但不去重；
- 每处理一个未跳过的项，路由都会读取任务、调用对应业务的同步 Callback Recovery，并在返回前重读任务。

因此，“数组中是两个不同 JSON 值”不等于“内部是两个不同任务”。例如：

```json
{
  "businessType": "report",
  "params": [
    {"reportId": 132},
    {"reportId": "000132"}
  ]
}
```

两个值都会规范为 report 业务键 `132`。weaponry 的 `10502`、`"10502"`、
`"00010502"` 也会规范为同一个业务键。

### 1.2 即使暂不开放 unknown，重复 report/weaponry 也可能形成多轮 failed 补发

以 report 为例，假设初始回调为 `pending`，接收方对第一次补发明确返回 HTTP 503：

```text
params[0] -> 读取 pending/attempt=0
          -> 取得 Guard
          -> 第一次 HTTP
          -> 明确 rejected
          -> callback_status=failed, attempt=1, Guard=idle

params[1] -> 再次读取同一个 reportId
          -> 现在看到 failed/attempt=1
          -> EXPLICIT_CHECK_TASK_RECOVERY 允许 failed 重试
          -> 第二次 HTTP
```

这意味着一个 HTTP check-task 请求可能造成两次外部回调。若数组中有更多等价 ID，次数还会继续增加。
当前 file 已通过请求内稳定去重阻止这种行为，report/weaponry 尚未具备同一保护。

### 1.3 开放 unknown 后风险更直接

假设第一项 HTTP 已发出，但发生读取超时：

```text
params[0] -> unknown/attempt=N
          -> 显式授权
          -> HTTP 结果仍未知
          -> outcome_unknown/attempt=N+1

params[1] -> 顺序执行时重新读取
          -> 看到新的 unknown/attempt=N+1
          -> 若没有 request 内去重，会被当成又一次新的显式授权
          -> 再发一次 HTTP
```

此处即使已经实现 `expected_callback_attempts` 也无法阻止第二次发送，因为第二项是在第一项完成后
重新读取到了合法的新快照 `N+1`。

### 1.4 attempt 快照解决的是另一类竞态

`expected_callback_attempts` 解决多个并发请求预先读取到同一个 attempt 的问题：

```text
请求 A 读取 attempt=N ─┐
请求 B 读取 attempt=N ─┼─> 只有一个能在事务中把 N 更新为 N+1
请求 C 读取 attempt=N ─┘   其余因快照过期返回 stale
```

它不能识别同一个 HTTP 请求中的重复数组项，也不能阻止在上一轮完成后发起的真正新请求。

因此必须同时具备：

1. **请求内稳定去重**：一次 HTTP 请求对每个规范化业务键至多进入一次恢复用例；
2. **attempt 快照 CAS**：同一 attempt 的并发请求至多一个取得 SQLite Guard；
3. **latest/owner/lease/fencing 复核**：旧 execution 或失权发送者不能执行或完成 HTTP；
4. **接收方幂等**：不同的新 check-task 请求仍可逐轮显式授权，跨系统无法实现 exactly-once。

---

## 2. 目标、非目标与完成定义

### 2.1 必须完成的目标

1. file/report/weaponry 均按规范化业务键做请求内稳定去重，保留首次出现顺序。
2. report 和 weaponry 可以分别启用：新一次 `/llm/check-task` 请求显式授权一次
   `outcome_unknown -> sending` 的 at-least-once 补发。
3. 普通 Worker、Dispatcher、Guard 维护线程和资源恢复线程仍然不得重试 unknown。
4. report/weaponry Recovery Candidate 和 Acquire Command 携带不可变
   `callback_attempts` 快照，failed 与 unknown 的显式恢复均受同一快照约束。
5. latest execution、latest projection、Guard owner/state、attempt、lease version 和 fencing token
   在一个短事务内完成一致性检查及条件迁移。
6. 每次 unknown 显式授权和最终投递结果形成持久、可查询、追加式审计事件；审计写失败时不得发送 HTTP。
7. 公开 `/llm/check-task` 继续保持既有请求参数、HTTP 200 空响应体以及 400/404 行为；内部 TaskId、
   attempt、lease、fencing、trace 或审计 ID 均不得对外暴露。
8. 现存一致的 report/weaponry unknown 可以由未来的新 check-task 请求恢复；损坏、owner 不一致或
   无法证明 latest 的历史数据继续 fail-closed。
9. 当前 SQLite 实现仍明确标注为 `single_instance` 过渡实现，不宣称已经实现可靠队列或多实例生产能力。

### 2.2 明确不做

- 不增加诸如 `retryUnknown`、`force`、`operator`、`requestId` 的前端参数。
- 不增加新的 HTTP 路由、响应字段、Callback 字段、SSE/WS 消息或状态码。
- 不把 `outcome_unknown` 改为所有调用方都可以取得的普通 Guard 状态。
- 不由后台扫描、进程启动、部署升级或可靠队列消息自动补发历史 unknown。
- 不删除人工解除能力；人工解除仍作为经过外部核查后的 break-glass 处置路径。
- 不把 check-task 重试误解为重跑报告生成、武器谱抽取、RAG 或模型调用；只重发已持久化的最终公开 payload。
- 不在本次修改中迁移 MySQL、RabbitMQ、Celery、Outbox、跨实例通知或完整 Task Attempt/Reaper。
- 不承诺 exactly-once。HTTP 请求可能已经被接收方处理但响应丢失时，只能提供明确的 at-least-once 选择。
- 不顺带切换尚未接入当前生产路由的通用 `CheckTaskStatusService` 原型，避免扩大变更面。

### 2.3 完成定义

只有同时满足以下条件才允许关闭本计划：

1. report 与 weaponry 的接口语义分别得到明确批准；未批准类型仍保持冻结。
2. 一个合法 check-task 请求对每个规范化业务键最多形成一次 Recovery 调用和一次新 attempt。
3. 同一 attempt 快照的并发竞争只有一个 owner；旧 execution、迟到 lease 和损坏事实网络调用为零。
4. 正常 Worker/维护线程对 unknown 的 HTTP 调用次数严格为零。
5. unknown 显式授权、发送结果、再次 unknown 和 stale 都有持久审计证据。
6. HTTP 200 成功体仍为严格零字节；既有 400/404 JSON、ID 规范化和批量缺失规则逐字节通过。
7. 定向测试、架构门禁、安全全仓回归和 `compileall` 全部通过，并报告实际发现、排除、执行、失败、错误和跳过数量。
8. 未启动 `run.py`，未连接真实 AnythingLLM、模型或甲方 Callback；若需要真实接收方验收，必须另行确认窗口和数据。

---

## 3. 冻结的目标语义

### 3.1 一次请求的授权粒度

推荐冻结为：

> 每个新的、参数合法的 `/llm/check-task` HTTP 请求，对每个规范化业务键至多授权一轮同步恢复。
> 同一请求中的等价重复项只保留首次出现项；后续新的 HTTP 请求可以基于新的 attempt 快照再授权一轮。

这一定义避免把数组长度误当成重试次数，也不需要新增公开幂等键。

### 3.2 去重发生在规范化之后、副作用之前

路由采用两阶段处理：

```text
阶段 A：完整校验全部 params -> 规范化业务键 -> 形成有序项 -> 稳定去重
阶段 B：按首次出现顺序读取任务 -> 同步恢复 -> 按原 execution 重读
```

任何参数校验失败必须在阶段 A 返回 400，阶段 B 尚未开始，因此不会出现“前一项已经发送回调，
后一项无效导致整个请求返回 400”的部分副作用。

> 这一点需要在开工确认时一并冻结。它不改变错误体或状态码，但会修复无效批量请求可能已经产生前置副作用的内部行为。

### 3.3 单项与批量缺失语义

为避免未经批准改变 404/200 契约，推荐继续用**原始 `params` 元素数量**判断单项或批量：

- 原始数组只有一项且任务缺失：HTTP 404；
- 原始数组有多项：缺失项不终止其他唯一键，最终仍按既有规则返回 HTTP 200 空体；
- 即使多项最终规范为一个唯一键，也不擅自把既有“批量请求”改判为单项 404。

该边界必须由黄金测试固定，不能在去重重构中顺便改变。

### 3.4 投递状态机

| 当前状态 | 触发来源 | 是否可取得发送权 | 结果 |
| --- | --- | --- | --- |
| `pending` | 正常 Worker 或显式 check-task | 是，受 latest/Guard 约束 | 进入 `sending` |
| `failed` | 正常 Worker/后台维护 | 否 | 保持原事实 |
| `failed` | 显式 check-task + attempt 快照 | 是 | 新 attempt |
| `outcome_unknown` | 正常 Worker/后台维护/启动扫描 | 否 | 保持冻结 |
| `outcome_unknown` | 显式 check-task + 一致三态 + attempt 快照 | 是 | at-least-once 新 attempt |
| `sending` 且租约有效 | 任意竞争者 | 否 | `busy` |
| `sending` 且租约过期 | Guard 维护 | 否 | 原子冻结为 `outcome_unknown` |
| `success/skipped` | 任意来源 | 否 | `already_completed` |
| 非 latest execution | 任意来源 | 否 | `stale`，网络调用为零 |

新 attempt 的 HTTP 结果：

- 严格 2xx：`success`，Guard 回到 idle；
- 明确拒绝或确认未发送：`failed`，Guard 回到 idle，后续新的 check-task 可再试；
- 请求可能已送达但结果无法确认：重新进入 `outcome_unknown`，继续冻结；
- 发送前 lease/fencing 复核失败：`stale`，不得发出网络请求；
- HTTP 已发生但完成事务失权：不得伪装 failed，应保守保持或冻结为 unknown 并报警。

### 3.5 接收方幂等前提

批准 report/weaponry 前必须确认接收方能够承受相同业务结果重复到达：

- report：至少按 `reportId + 规范化公开回调 payload 身份` 幂等；
- weaponry：至少按 `architectureId + 规范化公开回调 payload 身份` 幂等；
- 不能使用内部 TaskId、lease、attempt 或审计 ID，因为这些值不得进入公开回调。

如果接收方只能按业务键盲覆盖，也应确认同一业务结果重复覆盖不会产生二次计费、重复工作流、重复通知或不可逆副作用。

---

## 4. 目标内部模型与持久化设计

### 4.1 业务 Port 扩展

为 report 增加：

- `ReportCallbackRecoveryCandidate.callback_attempts: int`
- `ReportCallbackAcquire.expected_callback_attempts: int | None`
- 可选内部 `request_trace_id`，仅用于日志与审计，不进入公开结构

为 weaponry 增加：

- `WeaponryCallbackRecoveryCandidate.callback_attempts: int`
- `AcquireWeaponryCallback.expected_callback_attempts: int | None`
- 可选内部 `request_trace_id`

约束规则：

- `INITIAL_DELIVERY` 必须令 `expected_callback_attempts is None`；
- `EXPLICIT_CHECK_TASK_RECOVERY` 必须携带非负 attempt 快照；
- unknown 重试必须同时具备显式 reason、attempt 快照和业务类型授权；
- Port 构造阶段即拒绝矛盾组合，不把错误拖到 SQLite Adapter。

### 4.2 Recovery Source

report/weaponry Recovery Source 的候选状态从 `{pending, failed}` 扩为
`{pending, failed, outcome_unknown}`，并读取：

- latest `execution_id`；
- 业务终态；
- 最终公开 `result_payload`；
- `callback_status`；
- `callback_attempts`。

Recovery Source 仍只负责候选读取和公开 payload 无损重建，不拥有发送权。payload 无法无损重建、
业务键不一致或状态损坏时必须停止，不得重新运行生成/抽取来猜测结果。

### 4.3 共享 Guard 的原子 unknown 授权

`LLMTaskService.acquire_callback_delivery_guard` 继续是唯一发送授权点。推荐保留默认
`allow_outcome_unknown_retry=False`，并把允许条件从“仅 file”收窄为以下全部成立：

1. business type 已经被阶段性批准；
2. 来源是 `EXPLICIT_CHECK_TASK_RECOVERY`；
3. `allow_failed_retry=True`；
4. `allow_outcome_unknown_retry=True`；
5. `expected_callback_attempts` 非空；
6. execution、latest projection、Guard owner 三者为同一 execution；
7. execution 和 projection 都是 `outcome_unknown`；
8. Guard 为同 owner 的 `outcome_unknown`；
9. 当前 attempt 与快照相等；
10. execution 和公开投影均已处于该业务的合法终态。

一个 `BEGIN IMMEDIATE` 事务内完成：

```text
Guard outcome_unknown(v=N) -> sending(v=N+1, new lease token)
Execution callback_status  -> sending
Latest callback_status     -> sending
Latest callback_attempts   -> A+1
INSERT unknown_retry_authorized audit event
```

任何更新行数不是 1、审计插入失败或事实不一致均整体回滚，事务外 HTTP 调用次数必须为零。

### 4.4 追加式审计

不得复用 `callback_guard_release_audits`：人工解除表示“经过核查后解除冻结”，显式 unknown
重试表示“调用方接受可能重复投递并再次发送”，两者风险与语义不同。

建议新增 `callback_delivery_attempt_events`：

| 字段 | 说明 |
| --- | --- |
| `id` | SQLite 内部递增 ID，不对外 |
| `business_type/business_key` | 规范化业务引用 |
| `owner_execution_id` | 被授权的 latest execution |
| `callback_attempt` | 本轮递增后的 attempt |
| `lease_version` | 本轮 fencing 版本 |
| `trigger` | `initial_delivery` 或 `explicit_check_task_recovery` |
| `event_type` | `unknown_retry_authorized`、`delivery_completed`、`lease_expired_unknown` 等 |
| `delivery_outcome` | success/rejected/definitely_not_sent/outcome_unknown/stale；授权事件为空 |
| `request_trace_id` | 服务端生成的内部请求关联标识 |
| `occurred_at` | UTC 时间 |

推荐唯一约束：

```text
UNIQUE (
  business_type,
  business_key,
  owner_execution_id,
  lease_version,
  event_type
)
```

授权事件与 Guard acquire 同事务写入；完成事件与 Guard complete 同事务写入；过期 sweep 进入
unknown 时追加过期事件。日志可以使用业务键摘要和短 trace，不输出完整 payload、lease token 或内部审计内容。

### 4.5 进程内并发合并

report/weaponry 的同步 Recovery 用例应采用与 file 相同的“当前进程活跃业务键集合”：

- 第一个调用成为本进程 owner；
- 同进程相同键的活跃 follower 立即返回 `False`，不等待 HTTP，不读取新 attempt；
- `finally` 必须释放活跃键；
- 集合只保存活跃键，不缓存历史结果，不随任务历史增长；
- 该集合只是降低同进程请求线程堆积和 SQLite 竞争的优化，跨进程正确性仍由 attempt CAS、lease 和 fencing 保证。

为避免扩大跨模块耦合，本阶段可在 report/weaponry 各自 Recovery 用例内按 file 的已验证模式实现；
只有三处行为稳定且架构门禁允许时，才另立计划抽取通用 in-process single-flight 组件。

---

## 5. 分阶段实施步骤

### 阶段 U0：契约确认与基线冻结

#### 工作内容

1. 分别确认 report、weaponry 是否采用显式 at-least-once unknown 补发。
2. 确认接收方幂等能力及责任边界。
3. 确认完整批量预校验后再产生副作用。
4. 冻结“原始 params 数量决定单项 404/批量 200”的兼容口径。
5. 记录最新分支、提交、接口文档哈希、相关黄金资产哈希和 Git 状态。
6. 确认实施期间不运行真实服务，不处理生产 unknown 数据。

#### 测试与验收

- 本阶段只做只读检查，不执行回调或数据库写入。
- 对照 `docs/接口文档/文件处理和报告生成.md` 与 `docs/接口文档/知识谱系解析.md`，列出需要批准的行为文字。
- `git status --short --branch` 必须明确记录，不能把既有工作树修改算入本计划。

#### 达成效果

形成明确的逐业务开工许可；未获批准的类型不会因共享代码修改被意外启用。

### 阶段 U1：黄金契约与失败测试先行

#### 工作内容

1. 先增加会失败的测试，固定请求内规范化去重。
2. 固定 report/weaponry unknown 在显式 check-task 下的目标行为。
3. 固定 Worker、维护线程、启动扫描仍不得重试 unknown。
4. 固定公开响应严格不变。
5. 固定重复键中的等价表示、首次出现顺序和原始数组长度的 404/200 语义。

#### 重点用例

- report `[132, "000132"]` 只调用一次 Recovery；
- weaponry `[10502, "00010502"]` 只调用一次 Recovery；
- 同一请求首项明确失败后，重复项不能形成第二 attempt；
- 同一请求首项再次 unknown 后，重复项不能形成第二 attempt；
- 新的第二个 HTTP 请求可以读取新快照并形成下一 attempt；
- 单项缺失仍为 404，多项含重复键的缺失行为保持冻结口径；
- 后置非法项使整次请求在任何 Callback HTTP 前返回 400；
- HTTP 200 仍为 `b""`，不出现内部状态。

#### 测试与验收

先运行新增测试并确认它们因尚未实现目标能力而失败，失败原因必须精确落在去重、unknown
候选或 Guard 授权，不得是测试装配错误。

#### 达成效果

在实现前建立可执行合同，防止后续为让代码“看起来可用”而放宽 Worker 或响应结构。

### 阶段 U2：路由两阶段解析与三类型稳定去重

#### 工作内容

1. 将当前逐项“校验后立即副作用”拆为：完整解析阶段和执行阶段。
2. 每个合法项形成内部不可变结构，至少包含原始索引、规范化业务键及已解析业务 ID。
3. 使用 `(business_type, normalized_business_key)` 做稳定去重，只保留首次出现项。
4. 保留原始请求项数，用于既有单项/批量缺失判定。
5. 将 `duplicate_file_count` 收口为内部 `duplicate_item_count`，日志记录 business type、原始数量、唯一键数量和 trace；业务键按既有脱敏要求输出摘要。
6. 不在本阶段开放 report/weaponry unknown。

#### 测试与验收

- `tests.test_stage1a1_check_task_contract`
- `tests.test_progress_and_check_task`
- 新增三种业务类型的规范化重复键矩阵；
- Mock 三个 Recovery，断言每个唯一键恰好调用一次且顺序稳定；
- 后置无效项场景断言三个 Recovery 和 HTTP transport 全部未调用。

#### 达成效果

无论是否批准 unknown 重试，一个 check-task 请求都不会因重复 reportId/architectureId 连续补发；
批量参数先完整校验，不再产生隐藏的部分副作用。

### 阶段 U3：共享 Guard attempt CAS 与审计基础

#### 工作内容

1. 新增追加式回调 attempt 事件表和索引，迁移只增不删。
2. 为 Guard acquire 增加显式 trigger/trace 审计上下文，默认仍禁止 unknown 重试。
3. 将 report/weaponry 纳入“只有显式 check-task + attempt 快照才可 unknown 重试”的业务白名单，但此时业务 Recovery Source 尚不加载 unknown，因此不会改变路由行为。
4. 在同一事务内完成 attempt 校验、Guard CAS、投影更新、attempt 递增和授权审计。
5. Guard complete、过期冻结和不一致冻结追加对应事件。
6. 保留人工解除表及其既有唯一约束，不迁移、不复用、不覆盖历史。

#### 测试与验收

- 三类业务的初始 Worker acquire 不携带 attempt 时行为不变；
- 任何普通路径传 `allow_outcome_unknown_retry=True` 都被拒绝；
- unknown 三态一致且快照匹配时只有一方取得租约；
- attempt 不匹配、owner 不一致、Guard 缺失/损坏、非终态、非 latest 均网络调用为零；
- 审计插入故障导致事务整体回滚；
- 50 线程相同快照：1 个 acquired、49 个 stale/busy；
- 50 个不同业务键互不串扰；
- release audit 与 attempt event 各自保持追加、唯一且语义独立。

#### 达成效果

共享存储具备按业务显式启用的通用原子能力，但默认 Guard 和所有后台路径仍然 fail-closed。

### 阶段 U4：Report 显式 unknown 恢复

#### 工作内容

1. 扩展 Report Port 的 Candidate/Acquire attempt 快照及构造约束。
2. Recovery Source 读取 report 的 `outcome_unknown` 和 `callback_attempts`。
3. `RecoverReportCallbackSynchronously` 使用首次候选快照获取 Guard，并增加进程内活跃键合并。
4. `SQLiteReportCallbackAdapter` 仅在 reason 为 `EXPLICIT_CHECK_TASK_RECOVERY` 时传递
   failed/unknown 授权和 expected attempt。
5. 保持 Worker 的 `INITIAL_DELIVERY` 调用完全不变。
6. 保持最终 report Callback payload 字节语义不变，不重新生成报告。

#### 测试与验收

- pending、failed、unknown 三类恢复；
- unknown -> success、unknown -> rejected、unknown -> unknown；
- 首轮 unknown 后第二个独立 check-task 形成下一 attempt；
- 50 并发同一 attempt 最多一个 HTTP；
- 同进程 follower 不等待、不滚动读取新 attempt；
- 新 report execution 抢先受理后旧候选 stale，HTTP 为零；
- 空 Callback URL、3xx、连接失败、读取超时继续按现有精确 outcome 分类；
- 公开报告回调 JSON 与既有黄金样例完全一致。

#### 达成效果

report 获得独立、可审计的显式 unknown 补发能力，不影响 weaponry，也不改变报告生成和正常 Worker。

### 阶段 U5：Weaponry 显式 unknown 恢复

#### 工作内容

1. 扩展 Weaponry Port 的 Candidate/Acquire attempt 快照及构造约束。
2. Recovery Source 读取 weaponry 的 `outcome_unknown` 和 `callback_attempts`。
3. `RecoverWeaponryCallbackSynchronously` 使用首次候选快照并增加进程内活跃键合并。
4. `SQLiteWeaponryCallbackAdapter` 仅对显式 check-task 开启 failed/unknown 重试。
5. 保持 INPUT/TABLE、空结果、保留字段、Evidence 和来源数组的公开 payload 无损往返。
6. 不触发字段抽取、AnythingLLM Retrieval、Provided-Evidence 或资源清理。

#### 测试与验收

- 复用 U4 的完整状态、并发、stale 和审计矩阵；
- 覆盖 JSON number、普通数字字符串和前导零字符串的同键去重；
- INPUT、TABLE、仅模板空结果、强制空字段和包含来源证据的 payload 重发前后完全相等；
- 50 并发同一 architectureId 最多一个 HTTP；
- 50 个不同 architectureId 的任务身份、payload、Guard、审计和 transport 不串扰；
- Production Attestation/AnythingLLM readiness 不因本修改被伪造为通过。

#### 达成效果

weaponry 获得与 report/file 同等级的显式 unknown 补发控制面，同时保持武器谱业务 Port 和供应商 Adapter 边界。

### 阶段 U6：组合根、接口契约与文档同步

#### 工作内容

1. 复核生产容器中正常 Worker 和 check-task Recovery 确实引用同一个业务 Callback Adapter/Guard。
2. 复核 Dispatcher 维护线程只有冻结能力，没有显式恢复命令依赖。
3. 在得到明确许可后更新接口文档：
   - report/weaponry check-task 的 at-least-once 副作用；
   - report/weaponry 接收方幂等责任；
   - unknown 不自动重试，但新 check-task 可显式授权；
   - 新任务受理仍在 unknown 期间返回既有 409；
   - 不新增公开参数或内部 ID。
4. 更新模块 README 和新增实施执行记录，明确实现状态、测试证据和未验证边界。
5. 更新黄金契约资产的批准说明和文档哈希；不得只改哈希绕过语义审查。

#### 测试与验收

- `tests.test_dependency_container`
- `tests.test_architecture_boundaries`
- `tests.test_stage0_contract_assets`
- `tests.test_stage1d_weaponry_contract_assets`
- `tests.test_analysis_contract_assets`
- 对接口文档执行关键词复核，确保不再残留“report/weaponry 只能人工解除”的现行表述；历史记录必须标注为历史，不篡改原执行事实。

#### 达成效果

运行接线、公开契约、黄金资产和当前实现一致；内部 owner、attempt、lease、fencing 和审计身份不泄漏。

### 阶段 U7：关闭验收与发布门禁

#### 工作内容

1. 运行定向测试、相邻模块回归、架构门禁和安全全仓回归。
2. 运行 `compileall` 与 `git diff --check`。
3. 使用临时 SQLite 演练旧 Schema 启动、新表创建、重复启动幂等和只增不删升级。
4. 演练 unknown 历史数据：一致事实可由新请求恢复；损坏事实继续冻结。
5. 检查结构化日志不包含完整 Callback payload、lease token 或不必要的业务敏感内容。
6. 发布前只读统计 report/weaponry 的 `sending/outcome_unknown`、过期 lease 和人工解除记录；不自动处理生产数据。
7. 确认所有应用实例版本和策略一致。当前仍只允许项目声明的单实例运行模式。

#### 推荐离线命令

```powershell
venv\Scripts\python.exe -B -m unittest `
  tests.test_task_service `
  tests.test_report_callback_guard `
  tests.test_report_callback_recovery `
  tests.test_weaponry_stage1d6 `
  tests.test_stage1a1_check_task_contract `
  tests.test_progress_and_check_task `
  tests.test_dependency_container `
  tests.test_architecture_boundaries

venv\Scripts\python.exe -B -m compileall -q app tests
git diff --check
```

安全全仓测试必须动态重新发现，并报告：

```text
discovered = 实际发现数
excluded   = 环境/平台/真实服务模块数
executed   = 实际执行数
failures   = 失败数
errors     = 错误数
skipped    = 跳过数
```

不得沿用旧记录中的固定总数，也不得因为故障注入测试输出 ERROR/CRITICAL 日志就误判失败。

#### 达成效果

形成可审计的离线关闭证据和明确发布阻断项，但不把 SQLite 单实例、Fake Callback 或离线并发结果描述为生产 ready。

---

## 6. 文件修改范围

### 6.1 必改生产代码

| 文件 | 计划修改 |
| --- | --- |
| `app/blueprints/llm.py` | check-task 完整预校验、规范化后稳定去重、通用重复计数、内部 trace；公开响应不变 |
| `app/services/llm_service/task_service.py` | attempt 事件表、unknown 显式授权白名单、attempt CAS、原子审计、完成/过期事件读取能力 |
| `app/modules/report/ports/callbacks.py` | Report Candidate/Acquire attempt 快照和强类型约束 |
| `app/modules/report/adapters/callback_recovery.py` | 加载 unknown、attempt 和最小权威投影 |
| `app/modules/report/adapters/callback_guard.py` | 映射显式 failed/unknown 授权、attempt、trigger 和 trace |
| `app/modules/report/application/recover_callback.py` | 首次快照、进程内活跃键合并、显式 unknown 编排和日志 |
| `app/modules/weaponry/ports/callbacks.py` | Weaponry Candidate/Acquire attempt 快照和强类型约束 |
| `app/modules/weaponry/adapters/callback_recovery.py` | 加载 unknown、attempt，并保持公开 payload 无损解码 |
| `app/modules/weaponry/adapters/callback_guard.py` | 映射显式 failed/unknown 授权、attempt、trigger 和 trace |
| `app/modules/weaponry/application/recover_callback.py` | 首次快照、进程内活跃键合并、显式 unknown 编排和日志 |
| `app/modules/analysis/application/recover_callback.py` | 仅在统一审计/trace 所需时做等价接入，不改变已批准 file 语义 |
| `app/modules/analysis/ports/callbacks.py` | 仅在统一内部审计上下文需要时扩展，不改变公开结构 |
| `app/modules/analysis/adapters/callback_guard.py` | 把既有 file 显式授权写入统一追加式审计 |

若实现时发现 report/weaponry 的最小恢复读取必须复制 file 的专用查询，应优先在
`LLMTaskService` 增加通用、只读最小投影方法，不允许继续通过完整请求 payload 或 execution 输入解码来恢复 Callback。

### 6.2 必改测试

| 文件 | 计划覆盖 |
| --- | --- |
| `tests/test_stage1a1_check_task_contract.py` | 三类型请求去重、空成功体、400/404、unknown 路由行为 |
| `tests/test_progress_and_check_task.py` | 既有兼容路径和批量行为回归 |
| `tests/test_task_service.py` | 原子状态转换、attempt CAS、审计、迁移、故障注入 |
| `tests/test_report_callback_recovery.py` | Report pending/failed/unknown、并发、独立后续请求 |
| `tests/test_report_callback_guard.py` | Report latest/lease/fencing/unknown/审计/人工解除隔离 |
| `tests/test_weaponry_stage1d6.py` | Weaponry 恢复、payload 无损、并发、路由和维护线程 |
| `tests/test_dependency_container.py` | Worker 与 check-task 共享同一 Guard，维护线程无显式授权 |
| `tests/test_architecture_boundaries.py` | 不新增跨层反向依赖 |
| `tests/test_stage0_contract_assets.py` | 公共 check-task/Callback 契约黄金资产 |
| `tests/test_stage1d_weaponry_contract_assets.py` | Weaponry 契约资产与文档哈希 |
| `tests/test_analysis_contract_assets.py` | file 既有显式 unknown 语义不回退 |

### 6.3 获得接口确认后才允许修改的文档

| 文件 | 计划修改 |
| --- | --- |
| `docs/接口文档/文件处理和报告生成.md` | report/weaponry check-task 显式 unknown、重复投递和幂等说明；修订“仅 file”文字 |
| `docs/接口文档/知识谱系解析.md` | weaponry 受理 409、check-task 恢复及真实 runner 说明 |
| `app/modules/report/README.md` | Report 当前恢复策略、审计和 Worker 禁止项 |
| `app/modules/weaponry/README.md` | Weaponry 当前恢复策略、审计和生产门禁 |
| `app/modules/tasks/README.md` | 共享 Guard 原子能力与业务显式授权边界 |
| `docs/更新记录/<实施日期>-report与weaponry显式unknown重试执行记录.md` | 实际修改、测试数字、风险、未验证项和发布边界 |
| `docs/更新记录/README.md` | 新执行记录索引 |
| `docs/重构记录/README.md` | 本计划状态由待确认更新为实施中/已完成；不得提前标记完成 |

### 6.4 可能受影响但应尽量不修改

- `app/container.py`、`app/modules/report/composition.py`、`app/modules/weaponry/composition.py`：
  只有依赖身份断言或审计 Reader 需要注入时才改；不得创建第二个 Callback Adapter。
- `app/modules/tasks/application/check_status.py` 与可靠恢复命令原型：本计划不切生产路由；若确需接入，必须另行审查其 callback 状态集合、latest TaskId 重读和空响应 Presenter，不能顺带切换。
- Callback payload 构造、报告生成、武器谱字段执行、AnythingLLM 集成：原则上零修改。

---

## 7. 分层测试矩阵

| 层级 | 场景 | 核心断言 |
| --- | --- | --- |
| Domain/Port | reason 与 attempt 组合 | 初始发送无快照；显式恢复必须有快照；非法组合立即失败 |
| Web Adapter | 等价 ID 重复 | 规范化后只保留首次项；响应和顺序不变 |
| Web Adapter | 后置非法项 | 返回既有 400；所有 Recovery/HTTP 调用为零 |
| Recovery Source | pending/failed/unknown | 只读取 latest 终态及公开 payload；attempt 准确 |
| Recovery Source | payload 损坏 | fail-closed；不重新运行业务任务 |
| Guard | unknown 三态一致 | 事务内取得新 lease、attempt+1、审计一条 |
| Guard | 任一事实不一致 | 返回 frozen/stale；数据库不产生部分更新；HTTP 为零 |
| Guard | 50 同快照并发 | 至多一个 acquired；attempt 只增加一次 |
| Application | 同进程 follower | 立即返回，不等待、不读取新快照、不伪报 replayed |
| Delivery | 2xx/3xx/4xx/5xx/连接失败/读取超时 | 精确映射 success/rejected/definitely-not-sent/unknown |
| Completion | 迟到 fencing | 不能覆盖新 lease 或新 execution |
| Sweep | 过期 sending | 只冻结 unknown 并审计，网络调用为零 |
| Manual release | 与显式重试并发 | 只有一个条件迁移成功；两类审计不混写 |
| Route | 单项/批量缺失 | 既有 404/200 和空响应保持 |
| Contract | HTTP/Callback JSON | 参数、字段、类型、状态码和零字节响应不变 |
| Isolation | 50 不同键 | payload、owner、attempt、Guard、审计、日志 trace 不串扰 |
| Migration | 旧 SQLite | 新表幂等创建；旧任务不自动重放；一致 unknown 可显式恢复 |

---

## 8. 发布、回退与运维边界

### 8.1 发布前置检查

1. 确认 report、weaponry 接收方均已理解并接受重复投递可能性。
2. 确认目标数据库路径，统计 `sending/outcome_unknown` 和异常 owner，不使用开发库结论替代目标库检查。
3. 确认不存在旧版本 Worker 与新版本同时发送同一 Callback。
4. 所有实例必须使用同一版本；当前项目仍只声明 `single_instance`，不能借本修改启动多实例。
5. 历史 unknown 不因部署自动重试，必须等待部署后的新 check-task 请求。

### 8.2 回退策略

- Schema 仅新增审计表/索引，代码回退时不删除、不清空，旧版本可忽略新表。
- 回退代码后 report/weaponry Recovery Source 再次不加载 unknown，现有 unknown 继续冻结。
- 已经成功投递的 Callback 不回滚；已经 unknown 的投递仍按可能送达处理，禁止批量盲重放。
- 不使用 `git reset --hard`、清库或强制修改 Guard 来伪造回退成功。
- 若发现重复投递超出接收方承受能力，停止新的 check-task 流量，保留 Guard、attempt 与审计现场，再决定是否回退。

### 8.3 监控与日志

至少统计：

- 按业务类型的 check-task 请求数、唯一键数、重复项数；
- unknown 显式授权次数；
- 授权后的 success/rejected/definitely-not-sent/unknown/stale 数量；
- busy、attempt stale、owner 不一致和审计写失败数量；
- 同一业务键短时间内连续显式授权次数，用于识别调用方重试风暴；
- Guard 过期冻结和人工解除数量。

日志不得输出完整 Callback payload、lease token、完整审计事件或不必要的文件路径。内部 TaskId、
fencing 和 trace 只用于服务端结构化诊断，不得进入 HTTP、Callback、Progress、SSE 或 WebSocket。

---

## 9. 风险与停止条件

出现下列任一情况必须停止实施并确认：

1. report 或 weaponry 接收方不能保证重复业务结果幂等。
2. 需要新增前端参数、Callback 字段或改变 HTTP 状态码才能区分授权。
3. 现有接口文档对单项/批量缺失、无效后置项或重复 ID 的解释与本文推荐口径冲突。
4. Recovery 必须重新运行报告生成、字段抽取或访问 AnythingLLM 才能构造 payload。
5. 无法在同一事务中同时写 Guard、attempt、投影和授权审计。
6. 正常 Worker 或维护线程必须使用 unknown 重试权限才能通过测试。
7. 发现生产存在多实例或旧 Worker，且没有共享数据库一致性和唯一 owner 证据。
8. 历史 unknown 的 execution、latest projection、Guard owner 或 payload 不一致。
9. 定向测试只能通过放宽 latest、lease、fencing、attempt 或审计失败策略。
10. 需要修改本文列举之外的公开接口、Callback、Progress、SSE 或 WebSocket 契约。

---

## 10. 最终达成效果

完成并通过发布门禁后，系统将具备以下能力：

1. 一个 `/llm/check-task` 请求中，无论同一 reportId/architectureId 使用多少种等价 JSON 表示，
   每个规范化业务键最多触发一轮回调恢复。
2. report 和 weaponry 可以像 file 一样，由**新的** check-task 请求明确选择 at-least-once
   unknown 补发，而不是依赖人工改库或把后台 Worker 全局放开。
3. 同一 attempt 的并发请求通过 SQLite CAS、lease 和 fencing 收敛为一个发送 owner；跨业务键互不影响。
4. 旧 execution、损坏事实、过期 lease 和审计失败全部 fail-closed。
5. 每次高风险 unknown 重试均有持久授权和结果审计，可用于事故追踪、重试风暴识别和未来 MySQL/可靠队列迁移。
6. 正常 Worker、后台维护和未来队列消费者继续遵循 unknown 不自动重试的安全边界。
7. 前端请求参数、HTTP 响应、Callback payload 及所有公开内部身份保持不变。
8. 该成果仍是 SQLite 单实例控制面的增强，不等同于 exactly-once、可靠队列、多实例一致性或生产高并发验收。
