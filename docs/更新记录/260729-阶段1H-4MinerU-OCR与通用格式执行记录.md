# 阶段 1H-4：MinerU、OCR 与通用格式执行记录

## 0. 执行结论

阶段 1H-4 已完成并通过离线门禁：

- MinerU 提交、轮询、排队、下载、安全解压和结果选择的唯一实现迁入
  `app/modules/document_processing/adapters/mineru.py`；
- 内置 OCR、扫描件判断和既有降级编排的唯一实现迁入
  `app/modules/document_processing/adapters/builtin_ocr.py`；
- `services/translator/MinerUConverter.py` 与 `services/utils/ocr_preprocessor.py`
  只保留兼容导出，`MarkdownHandler` 改为单向依赖 DocumentProcessing；
- 新增冻结的 MinerU、内置 OCR、TXT/MD/PDF 直通 Profile；
- 新增共享 FIFO 重型许可，只限制实际 MinerU/OCR I/O，不把 50 个 accepted/in-flight
  任务收进进程内业务队列；
- 新增模块自有 `document_processing_external_operations` 表：供应商提交前先写意图，
  收到 task id 后写供应商身份；
- 提交响应丢失、轮询/下载结果不确定时进入 `outcome_unknown` 并保留带所有权标记的
  scratch；供应商明确终态失败才记录普通失败；
- 完成 188 项扩展离线回归，失败 0、错误 0、跳过 3；
- 未运行 `run.py`、未连接真实 MinerU/OCR/AnythingLLM，也未修改接口文档。

---

## 1. 所有权与兼容边界

迁移后唯一实现位置如下：

| 能力 | 唯一实现 | 旧路径处置 |
| --- | --- | --- |
| MinerU 提交、轮询、下载、解压 | `adapters/mineru.py` | `translator/MinerUConverter.py` 仅重新导出 |
| 扫描件检测与内置 OCR | `adapters/builtin_ocr.py` | `utils/ocr_preprocessor.py` 仅重新导出 |
| TXT/MD/PDF 直通校验 | `adapters/passthrough.py` | 无旧实现副本 |
| 重型并发许可 | `adapters/capacity.py` | 不复用 Translation 全局锁 |
| 供应商身份事实 | `adapters/sqlite_operations.py` | 不写入旧 TaskService 私有表 |

DocumentProcessing 不导入 Translation、Flask、业务 Service 或遗留 TaskService。旧 Python
函数参数暂时保持，公开 HTTP/SSE/WebSocket/Callback 契约没有任何字段变化。

---

## 2. MinerU 外部副作用语义

一次 MinerU 调用按以下顺序执行：

1. Processing Record 取得当前步骤执行权；
2. 进入共享重型许可；
3. 在独占 `step_key` scratch 中物化源 Artifact；
4. 健康检查通过后，先持久化 `submission_intent`；
5. 向 MinerU 提交，取得供应商 task id 后立即持久化 `provider_identified`；
6. 轮询完成、下载结果、调用 MinerU 官方 `safe_extract_zip`；
7. 校验单文档只生成一个非空 Markdown，发布 Artifact 与 Lineage；
8. 成功或确定失败清理 scratch；未知结果保留现场等待显式对账。

健康检查明确失败发生在提交之前，可以作为确定失败。提交调用发出后响应丢失，或已取得
供应商身份后轮询/下载中断，都不能证明供应商没有执行，因此标记 `outcome_unknown`，禁止
盲目重投。供应商返回明确失败终态时可以记录普通失败。

当前 SQLite Observer 只证明单实例开发边界；阶段 2 必须纳入 Attempt/lease/fencing，阶段 3
再迁移到共享 MySQL 事务与可靠队列恢复流程。

---

## 3. OCR、直通与资源许可

- 内置 OCR Profile 冻结语言、DPI 和 PDF 输入格式；
- 新 Processor 不在任务执行中修改 `TESSDATA_PREFIX` 或 `MINERU_MODEL_SOURCE`，组合根必须在
  启动并发任务前一次性冻结进程环境；
- OCR 输出去除会导致同一步骤内容漂移的运行时时间戳；
- TXT/MD 直通校验 UTF-8、非空白、MIME 和尺寸上限；
- PDF 原件直通校验非空、MIME、尺寸和 `%PDF-` 文件头；
- FIFO 许可只包围实际 Processor 调用，Processing Record、Artifact 发布和任务等待不占用许可；
- 许可异常或 Processor 异常都能释放容量，不创建无界线程或内存任务队列。

Analysis 既有降级顺序仍为 MinerU → 内置 OCR → 原 PDF，Report 仍为内置 OCR → 原 PDF。本阶段
只迁移能力所有权，生产调用方的 Artifact 用例切换属于 1H-6。

---

## 4. 测试与验收

新增及扩展门禁覆盖：

- MinerU 服务不可用、提交响应丢失、未知结果现场保留；
- 空 Markdown、多个 Markdown、输出目录逃逸和安全解压调用；
- 外部操作必须先有提交意图，供应商身份漂移严格拒绝；
- 50 个 MinerU 任务具有不同 source/scratch/result/供应商身份；
- 50 个 OCR 任务实际并发不超过共享容量；
- FIFO 到达顺序、许可释放和零共享 Converter/Session/Callback；
- TXT/MD 非 UTF-8、纯空白、MIME/尺寸校验；
- 旧 OCR 降级顺序、MHTML、Legacy Office、Translation、Analysis、Report、Container 回归；
- AST 证明旧 MinerU/OCR 文件为薄 Facade，DocumentProcessing 零 Translation/业务层导入；
- 接口文档只读 Hash 保持不变。

```text
Ran 188 tests
OK (skipped=3)
```

`compileall` 与定向静态检查通过；故障注入产生的 ERROR/WARNING 日志属于预期测试证据。

---

## 5. 阶段边界与下一步

1H-4 没有把 Analysis、Report、RAG 或 Translation 的生产路径改为 Artifact 用例，也没有声称
具备跨实例许可、可靠队列或生产 MinerU/OCR 容量。下一步 1H-5 只建立独立 Translation
Domain/Application/Ports/Adapters 和兼容 Facade；原文件准备策略不得重新进入 Translation。

阶段复核未发现新的公开契约或业务语义待确认项，可以进入 1H-5。
