# 更新记录目录说明

本目录保存已经实施、正在实施或需要长期追溯的设计决策与改造记录。它用于解释“为什么这样改、改动范围、验证方式和剩余边界”，但不替代接口文档的对外契约地位。

## Legacy Office 分支集成记录

| 文件 | 作用 |
| --- | --- |
| `260728-main与file-analysis分支Legacy-Office集成M0执行记录.md` | 冻结 Git 基线、文件处置矩阵、公开契约摘要和离线回归基线。 |
| `260728-main与file-analysis分支Legacy-Office集成M1执行记录.md` | 保留双父历史完成非快进合并，并人工处置 Analysis 等冲突。 |
| `260728-main与file-analysis分支Legacy-Office集成M2执行记录.md` | 验收 LibreOffice 转换内核、所有权清理和离线交付资产。 |
| `260728-main与file-analysis分支Legacy-Office集成M3执行记录.md` | 验收 AnythingLLM 单 Sheet 协议、多 Sheet 拒绝和 Folder 安全清理。 |
| `260728-main与file-analysis分支Legacy-Office集成M4执行记录.md` | 验收 Report Legacy 来源、单 Sheet、Artifact 和失败收敛。 |
| `260728-main与file-analysis分支Legacy-Office集成M5执行记录.md` | 将 Legacy Office 能力接入 Stage 1F Analysis 唯一生产链并冻结 V2 策略。 |
| `260728-main与file-analysis分支Legacy-Office集成M6执行记录.md` | 验收部署默认值、共享 Preparer、Preflight 和启动失败资源回收。 |
| `260728-main与file-analysis分支Legacy-Office集成M7执行记录.md` | 完成共享业务回归，并增加无删除入口的 XLSX Folder 只读库存治理。 |
| `260728-main与file-analysis分支Legacy-Office集成M8执行记录.md` | 同步接口语义、README、索引和契约资产，不增删任何公开字段。 |
| `260728-main与file-analysis分支Legacy-Office集成M9执行记录.md` | 完成九组离线关闭门禁和发现 1,986/排除 13/执行 1,973 项安全全仓验收。 |

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
| `260727-文件对话首次空文件自动全量阶段0与1执行记录.md` | 首次空文件自动全量改造的阶段 0、1：契约黄金基线、单次全量目录快照、严格冲突检测、106 项离线回归及尚未接入公开路由的边界。 |
| `260727-文件对话首次空文件自动全量阶段2执行记录.md` | 阶段 2 内部选择候选 DTO：显式/首次默认候选互斥冻结、严格 Schema v1 往返、已有 session 快速路径、阶段 3 接线门禁及 113 项离线回归。 |
| `260727-文件对话首次空文件自动全量阶段3执行记录.md` | 阶段 3 受理事务原子选择：纯领域候选快照、事务内 `session_created` 判定、最终数量校验、全事实回滚、同会话 50 并发唯一受理、公开路由接线及 132 项扩大回归。 |
| `260727-文件对话首次空文件自动全量阶段4执行记录.md` | 阶段 4 执行/绑定/历史等价：自动与显式文件共用任务级 Port、resource lease、attach、binding、模型流和本地历史；快照抗目录变化、后续空数组复用 current heads、空范围不 attach，并通过 166 项 Chat 及 59 项网关/架构回归。 |
| `260727-文件对话首次空文件自动全量阶段5执行记录.md` | 阶段 5 路由、日志与调试一致性：区分原请求/候选/最终有效数量，记录脱敏选择模式和事务上下文；调试页以 current bindings 与本地 committed history 为准，并通过 169 项 Chat 及 59 项网关/架构回归。 |
| `260727-文件对话首次空文件自动全量阶段6执行记录.md` | 阶段 6 离线全面回归：补齐上限等值、目录整体拒绝和 50 个不同会话完整隔离，修复 SQLite 5 秒 busy timeout 导致的并发 500；174 项 Chat、59 项网关/架构及发现 1,846/排除 13/执行 1,833 项安全全仓回归通过。 |
| `260727-文件对话首次空文件自动全量阶段7关闭验收执行记录.md` | 阶段 7 最终关闭：修复受理后日志计数读取故障误报，完成契约与代码审计、175 项 Chat、67 项契约/网关/容器/架构及发现 1,847/排除 13/执行 1,834 项安全全仓回归；代码与 SQLite 单实例离线计划关闭。 |
| `260728-文件对话请求与活动范围分离阶段0执行记录.md` | Requested/Active/Effective Scope 后续改造的阶段 0：冻结最后显式范围、禁止自动吸收、history 仅展示前端显式文件及公开字段零增删；11 项黄金/契约与 178 项 Chat 基线通过，生产代码和接口文档尚未切换。 |
| `260728-文件对话请求与活动范围分离阶段1执行记录.md` | 阶段 1 纯领域实现：新增 Scope Revision/Head/Decision、Requested File、严格 Schema v1、重复身份门禁和 Requested→Active→Effective 状态机；38 项定向及 186 项 Chat 回归通过，生产链尚未切换。 |
| `260728-文件对话请求与活动范围分离阶段2执行记录.md` | 阶段 2 SQLite Schema v4 与 Repository：新增 Scope Revision/Member/Head、CAS、requested/effective run input Codec、完整性触发器、源 run 清理隔离和 50 会话持久化隔离；61 项定向及 196 项 Chat 回归通过。 |
| `260728-文件对话请求与活动范围分离阶段3执行记录.md` | 阶段 3 原子受理状态机：在同一 SQLite 事务内提交 Scope Revision/Head、requested/effective run input 和 pending message；同会话 50 首次/显式并发均唯一受理，超限与故障全量回滚，198 项 Chat 回归通过。 |
| `260728-文件对话请求与活动范围分离阶段4执行记录.md` | 阶段 4 执行、绑定与恢复：Worker 仅凭 run_id 恢复 requested/effective；Workspace 累计绑定不扩大模型范围，显式失败后空请求仍复用最后 accepted 范围；41 项执行器、203 项 Chat 与 59 项网关/容器/架构回归通过。 |
| `260728-文件对话请求与活动范围分离阶段5执行记录.md` | 阶段 5 历史、日志与调试一致性：history 只投影 requested，1,000 个 effective 文件不扩大空请求历史；调试 `fileNames` 校正为 Active Scope，bindings 独立脱敏计数且不新增 JSON 字段；204 项 Chat 回归通过。 |
| `260728-文件对话请求与活动范围分离阶段6执行记录.md` | 阶段 6 开发 Chat 库精确清理与安全全仓回归：空库重建 Schema v1～v4，发现 1,879/排除 13/执行 1,866 项全部通过；证据限定为 Windows、SQLite 单实例与离线 Fake。 |
| `260728-文件对话请求与活动范围分离阶段7关闭验收执行记录.md` | 阶段 7 最终关闭：同步权威接口文档且公开字段零增删，完成 204 项 Chat、70 项契约/网关/容器/架构、1,866 项安全全仓及静态审计；开发分支 SQLite 单实例离线计划关闭。 |
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
| `260723-武器谱AnythingLLM删除400幂等清理修复执行记录.md` | 修复 AnythingLLM 对缺失 workspace 返回 400 导致资源长期 `cleanup_pending`：仅在完整清单确认精确 slug 不存在时幂等成功，并覆盖任务内关闭、后台恢复、查回失败和既有重试边界。 |
| `260727-武器谱术语目录自动上传恢复执行记录.md` | 恢复本地术语卡启动期自动上传：内容指纹、版本化 workspace、缺卡补齐、未知结果隔离、多代只读路由、同源 dry-run/apply 工具及公开契约不变边界。 |
| `260728-武器谱术语目录远端身份校验修复执行记录.md` | 修复真实 AnythingLLM 将 workspace 文档 title 改写为 location 文件名导致的启动门禁失败：统一完整 document_location 身份、SQLite 恢复事实、未知结果失败关闭、生产形态回归及真实只读 61/61 验证。 |
| `260724-阶段1E-0分类节点变更契约与故障资产执行记录.md` | 阶段 1E-0 的 `/llm/reassign` 当前/已批准目标双基线、400/500/200 黄金样例、显式 `false`/缺 slug/CAS 0 行/补偿失败矩阵、离线预算及安全全仓回归；未切换生产路由或修改接口文档。 |
| `260724-阶段1E-1分类节点变更领域模型与纯规则执行记录.md` | 阶段 1E-1 的 `reassign` 四层骨架、原始 ID 深冻结、Operation/Step 状态机、步骤幂等键、补偿决策、错误分类、架构门禁和 1,208 项安全离线回归；未定义 Port/Schema/Adapter 或切换公开路由。 |
| `260724-阶段1E-1R分类节点变更领域一致性修正执行记录.md` | 1E-1 全面审查后的终态证据、目标绑定一致性、独立补偿 Step、UTC lease、诊断字段上限、稳定公开 message、远端异常脱敏和契约资产补强；经确认修改接口文档文案，不增删字段或状态码。 |
| `260724-阶段1E-2分类节点变更Port严格Fake与SQLite事实执行记录.md` | 阶段 1E-2 的 Repository/UoW/Knowledge Port、严格 Fake、SQLite Operation/Step/Event、活动文档部分唯一索引、lease/fencing、条件 CAS、追加审计、50 并发和故障注入验收；未接入 AnythingLLM、Container 或公开路由，未修改接口文档。 |
| `260724-阶段1E-2R分类节点变更持久化一致性修正执行记录.md` | 1E-2 全面审查后的恢复 fencing、终态/成功事实门禁、Step 探测一致性、只读稳定游标扫描、恢复审计、Fake/SQLite 线程事务边界、workspace 查回和既有 Schema/索引升级修正；165 项联合回归通过，未修改接口文档。 |
| `260724-阶段1E-3分类节点变更AnythingLLM适配与目标准备执行记录.md` | 阶段 1E-3 的请求级 AnythingLLM Client Factory、有限 HTTP/总预算/补偿预留、单调 deadline、目标 workspace 精确查回/创建、完整 doc_path 探测、解绑/挂载/Pin 四分类和离线验证；未接入 Application、Container 或公开路由，未修改接口文档。 |
| `260724-阶段1E-3R分类节点变更适配一致性修正执行记录.md` | 1E-3 全面审查后的步骤驱动预算、请求级 Knowledge Factory、生产步骤门禁、workspace 三态归属、模糊 4xx 查回、双异常日志脱敏及 SQLite/Fake 加法兼容修正；未修改接口文档。 |
| `260724-阶段1E-4分类节点变更Application成功路径执行记录.md` | 阶段 1E-4 的 Application 前向成功路径、目标 workspace 持久化准备 claim/独立 fencing、无副作用失败专用收口、条件 CAS 和 SQLite/严格 Fake 离线组合验证；未接入 Container 或公开路由，未修改接口文档。 |
| `260724-阶段1E-4R分类节点变更Application一致性修正执行记录.md` | 1E-4 全面审查后的 claim 恢复保护、远端准备事实、local-only 分流、既有 slug 查回、严格 Step DTO、reserve 异常收口、lease 预算/续租和 Pin 审计修正；156 项联合及 1,314 项安全全仓回归通过，未修改接口文档。 |
| `260724-阶段1E-5分类节点变更补偿恢复与诊断执行记录.md` | 阶段 1E-5 的显式恢复、过期 lease/fencing 接管、恢复观测、写后检查点收敛、固定补偿顺序、SQLite 原子终态/claim 释放和默认只读诊断脚本；未接入 Container 或公开路由，未修改接口文档。 |
| `260724-阶段1E-5R分类节点变更恢复契约与运维语义修正执行记录.md` | 1E-5 全面审查后的恢复 workspace 身份绑定、接管/续租/claim 强类型校验、数据库读取失败分类和未收口恢复非零 CLI 退出码修正；未接入 Container 或公开路由，未修改接口文档。 |
| `260724-阶段1E-6分类节点变更组合根与公开路由切换执行记录.md` | 1E-6 的生产组合根、恢复四协作器、`/llm/reassign` Parser → Application → Presenter 切换、字节级公开契约回归和永久 AST 门禁；仅同步接口文档中的非契约实现状态，不增删参数、字段、状态码或 Header，真实供应商演练与生产校准仍未完成。 |
| `260725-阶段1E-6R分类节点变更全面审查修复执行记录.md` | 1E 全面审查后的有界同步补偿、目标 ID 有限兼容白名单、结果/message 不变量、Operation 历史外键迁移、文档删除事务门禁、单实例 fail-fast、故障矩阵消费和 1E-7 恢复实现下沉前置计划；经确认同步接口文档文案，不增删字段或状态码。 |
| `260725-阶段1E-7分类节点变更恢复实现下沉执行记录.md` | 1E-7 将恢复实际算法下沉到 Observer、Checkpoint Reconciler、Compensator、Finalizer 独立文件，保留 Facade/导入兼容，新增协作器直测与 callback-wrapper、最小 Port、Facade 规模/复杂度 AST 门禁；未修改公开接口文档或 Operation/Step/Event Schema。 |
| `260725-阶段1E整体审查修复执行记录.md` | 1E 整体审查后的提交确认丢失终态协调、Application 入口远端预算、事务内 lease 计时、活动状态单源与数据库等价门禁，以及数据库权威时间/可取消截止的未来计划修正；211 项 1E 回归和 1,358 项安全全仓回归通过，未修改接口文档，未处理明确排除的 Git 原子纳入事项。 |
| `260725-main与refactor并发重构分支集成执行记录.md` | 最新 main 与 `refactor/concurrency` 在 `refactor/integration` 的实际合并记录：8 文件、22 冲突块基线、保留双父关系的 merge commit、已确认的 Analysis/check-task 空响应体及 weaponry Progress 数值 ID、127 项定向和 1,612 项安全全仓离线回归；M9 已记录 PR #79、main 漂移复核和审核交接，PR 尚未合入，下一开发阶段为合入后的 1F。 |
| 260726-阶段1F-0文件分析契约黄金与现状资产执行记录.md | 阶段 1F-0 的文件分析公开契约、算法黄金、遗留阶段/副作用引用清单、468 项定向基线与 1,620 项安全全仓离线回归；未改生产代码或接口文档。 |
| 260726-阶段1F-1文件分析Domain与纯规则迁移执行记录.md | 阶段 1F-1 将文件分析纯规则迁入 Analysis Domain，保留旧导入身份、输出和诊断日志兼容；145 项迁移组合、196 项 Analysis 定向及 1,624 项安全全仓离线回归通过，未修改接口文档或公开接口。 |
| 260726-阶段1F-2文件分析端口Codec与Web适配准备执行记录.md | 阶段 1F-2 建立不可变任务输入、九类 Port、严格 Fake、fail-closed Codec、未接线 Parser/Presenter 与翻译单实例串行保护；170 项组合、221 项 Analysis 定向及 1,649 项安全全仓离线回归通过，未修改接口文档、路由或公开接口。 |
| `260726-阶段1F-2R文件分析端口与隔离契约审查修复执行记录.md` | 1F-2 全面审查后的显式 RAG 生命周期、两阶段与追加审计、资源 CAS、Callback Guard、严格 Fake execution 关联、Codec 重复键/损坏快照拒绝、空翻译失败分类和 Web 异常边界修复；未修改接口文档、路由或公开接口。 |
| `260726-阶段1F-3Application与任务级生产IO执行记录.md` | 1F-3 的 TaskId-only Application、任务目录/文件/RAG/知识/SQLite 审计 Adapter、审计与知识库 fail-closed 收口、遗留分类预算等价和完整离线回归；新链路未接 Worker、Container 或公开路由，未修改接口文档或公开接口。 |
| `260726-阶段1F-3RApplication与任务级IO审查修复执行记录.md` | 1F-3 全面审查后的部分创建失败审计、阶段 Conversation/attempt 关联、unknown close 幂等、stale 目录门禁、来源校验和精确审计查回修正；未修改接口文档或生产路由。 |
| `260726-阶段1F-3S文件分析Application等价拆分执行记录.md` | 1F-3S 将 2,162 行 `run_analysis.py` 等价拆分为 Facade、工作流、审计、知识库和失败收敛协作器，新增结构化轨迹/AST 门禁；公开契约零修改，1,683 项安全离线回归通过。 |
| `260726-阶段1F-4批量原子受理与顺序协调执行记录.md` | 1F-4 为新文件分析链路追加批次身份与部分唯一索引，在 SQLite 单事务内完成整批受理、全局顺序、旧公开投影和 Callback Guard 重检，并新增提交后有界唤醒/持久发现、32 项回滚和 50 并发验收；公开契约零修改，1,696 项安全离线回归通过，生产路由尚未切换。 |
| `260727-阶段1F全面审查修复执行记录.md` | 1F-0～1F-4 全面审查后的多候选审计、冻结 Prompt/候选策略、知识库/RAG unknown、毒快照专用终态、窄控制面查询和范围无损去重修复；经确认把接口文档 GJB 兜底统一为定向“通用要求”，不增删接口字段，274 项 Analysis 与 1,705 项安全全仓回归通过。 |
| `260727-阶段1F-6资源与Callback闭环执行记录.md` | 1F-6 为新 file execution 增加 SQLite 资源事实、意图/CAS/隔离、统一 Callback Guard 与同步恢复；资源恢复只追加可证明的审计事实，生产路由、Container、Dispatcher 和接口文档均未切换。 |
| `260727-阶段1F-7A切换前隔离与只读门禁执行记录.md` | 1F-7A 新增共享 Application 的 50 任务全链路隔离验收、旧 Worker AST 门禁及旧任务/Callback Guard/RAG 租约的只读切换预检；生产路由、Container、Dispatcher 和接口文档仍未切换。 |
| `260727-阶段1F-6与1F-7A全面复核修复执行记录.md` | 1F-6/1F-7A 全面复核后的 Callback attempt 原子授权、窄恢复投影、资源不可逆状态机、毒记录无 payload 隔离、已知 close 证据保留，以及五类切换硬阻断与确定性只读句柄释放；公开接口契约未修改。 |
| `260727-阶段1F-5ADispatcher配置与离线组合根执行记录.md` | 1F-5A 的 Analysis 严格单实例配置、持久 Dispatcher/指数退避、进程锁、组合根/生命周期/readiness、同 file 活跃 Callback 恢复合并、离线 Fake 隔离与公开路由未切换边界。 |
| `260727-阶段1F-5B唯一生产链路由接线与离线验收记录.md` | 1F-5B 的 `/llm/analysis` Parser → Submit → Presenter 路由接线、file check-task 新 Callback Recovery owner、永久 AST/契约回归与生产预检边界；代码/离线验收完成，真实目标库发布尚待确认。 |
| `260727-阶段1F-7B关闭验收执行记录.md` | 1F-7B 的 408 项定向回归、发现 1,752/排除 13/执行 1,739 项安全全仓回归、旧执行器引用盘点和文档收口；代码/离线关闭验收完成，真实目标库发布仍待确认。 |
| `260727-阶段1F关闭后全面审查修复与清库发布收口执行记录.md` | 1F 关闭后对 file `outcome_unknown` 显式 check-task 至少一次补发、请求内去重、毒快照回调、投影原子收敛、资源统计、Dispatcher 致命退出和 `clean.py` fail-closed 的修复；同步权威接口说明但不增删公开字段，并按停服清库重建制度把只读预检保留为存量/恢复诊断工具。 |

其余文件记录知识库、日志、运行时路径、技术选型等相关演进，阅读时应按业务主题选择。

## 与重构记录的关系

当前有效的 AnythingLLM、文件对话、低耦合、高并发、任务隔离和可靠队列重构计划已迁移至 `docs/重构记录/`，统一索引与阅读顺序见 `docs/重构记录/README.md`。本目录继续保存已经实施的功能变更、问题修复和历史演进记录。

重构计划落地后，应在本目录新增实施记录，写明实际修改文件、测试结果、发布/回滚过程和剩余风险，并回链对应的 `docs/重构记录/` 文档。

## 维护规则

- 新记录应写明日期、范围、影响接口、数据迁移策略、验证命令和未解决风险。
- 旧数据处理策略必须明确；开发阶段若允许清空旧数据，也应在记录中说明。
- 不得以更新记录替代接口确认流程，不得擅自增删任何前后端接口参数，尤其不得改变已经冻结的 `/llm/chat*` 契约。
