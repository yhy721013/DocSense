# 阶段 2 资产目录

本目录保存阶段 2 编码前和实施期间需要长期维护的内部基线、所有权矩阵、恢复注册表与关闭证据。
这些资产不定义任何公开 HTTP、Callback、SSE 或 WebSocket 合同；公开合同始终以
`docs/接口文档/` 为唯一权威。

## 当前资产

| 文件 | 作用 |
| --- | --- |
| `260812-阶段2-0当前代码Schema调用链与所有权基线.md` | 冻结阶段 2 开工前的 Git、Schema、调用链、运行拓扑和过渡所有权事实。 |
| `260812-阶段2-0数据库路径身份预检归档与回滚基线.md` | 冻结 v2 路径、数据库身份、旧库只读预检、文件集归档与回滚算法。 |
| `260812-阶段2-0服务职责调用方与SQLite唯一写入者矩阵.md` | 覆盖全部 `app/services` 文件，并为当前/计划 SQLite 表冻结唯一物理 Writer 与退出阶段。 |
| `260812-阶段2-0RuntimeConfig归属矩阵.md` | 逐键冻结 Task/Report/Analysis/Weaponry 配置默认值、校验、目标所有者、日志和样例文件状态。 |
| `260812-阶段2-0CallbackKnowledge与Web边界冻结.md` | 冻结 Callback History 非权威语义、Knowledge 阶段 3A 输入和保留的 Web 协议边界。 |
| `260812-阶段2-0直接父节点专项独立阻塞边界.md` | 将未决接口/目录决策封存在直接父节点专项，保证不混入阶段 2 通用内核。 |
| `260812-阶段2-0三业务Step与恢复注册表.md` | 逐业务冻结最终 Step、恢复矩阵、Intent/Checkpoint/ExternalRef、幂等身份与 Canonical Input Profile。 |
| `260812-阶段2-0Executor事务时钟租约与受理解锁拓扑.md` | 冻结统一 Executor、分层公平容量、窄 UoW、Clock/owner/租约不等式及新受理/解锁矩阵。 |
| `260812-阶段2-0完成验收记录.md` | 汇总十步完成状态、37 项自动验证、旧库只读预检与阶段 2-1 入口条件。 |
| `260812-阶段2-1完成验收记录.md` | 汇总纯 Domain/Port/Fake、共享领域路径迁移、授权门禁修复、156 项聚合与 2255 项全量离线验收。 |
| `260812-阶段2-2根Schema与组件Manifest设计.md` | 冻结根 fingerprint、组件注册/安装、实际 SQLite 精确核验、内部 Port 补全、Control Store 实施结果及已确认的按业务切换边界。 |
| `260812-阶段2-2完成验收记录.md` | 汇总 Schema/Bootstrap、UoW、Control Store、双连接 fencing、方案 A 切换顺序、154 项聚合与 2314 项全量离线验收。 |
| `260813-阶段2-3ExecutionRuntime与AuthoritySession设计.md` | 冻结完整 Authority、claim/start/heartbeat、协作停止、本地执行器、公平容量与保守 Reaper 边界。 |
| `260813-阶段2-4Report试点设计.md` | 冻结 Report v2 Input/Profile/Step/UoW、Callback Control、资源维护和一次切换方案。 |
| `260814-阶段2-5Weaponry迁移设计.md` | 冻结 Weaponry v2 快照、组件事实、字段 Step、结果/终态、Callback 与生产切换方案。 |
| `260815-阶段2-6Analysis迁移设计.md` | 冻结 Analysis v5 Input/Profile、批内严格顺序、Authority-aware Step/UoW、Callback/恢复、一次切换与 fresh bootstrap 方案。 |
| `260821-阶段2-7业务恢复策略与有限Reaper设计.md` | 冻结三业务恢复策略、有限 Reaper、Recovery Coordinator、检查点续跑、恢复终态与 Callback 原子性及严格运维命令。 |

机器可读内部契约位于 `tests/contracts/stage2_task_execution_contract.json`、
`tests/contracts/stage2_interface_contract_hashes.json` 和
`app/modules/tasks/adapters/sqlite/database_contract.json`、
`tests/contracts/stage2_ownership_contract.json`、
`tests/contracts/stage2_runtime_config_ownership.json`、
`tests/contracts/stage2_boundary_contract.json`、
`tests/contracts/stage2_direct_parent_scope.json`、
`tests/contracts/stage2_business_step_registry.json`、
`tests/contracts/stage2_runtime_topology_contract.json`、
`tests/contracts/stage2_analysis_component_contract.json`、
`tests/contracts/stage2_analysis_v2_contract.json` 和
`tests/contracts/stage2_recovery_runtime_contract.json`；对应测试固定状态机、Authority、恢复、Canonical
Profile、公开接口哈希、数据库切换门禁、文件/表所有权、配置归属、跨层边界、专项隔离、三业务
Step/Runtime 拓扑，以及 Analysis 的组件身份和最终生产切换边界。

阶段 2-2 根 SQLite 的逐表语义 Manifest 位于
`app/modules/tasks/adapters/sqlite/root_schema_manifest.json`；它是生产 Bootstrap 与严格打开共同加载的
唯一根 Schema 真相源。测试侧数据库组合治理契约另外固定其规范化字节数和指纹，防止生产实现与验收资产
各自维护一份 Schema。

## 维护规则

1. “当前事实”与“目标设计”必须分开记录；未经代码和测试证明的设计不得标记为已实现。
2. 每张静态数据库表必须只有一个权威写入者；迁移期兼容入口只能单向委托，禁止双写和循环调用。
3. 每个阶段 2 波次完成后，更新实际所有者、已删除入口和仍保留的回滚点。
4. 离线 Windows、临时 SQLite、Fake/Mock 证据不能扩张为生产、多实例、可靠队列或容量结论。
