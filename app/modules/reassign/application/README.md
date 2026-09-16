# 分类节点变更应用层

阶段 1E-4/1E-4R 已实现并修正 `DocumentReassignmentService` 与显式注入的
`ReassignmentExecutionSettings`；阶段 1E-5/1E-5R 新增并修正 `RecoverReassignmentOperation`。
两个服务只依赖本模块 `domain/`、`ports/` 与 Python 标准库，不引用
`ReassignmentPortBundle`、SQLite、AnythingLLM Client、Flask 或组合根。阶段 1E-6/1E-7 由模块外的
`composition.py` 把它们装配为唯一 `ReassignApplicationServices`，并由 `/llm/reassign` 路由调用；
Application 本身仍不知道 HTTP、Container 或具体基础设施。

仅需要远端迁移的 `execute()` 才从请求级 Knowledge Factory 取得新的 Port，并按“本地短事务
→ 外部原子调用 → 本地短事务”执行。远端正常路径为：保留同文档执行权、来源解绑、目标
workspace 复用或准备、目标挂载、带审计意图的 Pin best-effort、条件 CAS 成功提交；空
`doc_path` 在创建 Knowledge Port 前进入 local-only 兼容分支。
新 mapping 的目标 workspace 创建前必须先取得持久化的按目标分类唯一 claim；mapping、prepare
Step 成功事实和 claim 释放在同一事务中提交。远端创建结果未知，或远端成功但 mapping 未能提交
时保留 claim；后一场景额外持久化准确 slug、三态归属和脱敏审计事实，供 1E-5 恢复使用。

`DocumentReassignmentService` 与 `RecoverReassignmentOperation` 必须遵守：

- 不读取环境变量、不创建线程、不创建 HTTP Client；
- 每次外部写前先通过 Repository 持久化写意图；
- 目标 architecture 没有本地映射时，必须在创建 workspace 前取得持久化的同目标准备权；
  同一目标的其他 Operation 只能等待、复用已提交映射或在过期接管后继续，不能并发发出第二次创建；
- 每次结果未知先探测并保留现场，不能盲目重放；
- 既有 mapping 必须按已持久化 slug 查回，不重新应用当前版本的确定性名称规则；
- 远端步骤边界续租；执行设置强制 `lease >= 远端总预算 + 显式安全余量`，续租同时延长活动 claim；
- 远端预算从 Application 收到命令时起算，Factory 创建 Adapter 时必须扣除前置本地事务与锁等待耗时；
- 初始保留、续租和恢复接管必须在取得 Repository 写事务后计算到期时间，避免等待提前消耗安全余量；
- 终态提交异常或返回契约错误后先重读权威 Operation；预期终态已提交时返回真实结果，否则继续隔离；
- 只有取得与目标终态匹配的 `ReassignmentTerminalEvidence` 才能释放文档保护；
- 公开失败只能选择接口文档批准的 `ReassignmentPublicMessage`，不能传入异常正文；
- 在写意图、调用、探测、补偿、lease/fencing 与终态边界记录脱敏结构化日志；
- 不生成 Flask Response，不泄露内部 Operation/Step/lease/fencing 信息。

## 1E-5～1E-7 恢复边界

`RecoverReassignmentOperation` 只接受精确 `operation_id`、预期 fencing token、操作者与原因码。
它先在短事务内接管过期 lease，再在 UoW 外探测本地权威行、目标 workspace 与两侧成员关系；任何
无法确认的结果都会保留为 `recovery_required`，不会按猜测重放外部写。

补偿严格按“先删除目标绑定、再恢复来源绑定”执行。每笔补偿写前持久化独立 Step 意图，写后再次
探测；若进程在 HTTP 返回与 Step 检查点之间退出，下一次恢复只能依据探测补齐该检查点，不能盲发
第二次请求。成功、失败和补偿终态均需先追加当前 fencing 的恢复观测，Repository 会重新核对
本地事实、成员关系和完整前向成功前提后才释放同文档保护。

恢复服务不启动后台线程、不批量扫描 Operation，也不删除永久 workspace。按持久化 slug 查回时，
Adapter 返回的引用必须与请求 slug 大小写无关地一致；不一致会在任何成员探测或补偿写之前进入
隔离。接管、续租及同步返回的 claim 还必须通过强类型和 operation/owner/token/fencing/目标身份
复核，数据库读取异常与明确不存在保持不同内部分类。

人工诊断脚本默认只读；真正写入恢复时必须显式提供上述四项审计/并发参数。仅已成功收口的恢复
返回进程退出码 0，未找到、未接管和仍待恢复分别返回稳定非零退出码。

1E-7 已将恢复实际算法拆到四个独立文件，且不再通过 Facade 绑定方法的 callback 转发：

- `recovery_observer.py` 直接拥有本地/远端观察、远端调用前 lease 续租和观察事实写入；
- `recovery_checkpoints.py` 直接拥有前向/prepare/补偿检查点收敛与纯领域补偿决策；
- `recovery_compensator.py` 直接拥有“先目标解绑、后来源恢复”的有序写意图、结果检查点和写后复探测；
- `recovery_finalizer.py` 是唯一执行恢复终态提交或 `recovery_required` 隔离的协作器。

`recovery_facade.py` 中的 `RecoverReassignmentOperation` 仍是唯一公开 Application 外观，只保留命令校验、
过期 lease 接管和 local-only/远端流程选择；`recover_reassignment.py` 只为既有 Python 导入路径重新导出
该外观。协作器不保存 Operation、客户端或全局可变状态，网络调用仍发生在短事务外，所有写入继续由
lease/fencing 保护。
