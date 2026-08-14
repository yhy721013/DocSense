# Weaponry Adapters

阶段 1D-2 已实现两项无外部副作用的基础 Adapter：

- `knowledge_documents.py`：以单次只读快照查询解析 explicit/category 文档范围，冻结请求顺序、
  execution 内文档键、甲方原名、实际入库名和外部引用；不写旧选文表，不访问 AnythingLLM；
- `task_codec.py`：严格编码/解码唯一 Schema v2 execution 输入与结果，把原始公开请求投影和 Worker
  输入隔离，并冻结完整 Evidence Selection、Extraction、TABLE 和辅助语境策略；缺失键、未知键、
  版本不一致、NaN/Infinity 或身份不一致均直接失败。成功终态还会在 CAS 前校验字段/列完整性
  以及领域结果、execution state 与 public status 一致性。

阶段 1D-3B 已新增下列生产 I/O 实现；1D-5 完成离线组合，1D-6 完成生产组合根与公开路由接线：

- `production_profile.py`：由显式 Provider、embedding 和文档处理指纹生成确定性 Schema v2
  profile；只采用合法 score 或稳定 rank，不包含绝对阈值、anchor 或 reranker；
- `anythingllm_clients.py`、`anythingllm_retrieval.py`：每 execution 独占 Transport/workspace，
  精确绑定完整文档位置与供应商 ID，返回 Candidate 和整批 score/rank 模式；永久来源 workspace
  零写入，同任务并发创建在外部副作用前拒绝；
- `provided_evidence_extraction.py`：每个来源 attempt 使用全新空 workspace/thread，只发送请求中
  已校验的 Selected Evidence/rows；禁止目标文档二次 RAG、共享父 Thread 和不安全回退；
- `no_auxiliary_guidance.py`、`terms_rule_guidance.py`：关闭路径真实零 I/O，开启路径通过可整体
  删除的只读术语 Provider 提供非事实辅助语境；
- `translation.py`：把既有纯文本翻译能力收敛到来源级 Port，失败兼容为空文本；
- `sqlite/interaction_audit_store.py`、`sqlite/resource_store.py`、`resource_registration.py`：
  SQLite 短事务完成
  reserve/complete 三态分类、资源 CAS、owned/shared、清理 lease/fencing 和创建后立即登记，
  不在事务中执行网络调用；终态 execution 的遗留 tracking 记录也能进入后续恢复候选。

Document Scope 与 Retrieval 使用同一完整文档位置规范化身份；创建确定性 workspace 返回 409
按外部副作用结果未知处理。Application 会依据 Interaction Audit 隔离无法唯一证明的现场，不会
在 Adapter 内盲重试；逐资源恢复使用持久 lease/fencing、退避和单次副作用边界。

阶段 1D-5 另新增：

- `runtime_config.py`：一次启动严格读取单实例扫描/维护参数、四类运行指纹和固定
  Query/score/rank/Extraction Context；模式 1 明确拒绝，术语关闭分支不读取五个术语专属键，
  且不存在 Query/Selected Evidence 字符上限；
- `local_dispatcher.py`：复用 tasks 通用持久扫描内核，装配一条 Weaponry 执行 Worker、资源与
  Callback Guard 两条隔离维护线程、队列诊断、共享 limiter 和进程锁；供应商容量、业务零结果、
  输入契约与其他失败使用不同内部日志/计数；业务终态提交后仅唤醒资源维护线程，不等待清理，
  不连带唤醒 Callback Guard；资源批次有进展且仍可能存在积压时在批次边界继续执行；
- AnythingLLM Retrieval/Extraction Adapter 将 HTTP 413/429 分类为稳定供应商容量错误，字段
  降级后的诊断事实仍可到达 Dispatcher，不会被计成普通零结果。

阶段 1D-6 另新增：

- `callback_guard.py`、`callback_recovery.py`：SQLite latest-wins Guard、严格 2xx HTTP 投递、
  3xx 禁止跟随、unknown 冻结、过期 sweep、人工解除审计和公开投影无损重建；
- `anythingllm_resource_cleanup.py`：按资源类型执行一次幂等删除，404 视为已清理，明确失败进入
  持久冷却，超时/断连等结果未知进入 quarantine；
- `sqlite/resource_store.py`：终态 tracking 候选扫描、立即清理水位、失败持久冷却，以及逐资源
  cleanup lease/fencing 条件提交；资源恢复的 `limit` 按本轮逐项恢复尝试数计量，同批任务
  采用轮转推进，停止请求只在单项边界生效。持久 Store 始终是事实来源，线程 Event 只是
  可丢失提示，启动与固定周期扫描仍负责恢复历史积压。

阶段 2-5 第 3 步新增 `sqlite/weaponry_control_manifest.json` 与
`sqlite/task_document_snapshot_store.py`。新组件把文档快照按 `task_id` 隔离，并为 Creation
Intent、Interaction Audit、资源记录及人工处置审计声明唯一 Schema 身份；组件 Bootstrap 必须在
运行线程启动前完成，运行期禁止自愈 DDL。

阶段 2-5 步骤 7～8 已完成生产源码的原子切换：`v2_runtime.py` 以通用持久 Executor 承接
claim/start/heartbeat/失权停止，`v2_callback.py` 使用 Task Control Guard lease/fencing，
`v2_callback_recovery.py` 从 latest Task 与完整 `weaponry_result_snapshots` 精确重建原 Callback。
旧 Store 的数据库路径模式只服务历史离线回归，不再是生产 Writer；旧 `LLMTaskService` 的
Weaponry 文档快照 DDL/读写已删除。术语目录 startup gate 仍早于任何线程启动，Provider/Profile
门禁及既有环境键保持不变。

当前开发分支已装配上述真实 Adapter 并切换 `/llm/weaponry`，但本轮自动验收没有连接真实模型、
回调接收端或修改现有 AnythingLLM 资源；部署前的真实供应商能力与运行容量仍需在受控集成环境验证。

任何 Adapter 都必须先把供应商返回值转换为领域 DTO，再交给 Application；禁止把供应商
字典穿透到领域规则和公开回调。
