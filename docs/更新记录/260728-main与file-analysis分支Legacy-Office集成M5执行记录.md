# `main` 与 `refactor/file-analysis` Legacy Office 集成 M5 执行记录

## 1. 阶段结论

M5“Stage 1F Analysis 集成”已完成，可以进入 M6。Legacy Office 转换和单 Sheet XLSX 能力已经
落入 `/llm/analysis` 的 Stage 1F 持久任务唯一生产链；没有恢复旧路由线程或旧
`analysis_service` Runner，也没有修改公开请求、响应、Callback、Progress、SSE 或 WebSocket
字段。

## 2. 已完成实现

1. Analysis 文件准备结果明确区分原始下载路径 `source_path`、业务处理路径
   `processing_path` 和 RAG 上传路径 `upload_path`；
2. `.doc/.ppt/.xls` 在下载后、创建任何远端 RAG Session 前完成转换，转换产物复制到当前
   execution 私有目录，后续不依赖转换器临时目录；
3. Legacy Office 的正文读取、全文翻译和 RAG 上传统一使用转换后的 OOXML；现代格式继续保持
   原有路径语义；
4. `.xls/.xlsx` 继续执行单 Sheet 规则，多 Sheet 明确失败并沿既有 Folder Cleanup Token、
   资源事实和恢复流程收敛；禁止把原始 Legacy 二进制文件作为降级成功结果上传；
5. 新增不可变 `AnalysisTaskInputV2`，冻结是否需要 Legacy 转换、处理 Profile、允许的
   LibreOffice 版本系列、`single-sheet-v1` 策略和策略指纹，同时严格兼容读取历史 V1；
6. 任务受理时冻结内部策略，Worker 不读取可能已经变化的功能开关决定历史任务语义；
7. 本地资源事实升级后同时登记 raw/processing/upload 路径，并兼容读取旧版资源事实；
8. 内部 `prepared-<id>.docx/.pptx/.xlsx` 名只在当前 execution 的已知身份范围内脱敏，业务
   `fileName`、`originalFileName`、`format` 和 `dataFormat` 保持请求值；
9. 单文件失败仍按持久 Dispatcher 的批次顺序继续处理后续 execution，知识转交继续以单文档
   committed 为成功门禁；
10. Analysis 与 Report 使用容器中的同一 `LegacyOfficePreparer` 实例，因此共享进程级容量，
    但不共享任务目录、资源身份或回调状态。

## 3. 可靠性与兼容边界

- V2 写入与 V1/V2 双读都经过严格 Schema 校验，未知字段、版本漂移和策略指纹不一致均
  fail-closed；
- 受理后的开关变化不改变已持久化任务，避免重启后同一 execution 获得不同处理语义；
- 版本系列不匹配在远端副作用前失败，日志只记录稳定错误码、异常类型和策略指纹；
- 多 Sheet Folder 清理结果未知时保留资源现场和恢复事实，不盲目重放或标记已清理；
- 当前门禁证明限于单实例 SQLite、Fake 和临时目录，不把进程内共享容量解释为多实例协调能力。

## 4. 门禁证据

最终执行全部 `test_analysis_*` 模块，共 318 项通过；另执行 AnythingLLM Documents、RAG
Gateway、Analysis 生产 Adapter、Dispatcher 和契约资产组合门禁，共 116 项通过。最后一次
聚焦复核执行 Task Adapter、Production Adapter、Application 和 Contract Assets 共 55 项通过，
并完成全部改动模块的语法编译。

上述测试覆盖：Legacy 三种格式、扩展名缺失的签名 URL、转换版本漂移、正文/翻译/RAG 路径、
单 Sheet、多 Sheet 拒绝与清理确认/未知、V1/V2 重启兼容、资源恢复、批次后续任务继续、内部名
脱敏、单任务身份隔离和公开契约 Hash。故障注入测试输出的异常日志均为预期路径；最终没有
Failure、Error 或 Skip。

`git diff --check` 通过，`app/modules/analysis` 对旧 `analysis_service` Runner 的生产运行时引用
为零，`docs/接口文档/` 没有修改。没有执行 `run.py`，没有连接真实 AnythingLLM、LibreOffice、
模型、Callback 或生产数据库。

## 5. 阶段后商讨项检查

M5 实现符合已确认的单 Sheet、部署策略快照、失败关闭、历史 V1 兼容和公开契约不变决策。
没有发现需要新增接口参数、改变 Callback 语义或扩展 Document Group 的问题；可以进入 M6。
