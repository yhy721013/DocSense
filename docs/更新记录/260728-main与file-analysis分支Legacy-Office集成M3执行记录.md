# `main` 与 `refactor/file-analysis` Legacy Office 集成 M3 执行记录

## 1. 阶段结论

M3“AnythingLLM 单 Sheet XLSX 协议”已完成，可以进入 M4。主线带入的实现已经满足已确认的
单 Sheet、清理所有权与恢复事实要求，经合并后的新 Analysis、Task Service、Weaponry 和普通
AnythingLLM 路径回归未发现语义冲突，因此本阶段没有追加生产代码改动。

## 2. 已验收协议

1. 普通 `custom-documents` 上传仍必须返回恰好一个成员，多个普通成员或混合结构明确拒绝；
2. XLS/XLSX 上传只在返回恰好一个合法 Sheet 时继续，不选择多 Sheet 的第一项；
3. 多 Sheet 响应必须先冻结同一顶层目录的全部非重复成员，签发 opaque Folder Cleanup Token；
4. 删除前重新读取 Folder 成员并与 Token 完全相等；成员漂移、端点缺失或网络结果未知均不删除；
5. 删除结果未知时，Token 进入 RAG 生命周期审计与后续恢复，不进入 HTTP、Callback 或日志路径；
6. Token 只能接受严格 XLSX Folder/Sheet 位置，编码穿越、控制字符、混合目录和重复成员均拒绝；
7. 单 Sheet 新版本提交后只从目标 Workspace 解绑旧 Sheet，不全局删除旧 Collector Folder；
8. 历史 Folder 没有完整成员所有权证明时保持原状，不能由单个旧 Sheet 位置推断整目录归属；
9. 普通 Markdown/PDF/DOCX、Weaponry 术语卡和 Chat 上传不调用 XLSX Folder 删除端点。

## 3. 任务一致性边界

- RAG 在多 Sheet 清理未知时先记录外部资源事实，再由 Session close 使用同一 Token 有界重试；
- 失败事实区分 `upload_protocol` 与 `upload_outcome_unknown`，不得把未知结果伪装成已清理；
- Task Service 持久化的 opaque 引用只供内部恢复，旧单文档记录继续兼容；
- 当前 SQLite 事务和恢复测试仍只证明单实例行为，不宣称多实例可靠队列或分布式所有权完成；
- 永久知识库替换采用“绑定新版本、本地切换、解绑旧版本”Saga，旧 Folder 的存储累积按既定
  可观测方案保留到拥有充分所有权证据的后续治理阶段。

## 4. 门禁证据

第一组执行 Documents、Knowledge Gateway、RAG Gateway、Task Service 和 Weaponry 生产适配器，
共 200 项全部通过。第二组执行全部 AnythingLLM Policy、Client、Chat、Documents、Knowledge、
RAG、Thread、Transport、Workspace 及 Weaponry 术语目录，共 194 项全部通过。两组存在有意的
Documents/Knowledge/RAG 重复回归，因此记录为 394 次测试调用，不声称 394 个互异用例。

测试覆盖以下关键场景：

- 单 Sheet 成功、多个 Sheet 确认清理和清理未知；
- 畸形成员、重复位置、混合 Folder、成员漂移与 404 对账；
- Token 穿越、Unicode 控制字符和非规范编码；
- 新旧 XLSX 替换、补偿失败、SQLite 协调恢复；
- Markdown/普通文档、Weaponry 术语目录和来源身份回归；
- 故障注入日志中的 `ERROR`/堆栈均为预期路径，最终无 Failure、Error 或 Skip。

没有执行 `run.py`，没有连接真实 AnythingLLM，`docs/接口文档/` 没有修改。

## 5. 阶段后商讨项检查

实现与既定 D-01 单 Sheet 和历史 Folder 治理决策一致，没有新增接口参数、清理所有权或恢复语义
疑问。历史 Folder 的全局回收仍严格等待未来可证明所有权的治理能力，不阻塞本次集成，可以进入 M4。
