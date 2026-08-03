# 知识谱系对话来源 Metadata 清洗实施计划

## 1. 文档状态与待确认事项

- 制订日期：2026-08-03。
- 目标分支：`feat/weaponry-chat`。
- 制订基线：`2434518`，制订计划时工作树干净。
- 状态：阶段 0～7 已全部完成。
- 本改造不增加、删除或改名任何前后端接口参数，不改变路径、请求字段、响应字段、HTTP 状态码、
  SSE Header、事件名称、事件字段或事件顺序。
- 当前处于开发阶段，不迁移、不兼容已经保存的旧 Chunk；实施验收前应在另行确认的开发环境维护
  窗口清理旧 Chat 数据，而不是在 History 读取路径增加双轨兼容或隐式改写。

本需求会改变公开字段 `sourceChunks.chunks[].content` 以及 History assistant
`chunks[].content` 的内容语义。现行权威文档要求“保持上游 JSON 字符串值不变”，与“删除开头的
`<document_metadata>...</document_metadata>`”直接冲突。因此进入代码实施前，负责人需要明确
批准同步修改：

1. `docs/接口文档/知识谱系类别文件对话.md` 的 Chunk 正文定义和完整规则；
2. `docs/接口文档/README.md` 的知识谱系对话合同摘要；
3. `tests/contracts/weaponry_chat_contract.json` 的内部黄金合同断言。

上述修改只调整既有 `content` 字段的内容规范，不涉及增删接口参数。负责人已在 2026-08-03
明确批准上述文档同步范围；实施仍须保证代码、黄金合同和权威文档在同一专项内共同收口。

## 2. 问题现状与已验证链路

当前来源内容依次经过：

```text
AnythingLLM finalizeResponseStream.sources[].text
  → AnythingLLMStreamSource.content
  → ChatSourceEvidence.content
  → ChatSourceMapper.map_sources()
  → source_snapshot
  → 同一成功事务保存 assistant 与 ChatMessageSourceChunk
  → SSE sourceChunks / History assistant.chunks
```

现有 `ChatSourceMapper` 仅校验来源正文非空并按结构化来源键映射文件，随后完整保留上游字符串。
使用带 Metadata 的最小样例复核时，映射结果仍为：

```text
<document_metadata>
sourceDocument: secret.pdf
</document_metadata>

正文
```

因此问题不是 SSE 序列化器额外拼接，而是供应商来源正文在进入公开快照前没有执行 Weaponry
专属清洗。制订计划时相关 Mapper、Executor、Route、History、合同资产及 AnythingLLM 协议测试
共 114 项通过，可作为实施前回归基线。

## 3. 目标行为

实施后，新完成的知识谱系对话必须满足：

1. `sourceChunks.chunks[].content` 不包含 AnythingLLM 在正文开头附带的完整
   `<document_metadata>...</document_metadata>` 包装；
2. 同轮成功事务保存的 History assistant `chunks[].content` 使用同一份清洗结果，SSE 与 History
   的数组顺序和三个字段值继续完全一致；
3. 不含前置 Metadata 的合法 Chunk 保持原值，不执行 Unicode 规范化、换行统一、Markdown 清理、
   通用 `strip()`、正文去重或内容截断；
4. 来源顺序、重复项、`fileName`、`originalFileName` 及结构化来源身份映射规则保持不变；
5. 文件对话 `/llm/chat*` 不新增 `sourceChunks`，也不改变任何公开或持久化行为；
6. 清洗必须在成功事务提交前完成，禁止 SSE 已发送清洗值但数据库仍保存原始 Metadata；
7. 对无法安全识别的前置 Metadata 失败关闭，不把可能含供应商内部信息的来源传给前端。

### 3.1 精确清洗规则

清洗器采用“只识别开头完整供应商包装”的窄规则：

1. 仅在正文有效开头（允许供应商产生的 BOM 或前导空白）识别大小写不敏感的
   `<document_metadata>`；
2. 找到第一个完整 `</document_metadata>` 后，删除开头包装以及标签后只承担分隔作用的空白行；
   如果正文第一行本身带缩进，不删除该缩进；
3. 从实际正文开始，其后的所有字符、换行类型、Unicode 码点和尾部空白保持原样；
4. 正文中部或尾部偶然出现的同名标签不处理；
5. 有前置开始标签但缺少闭合标签时，整轮失败关闭；
6. 删除后正文为空或仅含空白时，沿用现有“来源正文为空”规则令整轮失败；
7. 若删除第一段后仍以第二段 Metadata 包装开头，按异常供应商格式失败关闭，禁止循环吞掉未知
   层级后继续成功。

该规则只删除供应商包装，不复用 Weaponry 检索质量模块的 `normalize_evidence_text()`。后者还会
执行 NFKC、换行统一、零宽字符删除和 CJK 空白修复，不符合对话来源正文“除包装外保持原值”的
新合同。

## 4. 架构与所有权决策

清洗逻辑放在 `app/modules/chat/application/source_mapper.py` 的 Weaponry 公开来源映射边界：

- `AnythingLLMThreadClient` 继续忠实解析供应商 SSE，不承担业务公开投影；
- `AnythingLLMChatGateway` 继续把供应商 DTO 转为供应商无关 `ChatSourceEvidence`，不针对某一公开
  接口改写内容；
- `ChatSourceMapper` 只在 `policy.expose_source_chunks=True` 时由 Executor 调用，因此清洗天然只
  作用于 Weaponry Chat；
- Mapper 输出同时用于持久化和 SSE，避免 Presenter 与 History 各自实现一套规则；
- Presenter 继续只负责字段白名单和 SSE 顺序，不做内容修复；
- History 继续只读取本地 committed 快照，不在读取时二次清洗旧数据。

清洗函数必须是无 I/O、无共享可变状态、可重入的纯函数，不读取环境、数据库或网络。该设计适合
未来多线程 Worker、可靠队列和多实例部署，不引入进程级缓存或顺序依赖。

## 5. 分阶段实施与验收门禁

所有阶段必须按顺序执行。每个阶段完成后都要检查合同歧义、异常语义、日志安全、数据清理范围和
测试结果；存在未决事项时立即停止，不得进入下一阶段。

### 阶段 0：合同授权与基线冻结

1. 获得负责人对第 1 节三处合同文档/资产修改的明确批准；
2. 重新确认分支、HEAD、工作树和已有在制改动，禁止覆盖无关工作；
3. 冻结不增删接口参数、不改 SSE 顺序、不迁移旧数据、不运行 `run.py` 的边界；
4. 保存带 Metadata、无 Metadata、畸形 Metadata 和空正文四类脱敏输入基线；
5. 记录实施前定向测试的发现数、执行数、失败、错误和跳过数。

门禁：接口文档更新已获批准；当前代码链和合同冲突已形成书面结论；工作树来源清晰；无待商讨
事项。否则停止。

### 阶段 1：纯清洗规则与单元门禁

1. 在 `source_mapper.py` 增加私有、纯函数式前置 Metadata 清洗器；
2. 使用不引入 Application 层禁用依赖的确定性开头扫描规则，并显式识别未闭合和连续包装；
3. 添加详细中文注释，说明只删除供应商包装以及不能调用通用正文规范化的原因；
4. 为清洗器增加表驱动单元测试。

必须覆盖：LF/CRLF、多行 Metadata、可选 BOM/前导空白、大小写变化、正文 Unicode 组合字符、
尾部空白、无 Metadata 原值、正文中部标签不改写、未闭合标签、连续包装、包装后空正文。

门禁：纯函数测试全部通过；清洗后正文除被批准的前置包装和分隔外无变化；无 I/O、全局可变状态
或跨任务缓存；无待商讨事项。

### 阶段 2：Weaponry 来源映射接入

1. `ChatSourceMapper.map_sources()` 在文件身份唯一映射成功后，对每个来源正文调用清洗器；
2. 使用清洗后的正文构造 `MappedChatSource`；
3. 保留来源数组顺序与重复项，不改变文件名映射和严格失败策略；
4. 更新类、DTO 和方法注释中旧的“无损原文”表述，改为“移除前置供应商包装后保持正文”。

门禁：Mapper 单测证明同一输入总是得到同一输出；坏来源导致整个映射失败，不出现部分 Chunk；
通用 AnythingLLM Client/Gateway 测试继续证明其保留供应商原始 DTO；文件对话策略不受影响；无待
商讨事项。

### 阶段 3：执行、持久化与日志闭环

1. 保持 Executor 先生成完整 `source_snapshot`、再在 `done` 成功事务中保存的既有线性化顺序；
2. 增加结构化脱敏日志，可记录 `userId`、`architectureId`、`conversation_id`、`run_id`、来源数、
   已清洗来源数和删除字符总数；
3. 禁止日志记录 Metadata 内容、Chunk 正文、文件名、来源键、URL、原始 SSE 帧或供应商响应；
4. 畸形 Metadata 沿现有错误路径收敛，不提交 assistant/chunks，不发送 `sourceChunks` 或 `done`。

门禁：成功路径的 SSE 与 SQLite 快照精确一致；失败注入路径无部分提交；日志捕获测试确认允许的
业务 ID 可定位且敏感内容零泄漏；中断、断连、重复终态和 Query 空来源语义不退化；无待商讨
事项。

### 阶段 4：接口合同与项目文档同步

在阶段 0 已批准的范围内：

1. 修改 `docs/接口文档/知识谱系类别文件对话.md`，将“上游字符串完全不变”修订为“仅删除开头
   完整 Metadata 包装，其余正文保持不变”，同步 SSE 与 History 说明和示例；
2. 修改 `docs/接口文档/README.md` 的合同摘要；
3. 更新 `tests/contracts/weaponry_chat_contract.json`，用显式键冻结“删除前置 Metadata、保留
   剩余正文、SSE/History 一致”；该资产字段不是公开接口参数；
4. 更新根 `README.md`、Chat 模块 README 与测试说明；
5. 不追改旧更新记录和已完成计划中的历史事实，新增本专项执行记录并登记索引。

门禁：权威接口文档、黄金合同、代码注释和当前 README 无矛盾；公开字段集合、状态码、Header 和
事件顺序的黄金断言保持不变；无待商讨事项。

### 阶段 5：定向离线回归

使用 `venv\\Scripts\\python.exe -B`，不启动 `run.py`，至少执行：

- `tests.test_chat_source_mapper`；
- `tests.test_chat_run_executor`；
- `tests.test_weaponry_chat_routes`；
- `tests.test_chat_history_service`；
- `tests.test_weaponry_chat_contract_assets`；
- `tests.test_anythingllm_chat_gateway`；
- `tests.test_anythingllm_threads`；
- Chat 持久化、Presenter、策略、日志和架构边界相关测试。

路由级验收必须逐帧断言：

```text
chatInfo → textChunk* → sourceChunks → done
```

并验证 SSE/History 中均无 `<document_metadata>`，正文剩余部分完全一致；空 sources 仍返回
`chunks=[]`；畸形包装返回 `error` 且 History 不出现本轮 assistant/chunks。

门禁：定向测试失败和错误均为 0；预期故障注入日志不得误计为失败；不把 Fake/临时 SQLite 结果
表述为真实供应商或多实例证明；无待商讨事项。

### 阶段 6：安全全仓与静态关闭

1. 按项目既有精确排除清单执行安全全仓测试，报告发现、排除、执行、失败、错误和跳过数量；
2. 执行 `compileall`、架构边界、合同资产、日志泄漏扫描和 `git diff --check`；
3. 核对 `run.py`、数据库 Schema、迁移、公开 Parser/Presenter 字段集合均未改变；
4. 核对没有在 AnythingLLM 通用集成层、History 读取层或文件对话路径复制清洗逻辑；
5. 完成更新记录，明确旧数据零迁移和 SQLite 单实例证据边界。

门禁：失败与错误均为 0；所有改动均可归属本专项；没有待商讨事项后方可标记离线完成。

### 阶段 7：可选真实 AnythingLLM 协议级验收

该阶段会创建和删除真实远端资源，必须另行获得维护窗口、目标实例和资源范围授权。不得因批准
代码计划而推定获得远端写入或删除权限。

1. 确认目标为非生产或明确授权的隔离实例，并记录 Workspace 基线；
2. 使用虚拟 `userId + architectureId` 创建隔离 Weaponry Chat，不启动 `run.py`；
3. 上传不含敏感信息且能稳定召回的最小文档，确认 AnythingLLM 原始 Finalization 来源确含前置
   Metadata；
4. 通过 Flask 协议级客户端逐帧验证 SSE，随后读取 History，确认两者均已清洗且完全一致；
5. 删除对话、Workspace、Thread、文档和本地测试数据，确认远端基线恢复、目标残留为 0；
6. 如真实上游未返回目标形状、发生名称碰撞或清理所有权不明确，停止并报告，不能用人工构造输入
   冒充真实通过。

门禁：真实目标形状已观察、SSE/History 清洗一致、清理审计通过。证据只能称为协议级客户端和
本机/指定 AnythingLLM 验收，不得外推为浏览器 UI、生产、多实例、可靠队列、高并发容量或
exactly-once 证明。

## 6. 文件修改范围

### 6.1 预计修改的生产代码

- `app/modules/chat/application/source_mapper.py`：唯一清洗实现和来源映射接入；
- `app/modules/chat/application/run_executor.py`：必要的清洗统计与脱敏日志，不改变事务和事件顺序。

### 6.2 预计修改的测试与合同资产

- `tests/test_chat_source_mapper.py`；
- `tests/test_chat_run_executor.py`；
- `tests/test_weaponry_chat_routes.py`；
- `tests/test_chat_history_service.py`（如需补充直接历史投影断言）；
- `tests/test_weaponry_chat_contract_assets.py`；
- `tests/test_anythingllm_chat_gateway.py`；
- `tests/test_anythingllm_threads.py`；
- `tests/contracts/weaponry_chat_contract.json`；
- `tests/contracts/anythingllm_v1_15_stream.jsonl`（仅在需要增加脱敏真实形状 Fixture 时）；
- `tests/assets/document_processing/stage1h_baseline.json`（接口索引获批变更后的只读 Hash 同步）；
- `tests/README.md`。

### 6.3 预计修改的当前文档

- `docs/接口文档/知识谱系类别文件对话.md`（必须先获批准）；
- `docs/接口文档/README.md`（必须先获批准）；
- `README.md`；
- `app/modules/chat/README.md`；
- `docs/重构记录/README.md`；
- 后续新增 `docs/更新记录/260803-知识谱系对话来源Metadata清洗执行记录.md` 并登记索引。

### 6.4 明确不修改

- `/llm/weaponry-chat*` 的请求 Parser、路由参数和公开 Presenter 字段；
- `app/integrations/anythingllm/threads.py` 及通用供应商 DTO 的原始解析语义；
- 文件对话 `/llm/chat*` 的接口、来源策略和历史结构；
- Chat SQLite Schema、迁移版本和旧数据；
- Workspace/Thread 命名规则；
- `run.py`。

## 7. 验收矩阵

| 场景 | SSE `sourceChunks.content` | History `chunks.content` | 运行结果 |
| --- | --- | --- | --- |
| 完整前置 Metadata + 正文 | Metadata 被删除，正文保留 | 与 SSE 完全一致 | 成功 |
| 无 Metadata | 原值不变 | 与 SSE 完全一致 | 成功 |
| Metadata 后含 CRLF/Unicode/尾空白 | 仅包装与分隔删除，其余码点保持 | 与 SSE 完全一致 | 成功 |
| Metadata 标签位于正文中部 | 不改写 | 与 SSE 完全一致 | 成功 |
| 前置开始标签未闭合 | 不发送 | 不提交本轮 Chunk | `error` |
| 包装后为空或连续包装 | 不发送 | 不提交本轮 Chunk | `error` |
| AnythingLLM 空 sources | `chunks=[]` | `chunks=[]` | 成功 |
| File Chat | 不产生 `sourceChunks` | 不新增 assistant `chunks` | 既有行为 |
| 中断或客户端提交前断开 | 不发送 | 不提交不完整 assistant/chunks | 既有收敛 |

## 8. 完成定义与达成效果

全部完成必须同时满足：

1. 新产生的 Weaponry Chat SSE 和 History 不再暴露开头的 AnythingLLM
   `<document_metadata>` 信息；
2. 清洗发生在持久化前且只有一个实现位置，SSE/History 不会产生内容分叉；
3. 除明确批准删除的供应商包装外，Chunk 正文保持原值，来源顺序和重复项不变；
4. 畸形供应商包装失败关闭，无部分保存、部分来源或 Metadata 泄漏；
5. 五个公开接口的参数、字段、状态码、Header、SSE 事件名称与顺序全部保持不变；
6. 文件对话、AnythingLLM 通用协议解析、Workspace 命名和数据库 Schema 不受影响；
7. 日志能够用业务 ID、运行 ID 和数量定位清洗结果，但不记录 Metadata 或任何正文；
8. 旧数据不迁移、不兼容，开发环境按明确范围清理后验收；
9. 定向门禁和安全全仓门禁通过，真实验收若执行则单独报告证据边界与清理结果。

## 9. 阶段执行状态

- 阶段 0：已完成。基线为 `feat/weaponry-chat@2434518`；进入实施前工作树仅包含本计划和重构
  索引，未发现无关在制改动；负责人已批准修改既有 `content` 内容语义，且确认不增删接口参数。
- 阶段 1：已完成。新增纯清洗规则，覆盖完整包装、BOM/前导空白、LF/CRLF、正文缩进与 Unicode
  保留、正文中部标签、未闭合/未知属性/连续包装、包装后空正文及非字符串输入；9 项单元测试、
  `py_compile` 和 `git diff --check` 均通过。
- 阶段 2：已完成。`ChatSourceMapper` 已在唯一 Weaponry 来源公开映射点接入清洗规则；通用
  AnythingLLM Thread Client 与 Chat Gateway 继续保留原始供应商 DTO，File Chat 的来源公开策略
  保持关闭。Mapper/通用供应商边界 50 项、策略与 Executor 52 项测试通过。
- 阶段 3：已完成。Executor 在提交前复用清洗快照，并记录 architecture ID、内部关联 ID、来源数、
  清洗命中数与删除字符数；日志不记录 Metadata、Chunk、文件或来源身份。新增成功原子提交和畸形
  包装失败关闭门禁；Executor 49 项、Route/History 13 项测试通过，SQLite、Presenter、Blueprint、
  AnythingLLM 通用集成、迁移与 `run.py` 无改动。
- 阶段 4：已完成。已按负责人授权同步权威接口文档、接口索引、黄金合同 v2、根 README、Chat
  模块说明和测试索引；公开字段、状态码、Header 与事件顺序未变。合同/Route 19 项测试、JSON
  语法和 `git diff --check` 通过，当前文档中的旧“上游字符串完全不变”表述已清零。
- 阶段 5：已完成。首轮 186 项中业务测试全部通过，但架构门禁发现 Application 层不允许导入
  `re`；阶段未前推，已将实现改为仅依赖内建字符串扫描的确定性纯函数。完整重跑 186 项全部
  通过，覆盖 SSE/History 清洗一致、畸形包装公开 `error`、无部分 assistant/chunks、File Chat
  隔离、供应商原始 DTO 保留、日志脱敏和架构方向。
- 阶段 6：已完成。`compileall` 通过。首次安全全仓发现 2,203、排除 13、执行 2,190，只有已获
  授权修改的 `docs/接口文档/README.md` 与阶段 1H 公共文档旧 Hash 不一致；阶段未关闭，按既有
  治理方式同步黄金 Hash 后，失败点与专项合同 23 项通过。完整重跑仍为执行 2,190，失败 0、错误
  0、跳过 3；运行代码归属、接口字段、Schema、迁移和通用供应商边界核对通过。
- 阶段 7：已完成。负责人明确授权本地 DocSense/AnythingLLM、不限维护窗口和资源范围后，使用
  随机隔离文档与两个精确命名 Workspace 完成真实验收：原始 Finalization 唯一来源含前置
  Metadata 和隔离令牌；公开 SSE/History 来源已清洗且快照一致；公开删除、探针清理和全局文档
  删除均成功。Workspace 总数从 4 恢复为 4，独立只读复核确认目标 Workspace/文档残留为 0；
  `run.py` 未启动。证据仅覆盖本机协议客户端和当前 AnythingLLM 实例。
