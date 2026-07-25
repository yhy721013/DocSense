# `main` 与 `refactor/concurrency` 分支集成实施计划

## 0. 文档信息

| 项目 | 内容 |
| --- | --- |
| 编写日期 | 2026-07-25 |
| 文档层级 | L3 分支集成实施计划 |
| 集成分支 | `refactor/integration` |
| 目标主线基线 | `main@4472065a44cfd6e049f8fbe46cfc728ab26d3e58` |
| 待集成分支基线 | `refactor/concurrency@d2228ef9d6dd4d860bf74ad726340904ab40ecc9` |
| 共同基点 | `0023c4726b49791728a30b053e9da772f005e178` |
| 当前状态 | M0～M8 已完成；本地 merge commit 已创建，M9 的 PR 与主线漂移复核待负责人决定 |
| 公开契约依据 | `docs/接口文档/` |
| 运行限制 | 不启动 `run.py`，不连接真实 AnythingLLM、模型、Callback 或其他后台服务 |

本计划以最新 `main` 的 Analysis 能力为功能基线，以 `refactor/concurrency` 的模块化单体、
任务事实、Dispatcher、Callback Guard 与 Reassign Saga 为结构基线。集成后的代码仍明确运行在
单实例兼容模式；SQLite、进程锁和进程内 Progress Hub 不得被描述为多实例可靠队列。

---

## 1. 不可变范围与已确认例外

### 1.1 不可变范围

1. `docs/接口文档/` 是 HTTP、SSE、WebSocket 与 Callback 的唯一权威来源。
2. 不增加、删除或重命名任何前后端接口参数、响应字段、Callback 字段、Header、SSE 事件或
   WebSocket 消息字段。
3. 内部 `execution_id`、租约、fencing token、调度状态和回调结果不得通过成功响应体泄露。
4. 不启动 `run.py`；离线验证必须使用临时 SQLite、Fake 或 Mock。

### 1.2 2026-07-25 负责人明确确认的语义例外

原计划曾把下列目标保留给后续阶段。负责人已明确同意在本次集成分支落地，因此该确认覆盖原有
“不提前切换”的限制，但**不改变参数或字段集合**。

| 编号 | 已确认语义 | 实现边界 |
| --- | --- | --- |
| I-01 | `POST /llm/analysis` 成功返回 HTTP 202 严格空响应体 | 任务仍按原子受理后启动既有批量后台线程；调用方继续使用 `fileName`、Progress 与 Callback 关联任务。 |
| I-02 | `POST /llm/check-task` 成功返回 HTTP 200 严格空响应体 | 保留请求内同步回调恢复；任务状态、进度、回调状态和恢复结果只保存在内部。 |
| I-03 | check-task 的 `params` 必须是非空对象数组 | 任一非对象元素均拒绝整次请求并沿用既有 HTTP 400 错误结构；不得静默过滤。 |
| I-04 | weaponry 纳入 check-task/Progress 的既有公开 `businessType` 兼容范围 | `architectureId` 入站规范化保持既有规则；Progress 下行 `data.architectureId` 使用 JSON number。 |

---

## 2. M2 合并基线与冲突处置

在固定 refs 上执行 `git merge --no-commit --no-ff origin/refactor/concurrency` 后，实际基线为
**8 个冲突文件、22 个文本冲突块**。这是后续审查、验证和完成定义的唯一 M2 基线；先前虚拟
合并的 24 块预测仅保留为历史观测。

| 冲突文件 | 集成原则 |
| --- | --- |
| `.env.example`、`docker/.env.docker` | 保留 main 的 Analysis 模式与 refactor 的 Report/Weaponry/Reassign 单实例配置。 |
| `README.md` | 保留模块化结构与 main 的 Analysis 语义；不得把迁移期能力写成生产多实例能力。 |
| `app/blueprints/llm.py` | 以 refactor 路由结构为主体，移植 main 的 Analysis 严格校验、批量原子受理和 execution 所有权。 |
| `app/container.py` | 维持唯一组合根，并同时装配 Analysis 配置与各业务模块。 |
| `app/services/llm_service/task_service.py` | 保留 Schema 并集、文件 execution/Callback claim 与 Report/Weaponry Guard 边界。 |
| `tests/test_dependency_container.py`、`tests/test_routes.py` | 使用离线容器验证组合根、路由和公开契约。 |

冲突解决不得使用全局 `--ours` 或 `--theirs` 覆盖。集成分支以 `main` 为第一父，
`refactor/concurrency` 为第二父；评审中必须直接写分支名，避免混淆 Git 的 ours/theirs 语义。

---

## 3. 里程碑状态

| 里程碑 | 内容 | 状态与证据 |
| --- | --- | --- |
| M0 | 固定 refs、接口契约和工作区边界 | 已完成；refs、merge-base、分叉数量与接口文档已记录。 |
| M1 | 创建独立集成工作树 | 已完成；使用 `refactor/integration`，未改写 `main` 或 `refactor/concurrency`。 |
| M2 | 产生未提交合并现场 | 已完成；实际 8 文件、22 冲突块。 |
| M3 | Port、Gateway、Policy、Config、Prompt | 已完成；保留模块边界与 main Analysis 配置。 |
| M4 | Task Schema、迁移与 execution 所有权 | 已完成；保留数据库并集和文件/模块化任务隔离。 |
| M5 | Container、Analysis 主链与 Flask 路由 | 已完成；Report/Weaponry/Reassign 保持薄路由和唯一组合根。 |
| M6 | 配置、README、接口文档与资产 | 已完成；本文件与接口文档同步记录 I-01～I-04。 |
| M7 | 跨业务分层回归 | 已完成；定向路由、Progress、Callback Guard 与契约资产回归通过。 |
| M8 | 安全全仓、最终差异审查与 merge commit | 已完成；动态发现 1625 项，排除 13 项明确不安全/平台相关用例后 1612 项均通过，并已创建保留双父关系的 merge commit。 |
| M9 | PR、主线漂移复核与发布前交接 | 未开始；不在本次未提交集成现场自动创建 PR。 |

---

## 4. 本次接口落地清单

| 入口 | 修改后的成功语义 | 保持不变的语义 |
| --- | --- | --- |
| `/llm/analysis` | HTTP 202、零字节响应体，不返回任务快照或内部执行标识。 | 请求字段、400/409 错误结构、批量原子受理、任务线程和 Callback 语义。 |
| `/llm/check-task` | HTTP 200、零字节响应体；批量缺失项不阻断其余存在项的恢复。 | `file`/`report`/`weaponry` 业务键规则、单项不存在的 404、参数错误的 400、同步回调恢复副作用。 |
| `/llm/progress` | weaponry 下行 `data.architectureId` 为 JSON number。 | 无 action、错误后保持连接、无 ack、既有请求字段和消息结构。 |

任何未来请求如果需要改变状态码、响应体、Header、SSE/WebSocket 或 Callback 行为，仍必须先更新
接口文档并重新取得确认；I-01～I-04 不是对其他契约变更的泛化授权。

---

## 5. 验证与完成条件

### 5.1 已完成验证

| 验证 | 结果 |
| --- | --- |
| 修改文件 AST 与契约 JSON 解析 | 10 个 Python 文件和 2 个 JSON 资产通过。 |
| 路由、check-task、Progress、黄金资产、weaponry Callback Guard 定向回归 | 127 项通过，0 失败，0 错误。 |
| 安全全仓动态回归 | 发现 1625 项；排除 13 项；执行 1612 项，8 组全部通过，0 失败、0 错误、0 跳过。 |

安全全仓排除项仅包括 7 个可能启动本地 Shell/`run.py` 的 `test_local_scripts` 用例、5 个依赖
`.gitignore` 本地联调夹具的 `test_test_assets` 用例，以及 Windows 无法稳定表达 POSIX `0640`
权限位的 1 个迁移测试。排除清单在每组运行前动态校验为 13 项；未访问真实外部服务。

### 5.2 M8 完成证据与 M9 前置

1. `git diff --check`、暂存后的 `git diff --cached --check` 均通过，未解决冲突索引为 0；
2. 已复核集成工作树只包含本计划、已确认接口契约和合并所需文件；
3. merge commit 的第一父为
   `4472065a44cfd6e049f8fbe46cfc728ab26d3e58`（main），第二父为
   `d2228ef9d6dd4d860bf74ad726340904ab40ecc9`（refactor/concurrency）；
4. 创建 PR 前仍须重新核对 `origin/main` 是否漂移，并由负责人决定是否推送或发布。

---

## 6. 回滚与生产边界

后续如需回滚，应优先使用 `git revert -m 1 <merge-commit>`，保留两个原始分支的历史。不得通过重写共享分支历史回滚。

本次验证只证明离线 SQLite/Fake 环境下的代码和公开契约一致。它不证明 AnythingLLM 真机协议、
真实 Callback 投递、生产容量、可靠队列或多实例数据库一致性；这些仍需按后续 MySQL/Outbox/
可靠队列和生产 attestation 门禁分别验收。
