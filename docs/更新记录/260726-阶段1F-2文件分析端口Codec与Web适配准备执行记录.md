# 阶段 1F-2 文件分析端口、Codec 与 Web 适配准备执行记录

> - 执行日期：2026-07-26
> - 对应设计：[阶段 1F 文件分析高内聚收口文件级实施设计](../重构记录/260726-阶段1F文件分析高内聚收口文件级实施设计.md)
> - 实施范围：仅完成阶段 1F-2；1F-3～1F-7B 尚未实施
> - 公开契约结论：未修改 `docs/接口文档/`、`/llm/analysis` 路由、前后端接口参数、请求/响应字段、状态码、Header、Task Schema、callback 或 Progress 格式

## 完成内容

1. 建立受理期不可变输入：

   - `AnalysisSubmissionSnapshot` 深冻结完整公开 `params`，保留未知扩展字段、空值、插入顺序以及
     `originalFileName` 的缺失/空值/原始文本语义；
   - `AnalysisPolicySnapshot` 与有效范围在受理点固定，Worker 将来不得重新读取环境变量或可变请求字典；
   - `AnalysisTaskInputV1` 绑定 schema、task、batch、顺序、文件业务键、请求快照、范围、策略、受理时间
     和 trace，不持有 Flask Request、数据库连接、线程、回调或可变容器。

2. 定义九类 Analysis Port：批量命令、文件准备、RAG、知识写入、审计、翻译、资源、回调和
   Dispatcher。DTO 仅传递内部 `AnalysisExecutionRef`，不允许把 `TaskId`、`batch_id`、lease 或
   execution 标识投影到公开响应。

3. 实现严格 V1 Codec：固定 envelope 键，拒绝未知 schema、额外/缺少字段、非法策略快照和
   `expected_task_id`/业务键/batch 身份不一致。Codec 返回深复制投影，不与领域快照共享可变引用。

4. 新增严格零 I/O Fake：所有 Port 通过同一线程安全期望队列执行。未配置调用、乱序调用、错误返回
   类型和未消费期望都会失败，避免后续 Application 因宽松 Fake 漏测副作用顺序。

5. 新增未接线 Web Parser/Presenter：

   - Parser 保留既有 `/llm/analysis` 的请求大小、JSON、`businessType`、`params`、重复文件名、文件
     路径和领域树校验顺序；
   - Presenter 保留 202 空体、409 冲突、503 繁忙和 400/413 单一 `error` 字段；未知异常重新抛出，
     以保持 Flask 现有 HTML 500 边界；
   - 当前 Blueprint 未导入它们，原路由的任务预查、原子受理、Progress 发布和后台线程没有改变。

6. 收口翻译并发边界：移除遗留 `LLMTranslationService` 的全局可变进度 callback。由于底层
   `DocumentTranslator`/MinerU 会修改共享输出目录，全文与摘要翻译在服务内使用同一 `RLock` 串行。
   新 Translation Adapter 同时要求由组合根注入共享协调器；翻译字段映射和空结果降级语义保持兼容。

## 接口与运行边界

- 未改动 `docs/接口文档/`，接口黄金资产继续验证其 SHA-256；因此没有发生需要确认的接口文档修改。
- 未修改 `app/blueprints/llm.py` 的 `/llm/analysis` 实现，也未新增、删除或重命名前后端接口参数。
- 未运行 `run.py`，未连接真实 AnythingLLM、模型、回调或其他后台服务；本次新增验证使用离线替身、
  临时目录和临时 SQLite。
- 进程内翻译锁仅解决当前单实例共享对象交错，不能声称具备可靠任务队列、分布式锁、多实例任务接管、
  数据库最终一致性或生产并发吞吐能力。

## 验证结果

| 检查项 | 结果 |
| --- | --- |
| 新增/变更 Python 文件 `py_compile` | 通过 |
| 1F-2 Port、Codec、Parser/Presenter、翻译隔离、1F-0/1F-1 与架构组合回归 | 170 项通过 |
| `test_analysis*.py` 定向发现回归 | 221 项通过 |
| 安全全仓离线回归 | 动态发现 1,662 项；明确排除既定 13 项后，1,649 项以 12 个无重叠批次全部通过，0 failure / 0 error |
| 架构边界、接口黄金、当前路由未接线 AST 门禁 | 通过 |

安全全仓排除项保持既定范围：7 项 `test_local_scripts.*` 会启动本地脚本或服务，5 项
`test_test_assets.*` 依赖本机 `.gitignore` 测试资产，另有 1 项迁移安全测试依赖 Windows 不支持的
POSIX `0640` 权限语义。为避免单个既有重计算用例触发 60 秒看门狗，执行时按 12 个无重叠区间分批，
全部已完成；未把中途超时的大批次结果计入通过数。

## 未完成边界与下一步

- 尚未实现 `RunAnalysisTask`、真实文件/RAG/知识/审计/资源/回调 Adapter、批量事务受理、持久
  Dispatcher 或公开路由切换；这些分别属于 1F-3～1F-7B。
- `analysis_service.py` 仍是当前生产 I/O 与 Worker 执行入口；1F-2 只移除了其中对翻译可变 callback
  的依赖，未改变其外部调用顺序或公开行为。
- 下一阶段 1F-3 应仅在严格 Fake 上实现只接收 `TaskId` 的 Application 与任务级 I/O，继续保持
  生产路由未切换；若涉及接口文档、路由、参数、响应、状态码、Progress 或 callback，必须先确认。
