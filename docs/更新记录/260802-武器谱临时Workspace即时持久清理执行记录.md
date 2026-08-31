# 武器谱临时 Workspace 即时持久清理执行记录

## 一、问题与确认结论

`/llm/weaponry` 的 Provided-Evidence Extraction 为每个字段、来源文档和 attempt 创建独立
AnythingLLM workspace/thread。这些对象用于隔离一次抽取调用，任务完成后没有继续承载业务查询
的用途；但它们只能在本地资源记录能够证明 `owned`、任务已进入权威终态且外部删除结果可确认时
自动清理。

只读检查开发环境任务数据库快照时，曾观察到一个已成功 execution 仍保留 239 条
`cleanup_pending` 资源，其中包含 117 个 extraction workspace、117 个来源会话、1 个检索范围和
4 条文档绑定。原恢复器每个固定周期只为同一任务处理一条资源，因此大任务的清理吞吐可能明显
低于临时资源创建速度。这解释了 AnythingLLM 中大量 `docsense-weaponry-extraction-*` workspace
长期存在的现象；是否包含对话只反映该 attempt 是否已经创建/使用 Thread，不能作为安全删除依据。

本次确认并实施以下执行语义：

1. 业务终态和 Callback 不等待远端清理；
2. 资源身份必须先持久化；正常路径提交清理意图后立即提示维护线程，意图提交异常则由终态
   execution 与 `tracking` 事实进入同一保守恢复；
3. 固定周期扫描与进程启动扫描继续承担提示丢失、重启和历史积压的兜底；
4. 任何 Interaction Audit、DELETE 或检查点结果未知，以及所有权/任务身份无法证明的对象，均
   隔离等待人工对账，禁止自动重试删除；
5. 禁止仅凭 `docsense-weaponry-extraction` 名称前缀批量删除远端资源。

## 二、实现内容

### 2.1 通用 Dispatcher：提示唤醒而非内存任务队列

`LocalPersistentTaskDispatcher` 为每条维护线程持有独立 `threading.Event`。Event 只合并“可能有
工作”的提示，不保存 TaskId 或资源身份，因此内存占用为常量；SQLite Store 仍是唯一工作事实。
停止、启动失败和致命线程异常会唤醒全部维护等待，避免固定周期延长进程关闭时间。

该能力是通用基础设施，但现阶段只有 Weaponry 资源维护明确调用；Report、Analysis 和 Weaponry
Callback Guard 仍保持原有固定周期语义。

### 2.2 Weaponry 终态：落库后唤醒，调用方不等待

Weaponry Runner 返回已提交的终态结果后，Dispatcher 仅在 `cleanup_pending`、`port_error` 或
`cas_exhausted` 表明资源仍需后台收敛时提示资源维护线程。提示结果只写脱敏结构化日志，不参与
业务结果判定；Dispatcher 正在停止或提示已经合并，都不得回滚已经完成的任务终态。

资源提示不会唤醒 Callback Guard，也不会让 Callback 等待远端 DELETE。

### 2.3 资源批次：按操作数有界、按任务轮转

恢复器保留“每次 `recover` 最多执行一个外部副作用、每项独立 lease/fencing/checkpoint”的安全
边界，但将单轮 `limit` 的含义收敛为“最多执行多少次资源恢复操作”：

- 先为本轮候选任务各推进一项，再把仍有已确认清理进展的任务放回轮转队列；
- 一个大任务可以在同批次清理多项资源，又不会长期饿死后续任务；
- 每项外部副作用完成后检查停止信号，停机不会继续扫完整个大批次；
- 批次产生可证明进展且仍可能存在积压时，再设置同一 Event，让下一批在边界处立即继续；
- 明确失败不在当前批次热循环，而是遵守 Store 中已有持久冷却时间。

这只是单实例线程内的公平调度。跨实例互斥与正确性仍由现有 SQLite cleanup lease/fencing 和条件
提交保护；本次没有把进程内 Event 描述为可靠队列、分布式租约或多实例通知机制。

## 三、安全边界

以下情况保持保守失败，不因“立即清理”需求而放宽：

- `TRACKING` 资源只有在 execution 的 TaskId、业务键、输入快照和权威终态全部一致后，才能转入
  `cleanup_pending`；running 或缺失 execution 不按年龄推测；
- pending Interaction Audit 表示外部调用结果可能未知，资源现场直接 quarantine；
- DELETE 超时、断连、Adapter 异常或返回契约错误均不得盲目重试；
- 外部删除已发生但本地 checkpoint 不能可靠提交时，优先隔离而不是再次 DELETE；
- shared 文档绑定和未登记对象不进入 owned 自动删除候选。

因此，部署后既有数据库里仍为 `tracking/cleanup_pending` 且身份完整的 owned 资源可以由启动扫描
继续回收；既有 quarantined/outcome-unknown 资源不会自动删除，仍需人工对账。

## 四、公开契约与数据兼容性

- 未修改 `docs/接口文档/`；
- 未新增、删除或改名任何前后端请求/响应参数；
- 未改变 `/llm/weaponry` 的 202 空体、既有错误状态码、Header 或 Callback 数据结构；
- 未修改 SQLite Schema，无需数据库迁移；
- 仅增加内部 Port 的停机提示参数和内部恢复批次统计字段。

## 五、验证结果与证据边界

使用项目虚拟环境、临时 SQLite 和 Fake/Mock 完成离线验证，没有运行 `run.py`，没有连接真实
AnythingLLM，也没有修改开发/生产远端资源：

- `tests.test_weaponry_dispatcher tests.test_weaponry_stage1d6 tests.test_dependency_container`：
  66 项通过；
- `discover -p "test_weaponry*.py"`：290 项通过；
- `tests.test_report_dispatcher tests.test_analysis_dispatcher`：21 项通过，证明通用维护等待改动未破坏
  两条既有 Dispatcher 回归；
- `tests.test_architecture_boundaries tests.test_stage1g_closeout tests.test_dependency_container`：
  56 项通过；首次运行准确检出 Port 层错误地从 `collections.abc` 引入 `Callable`，改为允许的
  `typing.Callable` 后复跑通过，没有把首次失败计入成功证据；
- 新增用例覆盖终态立即提示、积压自动续扫、Callback Guard 不被连带唤醒、逐项恢复尝试数上限、
  多任务轮转公平性、停机单项边界和结果未知隔离；
- Python 定向编译与 `git diff --check` 均通过。

上述证据只证明 Windows、临时 SQLite、Fake/Mock 下的单实例离线行为，不证明真实 AnythingLLM
DELETE 延迟、跨实例 Event 传播、生产容量或 exactly-once。真实部署仍需观察清理积压、失败退避和
quarantine 告警，并由后续可靠任务队列/多实例协调阶段补齐跨进程即时通知能力。
