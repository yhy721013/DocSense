# 阶段 1D-0 契约资产与 Evidence Selection 校准执行记录

## 0. 执行信息

| 项目 | 内容 |
| --- | --- |
| 执行时间 | 2026-07-17～2026-07-18 |
| 执行分支 | `refactor/concurrency` |
| 对应计划 | `../重构记录/260717-阶段1D武器谱文件级实施设计.md` 的 1D-0 |
| 执行范围 | `/llm/weaponry` 当前/目标契约、黄金样例、术语双路径、来源/上下文隔离、故障矩阵、Evidence Selection 测试 Oracle、真实 AnythingLLM 只读分数校准 |
| 生产代码切换 | **无**；`app/` 未修改，现有 weaponry 路由和 Worker 仍是遗留实现 |
| 接口影响 | 在既有批准范围内补齐精确 HTTP 400/404/409 错误文本；没有新增、删除、重命名任何请求、响应、回调、Progress 或 check-task 参数 |
| 最终结论 | 契约与测试资产已完成；真实分数语义已验证，但当前 profile 无法形成可用生产阈值，1D-0 退出门禁未通过，阶段不能关闭 |

> **2026-07-18 地面真值更正：** 本记录初次校准把 `wrong-missile` 误标为负例，后续已从正文
> 核实 `RIM-7`、`RIM-116` 和 missile launcher，并将其纠正为正例。阈值统计已在机器资产中
> 重算；农业负例仍高于部分正例，所以“原始 score 不可单独作为生产相关性门禁”的结论不变。
> 修复过程与最新指标以 `260718-阶段1D-0R检索质量修复执行记录.md` 为准。

本波次的职责是先把后续实现必须遵守的行为写成可执行资产，而不是迁移业务代码。执行中按用户
明确授权，对已经存在的 AnythingLLM workspace 做了只读校准；没有启动 `run.py`，没有创建、
删除、上传、绑定或修改远端 workspace/document/thread，也没有调用真实模型或甲方回调。

---

## 1. 已完成的契约冻结

### 1.1 HTTP 与请求边界

`tests/contracts/stage1d_weaponry_contracts.json` 同时记录当前遗留行为和已批准目标：

- 合法受理目标为 HTTP 202 严格零字节响应体；当前遗留 JSON 任务快照继续标记为待 1D-6 切换；
- 活动任务、Callback Guard sending 或 outcome unknown 保持 HTTP 409
  `{"error":"任务正在处理中"}`；
- 正常积压不设置业务数量上限，可靠受理后依次持续处理；
- `architectureId` 接受 JSON 整数或只含 ASCII 数字的字符串，规范化到
  `1..9223372036854775807`；布尔、零、负数、小数、指数、符号、空白及越界值稳定 400；
- `status` 可选且完全忽略；未知扩展键按严格 JSON 深冻结并保留；
- `weaponryTemplateFieldList`、字段对象、`fieldType`、TABLE 行/列结构和请求阶段解析结果清空
  规则均有逐项、单字段 `error` 文本；非对象元素不得再变成 500；
- 显式选文的跨分类、首次顺序、大小写去重、未找到、同名歧义、同外部引用歧义，以及缺省/空
  `filePathList` 的受理时类别冻结全部进入矩阵；
- 受理时类别为空仍返回 202，随后沿用既有异步失败回调收敛。

精确错误文本已同步到 `docs/接口文档/知识谱系解析.md`。本次只补充校验规则，没有改变参数集合
或错误体结构。

### 1.2 模式、字段和回调

- 唯一目标策略固定为 `file-aggregate-v1`；模式 1 只保留“不可选择、不得回退”的删除契约；
- INPUT 和 TABLE 各保存一份完整成功回调黄金样例，另保存失败回调、空来源对象、来源排序、
  同文档聚合和 `rows` 的精确定义；
- `fieldDescription` 在 INPUT 精炼 Retrieval Query 和最终 Extraction Prompt 中均必须出现；
  TABLE 的表说明、每一列名称和每一列说明同样必须进入两个阶段；
- INPUT Retrieval Query 明确禁止混入输出格式、示例、“未找到”、多值分隔等生成指令；
- Progress 与 weaponry check-task 仍只是当前内部兼容能力，不借 1D-0 新增公开字段；甲方规定的
  check-task 请求内同步回调恢复副作用继续保留。

### 1.3 术语规则辅助

测试矩阵把两条路径完全分开：

- `none`：目录扫描、workspace、上传、embedding、术语检索和 Prompt 辅助项均为零调用；
- `terms-rules-v1`：术语准备只属于启动期幂等协调器，字段阶段最多执行一次独立术语检索；
  术语结果只能形成有界辅助语境，不进入正式 `rows`；
- 误导用例中术语示例写“35 节”，目标 Evidence 写“31 节”，合法结果只能是“31 节”；术语内容
  不得成为 `content` 的事实来源。

这使未来删除术语 Provider 时无需修改核心 INPUT/TABLE、Application 或公开接口。

---

## 2. 来源、会话和上下文隔离资产

### 2.1 A/B 串扰回归

资产使用互斥事实：A 文档为“甲级/31 节”，B 文档为“乙级/27 节”。B 的调用必须同时满足：

- Extraction Request 只含 B 的 `documentKey` 和最终 B Evidence；
- 回调 `rows` 与实际传给模型的完整 Evidence 文本、顺序逐项一致；
- 回答来源通过 `documentKey + evidenceDigest + callId + sourceTrace` 证明；
- B 的结果禁止出现 A 的“甲级”；两个文档合法得到相同 `content` 时不能仅按文本相等判错。

### 2.2 Thread 与上下文失败

子 Thread/Conversation 创建返回 `None`、抛异常或身份不完整时，只允许有界重试；重试耗尽后
当前来源为空并继续其他来源，父 Thread 调用次数必须为零。矩阵还覆盖：

- Evidence/rows 完全一致；
- 额外 Evidence、缺失 Evidence；
- A/B 混合上下文；
- Evidence-only Context 返回越界来源；
- provided-evidence 模型无供应商来源；
- 合法相同答案但身份、Evidence 和调用轨迹均正确。

当前生产 Adapter 尚未证明无文档二次检索能力，因此供应商能力矩阵没有选择任何生产 Extraction
策略。1D-3B 只能从通过完整生命周期契约的 `provided-evidence-model-v1` 或
`evidence-only-context-v1` 中固定选择一种，不能在运行时降级到任务/分类文档 workspace。

---

## 3. Evidence Selection 离线 Oracle

新增测试侧参考算法只用于证明契约自洽，生产代码不得从 `tests` 导入。它按固定顺序验证：

1. 冻结文档集合与 score profile；
2. 非空正文和有限数值分数；
3. 同一文档内按“只统一换行并去除首尾空白”的完整正文精确去重；
4. 相关性降序、供应商 rank、稳定 Chunk ID 排序；
5. 最低分数门禁，禁止为填满 TopN 补回；
6. INPUT 8、TABLE 16 只表示供应商 Candidate 批次；Selected Evidence 不设单条、总量或
   单文档数量/字符配额；表格答案仍保留 100 行结构化结果上限。

固定夹具覆盖强相关、近义但错误、同词不同义、跨文档互斥、完全无关、重复 Chunk、跨文档相同
正文、空正文、缺失分数、`NaN` 字符标记和 profile 不匹配。反转输入顺序后仍得到相同 Selected
Evidence、拒绝原因和完整保留文本。

夹具中的 `minimumScore=0.82` 仅用于验证纯选择算法，明确带有
`productionEligible=false`。它不代表真实 AnythingLLM 阈值，也不能进入配置或 execution 快照。

---

## 4. 真实 AnythingLLM 只读校准

### 4.1 安全边界

校准只执行：

- GET 系统信息；
- GET 已有 workspace 信息；
- 对已有单文档 workspace 调用有界 `vector-search`，显式使用 `scoreThreshold=0` 观察完整分数；
- 输出只保留安全指纹、查询类别、分数和短哈希，不保存 API Key、Base URL 或完整业务 Chunk。

没有执行 workspace/document/thread 的创建、更新、删除、上传、embedding 变更或模型生成。

### 4.2 安全指纹

| 项目 | 实测值 |
| --- | --- |
| Vector DB | `lancedb` |
| Embedding engine | `native` |
| Embedding model | `MintplexLabs/multilingual-e5-small` |
| AnythingLLM 版本 | 系统 API 未暴露，不能猜测 |
| Workspace similarity threshold | `0.25` |
| Workspace topN | `6` |
| Vector search mode | `default` |
| Workspace / 文档数 | 1 / 1 |

观察到 `score` 为有限单位区间值，按分数降序返回，并在全部抽样中满足
`score + distance = 1`（浮点容差内）。因此供应商分数方向和数值协议已验证。

### 4.3 阈值评估

| 查询类别 | Top-1 分数 |
| --- | ---: |
| 强相关：精确标题 | 0.829718 |
| 强相关：舰级 | 0.841926 |
| 强相关：国家/服役 | 0.842240 |
| 强相关：动力系统描述 | 0.853682 |
| 强相关：雷达 | 0.851122 |
| 后续复核为正例：导弹字段 | 0.853542 |
| 自然语言负例：农业字段 | 0.850500 |
| 随机哨兵负例 | 0.798590 |

进一步评估 `0.82`、`0.84`、`0.85`：

| 候选阈值 | 强相关查询召回率 | 全部负例拒绝率 | 自然语言负例拒绝率 | 非空查询精确率 |
| ---: | ---: | ---: | ---: | ---: |
| 0.82 | 1.00 | 0.500000 | 0.00 | 0.857143 |
| 0.84 | 0.833333 | 0.500000 | 0.00 | 0.833333 |
| 0.85 | 0.500000 | 0.500000 | 0.00 | 0.750000 |

结论不是“阈值需要再调一点”，而是**当前原始 score 在这份真实语料上没有可分离边界**：提高阈值
会先丢失强相关样例，却仍保留已验证的农业负例。重复文档元数据和参考文献类 Chunk 在多种查询
中持续排在前列，也说明原始向量分数不能单独证明字段相关性。

### 4.4 冻结决定

真实 profile 状态固定为：

```text
status = rejected-for-direct-score-thresholding
minimumRelevanceScore = null
rawProviderScoreAloneProvesFieldRelevance = false
```

因此：

- 不把 workspace 默认 `0.25` 当作业务相关性阈值；
- 不把测试 Oracle 的 `0.82` 当作生产阈值；
- 不把缺失/非法分数转换成 `0` 后继续；
- 不允许无阈值 TopN 或运行时放宽门禁；
- 在代表性多文档校准语料、经批准的额外相关性信号或入库/切块修复完成并形成新版本 profile
  前，阻止 1D-3B 生产 Retrieval Adapter 选择和 1D-6 路由切换。

---

## 5. 故障矩阵

契约资产逐项覆盖：Document Scope Repository、受理事务、workspace、文档绑定、Thread、检索、
分数协议、模型、TABLE 解析、Translation、交互审计、终态 CAS、Callback 和 cleanup。

关键收敛规则包括：

- 文档范围查询或受理事务失败不伪装 202；
- 外部副作用结果未知不盲目重做；
- 检索失败或空命中沿用字段级空结果并成功回调；
- 分数缺失、非法、profile 不匹配或真实 profile 不可用时拒绝 Candidate，不转 0；
- Translation 失败只使 `translate` 为空；
- 审计提交失败禁止成功终态；
- expected TaskId stale 禁止旧 Progress、终态和 Callback；
- Callback 仅 2xx 成功，3xx/4xx/5xx 为 failed，超时/读取失败为 outcome unknown；
- cleanup 中断进入持久待恢复状态。

---

## 6. 测试与检查

### 6.1 新增资产测试

```powershell
.\venv\Scripts\python.exe -B -m unittest `
  tests.test_stage1d_weaponry_contract_assets -q
```

结果：**20 项全部通过**。

### 6.2 合并定向回归

新增资产与现有 weaponry、路由、阶段 0、Progress/check-task 合并运行：**99 项全部通过**。

### 6.3 安全全仓回归

动态发现 `tests/test_*.py` 并逐项排除以下 4 个既有环境型模块后，**76 个模块、909 项测试全部
通过**，测试框架计时约 49 秒：

- `tests.test_local_scripts`：部分用例会启动本地脚本或 `run.py`；
- `tests.test_multilingual_translation_integration`：依赖真实多语言模型资源；
- `tests.test_migrate_analysis_security`：包含 POSIX 文件权限断言，当前运行环境是 Windows；
- `tests.test_test_assets`：依赖仓库未提供的历史 `tests/fixtures/llm` 夹具。

测试输出中的 ERROR/WARNING/Traceback 来自既有故障注入断言，最终退出码为 0。

### 6.4 静态检查

- `tests/contracts/stage1d_weaponry_contracts.json` 通过严格 JSON 解析，测试强制拒绝 NaN/Infinity；
- `python -m compileall -q app tests` 通过，共扫描 244 个 Python 文件；输出中的 `Can't list
  tests/.runtime/test-temp/...` 是并发测试已清理临时目录后的已知提示，不是编译失败；
- 新测试通过 `py_compile`；
- 架构边界测试包含在 909 项安全回归中；
- `git diff --check` 在文档同步完成后通过；
- `git status --short -- app` 无输出，证明本波次没有修改生产源码；
- 全程没有执行 `run.py`。

---

## 7. 变更文件

| 文件 | 作用 |
| --- | --- |
| `tests/contracts/stage1d_weaponry_contracts.json` | 1D-0 唯一机器可读契约、黄金、隔离、故障和校准资产 |
| `tests/test_stage1d_weaponry_contract_assets.py` | 20 项严格离线校验、接口文档同步断言和测试侧 Selection Oracle |
| `../接口文档/知识谱系解析.md` | 补齐已批准目标的精确 400/404/409 文本，不改变参数 |
| `../重构记录/260717-阶段1D武器谱文件级实施设计.md` | 回写实际执行、测试和校准停止门禁 |
| `../重构记录/260716-阶段1C至11滚动实施计划.md` | 同步阶段状态和后续阻塞关系 |
| `../重构记录/260715-其余业务分层统一改造实施计划.md` | 同步 1D 垂直切片状态 |
| `../重构记录/260715-低耦合高并发任务隔离与可靠队列改造总计划.md` | 同步阶段 1 总体状态 |
| `../重构记录/README.md`、`../../tests/README.md` | 增加文档和测试入口 |

---

## 8. 当前能力与下一步决策

当前已经获得的是：完整、可执行的 1D-0 行为基线，以及一个能够阻止“凭经验填阈值”的真实
校准证据。当前尚未获得的是：生产可用的 Evidence relevance profile、无文档二次 RAG 的真实
Extraction Adapter、weaponry execution/Dispatcher/Guard 和路由切换。

下一步不能直接把 `0.82` 写进代码。需要先确认并实施一种可重复方案，例如优先修复入库/切块
造成的重复元数据与参考文献 Chunk 主导问题，并在代表性多文档语料上重新校准；若仍不可分，
再单独评审经验证的重排/额外相关性信号。任何方案都必须版本化指纹、保存指标和误判样例，且
不得改变公开接口参数。

在该决策完成前，1D-0 保持“资产完成、退出门禁未通过”，不得误报为阶段关闭或生产就绪。
