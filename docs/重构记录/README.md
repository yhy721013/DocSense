# 重构记录目录说明

本目录集中保存项目当前有效、需要持续演进和长期追溯的重构计划、架构决策与执行级设计。它用于说明重构目标、依赖顺序、实施门禁和验证方式，但不替代 `docs/接口文档/` 的公开契约地位。

## 文档索引

| 文件 | 层级与作用 |
| --- | --- |
| `260703-AnythingLLM低耦合改造总计划.md` | 基础专项：AnythingLLM Transport、Client、Port、Gateway、Factory 和遗留调用收口。 |
| `260707-文件对话功能改造计划.md` | 基础专项：文件对话阶段 1～12、冻结契约、领域状态、持久化与调度边界。 |
| `260715-低耦合高并发任务隔离与可靠队列改造总计划.md` | L1 总计划：统一规划低耦合重构、共享持久化、可靠队列、FastAPI、多实例和 50+ 并发路线。 |
| `260715-其余业务分层统一改造实施计划.md` | L2 专项计划：模块化单体选型、剩余接口分层统一和阶段 1 的 1A～1H 波次。 |
| `260715-阶段0契约容量与基础设施决策清单.md` | L3 决策与执行清单：契约、性能、容量、基础设施、数据、安全及已确认约束。 |
| `260715-阶段0执行记录.md` | L3 执行结果：阶段 0 有条件关闭结论、离线资产、SQLite 盘点、目标拓扑、安全基线和延期压测门禁。 |
| `260715-统一任务最小契约与Progress语义预设计.md` | L3 跨阶段契约：统一任务、可靠回调恢复命令、保守投递策略、latest-wins 和 Progress 内部语义。 |
| `260715-阶段1A-1B文件级实施设计.md` | L3 文件级设计：波次 1A、1B-1 可靠命令边界与 1B-2 Progress 控制面迁移均已完成；原“阶段 6 异步替换 check-task”设计已被 2026-07-17 的同步保留口径取代。 |
| `260716-阶段1C至11滚动实施计划.md` | L2/L2.5 滚动计划：细化 1C～11 子波次、依赖、门禁、回滚、无业务积压数量上限、同步 Repository、测试 Compose、callback Guard 和保留周期延期。 |
| `260716-阶段1C报告生成文件级实施设计.md` | L3 文件级设计：报告原子 409、追加式 execution、按 task ID 恢复、持久化积压/有界唤醒、多文档 RAG/File/Artifact/Callback Port、latest-wins 和完整验收矩阵；1C-0～1C-7 已完成并关闭阶段。 |
| `260717-阶段1D武器谱文件级实施设计.md` | L3 文件级设计：1D-0/0R～1D-7 的开发分支代码与离线验收已关闭；公开路由、永久 AST/配置门禁、I01～I07 证据和遗留引用清单均已完成。localhost 实例已完成只读 score 与来源身份 8/8 复核；有时效 production attestation 仍因四类生产指纹未冻结而待生成，未通过前不得标记为 production ready。 |
| `260727-武器谱术语目录自动同步实施设计.md` | L3 专项设计：以自动内容指纹、版本化 workspace、启动门禁、未知结果隔离和多代只读路由恢复本地术语卡上传，不把外部写入放回单任务请求链。 |
| `260727-文件对话首次空文件自动全量范围实施计划.md` | L3 已完成专项计划：阶段 0～7 已完成契约黄金基线、全量目录快照、纯领域候选 DTO、受理事务原子选择、执行/绑定/历史等价、日志/调试一致性、离线全面回归及最终关闭验收；公开 `/llm/chat` 已启用首次空 `fileNames` 的全量不可变快照，参数与 SSE 契约不变。证据限定为 SQLite 单实例离线环境。 |
| `260727-文件对话请求范围与活动范围分离实施计划.md` | L3 已确认、待实施的后续语义修正：分离前端 Requested Scope、持久 Active Scope、本轮 Effective Scope 与 Workspace 累计绑定；首次空数组冻结一次全量范围，任意非空数组整体替换活动范围，后续空数组保持最后显式范围，history 只展示前端本轮显式文件。公开字段结构不变，开发 Chat 数据在切换时停服清理。 |
| `260728-知识谱系类别文件对话详细实施计划.md` | L3 已完成专项计划：阶段 0～7 后追加 2026-07-29 审查修复，当前包含 JavaScript 安全整数、Admission Guard、409 优先、Schema v6 对称约束、有界类别读取、远端绑定唯一确认和 `aborted` 合同补齐。 |
| `260728-知识谱系类别文件对话详细集成计划.md` | L3 部分执行集成计划：开发实现、离线 I0～I4/I6 及 C7 审查修复已完成；未来发布改按 v1/v4/v5→v6、真实绑定回执、409/429 优先级和前端 `aborted` 演练，最新 main 漂移与主线合入仍待执行。 |
| `260725-main与refactor并发重构分支集成实施计划.md` | L3 分支集成计划：以最新 `main` 为第一父基线，将 `refactor/concurrency` 阶段 0～1E 成果集成进主线；逐文件规定 8 个冲突文件、24 个冲突块、Analysis 最新功能保留、SQLite Schema 并集、里程碑、测试、回滚和完成定义。该计划取代 `260722-refactor与analysis优化分支合并实施计划.md` 的旧执行方向。 |
| `260728-main与file-analysis分支Legacy-Office集成实施计划.md` | L3 分支集成计划：把 `main` 的 Legacy Office、单 Sheet XLSX、Report 与离线交付能力集成到 Stage 1F 文件分析唯一生产链；冻结处理策略、失败关闭、共享转换容量、只读存储治理、逐阶段门禁和真实环境停止条件。 |
| `260729-report与weaponry-check-task显式unknown重试实施计划.md` | L3 已完成并离线关闭专项计划：U0～U7 已完成 report/weaponry 显式 at-least-once unknown 补发、三类型请求预校验/规范化去重、Guard attempt CAS/追加审计、业务恢复接入、组合根、接口黄金资产、迁移演练及 2,042 项安全全仓回归；负责人确认本次不执行真实环境发布门禁，目标库统计、实例版本/旧 Worker 核对和 Production Attestation 留待未来实际发布前执行。 |
| `260730-文件分析RAG内部Markdown投影与业务原名上传命名实施计划.md` | L3 已完成专项计划：P0～P7 已落地不含图片 Base64 payload 的 RAG-only Markdown Artifact、Provider 中立上传描述符及资源/审计事实；multipart filename 使用 `<originalFileName主干>.md`，AnythingLLM metadata.title、UI 与 Chunk sourceDocument 使用带原后缀的 `originalFileName`。缺失/空 originalFileName 回退 fileName，非法名称整批同步 HTTP 400，真实 PDF 降级保持 PDF；2,176 项安全全仓回归和 AnythingLLM Desktop 1.15.0-r2 单实例验收通过，尚未部署生产环境。 |
| `260724-阶段1E分类节点变更同步Saga文件级实施设计.md` | L3 文件级设计：保持 `/llm/reassign` 同步契约，通过持久化 Operation/Step/Event/恢复观测、Knowledge Port、SQLite Unit of Work、条件 CAS、写后探测、反向补偿和恢复审计完成 1E 功能闭环；1E-0～1E-7 已完成，真实供应商演练与生产容量校准仍是后续启用门禁。 |
| `260726-阶段1F文件分析高内聚收口文件级实施设计.md` | L3 文件级设计：保持 `/llm/analysis`、file check-task、Progress 和回调结构，将 Analysis 集中实现收口为垂直业务切片；按 TaskId 执行、批量原子受理、SQLite 持久调度、任务级 I/O、Callback Guard、资源恢复、50 并发隔离、切换与回退门禁均已细化。1F-0～1F-7B 代码/离线验收及关闭后全面审查修复均已完成；当前停服清库重建发布不要求对空库重复执行只读预检，保留/恢复存量数据库或清理存疑时仍须通过该门禁。 |
| `260726-阶段1F-3S文件分析Application等价拆分实施计划.md` | L3 已完成计划：在 1F-3R 后、1F-4 前，将 2,162 行 `run_analysis.py` 机械拆分为 473 行 Facade 和五个内部协作模块；公开导出、构造签名、调用顺序、Prompt/预算、异常/日志、审计/知识/终态和副作用语义均由轨迹、故障矩阵和永久 AST 门禁验证。 |
| `260718-阶段1H共享文档处理模块文件级实施设计.md` | L3 修订文件级设计：按最新主线把 Legacy Office 认定为可复用的供应商 Adapter 基础，规划 1H-0～1H-7 建立通用 Artifact/Profile/Lineage、共享 DocumentProcessing Application、独立 Translation 模块、逐调用方切换和永久门禁；1H 正式迁移尚未开始，阶段 3 再切换 MinIO。 |
| `../更新记录/260716-阶段0与1A审查修正记录.md` | 阶段 0/1A 全面审查后的实现修正、验证结果、生产边界及 TASK-09 latest-wins 决策和后置门禁。 |
| `../更新记录/260716-阶段1B-1可靠恢复命令边界执行记录.md` | 阶段 1B-1 的批量原子 Command Port、应用服务、Presenter、Fake、测试结果和生产未切换边界。 |
| `../更新记录/260716-阶段1B-2Progress控制面迁移执行记录.md` | 阶段 1B-2 的无 action 契约切换、类型化 Progress 端口、线程安全 Hub/Adapter、连接缓冲，以及全面审查后的投递水位、reportId 规范化和 Barrier 50 线程验收。 |
| `../更新记录/260716-阶段1C-0与1C-1报告契约及领域层执行记录.md` | 阶段 1C-0/1C-1 的 report 当前/目标双基线、不可变 DTO、纯规则、三项严格 HTTP 400、兼容转发、完整离线验证与未切生产执行链边界。 |
| `../更新记录/260716-阶段1C-2报告应用端口与严格Fake执行记录.md` | 阶段 1C-2 的 Task Command/Progress 与 Report File/Artifact/RAG/Audit/Callback/Dispatcher Port、Submit/Run Application、严格 Fake、故障矩阵和生产未切换边界。 |
| `../更新记录/260716-阶段1C-3SQLite任务事实与原子受理执行记录.md` | 阶段 1C-3 的追加 execution、SQLite 原子受理/领取/expected TaskId 条件写、report Codec、50 线程并发、事务回滚和生产路由未切换边界。 |
| `../更新记录/260716-阶段1C全面审查修复与并发补强执行记录.md` | 阶段 1C-0～1C-3 全面审查后的契约/身份/结果隔离、stale 收敛、Callback Guard latest/fencing、并发补强、226 项回归及 1C-5/1C-6 顺序调整。 |
| `../更新记录/260716-阶段1C-4报告生产IO与审计Adapter执行记录.md` | 阶段 1C-4 的任务级 Artifact、执行时文件处理、多文档 AnythingLLM RAG、审计 Schema v3、原子审计门禁、组合故障测试及生产未切换边界。 |
| `../更新记录/260716-阶段1C-5Callback Guard与资源恢复闭环执行记录.md` | 阶段 1C-5 的 Guard 人工解除追加审计、精确回调 outcome、终态权威 Artifact 所有权、CAS cleanup/quarantine 恢复、并发/崩溃故障测试及生产未切换边界。 |
| `../更新记录/260716-阶段1C第二轮全面审查风险修复执行记录.md` | 阶段 1C-0～1C-5 第二轮全面审查后的 callback、Artifact、AnythingLLM 未知副作用、逐事件资源恢复、并发补强、完整回归与 1C-6/阶段 2～6 硬门禁。 |
| `../更新记录/260717-阶段1C-6Dispatcher组合根与路由切换执行记录.md` | 阶段 1C-6 的 SQLite 持久积压、报告执行 Worker、启动/周期恢复、组合根生命周期、202/409 薄路由切换、Progress 原子 owner Guard 与尚未部署生产边界。 |
| `../更新记录/260717-阶段1C-6全面审查风险修复执行记录.md` | 阶段 1C-6 全面审查后的生命周期、调度饥饿、维护线程隔离、按键 Progress 锁、跨进程单实例门禁、离线测试隔离和 871 项安全回归。 |
| `../更新记录/260717-阶段1C-7阶段关闭验收执行记录.md` | 阶段 1C 最终关闭、遗留 Worker 引用证据、永久 AST 门禁、完整回归、生产边界与阶段 2～6 输入清单。 |
| `../更新记录/260717-阶段1C全面审查问题修复与同步回调加固执行记录.md` | 阶段 1C 关闭后全面审查的 Guard 统一、同步 check-task 恢复、维护扫描、下载/审计/资源恢复/Dispatcher 补强及完整验证。 |
| `../更新记录/260718-阶段1D-0契约资产与Evidence校准执行记录.md` | 阶段 1D-0 的 D01～D05、精确错误矩阵、INPUT/TABLE/术语/隔离/故障黄金资产、离线 Selection Oracle、真实 AnythingLLM 只读分数校准、909 项安全回归及未通过生产阈值门禁的结论。 |
| `../更新记录/260718-阶段1D-0R检索质量修复执行记录.md` | 阶段 1D-0R 的两项地面真值纠正、专用 Query、MHTML/Chunk 去噪、额外信号 Selection、脱敏复校准，以及随机临时资源的真实嵌入、清理和生产 profile 停止门禁。 |
| `../更新记录/260718-阶段1D-1武器谱领域模型与纯规则执行记录.md` | 阶段 1D-1 的四层骨架、不可变 DTO、Retrieval/Evidence/Prompt 类型边界、来源/TABLE/Callback 纯规则、模式 1 删除、遗留兼容、完整审查和 969 项安全回归。 |
| `../更新记录/260718-阶段1D-2请求适配文档范围与任务Codec执行记录.md` | 阶段 1D-2 的未绑定 Parser/Presenter、Document Scope Port/只读 Adapter/严格 Fake、不可变 execution 输入、Schema v1 Codec、原子受理、50 同键/不同键隔离和 1002 项安全回归。 |
| `../更新记录/260718-阶段1D全面审查问题修复执行记录.md` | 阶段 1D 全面审查后的 Evidence 全量保留、TABLE 行身份、完整策略快照、ArchitectureId 一致性、成功终态完整性和 1014 项安全回归。 |
| `../更新记录/260718-阶段1D-3A供应商无关端口与严格Fake执行记录.md` | 阶段 1D-3A 的 8 类供应商无关 Port、稳定调用身份、审计/资源/回调/Dispatcher 契约、严格故障 Fake、50 线程结构隔离和 1034 项安全回归。 |
| `../更新记录/260718-阶段1D-3B生产IO适配与Schema-v2执行记录.md` | 阶段 1D-3B 的唯一 Schema v2、score/rank Selection、任务级 AnythingLLM Retrieval、Provided-Evidence Extraction、可拔除术语、Translation、SQLite Audit/Resource、故障补偿、并发隔离、完整离线验证和生产未切换边界。 |
| `../更新记录/260718-阶段1D-4应用用例与严格Fake执行记录.md` | 阶段 1D-4 的 Submit/Run/字段 Application、expected TaskId 单终态、资源现场保护、严格 Task/Progress Fake、INPUT/TABLE/失败黄金 Callback、50 个在途任务与慢 I/O 隔离、完整安全回归和生产未切换边界。 |
| `../更新记录/260719-阶段1D-4全面审查问题修复执行记录.md` | 1D-4 全面审查后的 Query 无隐藏长度/语义词门禁、精确哨兵、Audit reserve 三态、清理意图先行、终态资源恢复候选、409 unknown 和文档完整位置身份补强。 |
| `../更新记录/260719-阶段1D-5通用Dispatcher配置与离线组合根执行记录.md` | 1D-5 的业务无关持久扫描 Dispatcher/进程锁、Report 薄包装、Weaponry 单执行 Worker、严格配置、固定策略、错误分类、离线组合根、完整回归和生产未绑定边界。 |
| `../更新记录/260719-阶段1D-6CallbackGuard资源恢复与公开路由切换执行记录.md` | 1D-6 的真实 Callback Guard、同步 check-task 恢复、资源有界恢复、生产组合根、公开 202 空体薄路由、50 并发与 1130 项安全回归，以及真实 AnythingLLM 环境未就绪边界。 |
| `../更新记录/260720-阶段1D-7前直接阻塞项修复执行记录.md` | 1D-7 前代码型阻塞的定向关闭：create 前置事实、崩溃只查回/隔离、HTTP 租约、生产证明/readiness、内部诊断与孤儿资源隔离；file 问题只归档至 1F/1H。 |
| `../更新记录/260720-阶段1D-7关闭验收执行记录.md` | 1D-7 的永久静态门禁、I01～I07 验收、遗留 Worker/Terms 引用清单、完整离线回归、生产未部署边界及待补的真实供应商证明。 |
| `../更新记录/260720-阶段1D全面审查修复与真实门禁补强执行记录.md` | 1D 关闭后真实协议与并发/运维补强：来源身份、FIFO limiter、证明 v2、生产 fail-fast、人工处置审计、8/8 只读实测和安全全仓回归。 |

## 推荐阅读顺序

1. 先阅读 `docs/接口文档/`，确定不可擅自改变的请求、响应、回调、SSE 和 WebSocket 契约。
2. 阅读 `260703-AnythingLLM低耦合改造总计划.md` 与 `260707-文件对话功能改造计划.md`，了解已经形成的基础边界。
3. 阅读 `260715-低耦合高并发任务隔离与可靠队列改造总计划.md`，掌握阶段 0～11 的依赖、门禁和最终目标。
4. 阅读 `260715-其余业务分层统一改造实施计划.md`，确定阶段 1 的业务模块、实施波次和完成定义。
5. 开工前核对 `260715-阶段0契约容量与基础设施决策清单.md`，并以 `260715-统一任务最小契约与Progress语义预设计.md` 约束任务、回调和进度语义。
6. 阅读 `260715-阶段0执行记录.md`，了解阶段 0 有条件关闭结论，以及首次完整集成环境和阶段 10 必须补齐的容量/生产门禁。
7. 已完成切片使用 `260715-阶段1A-1B文件级实施设计.md`、
   `260716-阶段1C报告生成文件级实施设计.md` 和对应执行记录回溯；后续总体节奏阅读
   `260716-阶段1C至11滚动实施计划.md`。阶段 1C 已关闭；1D 按
   `260717-阶段1D武器谱文件级实施设计.md`。1D-0 的契约/历史校准资产、1D-0R 本地检索质量
   修复与临时隔离清洗副本验证、1D-1 纯领域波次、1D-2 离线受理边界、1D-3A Port/严格 Fake
   及 1D-3B Schema v2/生产 Adapter、1D-4 Application、1D-5 Dispatcher/配置/离线组合根，以及
   1D-6 Callback/资源/生产装配/公开路由，以及 1D-7 永久门禁、遗留清单和离线关闭验收均已完成；
   localhost 已完成只读 score 与来源身份复核；完整 AnythingLLM production attestation 仍因四类
   生产指纹未冻结而是生产启用硬门禁，但不阻塞后续阶段继续开发。原始文件处理与
   Translator 解耦已移入平级 `260718-阶段1H共享文档处理模块文件级实施设计.md`，不再作为
   1D 的高精度门禁；阶段 1E 的 1E-0 已完成当前/目标双基线与故障资产，1E-1 已完成四层
   骨架、领域 DTO、状态机与补偿纯规则，1E-2 已完成 Port、严格 Fake 与 SQLite 本地事实，
   1E-3 已完成请求级 AnythingLLM Adapter、有限预算和目标 workspace 准备，1E-4/1E-4R 已完成
   Application 前向成功路径、目标 workspace 持久化 claim、恢复现场保留、步骤续租与条件 CAS，1E-5
   已完成显式恢复、补偿、过期 lease 接管、恢复观测和只读诊断脚本；1E-6 已完成组合根、
   公开薄路由、契约黄金回归和永久 AST 门禁，1E-7 已完成四个恢复协作器的实际实现下沉、直接单测
   与 callback-wrapper/规模复杂度门禁。真实供应商能力、预算校准和容量仍须在可用集成环境验收，未通过前
   不得标记 production ready。
8. 将 `refactor/concurrency` 阶段 0～1E 成果集成到最新 `main` 前，必须阅读
   `260725-main与refactor并发重构分支集成实施计划.md`，按其中固定提交、冲突文件、Schema 并集、
   分阶段测试、停止条件和回滚门禁执行。`260722-refactor与analysis优化分支合并实施计划.md` 只作为
   Analysis 语义移植的历史输入，不再作为当前 Git 合并方向。
9. PR #79 合并后的阶段 1F 已完成 1F-0～1F-7B 的代码/离线验收和关闭后全面审查修复。当前发布制度要求每次更新先停服，再由
   `clean.py` 清库重建；只有保留/恢复存量数据库或清理存疑时，才把只读预检作为发布门禁。后续开工前必须阅读
   `260726-阶段1F文件分析高内聚收口文件级实施设计.md`、
   `260726-阶段1F-3S文件分析Application等价拆分实施计划.md` 和
   `../更新记录/260726-阶段1F-4批量原子受理与顺序协调执行记录.md`、
   `../更新记录/260727-阶段1F全面审查修复执行记录.md`、
   `../更新记录/260727-阶段1F-6资源与Callback闭环执行记录.md`、
   `../更新记录/260727-阶段1F-7A切换前隔离与只读门禁执行记录.md`、
   `../更新记录/260727-阶段1F-5ADispatcher配置与离线组合根执行记录.md`、
   `../更新记录/260727-阶段1F-5B唯一生产链路由接线与离线验收记录.md`、
   `../更新记录/260727-阶段1F-7B关闭验收执行记录.md` 和
   `../更新记录/260727-阶段1F关闭后全面审查修复与清库发布收口执行记录.md`，复核其中的契约冻结、差分轨迹、
   TaskId 执行、批量原子受理、资源/回调闭环、Dispatcher 切换前置门禁和回退条件实施。

## 已实施记录

- 阶段 1F-3 的 TaskId-only Application、任务目录/文件/RAG/知识/SQLite 审计 Adapter、审计和
  外部结果未知的 fail-closed 收口、旧分类预算等价、离线回归以及未接生产路由边界见
  `../更新记录/260726-阶段1F-3Application与任务级生产IO执行记录.md`。
- 阶段 1F-3R 的部分会话失败审计、本次 attempt 隔离、活动 Conversation 引用、close 三态幂等、
  stale 前置门禁和精确审计查回修复见
  `../更新记录/260726-阶段1F-3RApplication与任务级IO审查修复执行记录.md`。
- 阶段 1F-3S 的 Application 等价拆分、结构化调用轨迹、Facade/协作器 AST 门禁和完整离线回归见
  `../更新记录/260726-阶段1F-3S文件分析Application等价拆分执行记录.md`。
- 阶段 1F-4 的追加批次 Schema、SQLite 单事务批量受理、请求内/全局调度顺序、提交后有界唤醒、
  并发/回滚验收和生产未切换边界见
  `../更新记录/260726-阶段1F-4批量原子受理与顺序协调执行记录.md`。
- 阶段 1F-6 的新 execution 资源事实、close 意图/CAS/隔离、统一 Callback Guard、同步恢复和生产未
  切换边界见 `../更新记录/260727-阶段1F-6资源与Callback闭环执行记录.md`。
- 阶段 1F-7A 的共享 Application 50 任务隔离、旧 Worker 静态隔离和只读切换预检见
  `../更新记录/260727-阶段1F-7A切换前隔离与只读门禁执行记录.md`。
- 阶段 1F-5A 的严格单实例配置、持久 Dispatcher、组合根生命周期/readiness、同 file 活跃 Callback
  恢复合并与公开路由未切换边界见
  `../更新记录/260727-阶段1F-5ADispatcher配置与离线组合根执行记录.md`。
- 阶段 1F-5B 的公开 `/llm/analysis` 唯一受理链、file check-task 新回调恢复 owner、路由 AST/契约
  离线验收，以及真实目标库发布仍待确认的边界见
  `../更新记录/260727-阶段1F-5B唯一生产链路由接线与离线验收记录.md`。
- 阶段 1F-7B 的定向/安全全仓回归、13 项环境/平台排除、旧执行器引用盘点和文档收口，以及真实发布仍待
  确认的边界见 `../更新记录/260727-阶段1F-7B关闭验收执行记录.md`。
- 阶段 1F 关闭后对 file unknown 显式补发、毒快照、资源统计、Dispatcher 致命故障和清库发布制度的
  修复与证据边界见
  `../更新记录/260727-阶段1F关闭后全面审查修复与清库发布收口执行记录.md`。

- 阶段 1A-1 的实际改动、测试矩阵和已确认的 1B 契约细节见 `../更新记录/260715-阶段1A-1接口契约基线执行记录.md`。
- 阶段 1A-2 的模块骨架、架构规则、自证测试与完整回归见 `../更新记录/260715-阶段1A-2模块骨架与架构边界执行记录.md`。
- 阶段 1A-3 的不可变 DTO、Task/Progress Port、Fake 应用契约、阶段门禁与完整回归见 `../更新记录/260716-阶段1A-3内部任务契约执行记录.md`。
- 阶段 1B-1 的可靠恢复命令 DTO/Port/Application/Presenter、原子 Fake、完整测试和后置基础设施责任见 `../更新记录/260716-阶段1B-1可靠恢复命令边界执行记录.md`。
- 阶段 1B-2 的 Progress 请求/展示边界、线程安全内存 Adapter、连接级单写入缓冲、完整测试和后置跨实例责任见 `../更新记录/260716-阶段1B-2Progress控制面迁移执行记录.md`。
- 阶段 1C-0/1C-1 的报告双基线、纯 Domain/DTO、兼容转发、测试结果和生产未切换边界见 `../更新记录/260716-阶段1C-0与1C-1报告契约及领域层执行记录.md`。
- 阶段 1C-2 的通用 Task Command/Progress、Report Ports、Submit/Run 编排、严格 Fake、
  故障测试和生产未切换边界见 `../更新记录/260716-阶段1C-2报告应用端口与严格Fake执行记录.md`。
- 阶段 1C-3 的 SQLite 追加 execution、原子受理/领取、expected TaskId 双表条件写、严格
  report Codec、50 线程 Barrier 和回滚测试见 `../更新记录/260716-阶段1C-3SQLite任务事实与原子受理执行记录.md`。
- 阶段 1C-0～1C-3 的全面审查修复、128 位 reportId、完整 trace、结果投影分离、stale
  收敛、Callback Guard fencing 与合并回归见 `../更新记录/260716-阶段1C全面审查修复与并发补强执行记录.md`。
- 阶段 1C-4 的任务级文件/Artifact、多文档 RAG、trace/call 审计 Schema v3、审计硬门禁和
  生产未切换边界见 `../更新记录/260716-阶段1C-4报告生产IO与审计Adapter执行记录.md`。
- 阶段 1C-5 的 Guard 人工解除追加审计、终态权威 Artifact 所有权、CAS 资源恢复、精确
  cleanup 事件重放、隔离与并发故障验收见
  `../更新记录/260716-阶段1C-5Callback Guard与资源恢复闭环执行记录.md`。
- 阶段 1C-0～1C-5 第二轮全面审查的发送前 latest 复核、即时 409、Artifact 完整性、
  AnythingLLM 未知写副作用隔离、逐事件清理检查点/心跳、有界扫描和后续硬门禁见
  `../更新记录/260716-阶段1C第二轮全面审查风险修复执行记录.md`。
- 阶段 1C-6 的持久积压/单执行 Worker Dispatcher、资源恢复周期、组合根生命周期、报告薄路由
  切换、原子 Progress latest Guard、完整测试结果和生产未部署边界见
  `../更新记录/260717-阶段1C-6Dispatcher组合根与路由切换执行记录.md`。
- 阶段 1C-6 全面审查后的真实关闭语义、稳定 FIFO、毒任务/坏资源冷却、维护线程隔离、
  同键 Progress 原子区间、跨进程门禁和最终扩大回归见
  `../更新记录/260717-阶段1C-6全面审查风险修复执行记录.md`。
- 阶段 1C 最终契约/并发/故障/架构关闭、遗留 Worker 三类引用证据、永久 AST 门禁、
  358 项定向/872 项安全回归和阶段 2～6 输入见
  `../更新记录/260717-阶段1C-7阶段关闭验收执行记录.md`。
- 阶段关闭后全面审查发现的回调竞态、过期 Guard、HTTP 分类、历史顺序、下载边界、审计
  身份、资源重试水位、Dispatcher 就绪与诊断问题，以及甲方同步 check-task 口径的加固结果见
  `../更新记录/260717-阶段1C全面审查问题修复与同步回调加固执行记录.md`。
- 阶段 1D-0 的契约、黄金、来源/上下文隔离、故障矩阵、Evidence Selection Oracle、真实只读
  校准、测试结果和生产阈值停止门禁见
  `../更新记录/260718-阶段1D-0契约资产与Evidence校准执行记录.md`。
- 阶段 1D-0R 的两项地面真值纠正、Query/Prompt 分离、MHTML/Chunk 去噪、额外信号纯 Selection、
  脱敏校准、临时隔离重建/清理和生产停止门禁见
  `../更新记录/260718-阶段1D-0R检索质量修复执行记录.md`。
- 阶段 1D-1 的 weaponry 四层骨架、深冻结 DTO、Retrieval/Evidence/Prompt 类型边界、来源与
  TABLE/Callback 纯规则、模式 1 删除、遗留模式 2 兼容转发、审查补强和完整回归见
  `../更新记录/260718-阶段1D-1武器谱领域模型与纯规则执行记录.md`。
- 阶段 1D-2 的请求 Parser/Presenter、Document Scope Port/只读 Adapter/严格 Fake、execution
  输入快照、Schema v1 Codec、原子受理、50 同键/不同键隔离及完整回归见
  `../更新记录/260718-阶段1D-2请求适配文档范围与任务Codec执行记录.md`。
- 阶段 1D 全面审查后的 Evidence 全量保留、TABLE 强行身份、Schema v1 策略补全、
  ArchitectureId 单一规范化、成功终态完整性及 1014 项安全回归见
  `../更新记录/260718-阶段1D全面审查问题修复执行记录.md`。
- 阶段 1D-3A 的 Retrieval/Extraction/Auxiliary/Translation/Audit/Callback/Resource/Dispatcher
  供应商无关 Port、严格故障 Fake、审计/资源顺序、cleanup lease/fencing、50 线程结构隔离和
  1034 项安全回归见
  `../更新记录/260718-阶段1D-3A供应商无关端口与严格Fake执行记录.md`。
- 阶段 1D-3B 的唯一 Schema v2、score/rank 双路径、最小多文档结构样例、任务级 Retrieval、
  Provided-Evidence Extraction、NoAuxiliary/Terms、Translation、SQLite Audit/Resource、资源登记
  补偿、结果未知和 50 任务持久化隔离见
  `../更新记录/260718-阶段1D-3B生产IO适配与Schema-v2执行记录.md`。
- 阶段 1D-5 的通用持久扫描 Dispatcher/进程锁、Report 薄包装、Weaponry 严格配置、固定策略、
  单执行 Worker、隔离维护任务、离线组合根、203+40 项定向与 1116 项安全全仓回归见
  `../更新记录/260719-阶段1D-5通用Dispatcher配置与离线组合根执行记录.md`。
- 阶段 1E-0 的 `/llm/reassign` 当前/目标双基线、400/500/200 黄金样例、显式 `false`、缺 slug、
  CAS 0 行和补偿失败目标矩阵、离线预算边界与接口文档/代码观测区分见
  `../更新记录/260724-阶段1E-0分类节点变更契约与故障资产执行记录.md`；该波次未切换公开路由。
- 阶段 1E-1 的 `reassign` 四层骨架、原始 ID 深冻结、Operation/Step 状态机、幂等键、补偿
  决策、错误分类、AST 门禁与完整离线回归见
  `../更新记录/260724-阶段1E-1分类节点变更领域模型与纯规则执行记录.md`；该波次仍未定义
  Port、SQLite Schema 或公开路由接线。
- 阶段 1E-1R 的终态证据、目标绑定一致性、独立补偿 Step、UTC lease、诊断上限、稳定公开
  message、远端异常脱敏和契约补强见
  `../更新记录/260724-阶段1E-1R分类节点变更领域一致性修正执行记录.md`；经确认仅修改公开
  文案，不增删接口字段或状态码。
- 阶段 1E-2 的 Repository/UoW/Knowledge Port、严格 Fake、SQLite Operation/Step/Event、活动文档
  部分唯一保护、lease/fencing、条件 CAS、追加审计与离线并发/故障注入验收见
  `../更新记录/260724-阶段1E-2分类节点变更Port严格Fake与SQLite事实执行记录.md`；该波次未接入
  AnythingLLM、Container 或公开路由，也未修改接口文档。
- 阶段 1E-2R 的恢复 fencing、终态/成功事实门禁、Step 探测一致性、只读稳定游标恢复扫描、
  审计补强、Fake 线程事务隔离、workspace 无副作用查回和既有 Schema/索引升级修正见
  `../更新记录/260724-阶段1E-2R分类节点变更持久化一致性修正执行记录.md`；该审查修正仍未接入
  AnythingLLM、Application、Container 或公开路由，也未修改接口文档。
- 阶段 1E-3 的请求级 AnythingLLM Client Factory、有限 HTTP/总预算/补偿预留、单调 deadline、
  目标 workspace 精确查回/创建、完整 doc_path 成员探测、解绑/挂载/Pin 四分类及离线 Fake 见
  `../更新记录/260724-阶段1E-3分类节点变更AnythingLLM适配与目标准备执行记录.md`；该波次尚未
  实现 Application Saga、步骤/映射持久化接线、本地 CAS、补偿恢复、Container 或公开路由，也未
  修改接口文档。
- 阶段 1E-3R 的步骤驱动预算、请求级 Knowledge Factory、生产动作门禁、workspace 三态归属、
  模糊 4xx 查回和日志脱敏修正见
  `../更新记录/260724-阶段1E-3R分类节点变更适配一致性修正执行记录.md`；其中“目标 workspace
  持久化准备权”作为 1E-4 前置约束。
- 阶段 1E-4 的 `DocumentReassignmentService` 前向成功路径、目标 workspace 持久化
  preparation claim/独立 fencing、无副作用失败专用收口、条件 CAS 和 SQLite/严格 Fake 组合验证见
  `../更新记录/260724-阶段1E-4分类节点变更Application成功路径执行记录.md`；该波次尚未实现
  补偿恢复、Container 或公开路由切换，也未修改接口文档。
- 阶段 1E-5 的 `RecoverReassignmentOperation`、过期 lease/fencing 接管、恢复观测、写后检查点
  收敛、固定补偿顺序、SQLite 原子终态/claim 释放和默认只读诊断脚本见
  `../更新记录/260724-阶段1E-5分类节点变更补偿恢复与诊断执行记录.md`；该波次仍未接入
  Container 或公开路由，也未修改接口文档。
- 阶段 1E-5R 的恢复 workspace 身份绑定、接管/续租/claim 强类型校验、数据库读取失败分类和
  机器可判定恢复退出码见
  `../更新记录/260724-阶段1E-5R分类节点变更恢复契约与运维语义修正执行记录.md`。
- 阶段 1E-6 的 `ReassignApplicationServices` 组合根、恢复四类职责接缝、`/llm/reassign` 薄路由、
  Parser/Presenter、接口契约字节级回归和永久 AST 门禁见
  `../更新记录/260724-阶段1E-6分类节点变更组合根与公开路由切换执行记录.md`；本波次仅同步接口
   文档中的非契约实现状态，不增删公开参数、字段、状态码或 Header。全面审查修正及 1E-7 前置门禁
   见 `../更新记录/260725-阶段1E-6R分类节点变更全面审查修复执行记录.md`。
- 阶段 1E-7 的 Observer、Checkpoint Reconciler、Compensator、Finalizer 实际算法下沉、兼容导出、
  协作器直接单测和永久结构门禁见
  `../更新记录/260725-阶段1E-7分类节点变更恢复实现下沉执行记录.md`；未修改公开接口文档。
- 阶段 1E 整体审查后的提交确认丢失终态协调、Application 入口远端预算、事务内 lease 计时、活动
  状态单源与数据库等价门禁，以及数据库权威时间和可取消数据库截止的未来计划修正见
  `../更新记录/260725-阶段1E整体审查修复执行记录.md`；未修改公开接口文档，也未处理负责人明确
  排除的 Git 原子纳入事项。

## 与其他文档目录的边界

- `docs/接口文档/`：对外契约的唯一权威来源。
- `docs/重构记录/`：当前有效的重构路线、架构决策和待实施设计。
- `docs/更新记录/`：已经实施的功能变更、问题修复和历史演进记录。
- `docs/plans/`：更早期的方案档案，仅供背景参考。

## 维护规则

- 新的跨模块重构计划、阶段设计和重大架构决策统一放入本目录。
- 实施完成后应在 `docs/更新记录/` 增加实际修改、验证结果、回滚信息和遗留风险，并回链对应重构计划。
- 文档必须区分“目标能力”“实施中能力”和“当前已具备能力”，不得把计划描述为已上线事实。
- 任何接口文档变更都必须先取得确认；任何情况下都不得擅自增删前后端请求参数。
  2026-07-15 已批准的成功空响应体、report 409、严格 `params` 元素校验和 Progress 显式
  action 错误后保持连接/无 ack，以及 2026-07-16 已批准的 reportId 128 位数字上限与
  报告可选文本兼容字符串化、2026-07-18 已批准的 weaponry ArchitectureId 单一规范化和
  Evidence 不截断，仅是定向例外，不得扩张解释。
