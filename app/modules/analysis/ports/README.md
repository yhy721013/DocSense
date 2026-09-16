# Analysis Ports

阶段 1F-2 已在本层定义批量命令、文件准备、RAG、永久知识写入、审计、翻译、资源、回调和
Dispatcher 九类抽象。它们只包含不可变 DTO、有限结果和运行时 Protocol；不会导入具体 SQLite、
HTTP、AnythingLLM、Flask 或旧 Service 实现。

Port 中的 `AnalysisExecutionRef` 是内部协作身份，严禁写入 `/llm/analysis` 公开响应。批量命令与
任务输入均要求受理期快照；1F-3/1F-6 的未接线 `RunAnalysisTask` 可消费 Task、Progress、Workspace、
File、RAG、Knowledge、Audit、Translation、Resource 和 Callback Port。真实 Dispatcher、生产组合根与
公开路由接线仍属于后续阶段。

1F-2R 进一步固定以下内部不变量：

- RAG 必须显式打开、查询和关闭任务级 Session；成功打开、失败现场及关闭结果都携带严格有序的
  生命周期事件，结果未知不能自动重试删除；
- 召回审计使用 `reserve/finalize`，完整模型交互原子保存 Prompt、全部 attempts 和初始生命周期，
  close/cleanup 事件只能按 Receipt 幂等追加；打开失败允许没有完整 Session，但必须保留已确认的部分
  资源 lifecycle，成功审计仍强制完整 Session；
- Resource 推进同时比较 state 与 version，并在 DTO 与 SQLite 两层执行同一迁移矩阵；
  `cleaned/quarantined` 是不可逆终态。恢复延期必须推进版本并保存下次时间和原因，毒记录隔离可只依赖
  identity/state/version，不要求先解码业务 payload；
- Callback 先取得带 token/version 的 Guard Lease，再发送并按同一 execution 条件完成；等待时间必须
  是有限正数，空 URL 必须显式收敛为 skipped，unknown 投递必须保留可判定 detail code；同步恢复候选
  只允许读取 latest 且已终态的新 file execution，并携带首次读取的 callback attempt 快照；
- 所有结果 DTO 和严格 Fake 都校验 execution、operation、session、幂等键及批内顺序，禁止跨任务
  结果在测试中被误当作成功。

1F-3 为历史单文档 RAG 补充了绑定文档引用、上下文/会话引用和来源 DTO；永久知识写入请求同时携带
原始文件名与冻结属性。上述字段只用于内部审计和所有权判断，不能添加到公开响应或 callback。
