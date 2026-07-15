# `/llm/analysis` Top-K 召回与两阶段分类实施计划

> 日期：2026-07-15
>
> 状态：实施中
>
> 适用范围：`/llm/analysis` 领域分类，不包含 AnythingLLM 标签向量召回

## 1. 目标与合同

- 甲方继续在 `params[].architectureList` 中传完整领域树；请求和 callback 结构不变。
- 服务端从完整树构建本地索引，以 exact、字符 BM25、树路由和业务规则召回模型候选。
- 单候选直接确定 ID；多候选优先返回叶子，证据不足时允许返回模型可见的最深可靠父节点。
- 父节点必须属于完整请求树、深度至少为 2，且不能是根节点或“数据标准”父节点。
- 没有有效召回证据，或有限分类修复后仍不能确定时，任务失败；禁止默认回退到 ID `1`。
- 数据标准六个叶子可以参与分类和 GJB 兜底；五个扩展字段仍由 `architectureStandardList` 控制。
- 完整树始终保留给合法性校验、数据标准判定、知识库存储归并和 callback；模型候选只是内部投影。

## 2. 树索引与缓存

- 节点 ID 使用 64 位整数语义；兼容数字字符串，拒绝布尔值、非正数和重复 ID。
- `parentId=0/null/缺失` 均按根节点处理；请求子集缺少父节点是合法的有限树边界；请求内父链环直接失败。
- 非空 `pathName` 作为不透明语义字符串，不能通过 `/` 推断层级。仅在缺失时按父链重建，并处理 `父名称-明细类型` 的重复前缀。
- fingerprint 对规范化后的节点顺序、ID、父 ID、名称、路径名称和 remark 计算 SHA-256；节点原始顺序用于稳定同分排序和 GJB 兜底。
- 索引缓存采用线程安全的进程内 LRU，默认容量 4；同 fingerprint 只构建一次，失败结果不进入缓存。

## 3. 文档信号与候选召回

### 3.1 文档信号

- 文件预处理后、创建远端 RAG Session 前读取正文，并复用给召回和 mapper。
- 支持文本、Markdown、JSON、CSV、PDF、MHTML/MHT 和现有 DOCX 提取器。
- 召回信号包含文件名、业务原始文件名、标题、最多 64 个标题层级、最多 128 个型号/标准号，以及最多 20,000 字符正文判别片段。
- 使用 Unicode NFKC、英文小写、连字符/空格变体、中文 2/3-gram；生成 `CVN78/CVN-78/CVN 78` 等格式别名。
- `remark` 是甲方可选概述，可能为空；非空时参与本地索引，模型投影最多携带 512 字符。

### 3.2 召回与融合

- exact/alias 命中作为 protected 信号。
- 字符 BM25 使用 `k1=1.2`、`b=0.75`，返回全局叶子 Top-200。
- 树路由保留根 beam 4、中间 beam 8，最多返回 100 个叶子；它只是一条召回通道，不能硬裁剪唯一分支。
- RRF 使用 `k=60`；lexical 权重 `1.0`，tree/rule 权重 `0.8`。
- 先取融合 Top-64，再补齐：
  - 装备七类叶子：基础、战技、运用、效能、模型、目特、声像；
  - 数据标准六类叶子；
  - 命中小根分支的必要多样性候选。
- 追加最多 16 个受限父节点：排除根和数据标准父节点，且必须直接命中 exact/tree，或覆盖融合 Top-16 中至少两个后代叶子。
- 最终模型候选只包含 `id/pathName/nodeType/remark?`，总数最多 128，分类 Prompt 最多 32,000 字符。
- 任何召回或预算失败均显式结束任务，不允许重新发送完整领域树。

## 4. 两阶段 RAG

- 增加 `ARCHITECTURE_CLASSIFICATION` 与 `ANALYSIS_EXTRACTION` 两种 PromptKind。
- 多候选首先执行领域分类，只输出 `{"architectureId": 数字或 null}`；结果必须同时属于模型候选和完整树。
- 分类非法或为空时，在分类阶段剩余预算内对完全相同的候选执行一次 repair。
- 分类确定后，在同一文档 Session 中执行字段抽取；抽取 Prompt 不包含 `architectureList`，只把已确认 ID、语义路径和节点类型作为只读上下文。
- 单候选跳过分类，直接把字段抽取作为首次模型查询。
- 分类和抽取阶段各最多使用 2 次模型查询；供应商重试与 repair 共享阶段预算，总上限 4 次。
- analysis 临时 workspace 使用 `openAiHistory=0`，避免分类候选污染抽取；永久知识库配置保持不变。

## 5. 审计、配置与失败边界

- 新增按 `execution_id` 幂等保存的召回决策记录：tree fingerprint、query digest、Top-64、最终候选、通道排名、RRF、protected 原因、Prompt 字符数、返回 ID/rank、耗时和错误阶段。
- 审计不保存正文；候选与 trace JSON 设置长度上限。
- 召回审计失败时不得创建远端 Session；最终 RAG trace 审计仍是永久知识库、翻译和成功 callback 的门禁。
- 稳定失败阶段：`architecture_index`、`architecture_recall`、`architecture_prompt_budget`、`architecture_contract`、`analysis_extraction`。
- 运行模式：
  - `topk_two_stage`：默认模式；
  - `topk_single`：两阶段回滚模式；
  - `legacy`：只允许完整 Prompt 不超过 32,000 字符的小树。

## 6. 原子提交策略

每个模块完成后先运行对应测试，只暂存本模块文件并立即提交。最终不 squash、不 amend；后续缺陷使用新的 `fix:` commit。

计划提交顺序：

1. `docs: 添加领域树分类实施计划`
2. `feat: 新增领域树索引与缓存`
3. `feat: 新增领域候选召回服务`
4. `feat: 拆分文件分类与字段抽取提示词`
5. `feat: 扩展文档 RAG 分阶段调用合同`
6. `feat: 新增领域召回审计`
7. `feat: 接入文件分析两阶段分类流程`
8. `feat: 增加领域分类运行模式配置`
9. `test: 添加领域召回基准与大树测试`
10. `docs: 更新文件解析分类说明`
11. `test: 记录三文件端到端验证结果`

每次提交在最终交付中记录 commit hash、文件范围、测试命令和结果；既有失败与新回归分别说明。

## 7. 测试与 E2E

### 7.1 离线测试

- 树索引：64 位 ID、重复/非法 ID、根节点三种形式、孤儿节点、环、有限树叶子、缺失 `pathName`、名称含 `/`、fingerprint、LRU 和单飞构建。
- 召回：型号变体、同名消歧、BM25/tree/RRF、装备七类、数据标准六类、根分支多样性、父节点资格、无信号失败和 Prompt 预算。
- 主链：单候选跳过分类、分类与抽取隔离、根节点拒绝、候选外 ID 拒绝、repair 候选一致、最多 4 次模型调用，以及完整树继续用于 mapper/storage。
- 审计：旧库增量建表、幂等冲突、长度门禁、SQLite 锁重试、历史 execution 保留和审计失败门禁。

### 7.2 三文件真实 E2E

固定使用：

- `测试文件/GJB 9001C-2017.pdf`
- `测试文件/Gerald R Ford (CVN 78) class (CVNM)-14-Jul-2023.pdf`
- `测试文件/Nimitz (CVN 68) class (CVNM) 16-Aug-2023.pdf`

领域树必须直接读取 `/Users/extrrlyria/Documents/文件解析领域树.json` 中
`.params[0].architectureList` 的完整节点数组构造请求，不得改用合成树、默认树、截断树
或二次生成的节点。执行前后记录源文件 SHA-256、节点数、叶子数和 tree fingerprint，
并保持节点原始顺序与字段内容。使用该完整领域树构造一个三文件 `/llm/analysis` 请求，
验证多文件串行处理。验收要求：

- GJB 结果属于数据标准六个叶子之一，不能返回数据标准父节点；
- Ford 结果属于 CVN-78 父节点或其七类叶子；
- Nimitz 结果属于 CVN-68 父节点或其七类叶子；
- 返回 ID 同时属于模型候选与完整树；候选不超过 128，Prompt 不超过 32,000 字符，模型查询不超过 4 次；
- callback JSON 与 `llm_tasks.result_payload` 一致；召回审计、交互审计和知识库映射可完整取证；
- 不发生完整树 Prompt 降级或审计超限。

三份文件当前没有人工精确 gold，因此本轮 E2E 验证正确子树与业务闭环，并记录实际 ID 和候选 rank。原始运行产物保留在 `.runtime`，仓库只提交脱敏验证摘要。

## 8. 上线门禁

- 后续 gold set 的 Recall@64 不低于 99%，小根分支 Recall@64 不低于 98%。
- 候选外 ID 成功回调为 0；Prompt p100 不超过 32,000；审计超限为 0。
- 在 gold 门禁通过前，不宣称生产分类准确率已经提升。
