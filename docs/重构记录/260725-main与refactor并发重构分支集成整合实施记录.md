# main 与 refactor/concurrency 分支集成整合实施记录

## 1. 文档状态

本文整合 2026-07-22 的 Analysis 优化分支合并草案和 2026-07-25 的 `main`/`refactor/concurrency` 实施计划。前者的 Git 基线和执行方向已被后者取代；后者也已经执行完成，不再作为活动合并指令。

- 历史目标：把阶段 0～1E 的模块化重构成果与 main 的 Analysis 最新能力合并。
- 最终方向：以当时最新 `main` 为第一父基线，在独立工作树产生 merge 现场，按层解决冲突并保留双侧语义。
- 实际结果：见 `../更新记录/260725-main与refactor并发重构分支集成执行记录.md`。
- 当前使用方式：仅用于历史追溯；后续合并必须重新核对当前分支、HEAD、工作树和契约，禁止复用文中的旧提交号执行 Git 操作。

## 2. 当时不可破坏的基线

1. `docs/接口文档/` 的 HTTP、Callback、Progress、SSE 和 WebSocket 契约优先于任一分支历史实现。
2. 保留 `refactor/concurrency` 已完成的 Tasks、Report、Weaponry、Reassign 模块边界、SQLite 事实、Callback Guard 和 Dispatcher。
3. 保留 main 的 Analysis Domain、召回、Prompt、任务级 AnythingLLM Transport、已确认容量/状态码和文件处理能力。
4. Factory 只持有不可变配置；每个任务/请求创建并关闭自己的 Transport/Session。
5. 不把 SQLite 50 线程隔离测试解释为真实模型 50 路并发容量。

## 3. 集成策略

| 波次 | 处理内容 |
| --- | --- |
| M0 | 固定双方提交、接口摘要、数据库样本和回归基线。 |
| M1～M2 | 创建独立工作树，产生不提交的 merge 冲突现场并盘点实际冲突。 |
| M3 | 先合并 Port、Domain、配置和供应商无关契约。 |
| M4 | 合并 SQLite Schema 并集、Repository、Callback/资源事实和迁移测试。 |
| M5 | 合并 Analysis Application、组合根和薄路由，保留其他重构模块。 |
| M6～M7 | 回归 Report/Weaponry/Reassign/Chat/Progress，核对自动合并文件的语义。 |
| M8 | 同步文档、配置和执行证据，完成安全全仓与回滚检查。 |

## 4. 高风险冲突原则

- `README.md`：只写合并后实际能力和新测试结果，不复制任一分支的历史通过数。
- `app/blueprints/llm.py`：保留所有公开路由和薄适配边界，不在冲突解决时重新塞入业务编排。
- `app/container.py`：合并对象图但保持唯一组合根，不共享有状态供应商 Session。
- 任务数据库：取双方 Schema 并集；业务 Guard、file claim 和各模块状态机分表、分方法、分所有权。
- 自动合并的 Port、Prompt、配置、接口文档和测试同样进行语义审查，不能以“Git 无冲突”替代设计检查。

## 5. 数据与验证门禁

1. 分别用空库、旧 main 库、旧 refactor 库和故障库执行幂等迁移。
2. 验证旧行、业务键、execution、Callback 和资源状态不被改写。
3. 定向测试按 Domain/Port、SQLite/Callback、Analysis 主链、相邻业务、Chat/Progress 顺序执行。
4. 安全全仓明确列出 discovered、excluded、executed、failures、errors 和 skips。
5. 真实服务、生产容量和多实例不在离线合并门禁中，不得补写为已验证。

## 6. 回滚原则

- merge commit 前只在确认处于该独立工作树的 merge 状态后中止，不影响其他工作树。
- 已提交未共享时使用可追溯的新分支或 revert 策略，不改写用户工作。
- 已共享或进入主线后使用新提交回滚，并先判断外部副作用结果、数据库兼容和资源所有权。
- outcome unknown 任务继续隔离，不能因代码回滚盲目重放或删除资源。

