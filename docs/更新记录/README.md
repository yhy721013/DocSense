# 更新记录目录说明

本目录保存已经实施、正在实施或需要长期追溯的设计决策与改造记录。它用于解释“为什么这样改、改动范围、验证方式和剩余边界”，但不替代接口文档的对外契约地位。

## 文件对话相关文件

| 文件 | 作用 |
| --- | --- |
| `260401-文件对话接口后端设计.md` | 文件对话后端的早期设计说明。 |
| `260405-文件对话接口实现计划.md` | 早期文件对话实施计划。 |
| `260424-文件对话接口需求变更.md` | 文件对话需求调整记录。 |
| `260503-文件对话接口需求升级.md` | 文件对话能力升级记录。 |
| `260510-对话历史结构改造.md` | 对话历史本地结构演进说明。 |
| `260523-对话历史文件原名支持.md` | 历史消息中文件原名支持说明。 |
| `260603-AnythingLLM并发过载问题修复.md` | AnythingLLM 并发问题的修复记录。 |
| `260705-文件对话新增接口实现计划.md` | 新增标题/中断等接口的实现计划。 |
| `260715-阶段1A-1接口契约基线执行记录.md` | check-task/Progress 当前与目标契约分层、离线测试结果，以及已确认的严格 params/action 错误连接策略。 |
| `260715-阶段1A-2模块骨架与架构边界执行记录.md` | tasks/Flask Adapter 包骨架、AST 导入门禁、规则自证、完整回归结果与 1A-3 后续边界。 |
| `260716-阶段1A-3内部任务契约执行记录.md` | 不可变 Task/Progress DTO、三类 Port、两个应用服务、Fake 契约测试、波次 1A 门禁与完整回归。 |
| `260716-阶段0与1A审查修正记录.md` | WebSocket/SQLite 基线工具修正、回调持久化一致性、连接有界缓冲、正向架构白名单、latest-wins TASK-09，以及当时的 TASK-10 异步方案；该方案已被 2026-07-17 的甲方同步保留口径取代。 |
| `260716-阶段1B-1可靠恢复命令边界执行记录.md` | check-task 共享请求 DTO、批量原子可靠命令 Port/Application、空响应 Presenter、活动命令复用、Fake 故障测试和未切生产边界。 |
| `260716-阶段1B-2Progress控制面迁移执行记录.md` | `/llm/progress` 无 action 契约切换、类型化应用服务、线程安全 Hub/Adapter、连接级有界缓冲与单写入；并记录全面审查后的同任务 sequence 水位、reportId 统一入站规范化、Barrier 50 线程纠偏与扩大回归。 |
| `260716-阶段1C-0与1C-1报告契约及领域层执行记录.md` | report 当前/目标双基线、不可变 Domain/DTO、HTML/回调/名称纯规则、三项已确认严格 HTTP 400 入站校验、遗留兼容转发、完整离线验证及尚未切换生产执行链的边界。 |
| `260716-阶段1C-2报告应用端口与严格Fake执行记录.md` | Task Command/Progress 与 Report File/Artifact/RAG/Audit/Callback/Dispatcher Port、Submit/Run 无框架 Application、严格 Fake、故障矩阵、完整回归和生产未切换边界。 |
| `260716-阶段1C-3SQLite任务事实与原子受理执行记录.md` | 追加式 execution、Callback Guard 表、SQLite 原子受理/领取/expected TaskId 条件写、report Codec、50 线程 Barrier、事务回滚和生产路由未切换边界。 |
| `260716-阶段1C全面审查修复与并发补强执行记录.md` | 1C-0～1C-3 全面审查后的 128 位 reportId、可选文本兼容、完整 RAG trace、结果投影隔离、stale 收敛、Callback Guard latest/fencing/HTTP 分类、226 项回归和生产未切换边界。 |
| `260716-阶段1C-4报告生产IO与审计Adapter执行记录.md` | 任务级 Artifact、执行时文件处理、多文档 AnythingLLM RAG、审计 Schema v3、原子审计门禁、组合故障注入、扩大回归和生产未切换边界。 |
| `260716-阶段1C-5Callback Guard与资源恢复闭环执行记录.md` | Callback Guard 人工解除追加审计、精确 HTTP outcome、终态权威 Artifact 所有权、CAS cleanup/quarantine 恢复、并发/崩溃故障测试、825 项安全回归和生产未切换边界。 |
| `260716-阶段1C第二轮全面审查风险修复执行记录.md` | 1C-0～1C-5 第二轮审查后的发送前权威复核、即时 409、Artifact 完整性、AnythingLLM 未知副作用隔离、逐事件清理恢复、有界扫描、836 项安全回归及后续硬门禁。 |
| `260717-阶段1C-6Dispatcher组合根与路由切换执行记录.md` | SQLite accepted 持久积压、Event 常量空间唤醒的报告执行 Worker、启动/周期资源恢复、60/90 秒清理边界、组合根生命周期、报告 202/409 薄路由切换、Progress 原子 latest Guard 及生产未部署边界。 |
| `260717-阶段1C-6全面审查风险修复执行记录.md` | 1C-6 全面审查后的真实关闭语义、许可等待停机取消、稳定 FIFO、毒任务/坏资源冷却、隔离维护线程、按键 Progress 锁、跨进程单实例门禁、测试离线隔离和 871 项安全回归。 |
| `260717-阶段1C-7阶段关闭验收执行记录.md` | 阶段 1C 最终契约/并发/故障/架构验收、遗留 Report Worker 三类引用证据、永久 AST 隔离门禁、358 项定向及 872 项安全回归，以及阶段 2～6 输入清单。 |
| `260717-阶段1C全面审查问题修复与同步回调加固执行记录.md` | 阶段关闭后全面审查修复：同步 check-task 与主链共用 Callback Guard、过期扫描、严格 2xx、下载/审计/资源/Dispatcher 补强、验证结果及生产边界。 |
| `260718-阶段1D-0契约资产与Evidence校准执行记录.md` | 阶段 1D-0 的契约、黄金资产、只读分数校准和生产阈值停止门禁。 |
| `260718-阶段1D-0R检索质量修复执行记录.md` | 纠正两项校准地面真值，落地专用 Query、MHTML/Chunk 去噪、额外信号 Selection，并完成随机临时资源的真实嵌入、复校准、补偿清理和生产停止门禁。 |
| `260718-阶段1D-1武器谱领域模型与纯规则执行记录.md` | weaponry 四层骨架、深冻结 DTO、Query/Candidate/Selected Evidence/Prompt 类型隔离、来源/TABLE/Callback 纯规则、模式 1 删除、遗留模式 2 兼容转发、969 项安全回归及生产未切换边界。 |
| `260718-阶段1D-2请求适配文档范围与任务Codec执行记录.md` | 未绑定路由的请求 Parser/Presenter、只读 Document Scope Port/Adapter/严格 Fake、不可变 execution 输入、Schema v1 Codec、原子受理、50 同键/不同键隔离、1002 项安全回归及生产未切换边界。 |
| `260718-阶段1D全面审查问题修复执行记录.md` | Evidence 全量保留、TABLE 强行身份、完整 Schema v1、ArchitectureId 单一规范化、成功终态 CAS 前完整性、接口/计划同步及 1014 项安全回归。 |
| `260718-阶段1D-3A供应商无关端口与严格Fake执行记录.md` | Retrieval/Extraction/Auxiliary/Translation/Audit/Callback/Resource/Dispatcher 供应商无关 Port、稳定调用身份、严格故障 Fake、50 线程结构隔离、190 项定向和 1034 项安全回归。 |
| `260718-阶段1D-3B生产IO适配与Schema-v2执行记录.md` | 唯一 Schema v2、score/rank Selection、任务级 AnythingLLM Retrieval、Provided-Evidence Extraction、可拔除术语、Translation、SQLite Audit/Resource、创建后登记补偿、结果未知、50 任务隔离及生产未切换边界。 |
| `260718-阶段1D-4应用用例与严格Fake执行记录.md` | 原子 Submit、只按 TaskId 的 Run、字段/来源编排、expected TaskId 单终态、资源现场保护、严格 Task/Progress Fake、INPUT/TABLE/失败黄金 Callback、50 个在途任务及慢 I/O 隔离、1086 项安全回归与生产未切换边界。 |
| `260719-阶段1D-4全面审查问题修复执行记录.md` | 1D-4 完成后的 Query 隐藏限制、精确“未找到”哨兵、Audit 三态防重放、清理意图先行、终态 tracking 恢复、AnythingLLM 409 unknown、文档身份一致性补强，183+40 项定向及 1095 项安全全仓回归。 |
| `260719-阶段1D-5通用Dispatcher配置与离线组合根执行记录.md` | 业务无关持久扫描 Dispatcher/进程锁、Report 薄包装、Weaponry 单执行 Worker、严格配置、固定策略、隔离维护线程、内部错误分类、离线组合根、203+40 项定向及 1116 项安全全仓回归。 |
| `260719-阶段1D-6CallbackGuard资源恢复与公开路由切换执行记录.md` | Weaponry 真实 Callback Guard、同步 check-task 恢复、资源有界恢复、生产组合根、公开 202 空体薄路由、50 并发、215+40 项定向和 1130 项安全全仓回归；真实 AnythingLLM 运行门禁因环境不可用待补测。 |
| `260720-阶段1D-7前直接阻塞项修复执行记录.md` | 只处理 1D-7 直接前置：持久 Creation Intent 与崩溃隔离、HTTP 租约预算、readiness/production attestation 门禁、降级诊断、缺失 execution 资源隔离；file 运行时未改，仅同步 1F/1H 计划。 |
| `260720-阶段1D-7关闭验收执行记录.md` | 阶段 1D 的开发分支代码与离线关闭验收：永久 AST/配置门禁、I01～I07 证据、遗留 Worker 与 Terms 三类引用清单、完整安全回归，以及真实 AnythingLLM 生产证明仍待实机补齐的边界。 |
| `260720-阶段1D全面审查修复与真实门禁补强执行记录.md` | 1D 关闭后全面审查修复：真实 Source URL/入库文件身份、FIFO 共享限流、Schema v2 有时效证明、生产 fail-fast、Callback/资源人工处置审计、真实只读 8/8 验证及 1165 项安全回归。 |
| `260722-武器谱创建意图恢复竞态修复执行记录.md` | 修复 `/llm/weaponry` 活跃 Worker 与创建意图维护器竞态：运行实例归属、恢复 claim/租约/fencing、旧 SQLite 表原位迁移、回归证据与多实例剩余边界。 |

其余文件记录知识库、日志、运行时路径、技术选型等相关演进，阅读时应按业务主题选择。

## 与重构记录的关系

当前有效的 AnythingLLM、文件对话、低耦合、高并发、任务隔离和可靠队列重构计划已迁移至 `docs/重构记录/`，统一索引与阅读顺序见 `docs/重构记录/README.md`。本目录继续保存已经实施的功能变更、问题修复和历史演进记录。

重构计划落地后，应在本目录新增实施记录，写明实际修改文件、测试结果、发布/回滚过程和剩余风险，并回链对应的 `docs/重构记录/` 文档。

## 维护规则

- 新记录应写明日期、范围、影响接口、数据迁移策略、验证命令和未解决风险。
- 旧数据处理策略必须明确；开发阶段若允许清空旧数据，也应在记录中说明。
- 不得以更新记录替代接口确认流程，不得擅自增删任何前后端接口参数，尤其不得改变已经冻结的 `/llm/chat*` 契约。
