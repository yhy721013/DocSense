# 知识谱系对话来源 Metadata 清洗执行记录

## 1. 改造目标与合同边界

本专项修复 `/llm/weaponry-chat` 成功 SSE 的 `sourceChunks.chunks[].content` 会把
AnythingLLM 前置 `<document_metadata>...</document_metadata>` 包装传给前端的问题，并保证
History assistant `chunks[].content` 使用同一份清洗后快照。

负责人已明确批准修改既有 `content` 字段的内容语义。本次没有增加、删除或改名任何请求参数、
响应字段、Header、HTTP 状态码、SSE 事件或事件顺序；文件对话、Workspace 命名、Chat SQLite
Schema 和迁移均未改变。项目仍处于开发阶段，旧 Chunk 不迁移、不在 History 读取路径兼容清洗。

## 2. 最终实现

### 2.1 唯一清洗边界

`app/modules/chat/application/source_mapper.py` 新增无 I/O、无共享状态的确定性字符串扫描规则，并由
`ChatSourceMapper` 在 Weaponry 来源公开映射时统一调用：

- 只识别有效开头处的完整、无属性、大小写不敏感 `document_metadata` 包装；
- 允许 BOM 和供应商前导空白，删除闭合标签后仅用于分隔正文的空白行；
- 正文起始后不做 `strip()`、Unicode 规范化、换行统一、去重或截断；
- 无包装或正文中部同名标签保持原值；
- 未闭合、未知属性、连续包装、包装后空正文和非字符串输入失败关闭。

清洗器不放入 AnythingLLM 通用 Client/Gateway，后者继续保存供应商原始 DTO；也不放入 Presenter、
History 或 SQLite Adapter，避免不同出口产生双轨内容。

### 2.2 原子提交与错误收敛

Executor 先完成全部来源映射和清洗，再把同一 `source_snapshot` 交给成功事务保存 assistant 与
Chunk；提交后才发送 `sourceChunks` 和 `done`。因此 SSE 与 History 不会分别清洗，也不会出现前端
已清洗但数据库仍保存 Metadata 的状态。

任一来源包装畸形时，沿既有错误路径发送 `error`，不发送 `sourceChunks`/`done`，不提交本轮
assistant 或任何 Chunk；本轮 user 消息仍按既有受理语义保留。

### 2.3 日志治理

成功映射后记录 `architectureId`、内部 `conversation_id`、`run_id`、来源总数、清洗命中数和删除
字符总数。日志允许业务链路按既有入口记录 `userId`，但本次未为获取该值扩大 Executor DTO 或公开
接口。所有新增日志均禁止记录 Metadata、Chunk 正文、文件名、来源键、URL、供应商原始帧或响应。

## 3. 接口文档与合同资产

已同步：

- `docs/接口文档/知识谱系对话.md`：冻结“删除一个完整前置包装、其余正文保持原值”以及
  畸形包装失败关闭语义；
- `docs/接口文档/README.md`：同步接口索引摘要；
- `tests/contracts/weaponry_chat_contract.json`：升级为 schema v2，新增前置包装删除、剩余正文保留
  和畸形包装拒绝断言；
- `tests/assets/document_processing/stage1h_baseline.json`：按已授权后的接口索引 SHA-256 同步只读
  公共文档基线；
- 根 README、Chat 模块说明和测试索引。

公开字段集合、状态码、Header、SSE 事件名和顺序的既有黄金断言保持不变。

## 4. 分阶段验收结果

1. 阶段 0：确认合同修改授权、Git 基线和零迁移/不运行 `run.py` 边界；实施前 114 项定向测试通过。
2. 阶段 1：纯清洗规则 9 项单元测试、`py_compile` 和 `git diff --check` 通过。
3. 阶段 2：Mapper/通用供应商边界 50 项、策略与 Executor 52 项通过，证明 File Chat 隔离和
   AnythingLLM 原始 DTO 保留。
4. 阶段 3：Executor 49 项、Route/History 13 项通过，覆盖成功原子提交、SSE/SQLite 一致、畸形
   包装无部分提交及日志内容不泄漏。
5. 阶段 4：合同/Route 19 项、JSON 解析和文档旧语义扫描通过。
6. 阶段 5：首轮 186 项中业务断言通过，但架构门禁发现 Application 层禁止导入 `re`；阶段未
   前推，改用仅依赖内建字符串操作的扫描器后，完整 186 项全部通过。
7. 阶段 6：首次安全全仓执行 2,190 项时仅公共接口文档 SHA 基线 1 项失败；按负责人已授权的
   文档变更同步黄金 Hash 后，失败点与专项合同 23 项通过。随后完整重跑：发现 2,203、精确排除
   13、执行 2,190，失败 0、错误 0、跳过 3；`compileall` 通过。

阶段 6 的预期故障注入会输出 ERROR/CRITICAL 日志，上述结论只以 unittest 最终统计为准。

## 5. 安全全仓排除项

安全全仓沿用项目既定 13 项精确排除集合，没有扩大：

1. `tests.test_local_scripts.LocalScriptTests.*` 共 7 项，可能执行本地 Shell、访问本地应用、启动
   文件服务或 `run.py`；
2. `tests.test_test_assets.LLMTestAssetsTests.*` 共 5 项，依赖被 `.gitignore` 排除的本地样例；
3. `tests.test_migrate_analysis_security.AnalysisSecurityMigrationTests.test_apply_is_idempotent_and_preserves_callback_metadata_and_audit`
   依赖 Windows 不支持的 POSIX 权限位断言。

## 6. 阶段 7 本机 AnythingLLM 真实协议验收

负责人确认 DocSense 与 AnythingLLM 均位于本地，维护窗口和资源范围不限后，执行真实协议验收：

1. `.env` 目标经 URL 解析确认为 `localhost:3001/api/v1`，API Key 只由配置加载器读取，未打印、
   未写入文档；写入前 AnythingLLM Workspace 基线为 4；
2. 使用随机隔离 `userId=1786000643943`、`architectureId=1787010386639`，公开 Workspace 名称为
   `wChat-user1786000643943-arch1787010386639`；另建随机探针 Workspace 直接读取供应商原始流；
3. 上传一份 207 字节、不含业务数据且带唯一令牌和结构化 `docSource` 的 Markdown；原子 Thread
   Client 得到唯一 Finalization、1 个来源，其中 1 个来源同时包含真实前置 Metadata 和隔离令牌；
4. 同一真实 Metadata 形状可被生产清洗器命中；随后使用真实 Chat Factory、临时知识库/Chat
   SQLite 与 Flask 协议客户端调用公开 `/llm/weaponry-chat`，没有启动 `run.py` 或后台 Dispatcher；
5. 公开 SSE 共 39 帧，其中 36 个 `textChunk`、1 个 `sourceChunks`；来源数为 1，清洗命中数为 1，
   删除 123 个字符；SSE 和 History 均无 Metadata，且 `chunks` 数组逐字段完全一致；
6. 公开 delete 成功删除 Thread、Workspace 和本地 Conversation 聚合；探针资源、全局测试文档和
   临时本地数据随后全部删除；Workspace 总数恢复为 4，目标 Workspace/文档残留均为 0；
7. 主流程退出码为 0。结束后使用独立任务级 Client 只读复核，再次得到 Workspace=4、公开目标
   残留=0、探针目标残留=0、全局测试文档残留=0。

独立复核前两次内联命令因 PowerShell 剥离 Python 引号而在解释阶段触发 `SyntaxError`，没有建立
网络 Client、没有发起请求，也未计为验收通过；改用临时只读脚本后退出码为 0。所有临时脚本均已
删除，不属于交付物。

## 7. 证据边界

阶段 0～6 证明 Windows、临时 SQLite、Fake/协议 Fixture 和单实例离线链路满足新合同；该部分证据
自身不证明真实 AnythingLLM 一定返回目标 Metadata 形状。

阶段 7 进一步证明当前本机 AnythingLLM 实例真实返回目标 Metadata 形状，且 Flask 协议级
SSE/History 清洗与资源恢复正确。它仍不证明仓库外真实浏览器/UI、生产代理与访问日志、多实例
一致性、可靠队列、共享数据库、并发容量、其他 AnythingLLM 版本或 exactly-once 行为。
