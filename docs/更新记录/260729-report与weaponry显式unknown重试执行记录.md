# Report 与 Weaponry `check-task` 显式 unknown 重试执行记录

## 1. 变更范围

本次改造按照
`docs/重构记录/260729-report与weaponry-check-task显式unknown重试实施计划.md`
的 U0～U7 顺序执行。负责人已在 U0 明确确认：report 与 weaponry 均采用“每个新的
`/llm/check-task` 请求可对每个规范化业务键显式授权至多一轮 at-least-once unknown 补发”；
接收方按业务键与业务结果幂等；整批参数在任何回调副作用前完整校验；单项 404 与批量 200
继续按原始 `params` 项数判断。

公开 HTTP、Callback、Progress、SSE 和 WebSocket 的参数、字段、Header 与状态码均未增删。
本次变化仅是已经确认的回调恢复副作用语义。

## 2. 实际实现

### 2.1 请求边界

- `/llm/check-task` 先解析、校验并规范化全部 `params`，再执行任何查询或回调恢复；后置非法项
  不会留下前置合法项的部分副作用。
- file、report、weaponry 统一按 `(businessType, normalizedBusinessKey)` 稳定去重，只处理请求中
  首次出现的等价键。
- 单项 404/批量 200 仍按原始请求项数决定，成功响应继续是严格空体。
- 每次请求生成仅供服务端审计的 trace；内部身份不会出现在公开协议中。

### 2.2 原子授权与追加审计

- `LLMTaskService` 加法创建 `callback_delivery_attempt_events` 及查询索引，不删除、不重建旧表。
- 显式授权事件、Callback Guard CAS、execution/投影更新和 `callback_attempts + 1` 位于同一
  `BEGIN IMMEDIATE` 事务；审计插入失败会整体回滚。
- 初次发送、显式 check-task 授权、完成、租约过期冻结及 Guard 不一致冻结均有追加事件；
  人工解除继续使用独立审计表，不与 attempt 事件混写。
- 只有固定 `explicit_check_task_recovery` trigger、file/report/weaponry 白名单及首次读取的 attempt
  快照共同满足时，unknown 才可能取得新发送权。普通 Worker 和维护线程不能使用该能力。

### 2.3 Report 与 Weaponry

- 两类 Recovery Source 均只读取 latest 终态的最小恢复投影、公开 Callback payload 和 attempt
  快照，不重新运行报告生成、字段抽取或 AnythingLLM 业务处理。
- 两类同步恢复用例均按业务键合并同进程并发竞争；跨线程和未来跨实例裁决仍由 SQLite CAS、
  lease 与 fencing 完成。
- unknown 可以经新的独立 check-task 请求再次显式授权；若前一轮得到 rejected，下一次新请求
  可以形成下一 attempt。一次请求内的等价 ID 不会连续形成多轮补发。
- Weaponry 的公开 Callback payload 继续无损往返；Report 与 Weaponry 的 2xx/3xx/4xx/5xx、
  连接失败和读取超时分类规则不变。

### 2.4 组合根与维护边界

- Weaponry 继续由组合根强制 Runner、check-task 和 Guard 维护共享同一 Callback Adapter。
- Report 新增同等级的只读依赖身份与容器 fail-fast 断言，阻止未来装配第二套发送权。
- Guard 维护线程只冻结过期 `sending` 为 unknown，不调用同步恢复用例、不发送网络请求。

## 3. 接口与文档同步

依据 U0 的明确确认，已更新接口文档目录说明、报告/文件统一接口文档及知识谱系解析文档，
并同步 Stage 0、Stage 1D 与 Stage 1F 黄金资产。文档明确：

1. 三类业务的 unknown 都不是后台自动重试；
2. 新的 check-task 请求是显式 at-least-once 授权；
3. 接收方必须按对应规范化业务键和业务结果幂等；
4. 整批先校验、请求内去重、原始数量决定 404/200；
5. 既有提交接口在 sending/unknown 未收敛期间仍返回原 HTTP 409。

`docs/接口文档/文件处理和报告生成.md` 当前跨平台规范化 SHA-256 为
`A565F7ED512CDC81CD5ECCECA7AD58C082AB0D672937F6514A895106C1451F84`。

## 4. 分阶段验证

| 阶段 | 结果 | 主要证据 |
| --- | --- | --- |
| U0 | 通过 | 四项公开语义和基线明确确认 |
| U1 | 通过 | 6 个目标测试先行，5 个按预期失败、1 个兼容语义通过 |
| U2 | 通过 | 36 项路由/契约测试 |
| U3 | 通过 | 75 项 Guard、attempt、事务审计与并发测试 |
| U4 | 通过 | 68 项 Report 恢复、Guard、并发与契约测试 |
| U5 | 通过 | 68 项 Weaponry 恢复、Fake、隔离与契约测试 |
| U6 | 通过 | 85 项组合根、架构边界及三套黄金资产测试 |
| U7 | 按本次范围完成并离线关闭 | 262 项定向回归；动态发现 2,055、排除 13、执行 2,042、成功 2,040、失败 0、错误 0、跳过 2；迁移/损坏事实、compileall、JSON、文档摘要和 diff 门禁通过；负责人确认本次不执行真实环境发布门禁 |

所有验证均使用项目 `venv`、临时 SQLite、严格 Fake 或受控离线 Transport；没有运行 `run.py`，
没有连接真实 Callback、AnythingLLM 或模型服务。

## 5. 数据迁移与回退

- Schema 为加法、幂等创建；旧版本代码可忽略新事件表。代码回退不删除新表、不清空审计。
- 历史 unknown 不会因升级自动重放，只有部署后的新 check-task 请求才可能显式授权。
- 回退后已经成功或可能送达的 Callback 不回滚；禁止批量盲重放或强制修改 Guard。
- 当前证明范围仍是 SQLite 单实例。多实例需要共享数据库事务、可靠队列、消费者租约和跨实例
  fencing 的后续实现与独立验收。

## 6. 发布前仍需满足

- report/weaponry 接收方生产实现需确认幂等能力和重复投递监控。
- 目标数据库需只读统计 sending/unknown、过期 lease、异常 owner 和人工解除记录。
- 所有运行实例需使用同一版本，旧 Worker 必须停止或隔离。
- Weaponry Production Attestation 与真实供应商协议门禁仍须在受控集成环境完成。
- 本记录中的离线并发与 Fake Callback 结果不构成生产 ready 或 exactly-once 证明。

负责人已确认本次只完成代码与离线验收，不执行真实环境发布门禁。因此本次没有读取任何生产
数据，也没有用开发机 `.runtime` 数据冒充发布证据；该范围现已离线关闭。未来实际发布时仍须
重新提供并确认目标任务 SQLite、实例清单与旧 Worker 状态，补录只读统计，并在所有实例版本
一致、旧 Worker 停止且 Production Attestation 通过后，才能宣称真实发布门禁关闭。
