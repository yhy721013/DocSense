# 测试替身目录说明

本目录保存离线测试使用的端口替身。替身模拟供应商无关协议，而不是复制 AnythingLLM 的 HTTP 细节；这样应用服务、路由和状态机测试无需启动任何后台服务。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `__init__.py` | 导出各类测试替身。 |
| `analysis.py` | Analysis 九类 Port 的统一严格 Fake 与线程安全期望脚本；未配置、乱序、错误类型或未消费期望均立即失败。 |
| `chat.py` | 文件对话替身：共享内存后端、任务级对话工厂和对话端口，模拟工作区/线程、文档绑定、流消息、临时回复、历史和幂等删除。 |
| `knowledge_index.py` | 知识索引端口替身。 |
| `rag.py` | RAG 端口替身。 |
| `tasks.py` | Task Read、同步 Callback Recovery 原型、批量原子 Callback Recovery Command、Progress Snapshot/Subscription 可编程替身。 |
| `report.py` | Report Task Command、Progress Publisher、File、Artifact、RAG、Interaction Audit、Callback、Resource Store 和 Dispatcher 严格 Fake；共享全局调用记录，支持逐步骤/任务事实写入故障、CAS、Artifact 身份与类别错误注入。 |
| `weaponry.py` | Weaponry Fake 稳定聚合导出面，以及线程安全 Document Scope 严格 Fake。 |
| `weaponry_support.py` | 不记录正文、URL、Prompt 或 Token 的线程安全调用轨迹。 |
| `weaponry_processing.py` | Retrieval、Extraction、Auxiliary Guidance、Translation、Interaction Audit 严格 Fake；验证审计/资源前置、task/call/document 身份、Evidence/来源边界和故障分类。 |
| `weaponry_control.py` | Task 原子受理/领取/expected 条件写、Progress Publisher、Callback latest/Guard、Resource ownership/CAS/cleanup lease/fencing 和 Dispatcher 生命周期严格 Fake。 |

## 文件对话替身工作流程

1. 测试创建 `FakeChatConversationFactory`。
2. 每次 `create()` 生成独立的对话端口实例，以模拟请求级网络对象隔离。
3. 各端口共享同一个内存后端，因此后续请求仍可读取先前创建的会话与文档状态。
4. 测试可配置流事件、异常和删除结果，以验证应用层的持久化与收敛逻辑。

## 维护规则

- 替身必须满足 `app.modules.chat.ports` 的运行期协议，不能将供应商私有字段引入应用服务测试。
- 不要在替身中隐藏生产问题：资源创建、流关闭、失败和幂等删除都应可显式配置和断言。
- 替身只用于测试，生产包的公开导出不能依赖或暴露这些对象。
- 任务替身必须按 TaskId 保存历史执行、按业务引用保存最新投影；故障注入和调用记录必须显式，不能自动吞掉端口契约错误。
- 可靠命令替身必须在锁保护下先于局部副本计算整批结果，全部成功后才提交活动命令
  状态；串行或并发重复 TaskId 均复用同一 recovery request ID，事务故障不得留下部分状态。
- 报告替身不得访问真实文件或“自动完成”Application 应负责的补偿；跨 TaskId、trace、
  Artifact 和 Receipt 身份错误必须原样返回，让应用层门禁测试能够确定性失败。
- 武器谱严格 Fake 不得猜测未配置结果、回退到共享会话或把跨文档来源当作成功；目标检索和
  模型调用前必须存在成功审计预留，检索资源创建后必须完成正确类别的资源登记。
- 武器谱 Task Fake 必须按真实 Repository 状态自动区分 accepted、already-running、terminal
  与 stale；同一业务键存在活动 execution 时默认冲突，不能让重复提交或重复派发在测试中
  产生第二终态、第二回调或第二套外部资源。
- `none` 辅助策略的 Provider I/O 计数必须严格为零；Extraction 每个 attempt 使用独立虚拟会话，
  相同回答文本不能替代 document/evidence 身份校验。
- Resource Fake 必须用锁、expected version、cleanup lease 和 fencing token 模拟并发；shared
  资源永不进入清理状态，unknown 清理在对账或隔离前不得直接重试。
- Analysis 严格 Fake 不得提供默认成功值或将未声明调用静默忽略。1F-3 已由
  `StrictAnalysisTaskCommandFake`、`StrictAnalysisGuardedProgressFake`、
  `StrictAnalysisTaskWorkspaceFake` 与 `StrictAnalysisRagFactoryFake` 补齐 TaskId 运行所需的领取、
  进度、目录和 Factory 作用域；并发测试仍应按 execution 使用独立脚本队列，所有返回值都必须再次
  核对 execution/session/operation/Receipt。1F-6 的资源、回调和恢复测试以显式的内存 Port 或临时
  SQLite/替身 Transport 驱动，必须断言 Guard、CAS、unknown 与隔离事实；不得由未接线 Application
  虚构成功。Dispatcher 交互仍属于后续阶段。
