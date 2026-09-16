# Weaponry Domain

本层只表达武器谱检索和证据选择的不变量，不读取环境变量，不连接 AnythingLLM，也不
决定 Flask 响应。

阶段 1D-0R/1D-1 固定以下安全原则：

1. Retrieval Query 与 Extraction Prompt 分离，召回文本中不出现回答格式、模型角色或
   “未找到”等抽取指令；
2. AnythingLLM 整批结果要么提供合法的 0～1 分数，要么明确采用互不重复的稳定正整数 rank；
   分数仅用于稳定降序、rank 仅用于稳定升序，不设置绝对阈值、内容 anchor 或独立 reranker；
3. 供应商元数据（含 AnythingLLM 的 `passage: <document_metadata>` 包装）、参考文献密集块、
   错误来源、非法分数和 profile 指纹不一致均不得静默降级进入 `rows`；
4. Extraction Prompt 只能接收同一文档的 `SelectedEvidence`；Prompt 中的 `rows` 与 Evidence ID
   逐项冻结，不能直接传入 `EvidenceCandidate`，也不能混入其他文档。
5. 字段模板、未知扩展键、文档身份、结果和 Callback DTO 均为深冻结快照；公开投影每次生成
   新对象，避免并发任务共享可变字典或列表。
6. TABLE 解析、单元格规范化、行身份、合并、来源去重和二维结果组装均为确定性纯规则；
   “类型”不是强行身份，仅有弱类型信息时不得合并不同行；
7. 所有通过门禁的 Selected Evidence 完整保留，不设置单条、总量或单文档数量/字符配额；
8. ArchitectureId、执行策略和成功结果完整性均对照不可变快照验证，终态状态矛盾或漏字段/列
   不能进入 Repository CAS。
9. Retrieval Query 不设置内部字符上限，也不要求辅助 `semantic_terms` 非空或至少两个字符；
   单字字段和仅能依赖 `fieldDescription` 的合法公开字段不得在受理后因隐藏规则异步失败。
10. INPUT 只有完整回答精确等于“未找到”时才按无信息处理；TABLE 回答始终先进入严格 JSON
    纯规则，单元格文本不能让整份来源被子串判断提前丢弃。

显式多词同义词按完整短语匹配，不自动拆成宽泛单词；ASCII 锚点使用词边界。需要更短的
同义词时必须由调用方显式提供并进入校准资产，不能在运行时隐式扩展。

纯规则允许保守返回零 Evidence。调用方不得为了填满 TopN 放宽门禁，也不得从旧链路补回
被拒绝的候选。Candidate top-N 只是供应商召回批次，不是 Selected Evidence 配额。

阶段 1D-3B 已将运行策略冻结为唯一 Schema v2 production profile：它记录 Provider、embedding、
文档处理、Query、score/rank 协议、稳定排序、Candidate 批次、正文质量和去重事实，但不是精度
证书。1D-1 遗留模式 2 映射出的 `legacy-mode2-unprofiled` Evidence 只服务尚未切换的旧链路，
不等价于通过新 Selection，也不得进入新的 Run Application。

阶段 1D-2 进一步冻结 `WeaponryDocumentScope`、`WeaponrySubmission` 和
`WeaponryInputSnapshot`；阶段 1D-3B 又按“无历史数据、无旧 Worker”的已批准前提直接删除开发期
Schema v1，只保留 Schema v2 Codec。文档顺序、execution 内唯一 `document_key`、字段模板、
选择 profile、抽取/模型/上下文/TABLE 合并策略、辅助策略和受理时间均为不可变任务事实。
Worker 只能读取 Codec 解码后的快照，不得按文件名、类别或当前环境重新选择文档和策略。
