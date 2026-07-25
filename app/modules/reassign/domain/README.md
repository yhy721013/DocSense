# 分类节点变更领域层

本层只表达分类节点变更的稳定业务语言：不可变原始 ID、文档快照、Operation、Step、错误分类、
状态转换、步骤幂等键和补偿决策。它不读取环境变量，不连接 SQLite/AnythingLLM，不创建线程，
不记录日志，也不决定 Flask 响应。

## 已冻结的不变量

1. `ReassignDocumentCommand` 保留 Web 入站的新旧 ID 原始 JSON 值，并只接受边界层已完成的
   `old_architecture_id_query_value`；本层不拥有首次转换和 HTTP 失败语义，只按相同转换规则
   复核原始值与查询值一致，防止 Adapter/Fake/恢复 Codec 组合出矛盾事实。
2. `ReassignmentDocumentSnapshot` 固定本地行 ID、文件名、来源分类、`anything_doc_id`、
   `doc_path` 和原始文件名。空 `doc_path` 是兼容的本地条件更新分支，不得在领域层收紧。
3. `reserved`、`running`、`compensating` 和 `recovery_required` 继续占用同文档保护；
   `succeeded`、`compensated` 及无副作用的 `failed` 必须携带匹配的终态证据才可释放保护。
4. 每个前向或补偿 Step 都必须先记录写意图，再进入 `mutation_started`。补偿写使用独立
   `COMPENSATE_*` Step；未知结果必须先探测，明确失败的重试必须带恢复授权和新 fencing。
5. 补偿只根据已确认事实判断：未知即恢复保护；目标绑定确认时先删除目标，再按需恢复来源；
   本地分类提交已生效但目标绑定缺失或未知时必须进入 `recovery_required`。
6. lease 到期时间是规范化 UTC ISO-8601；诊断字段有固定长度上限；公开文案只能从
   `ReassignmentPublicMessage` 选择。

## 日志边界

领域纯函数故意不依赖 `logging`，从而保证同一持久化事实在任意 Worker/实例中得到相同决策。后续
应用层与适配器必须在外部写前后记录脱敏的步骤、状态、attempt、耗时、探测结果和错误分类；禁止
记录 API Key、Authorization、文档正文、完整供应商响应或公开响应中不可出现的内部标识。
