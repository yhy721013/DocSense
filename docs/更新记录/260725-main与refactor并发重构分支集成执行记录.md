# `main` 与 `refactor/concurrency` 分支集成执行记录

## 0. 文档信息

| 项目 | 内容 |
| --- | --- |
| 执行日期 | 2026-07-25 |
| 执行分支 | `refactor/integration` |
| 第一父基线 | `main@4472065a44cfd6e049f8fbe46cfc728ab26d3e58` |
| 第二父基线 | `refactor/concurrency@d2228ef9d6dd4d860bf74ad726340904ab40ecc9` |
| 共同基点 | `0023c4726b49791728a30b053e9da772f005e178` |
| 对应计划 | `../重构记录/260725-main与refactor并发重构分支集成实施计划.md` |
| 执行状态 | 已完成；已创建保留 main/refactor 双父关系的本地 merge commit |
| 接口依据 | `docs/接口文档/` |
| 生产结论 | 未部署、未启动真实后台服务；当前仍为单实例兼容实现 |

---

## 1. 执行结论

本次在独立 `refactor/integration` 工作树中，将 `refactor/concurrency` 的模块化单体和
`main` 的最新 Analysis 能力合并为本地 merge commit。M2 的实际冲突基线为 **8 个文件、22 个冲突块**；
所有冲突均已逐文件解决，未使用全局 ours/theirs 覆盖。该提交第一父为 main，第二父为
`refactor/concurrency`。

负责人随后明确同意此前已经确认、但尚未在 Analysis/check-task 路由落地的公开语义。本次仅据此
切换成功响应体与既有业务类型兼容范围：没有增加、删除、重命名或回写任何前后端接口参数、错误
字段、Callback 字段、SSE 事件或 WebSocket 消息字段。

---

## 2. 已确认语义的实际实现

| 契约点 | 实际实现 | 保留的边界 |
| --- | --- | --- |
| 文件解析受理 | `/llm/analysis` 在任务原子受理、初始 Progress 发布和既有批量线程启动后，返回 HTTP 202 零字节响应体。 | 请求字段、400/409 错误体、批量原子受理、`fileName` 业务关联键和 Callback 不变。 |
| 任务检查成功 | `/llm/check-task` 在检查与必要的同步回调恢复完成后，返回 HTTP 200 零字节响应体。 | 单项不存在仍为 404；参数错误仍为 400；恢复结果只持久化和记录日志，不向成功体公开。 |
| task 参数校验 | `params` 必须为非空对象数组；任一非对象元素拒绝整次 check-task 请求。 | 沿用既有 `params不能为空` HTTP 400 错误结构，不部分处理混合数组。 |
| 批量缺失项 | 缺失项不阻断其余存在任务的必要回调恢复；整批成功仍为空体。 | 不再通过占位 JSON 暴露缺失项、任务状态或内部执行细节。 |
| weaponry Progress | `data.architectureId` 下行输出规范化后的 JSON number。 | `architectureId` 的既有入站正整数/数字字符串规范化与消息字段名不变。 |

### 2.1 实际修改文件

| 文件 | 作用 |
| --- | --- |
| `app/blueprints/llm.py` | 增加可复用的严格空 HTTP 响应构造器；切换 Analysis/check-task 成功响应；将 check-task 的混合对象数组解析改为原子拒绝；补充受理、恢复和完成日志。 |
| `app/presenters/task_progress.py` | 将 weaponry Progress 的 `architectureId` 投影为整数 JSON number，并为异常身份记录错误日志。 |
| `tests/test_routes.py`、`tests/test_progress_and_check_task.py`、`tests/test_stage1a1_check_task_contract.py` | 将路由与公开契约断言改为零字节成功体，内部回调状态改由持久化任务事实断言。 |
| `tests/test_task_progress_presenter.py`、`tests/test_stage1a1_progress_contract.py` | 固定 weaponry Progress 的数值 ID 和已公开业务类型。 |
| `tests/contracts/stage0_contracts.json`、`tests/contracts/stage1d_weaponry_contracts.json` 及对应资产测试 | 将已批准目标推进为集成分支已实现状态，冻结字段不扩张。 |
| `docs/接口文档/`、`README.md`、`tests/README.md` | 同步当前实现状态、成功体边界、严格参数规则和单实例生产边界。 |

---

## 3. 验证证据

所有验证均使用 `C:\.me\codes\DocSense\venv\Scripts\python.exe -B`、临时 SQLite、Fake 或 Mock。
未执行 `run.py`，未连接真实 AnythingLLM、模型或 Callback 服务。

| 验证 | 结果 |
| --- | --- |
| AST 与 JSON 静态校验 | 10 个修改后的 Python 文件和 2 个契约 JSON 资产通过。 |
| 定向回归 | `test_routes`、check-task、Progress、契约资产、weaponry Callback Guard 等 8 个模块共 127 项通过，0 失败、0 错误。 |
| 安全全仓动态回归 | 发现 1625 项，排除 13 项，执行 1612 项；按 `202 × 7 + 198` 分为 8 组，全部 0 失败、0 错误、0 跳过。 |

全仓排除项在运行时动态核对为 13 项：

1. `test_local_scripts` 的 7 项用例可能通过 Shell 启动本地脚本或 `run.py`；
2. `test_test_assets` 的 5 项用例依赖 `.gitignore` 排除的个人联调夹具；
3. `test_migrate_analysis_security` 的 1 项用例断言 POSIX `0640` 权限位，Windows 文件系统不稳定支持。

测试日志中的网络错误、资源隔离、恢复和后台服务字样均来自故障注入的 Fake 或临时离线容器；
它们是断言路径，不表示真实服务已启动或真实外部请求已发送。

---

## 4. 文档与架构边界

- `docs/接口文档/README.md` 与 `文件处理和报告生成.md` 已记录负责人确认、实际成功体和
  weaponry 数值 ID 语义。
- `docs/重构记录/260725-main与refactor并发重构分支集成实施计划.md` 已把建议分支统一为
  `refactor/integration`，并以 8 文件、22 冲突块作为 M2 基线。
- 本次不把 Analysis 接入可靠队列；它仍使用迁移期批量后台线程。Report/Weaponry 的持久积压和
  Dispatcher 仍明确为单实例实现，不能据此宣称多实例、跨进程高可用或生产吞吐能力。
- 外部副作用结果未知时的隔离、`recovery_required` 和人工处置语义未因本次响应体切换而放宽。

---

## 5. 回滚与后续

如需回滚，应使用
`git revert -m 1 <merge-commit>`，保留两个原始分支历史。不得 rebase 或重写共享分支。

后续仅 M9：在 `origin/main` 未漂移前由负责人决定是否创建 PR、推送或发布。真实供应商协议、
生产 Callback、容量压测、可靠队列和多实例数据库一致性仍是独立的后续验收项。
