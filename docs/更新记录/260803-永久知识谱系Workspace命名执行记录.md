# 永久知识谱系 Workspace 命名执行记录

## 1. 当前结论

- 执行日期：2026-08-03。
- 目标分支：`feat/weaponry-chat`。
- 阶段 0～7 已全部完成并通过门禁；本机回环 AnythingLLM 隔离真实验收及资源清理已完成。
- Analysis 永久知识入库、Reassign 前向执行与恢复观察统一使用
  `archId-{规范化后的 architectureId}`。
- 未增加、删除或改名任何公开接口参数；请求、响应、Callback、状态码、Header、SSE、WebSocket
  与同步语义均未改变。
- 未修改 SQLite Schema 或迁移，未运行 `run.py`，也未迁移或删除旧远端数据。

## 2. 实现事实

### 2.1 单一共享规则

`app/domain/knowledge_workspace.py` 是永久知识谱系 Workspace 名称的唯一来源：

- 固定前缀为 `archId-`；
- 输入必须是非布尔整数并处于有符号 64 位范围；
- 函数不访问环境、数据库、时钟、日志或网络；
- 架构门禁只允许 Analysis 与 Reassign Application 导入该精确共享模块，并禁止共享领域层
  反向依赖 Flask、数据库或 AnythingLLM。

### 2.2 Analysis 与 Reassign

- Analysis 以已校验的数据库权威 `architecture_id` 生成 `CollectionSpec.name`，Knowledge Gateway
  仍只负责供应商协议、创建协调和本地映射，不吸收业务命名规则。
- Reassign 先复用 `architecture_id_storage_value()` 规范化目标 ID，再生成名称；因此 `12`、
  `"0012"` 与 `" 12 "` 等既有输入表示不会产生多套 Workspace 名称。
- Reassign 的前向创建/查回和恢复观察使用相同函数，防止恢复阶段回到旧名称。
- 应用日志可记录 task/operation ID、architecture ID、精确 Workspace 名称、匹配数量和结果分类；
  不记录凭据、文档或模型正文、Prompt、完整供应商响应或未脱敏异常正文。

## 3. 旧数据与资源边界

- 生产 Python 代码中 `architectureId-`/`architectureid-` 创建或查找逻辑为零。
- 不实现旧前缀 fallback、双读、双写、远端重命名、自动认领或迁移。
- `/llm/weaponry` 的任务级临时检索 Workspace、Provided-Evidence Workspace、术语 Workspace 和
  Chat Workspace 命名均未改变。
- 本次只完成只读残留审计；开发数据若需整体清理，仍须在明确维护窗口内先核对活动 Worker、
  资源所有权和精确范围，禁止按名称前缀盲删所有权不明资源。

## 4. 接口文档

经负责人批准，只将 `docs/接口文档/分类节点变更.md` 处理逻辑中的内部名称说明更新为
`archId-{规范化后的 newArchitectureId}`。该文字不新增或删除前后端参数，也不改变公开错误体、
状态码或同步 Saga 语义。`/llm/weaponry` 接口文档无需修改，因为该接口不会创建永久知识谱系
Workspace。

## 5. 阶段 0～6 验收证据

所有 Python 测试均使用项目 `venv`、临时 SQLite 与 Fake/Mock，不连接真实 AnythingLLM。

| 门禁 | 结果 |
| --- | --- |
| 命名、Analysis、Knowledge Gateway、Reassign、架构联合 | 154 项通过 |
| Weaponry 动态发现 `test_weaponry*.py` | 290 项通过 |
| 接口路由与契约资产 | 67 项通过 |
| 最终命名与架构复核 | 38 项通过 |
| 安全全仓动态发现 | 发现 2,193、精确排除 13、执行 2,180 |
| 安全全仓结果 | 失败 0、错误 0、跳过 3、预期失败 0、意外成功 0 |
| 静态检查 | `compileall`、旧前缀生产扫描、`git diff --check` 通过 |
| 主入口检查 | `run.py` 零修改、零执行 |

新增并发门禁以 10 个线程计算 50 个不同 architecture ID，得到 50 个精确且唯一的名称；既有
Knowledge Gateway 测试继续覆盖共享 SQLite 下的幂等创建/文档协调。测试日志中的 ERROR/CRITICAL
来自预期故障注入，不是 unittest failure/error。

安全全仓沿用项目长期冻结的 13 个精确排除项：7 个可能启动本地服务或 Shell 的环境测试、5 个
依赖 `.gitignore` 本地样例的资产测试，以及 1 个 Windows 不支持的 POSIX 权限位断言。首次统计
脚本因 discovery 模块名前缀不一致而未实际排除这些用例；该轮仍执行 2,193 项并全部通过、跳过
13 项，但不作为正式安全套件结果。修正后确认 13/13 ID 精确匹配并重新执行，以上 2,180 项统计
才是阶段 6 的正式门禁证据。

## 6. 本机 AnythingLLM 隔离真实验收

负责人已授权当前开发环境执行计划各阶段。阶段 7 仍先独立校验 `.env` 目标为回环地址，随后
仅使用虚拟 architecture ID 和本次新建回执确认归属的资源，没有启动 Flask 或 `run.py`：

- 写入前 Workspace 基线为 4；`archId-1785689667947`、`archId-1785689667948` 均为零碰撞；
- 生产 `LegacyAnalysisKnowledgeAdapter + AnythingLLMKnowledgeIndexFactory` 完成无敏感内容临时
  Markdown 的永久入库，本地协调状态为 `committed`，远端精确名称、slug、本地 architecture
  mapping、协调表 collection name 和文档成员关系全部核对通过；
- Weaponry 任务级生产 Client 创建隔离临时 Workspace、绑定同一验证文档、精确查回并删除，
  证明永久文档可进入其临时范围；这不等同于完整模型抽取或生产质量验收；
- 生产 Reassign Adapter 对 `1785689667948` 返回 `APPLIED + CREATED_BY_OPERATION`，且远端
  唯一精确匹配名称为 `archId-1785689667948`；
- 最终复核再次由永久 Knowledge Gateway、Weaponry 任务级 Client 和 Reassign Adapter 创建并
  核名；三个目标残留均为 0、清理错误为空，Workspace 全量 slug/name/document-membership 快照
  与写入前完全一致，总数从 4 恢复为 4；本机验收数据库目录在子进程退出后完成边界校验并删除。

首次和第二次完整验收的远端业务步骤均执行到 Python 临时目录退出，但 Windows 拒绝删除仍被
SQLite 服务短暂占用的本机数据库文件，因此两轮退出码为 1，不计为门禁通过。每轮随后都只读
确认：两个永久名称与 Weaponry 临时名称残留为 0、Workspace 总数为 4、Analysis 协调状态为
`committed`、验证文档的根清单 location/basename 均无残留。最终轮改为在 Python 子进程退出后
由 PowerShell 校验临时目录绝对路径和固定前缀再删除，退出码为 0；前两轮遗留的两个本机临时
SQLite 目录也已按相同边界校验后删除。

## 7. 证据边界

当前证据证明 Windows 临时 SQLite/Fake 下的单实例命名、创建协调、补偿和恢复语义，不证明真实
AnythingLLM、浏览器/UI、多实例分布式唯一性、可靠队列、共享数据库一致性、容量或 exactly-once。
真实证据仍不证明浏览器/UI、完整 Weaponry 模型抽取质量、多实例分布式唯一性、可靠队列、共享
数据库一致性、生产容量或 exactly-once。未来更换 AnythingLLM 版本或部署形态后仍须重新执行
隔离验收；若服务不可用、目标名称碰撞、资源范围不明或清理结果未知，必须停止并保留现场。
