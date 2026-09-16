# 阶段 1C-4：报告生产 I/O 与审计 Adapter 执行记录

## 0. 执行结论

阶段 1C-4 已于 2026-07-16 完成。报告模块已经具备可独立装配的任务级 Artifact、执行时
文件处理、多文档 AnythingLLM RAG 和原子 Interaction Audit Adapter，并通过真实 SQLite
任务事实与本地文件系统参与的离线组合测试。

本阶段没有切换 `/llm/generate-report` 生产路由，没有启动 `run.py`，没有连接真实
AnythingLLM、文件服务器或回调方，也没有部署到生产环境。遗留 Flask 路由和
`report_service.py` 仍是当前实际运行链路，因此本记录不能被解释为阶段 1C 全部完成或
可靠队列已经上线；下一子波次为 1C-5。

| 项目 | 结论 |
| --- | --- |
| 执行分支 | `refactor/concurrency` |
| 对应计划 | `../重构记录/260716-阶段1C报告生成文件级实施设计.md` 的 1C-4 |
| 公共接口 | **无变化**；没有增删请求、响应、回调、Progress 或 WebSocket 参数 |
| 接口文档 | 已复核，无需修改 `docs/接口文档/`；已批准的 202 空体/409 仍待 1C-6 切路由 |
| 内部数据 | 审计 Schema v2 → v3，只增不删；新增 `trace_id`/`call_id`，初始化可幂等补列 |
| 历史数据 | 测试数据无需迁移；新增列提供空字符串默认值，不重写既有审计记录 |
| 生产状态 | 未装配、未发布、未进入生产 |

---

## 1. 完成的改造

### 1.1 任务级 Artifact Adapter

新增 `app/modules/report/adapters/local_artifacts.py`：

1. 不把原始 TaskId 直接拼入路径，而是使用 TaskId 的 SHA-256 摘要建立
   `report/<digest>/` 命名空间，避免斜杠、盘符、`..` 或超长业务值变成路径语义。
2. scratch 按源文件、规范化文件、模板和临时输出分类；final 报告单独保存。Adapter 会
   验证解析后的绝对路径仍位于任务根目录且类别匹配，伪造或跨任务引用会被拒绝。
3. 每个发布结果返回不透明相对 artifact ID、字节数和 SHA-256 摘要，不向 Domain 或
   Application 泄漏本地绝对路径。
4. 最终 HTML 使用同目录临时文件、flush、`fsync` 和 `os.replace` 原子发布，进程中途失败
   不会把半个报告暴露为最终产物。
5. 成功路径只清理 scratch，保留 final；失败路径保留当前任务现场，交由后续 cleanup/
   quarantine 恢复闭环处理。

50 个 Barrier 同步线程分别创建和发布 Artifact，验证了命名空间唯一、内容不串扰、引用
不可跨类别伪造以及清理不会删除其他任务或最终报告。

### 1.2 执行时文件处理 Adapter

新增 `app/modules/report/adapters/legacy_files.py`：

1. 下载发生在 `RunReportTask(task_id)` 恢复任务快照之后，而不是 Web 请求受理阶段，后续
   Worker 排队不会长期占用 Web 线程或依赖请求上下文。
2. 兼容包装既有 downloader、文件规范化/MHTML-OCR 处理和 Word 文本提取能力，暂不重写
   已验证的复杂文件算法。
3. downloader 或遗留工具返回的每一份文件都会重新发布到当前 TaskId 的 Artifact
   命名空间；Application 只能继续使用任务级引用，不能持有共享临时路径。
4. URL 查询参数不会进入文件名或日志；底层异常统一映射为报告 Port 的稳定阶段错误，
   日志只记录任务、阶段和异常类型，不记录完整 Prompt、URL 查询参数或文件正文。
5. 按已确认决策，本阶段不新增 URL host/协议安全策略；该延期不等于永久放弃安全评审。

### 1.3 多文档 AnythingLLM Report RAG Adapter

新增 `app/modules/report/adapters/anythingllm_rag.py`：

1. `AnythingLLMReportClientFactory` 每次 RAG 调用创建独占 Transport、Document、Workspace
   和 Thread Client；调用结束统一关闭，不复用跨任务的可变会话对象。
2. Adapter 按请求快照中的原始顺序上传和绑定全部文档，每份文档生成唯一来源标记，随后
   只进行一次报告查询。输入重复和顺序均不会被集合去重破坏。
3. 上传、绑定和查询分别记录资源生命周期；绑定仅对明确的瞬时失败做有限重试。任意阶段
   失败都会携带截至失败点的完整 trace，不把“部分文档已经创建”伪装成全失败无现场。
4. 每次执行保留外部 `trace_id`，每次模型请求生成唯一 `call_id`；attempt 记录 Prompt
   摘要、来源匹配/缺失/冲突统计、状态、失败阶段和脱敏错误。`None` 或空模型内容仍按既有
   兼容契约视为成功的空报告内容。
5. cleanup token 是版本化、不透明、可校验的编码值，包含清理所需的 Workspace、Thread、
   文档位置和生命周期序号。清理使用新的任务级 Client 租约，Thread、Workspace 和每份
   Document 独立尝试；某一步失败不会抹去其他步骤结果。
6. 如果清理阶段连 Transport 都无法创建，也会为所有待删除资源生成连续、可持久化的失败
   lifecycle，而不是只写一条日志后丢失恢复依据。
7. 如果上传/绑定/查询已经失败且 Transport 关闭同时失败，主阶段错误不会被 close 异常
   覆盖；两类失败会分别进入同一 trace。清理 Transport 无法建立时，Thread、Workspace
   和每份 Document 都会形成明确的删除失败事件。

### 1.4 原子 Interaction Audit Adapter 与 Schema v3

新增 `app/modules/report/adapters/interaction_audit.py`，并扩展共享审计存储：

1. `SQLiteReportInteractionAuditAdapter` 将报告 RAG trace 无损转换为共享审计 DTO，通过
   `LLMTaskService` 的单个事务同时保存主交互、全部 attempts 和初始 lifecycle。
2. 只有事务提交并返回成功凭据，Application 才允许写成功终态或进入外部资源清理；审计
   异常会阻断成功 callback，并保留本地/AnythingLLM 现场供人工恢复。
3. cleanup lifecycle 可以按已保存序号幂等追加，并同步更新 cleanup 状态。重复提交同一
   审计幂等键必须保持摘要一致；旧 execution 在同一业务键已有新 owner 后不得追加审计。
4. `RagExecutionTrace` 增加 `trace_id`，`RagAttempt` 增加 `call_id`；内部审计版本由 2 升为
   3。`llm_interactions.trace_id` 和 `llm_interaction_attempts.call_id` 均使用只增不删的
   Schema 初始化和幂等补列，不修改任何公共接口字段。

### 1.5 离线生产组合与故障门禁

新增组合测试，以真实 SQLite Task Adapter、真实本地 File/Artifact Adapter、真实 Report
RAG Adapter、真实 Audit Adapter以及仅替代网络边界的内存 Client 驱动
`RunReportTask(task_id)`：

- 成功链路从不可变 execution 快照恢复输入，生成最终 HTML、保存 trace/call 审计并写入
  条件终态；所有写入仍携带 expected TaskId。
- 通过 SQLite trigger 强制审计明细插入失败后，主审计、attempt 和 lifecycle 全部回滚；
  任务进入失败路径，只产生一次失败 callback，最终 Artifact 与外部 RAG 资源现场保留。
- 构造/导入 Adapter 不连接网络、不启动线程、不导入 Flask 路由或生产组合根。

### 1.6 扩大回归中修复的既有幂等性缺陷

扩大回归发现 `AnythingLLMKnowledgeGateway.store_prepared_document` 对已 committed 记录直接
返回，导致“同一幂等键 + 不同 metadata/文档位置”的后到请求绕过
`KnowledgeIndexOperationService.begin` 的不可变身份比较。

现已删除该提前返回，让精确重放和身份冲突统一经过 `begin` 判断。修复不改变公共请求/
响应参数，也不新增网络调用；对应 Knowledge Gateway 14 项测试全部通过。

---

## 2. 主要修改文件

| 文件 | 作用 |
| --- | --- |
| `app/modules/report/adapters/local_artifacts.py` | TaskId 哈希命名空间、Artifact 发布/解析、原子最终 HTML、scratch 清理 |
| `app/modules/report/adapters/legacy_files.py` | 执行时下载、规范化、Word 提取及遗留输出重新收口 |
| `app/modules/report/adapters/anythingllm_rag.py` | 任务级 Client、多文档保序 RAG、完整 trace、cleanup token 和清理轨迹 |
| `app/modules/report/adapters/interaction_audit.py` | 报告 trace 原子持久化与 cleanup lifecycle 追加 |
| `app/ports/rag.py` | 共享 RAG trace/attempt 的可选 trace/call 身份 |
| `app/services/llm_service/interaction_audit_service.py` | 内部审计版本升级为 v3 |
| `app/services/llm_service/task_service.py` | trace/call 列、幂等补列、摘要、写入与读取投影 |
| `app/integrations/anythingllm/knowledge_gateway.py` | committed 重放仍执行不可变身份校验 |
| `tests/test_report_io_adapters.py` | 文件与 Artifact 隔离/并发/失败测试 |
| `tests/test_report_rag_adapter.py` | 多文档 RAG、trace、来源和 cleanup 故障测试 |
| `tests/test_report_interaction_audit_adapter.py` | 原子审计、幂等追加和旧执行拒绝测试 |
| `tests/test_report_runtime_adapters.py` | 真实本地/SQLite 组合与审计回滚门禁 |

---

## 3. 测试与检查结果

全部 Python 测试均使用项目 `venv`，没有运行 `run.py`。

| 检查范围 | 结果 |
| --- | --- |
| 1C-4 新增 Adapter/组合测试 | **18 项通过**：5 I/O + 8 RAG + 3 Audit + 2 runtime composition |
| 报告、任务、审计、架构和 AnythingLLM 定向扩大回归 | **284 项通过，0 失败，0 错误** |
| 受运行约束允许的全仓安全回归 | **71 个模块、808 项通过，0 失败，0 错误，0 跳过** |
| Knowledge Gateway 修复后定向回归 | **14 项通过** |
| Python 编译检查 | `compileall` 通过 |
| 架构/AST 与 `print` 门禁 | 通过 |
| 差异格式检查 | `git diff --check` 通过；仅有既有 CRLF 转换提示，无空白错误 |

安全全仓回归排除了以下不适合在本阶段自动执行的历史测试，而不是把它们计为通过：

- `test_local_scripts.py`：部分用例会启动 `run.py`，违反本次明确运行限制；其依赖的若干
  请求 fixture 在当前仓库也不存在。
- `test_multilingual_translation_integration.py`：可能加载真实翻译模型或外部资源，不属于
  离线 Adapter 验收。
- `test_migrate_analysis_security.py`：包含 Windows 上无法表达为 POSIX `0640` 的既有权限
  断言，与本阶段修改路径无交集。
- `test_test_assets.py`：依赖当前仓库缺失的历史请求 fixture，与 1C-4 无交集。

以上排除项均已在既有阶段记录中作为历史基线问题说明；本阶段没有新增同类失败。最终
门禁使用显式安全模块集合，避免测试发现过程误启动主进程或连接真实服务。

---

## 4. 架构与并发检查结论

1. Report Application 仍只依赖 Port；供应商 Client、SQLite、路径和遗留文件工具全部留在
   Adapter。业务代码未新增 Flask、线程、全局 Queue 或供应商 DTO 依赖。
2. TaskId 是所有目录、审计和 RAG 生命周期的隔离主键；同一 `reportId` 的旧 execution
   不能借本阶段 Adapter 绕过 expected TaskId 条件写覆盖新投影。
3. 并发测试证明 50 个任务的本地目录与 Artifact 内容隔离，但不等价于 50 个真实模型重型
   任务同时执行。模型任务仍应由后续 Worker 并发许可进行资源限流并持续排队。
4. Client 是调用级对象，未引入跨任务共享可变会话；这为后续多线程 Worker 和多实例部署
   保留了替换空间。
5. SQLite 与本地文件系统仍是开发阶段兼容实现。阶段 3 将以同步 SQLAlchemy/MySQL
   Repository 与 MinIO 替换对应 Adapter，Domain/Application 无需随基础设施重写。

---

## 5. 尚未进入生产的能力

以下内容明确不属于 1C-4 完成范围：

1. 1C-5：把已验证的 Callback Guard 装配进报告执行链，完成 Artifact 所有权、cleanup/
   quarantine 的持久恢复事实和人工解除审计。
2. 1C-6：持久化积压 + 有界唤醒 Dispatcher、Container、旧在途任务排空、Flask 薄路由
   切换，以及已批准的 HTTP 202 空体和 409 正式生效。
3. 1C-7：阶段 1C 最终全量契约、故障、并发和发布前验收。
4. 阶段 3～6：MySQL/SQLAlchemy、MinIO、Outbox、RabbitMQ/Celery、late ACK、DLQ 和可靠
   callback Worker。当前实现不能描述为可靠队列或多实例一致性已经完成。
5. 阶段 10：真实 50+ 在途任务、50 条长连接和短请求稳定并发，以及真实模型限流排队的
   集成容量基线。该门禁仍按已批准决定延期到完整集成环境。

---

## 6. 发布与回滚边界

本阶段没有发布动作。新 Adapter 没有被生产路由或组合根引用，因此代码级回滚只需停止
后续装配并回退本阶段 Adapter；不需要处理生产在途任务。审计 v3 为只增不删列，未来即使
回退使用旧代码也应保留新增列，避免破坏已经生成的 trace/call 证据。

阶段 1C-5/1C-6 以后若开始组合或切流，必须另行执行旧在途排空、Guard/cleanup 恢复和
停受理回滚流程，不能沿用本阶段“未接生产”的简化回滚结论。

---

## 7. 下一步

下一子波次为 1C-5：在不改变任何公共接口参数的前提下，完成 Callback Guard、任务级
Artifact 所有权与 cleanup/quarantine 可恢复闭环。只有 1C-5 门禁通过后，才能进入 1C-6
的 Dispatcher、Container 和公开路由切换。

当前没有需要新增确认的公共契约问题。URL 安全策略、真实服务联调和保留期仍沿用此前
已确认的延期口径；若后续实现需要改变 HTTP 状态、错误文本、回调字段或任何前后端参数，
必须先停止实施并取得确认。
