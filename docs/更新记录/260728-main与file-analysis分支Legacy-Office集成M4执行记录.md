# `main` 与 `refactor/file-analysis` Legacy Office 集成 M4 执行记录

## 1. 阶段结论

M4“Report 集成”已完成，可以进入 M5。主线带入的 Report Legacy Office、单 Sheet XLSX RAG、
Artifact 发布与清理实现已经适配当前持久 Dispatcher、Callback Guard、交互审计和资源恢复链，
经全套 Report 离线回归没有发现合并语义缺口，因此本阶段没有追加生产代码改动。

## 2. 已验收业务流程

1. `.doc/.ppt/.xls` 来源先下载到当前 execution 私有目录，再调用共享 Preparer 转为 OOXML；
2. 转换产物重新发布为任务内 Artifact，后续规范化和 RAG 不引用转换器临时目录；
3. Preparer 缺失、转换失败、结果不是有效文件或目标扩展名不合法时，整份报告失败；
4. 上述失败禁止把原始 Legacy 二进制文件直接交给普通上传流程作为“降级成功”；
5. `.xls/.xlsx` 在 AnythingLLM 返回一个 Sheet 时继续，多个 Sheet 明确拒绝；
6. 多 Sheet Folder 清理确认或结果未知均进入 Report 生命周期事件，opaque Token 可供资源恢复；
7. 多来源顺序保持请求顺序，任一来源失败时不生成部分报告成功结果；
8. 远端或内部文件名经过严格 percent decode、Unicode 安全清洗和碰撞检测，内部
   `prepared-<uuid>`、Sheet JSON、Folder 名不进入报告正文或 Callback；
9. 报告模板继续只允许 `.docx`，Legacy `.doc` 在下载和转换前即明确拒绝；
10. 最终 HTML Artifact 的保留、摘要校验和旧 execution 隔离规则保持不变。

## 3. 可靠性与架构边界

- Report 只依赖共享 `LegacyOfficePreparer` Port，不直接管理 LibreOffice 进程或全局目录；
- 每个 execution 拥有独立 Artifact scope、RAG Workspace、Conversation、文档与审计身份；
- 外部清理事件先逐项持久化再进入下一副作用，失败后按租约、版本和 fencing 有界恢复；
- 创建或删除 outcome unknown 时保持隔离，不盲目重放；
- 当前证明仍限于单实例 SQLite/Fake，不把进程内共享容量解释为多实例并发控制。

## 4. 门禁证据

首轮 Report Contract、Application、IO Adapter 和 RAG Adapter 共 73 项通过。随后运行以下全部
16 个 Report 测试模块，共 194 项全部通过、无 Skip：

- Contract、Request Adapter、Submission Presenter；
- Domain、Ports、Application、Service；
- IO、RAG、Runtime、Task、Interaction Audit Adapter；
- Dispatcher、Callback Guard、Callback Recovery、Resource Recovery。

全量 194 项包含首轮 73 项，因此以 194 个互异用例作为本阶段最终数量。故障注入产生的
`ERROR`、`CRITICAL` 和堆栈是预期可审计路径，最终没有 Failure 或 Error。

没有执行 `run.py`，没有连接真实 AnythingLLM、LibreOffice、模型或 Callback；
`docs/接口文档/` 没有修改。

## 5. 阶段后商讨项检查

Report 的来源格式、模板边界、单 Sheet 和失败终态均符合既定决策，没有新增接口参数、模板格式
或部分成功语义需要确认。M4 不改变公开请求或 Callback 字段，可以进入 M5。
