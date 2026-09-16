# 阶段 1C-5 Callback Guard 与资源恢复闭环执行记录

## 0. 记录信息

| 项目 | 内容 |
| --- | --- |
| 执行日期 | 2026-07-16 |
| 所在分支 | `refactor/concurrency` |
| 对应设计 | `../重构记录/260716-阶段1C报告生成文件级实施设计.md` 的 1C-5 |
| 执行结论 | **阶段 1C-5 已完成并通过离线验收；阶段 1C 尚未完成，下一子波次为 1C-6** |
| 公开接口影响 | 未增删参数或改变公开字段；本轮原始实现未改接口文档，后续全面审查按明确许可补充“发送中立即 409”和人工解除安全前提 |
| 数据处理 | 新增内部 SQLite 表；全面审查后资源 payload 升级为 Schema v3 并兼容读取 v2。历史数据仅为测试数据，按已确认口径不迁移 |
| 运行限制 | 未启动 `run.py`，未连接真实 AnythingLLM、模型、回调、翻译或其他后台服务 |

---

## 1. 本阶段完成内容

### 1.1 Callback Guard 写闭环

1. `SQLiteReportCallbackAdapter` 已作为真实 Adapter 进入新的报告离线执行链：
   - acquire 在 SQLite 短事务内权威复核 latest、execution 终态和 Guard 状态；
   - complete 使用 lease token 与单调 fencing token 做 CAS；
   - HTTP 调用始终位于数据库事务外；
   - 2xx、非 2xx、明确未送达和发送结果未知分别收敛为稳定内部 outcome。
2. 新增内部 `ReleaseUnknownReportCallback` 命令和 `release_unknown` Port：
   - 只把 `outcome_unknown` Guard 释放为 `idle`，不重发旧回调；
   - 不修改旧 execution 已保存的 `delivery_outcome_unknown`；
   - 普通成功/拒绝后的 idle Guard 返回 `not_frozen`，不会误报为“已人工解除”。
3. 新增追加式 `callback_guard_release_audits`：
   - 唯一键为 `business_type + business_key + lease_version`；
   - owner execution、fencing 版本、操作者、原因和时间与 Guard 释放在同一事务提交；
   - 下一次 acquire 即使清空 Guard 当前快照的 release 字段，也不会覆盖历史人工解除审计；
   - 50 个并发解除命令只有一个写入者，其余 49 个幂等观察首次结果。

### 1.2 终态权威 Artifact 所有权

1. 新增 `ReportResourceRecord`、`ReportResourceStorePort` 和
   `SQLiteReportResourceStoreAdapter`，以一条 execution 对应一条任务资源恢复记录。
2. 资源记录持久化以下事实：
   - Artifact 命名空间和最终报告引用；
   - AnythingLLM cleanup ref；
   - 原子交互审计 receipt；
   - 外部/本地清理分状态；
   - 待追加的精确 lifecycle 事件、下一序号和待清理 Artifact；
   - 外部调用占用、尝试次数、错误阶段及 version CAS。
3. `prepare_cleanup` 在一个 SQLite 写事务中读取不可变 execution 终态：
   - 只有 `succeeded` 结果中的 `report_artifact` 与资源记录完全一致时才进入 `retained`；
   - `failed` 或 `stale` execution 的 `retained` 恒为空；
   - 因此旧 Worker 即使已经写出 `output/report.html`，终态条件写未命中时也不能自行声明
     所有权，未提交 final 会被清理，不会成为永久孤儿文件。

### 1.3 cleanup、审计恢复与隔离

新增 `ReportResourceRecoveryService`，顺序固定为：

```text
execution 终态权威确认
  -> 外部清理占用事实 CAS
  -> AnythingLLM 清理（事务外）
  -> 精确 lifecycle 事件持久化
  -> 交互审计幂等追加
  -> 本地未保留 Artifact 清理
  -> cleaned
```

具体收敛规则如下：

- conversation、context 和 global document 三类删除均参与整体成功判断；漏删对话线程不再
  被误报为 cleaned。
- AnythingLLM 删除返回 404 表示目标已经不存在，按幂等成功处理，允许崩溃后的重复确认
  最终收敛。
- 外部删除已完成但审计追加失败时，精确事件先保存在资源记录中；恢复只重放同一批事件，
  不再次调用 AnythingLLM。
- 明确删除失败可以用更高连续 sequence 再次尝试；共享交互审计允许 `failed -> failed` 或
  `failed -> deleted`，旧失败证据只追加、不覆盖。
- 本地 Artifact 清理失败只保存精确 pending 引用；重试本地文件时不重复已经成功的外部
  删除。
- 外部调用开始前保存 token、开始时间和心跳；每个 lifecycle event 在下一项 DELETE 前
  CAS 落库。占用期内的并发恢复只返回 pending；过期/中断后先追加已保存事件，再按连续
  sequence 重放幂等 DELETE。只有创建/上传等非幂等写副作用结果未知时才进入隔离。
- 审计凭据缺失、审计硬门禁失败或外部结果无法证明时进入 `quarantined`，自动恢复不得
  越过该状态。

这里的 quarantine 是**持久状态上的逻辑隔离**：保留任务命名空间和外部引用，不执行递归
删除，也不宣称已经把文件物理移动到 `quarantine/`。后续运维命令必须先核实现场再解除或
补偿。

### 1.4 报告执行链接入

`RunReportTask` 已在以下时点保存资源事实：

1. Artifact scope 建立后、下载业务文件前登记命名空间；
2. RAG 返回 cleanup ref 后、提交审计前登记外部资源引用；
3. 审计成功后登记 receipt；
4. 最终 HTML 写入后、提交成功终态前登记 final Artifact；
5. 终态提交后执行权威 cleanup；审计证据不完整时只隔离现场。

资源事实写入异常被提升为 `ReportTaskPersistenceError`，禁止继续形成第二个终态。终态后的
回调或清理控制面失败仍不会反向覆盖已经确定的业务成功/失败。

---

## 2. 主要文件

| 文件 | 作用 |
| --- | --- |
| `app/modules/report/ports/callbacks.py` | 人工解除命令、结果及 Callback Port 扩展 |
| `app/modules/report/adapters/callback_guard.py` | SQLite Guard、事务外 HTTP、人工解除映射 |
| `app/modules/report/ports/resources.py` | 资源记录、分状态、恢复结果及 Store/Recovery Port |
| `app/modules/report/adapters/resource_store.py` | 资源 payload Schema v3（兼容读取 v2）、SQLite DTO 映射和 CAS |
| `app/modules/report/application/resource_recovery.py` | 外部清理、事件重放、Artifact 清理和隔离编排 |
| `app/modules/report/application/run_report.py` | 在执行关键点登记资源事实并使用统一恢复服务收口 |
| `app/modules/report/adapters/local_artifacts.py` | 清理 scratch 与未获终态所有权的最终报告 |
| `app/modules/report/adapters/anythingllm_rag.py` | 可恢复 sequence、三类资源删除和 404 幂等处理 |
| `app/modules/report/adapters/interaction_audit.py` | 三类删除事件的 cleanup 状态映射 |
| `app/services/llm_service/task_service.py` | 人工解除追加审计、资源表、终态所有权和恢复扫描 |
| `tests/test_report_callback_guard.py` | latest/fencing/HTTP/过期/50 并发解除/审计保留 |
| `tests/test_report_resource_recovery.py` | CAS、重放、重试、占用期限、隔离和 stale final 删除 |

---

## 3. 审查中发现并修复的问题

本阶段没有只按“测试先通过”即结束，而是在合并验收前再次检查了状态机，修复了三类会在
后续并发/可靠队列阶段放大的缺陷：

1. **人工解除审计会被下一租约覆盖**：原实现只写 Guard 当前行。现改为追加式审计表，
   Guard 快照仅用于控制，审计表用于历史追溯。
2. **对话线程删除失败可能漏报**：原成功判断只看 context/document。现三类外部资源统一
   参与 Application、Audit Adapter 和 TaskService 的成功/失败判断。
3. **并发恢复可能被误判为进程崩溃**：第一轮修复只增加固定占用期限，仍会把长批次误
   隔离。复查后改为 token + 逐步心跳 + 每事件检查点；过期租约只在幂等 DELETE 协议下
   恢复，不再把整批永久隔离。

此外，删除 404 已改为幂等成功；运行中的 tracking 记录不进入可恢复扫描，避免未来
Dispatcher 对尚未终态的任务形成无效热循环。

---

## 4. 测试与静态检查

全部 Python 命令均使用项目 `venv`，没有运行 `run.py`。

| 检查范围 | 结果 |
| --- | --- |
| Guard/资源/RAG/Audit 定向测试 | 33 项通过 |
| I/O + Guard + Resource + RAG + Audit + 架构复核 | 54 项通过 |
| 阶段 1C 报告合并回归 | **159 项通过，0 失败，0 错误** |
| 安全全仓回归 | **72 个模块、825 项通过，0 失败，0 错误** |
| 并发验收 | Callback acquire 50 线程唯一 owner；人工解除 1/49；资源 CAS 1/49；在途外部清理不误隔离 |
| Python 编译检查 | 阶段相关生产代码和测试通过 |
| 架构/AST/print 门禁 | 全部通过；Application/Port 未放宽依赖白名单 |
| 差异检查 | `git diff --check` 通过，仅有既有 Windows CRLF 转换提示 |

安全全仓回归显式排除了以下历史测试：

- `test_local_scripts.py`：部分用例会启动 `run.py`；
- `test_multilingual_translation_integration.py`：可能加载真实翻译模型或外部资源；
- `test_migrate_analysis_security.py`：包含 Windows 无法表达为 POSIX `0640` 的历史断言；
- `test_test_assets.py`：依赖仓库中缺失的历史请求 fixture。

这些排除项与 1C-5 修改路径无交集，且已在先前执行记录中登记；本阶段没有新增失败类别。

本表记录 1C-5 当次验收快照。后续第二轮全面审查的 836 项安全回归、逐事件心跳恢复和
新增风险门禁见 `260716-阶段1C第二轮全面审查风险修复执行记录.md`。

---

## 5. 接口文档核对

本阶段只改变内部执行、回调交付控制和资源恢复事实：

- 没有增删 `/llm/generate-report` 请求参数；
- 没有改变响应体、HTTP 状态、Progress 或 callback payload；
- 人工解除是内部 Application/Adapter 能力，不是新的公开 HTTP 接口；
- 生产 Flask 路由仍走遗留 `report_service.py`。

全面审查后，已按用户明确许可同步 `docs/接口文档/文件处理和报告生成.md`：旧 callback
处于 sending/unknown 时立即返回既有 409，不再同步等待；人工解除前必须确认旧 Worker
已停止或隔离。没有增删任何请求、响应或回调参数。202 空体和 409 的生产路由仍待 1C-6
切换，并继续由接口契约测试保护。

---

## 6. 明确未完成和后置责任

1. **尚未进入生产**：新 `RunReportTask`、Callback Adapter 和资源恢复链只在离线组合测试中
   运行；当前生产入口、daemon 线程和 JSON 受理体均未切换。
2. **阶段 1C 尚未完成**：1C-6 仍需实现数据库扫描 + Event 有界唤醒 Dispatcher、Container
   组合、旧在途排空和薄路由切换；1C-7 仍需完成切流后的总验收与旧路径证据。
3. **不是可靠队列**：尚无 MySQL、MinIO、Outbox、RabbitMQ/Celery、late ACK、DLQ、
   Worker lease/reaper 或跨实例锁；这些仍属于阶段 2～6。
4. **人工解除尚无公开/运维命令入口**：当前只有内部 Port/Adapter 和追加审计读取能力；
   正式受控运维命令、权限和运行手册仍由阶段 5/11 完成。
5. **外部调用占用期限**：当前内部默认值为 300 秒且可注入，仅用于 1C 单实例过渡链。
   1C-6 组合根必须按 AnythingLLM 请求超时和单任务最大资源数显式配置；多实例心跳/续租
   仍由后续通用任务基础设施承担。
6. **保留周期仍未确定**：未启用自动 TTL；quarantined、待审计、待回调和恢复中的资源
   均受保护。

---

## 7. 下一步

进入 1C-6 前，应以本记录和文件级设计为门禁，依次完成：

1. 实现持久化 accepted 扫描 + 常量空间 Event 唤醒的单实例 Dispatcher；
2. 在启动和周期任务中有界调用 `ReportResourceRecoveryService.sweep(limit)`，并输出分类
   指标；只恢复 accepted，阶段 2 前不得把 running 盲目重置为 accepted；
3. 在唯一 Container 中显式装配 Task/File/Artifact/RAG/Audit/Callback/Resource/Dispatcher；
4. 停止新受理，盘点并排空或隔离旧 daemon 与新 execution，证明不会双执行/双回调；
5. 最后切换 `/llm/generate-report` 为 Parser → Submit → Presenter，启用已批准的 202 空体和
   活动/unknown 409；
6. 切换后再进入 1C-7 全量验收和旧路径删除证据整理。
