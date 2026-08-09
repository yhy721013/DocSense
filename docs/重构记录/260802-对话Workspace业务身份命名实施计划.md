# 对话 Workspace 业务身份命名实施计划

## 1. 文档状态与取代关系

- 制订日期：2026-08-02。
- 目标分支：`feat/weaponry-chat`。
- 状态：阶段 0～6 已全部完成；离线关闭与本机回环 AnythingLLM 隔离真实验收均已通过。
- 本计划只修改 DocSense 内部远端资源命名、资源冲突处理和日志治理，不增删或改变任何
  HTTP、SSE、Header、状态码、请求字段或响应字段。
- 本计划取代以下历史内部决策，但不回写历史执行记录：
  - `260801-对话模块迁移与知识谱系独立接口详细实施计划.md` 中“主远端资源使用随机内部
    Conversation ID 命名”的决定；
  - `../更新记录/260802-对话模块迁移与知识谱系独立接口离线关闭执行记录.md` 中“不拼接 userId、
    architectureId”的当时实现事实。
- `docs/接口文档/` 仍是公开接口唯一权威。本计划实施期间禁止增加、删除或改名任何前后端
  接口参数。

## 2. 目标命名规则

AnythingLLM Workspace 的 `name` 必须严格使用规范化业务身份：

| 对话类型 | 规则 | 示例 |
| --- | --- | --- |
| 文件对话 | `chat-id` + `{chatId}` | `chat-id10001` |
| 知识谱系对话 | `wChat-user` + `{userId}` + `-arch` + `{architectureId}` | `wChat-user10001-arch20001` |

规则说明：

1. 字母大小写、连接符和前缀固定，不增加额外分隔符；
2. ID 使用领域层校验后的规范十进制整数文本，不保留请求中的前导零；
3. 不截断、不哈希、不使用内部 `conversation_id` 替换业务 ID；
4. 只约束 Workspace 的展示名称 `name`，不约束 AnythingLLM 生成的 `slug` 或内部 ID；
5. 主 Thread 继续使用 `thread-{conversation_id}`，标题临时线程继续使用随机 attempt ID；
6. 内部 `conversation_id` 继续承担聚合、运行、租约、清理和将来队列隔离职责。

## 3. 日志治理决策

负责人已确认应用日志可以记录 `userId`，并授权实现根据排障需要记录其他必要内容。为兼顾
可观测性与数据安全，本计划冻结以下分级规则：

### 3.1 INFO/WARNING 可以记录

- 规范化 `chatId`、`userId`、`architectureId`；
- 精确 Workspace 名称、内部 `conversation_id`、`run_id`、资源租约 ID；
- 操作类型、身份类型、是否新建、状态、数量、字符数、重试/补偿结果和异常类型；
- 为定位资源清理问题所必需的 Workspace/Thread 不透明引用，但不得把引用写入公开响应。

### 3.2 DEBUG 可以额外记录

- 已通过领域校验的供应商资源引用和调用阶段；
- 不包含正文的供应商响应结构摘要、字段存在性和计数。

### 3.3 所有级别仍禁止记录

- AnythingLLM API Key、Authorization、Cookie、Callback 凭据和其他 secret；
- 用户消息正文、模型完整回答、Chunk 原文、文件正文、Prompt 全文；
- 原始请求体、完整响应体、SSE 原始帧；
- 含凭据或敏感 Query 的完整 URL、未脱敏异常响应正文和请求 Header。

文件名、原文件名和来源 URL 默认不写 INFO；只有出现无法通过 ID、计数、结构摘要定位的明确
故障，且完成脱敏后，才允许在受控 DEBUG 日志中记录。日志授权不改变任何公开返回结构。

## 4. 当前实现基线

当前 `SynchronousChatRunExecutor._open_or_reuse_conversation()` 在首次创建远端资源时传递：

```python
context_name=f"chat-{request.conversation_id}"
conversation_name=f"thread-{request.conversation_id}"
```

`ChatRunStreamRequest` 只保存 `identity_kind`，但执行恢复已经能够根据持久化 `run_id` 和内部
`conversation_id` 读取不可变 `ConversationIdentityBinding`。因此命名必须从该权威 Binding
生成，禁止重新读取 Web 请求或让 Blueprint 拼接供应商名称。

`AnythingLLMChatGateway` 当前会按名称查找并自动复用同名 Workspace。业务身份名称允许
Weaponry 删除后由下一世代再次使用，因此没有本地远端引用时不能把同名 Workspace 自动认领为
当前世代资源。

## 5. 分阶段实施

### 阶段 0：合同、日志与供应商边界冻结

1. 修改知识谱系接口文档中的日志条款，允许应用日志记录业务 ID 和必要资源定位信息；
2. 明确公开接口字段、状态码、Header、SSE 和 History 投影零变化；
3. 写入本计划并登记重构索引；
4. 记录供应商名称长度/规范化停止条件：如果 AnythingLLM 无法完整保存合同允许的 ID，禁止
   静默截断、哈希或新增接口上限，必须停止并重新确认。

门禁：文档规则无冲突；除已确认的日志条款外不存在公开接口语义变化；`git diff --check` 通过。

### 阶段 1：纯领域命名策略

1. 新增供应商无关的 Workspace 名称值对象或纯函数；
2. 输入为不可变 `ConversationIdentityBinding`，输出为非空规范名称；
3. File/Weaponry 字段不完整、身份类型未知或绑定不一致时失败关闭；
4. Domain 不导入 Flask、SQLite 或 AnythingLLM。

门禁：纯单元测试覆盖两类规则、边界整数、无前导零、未知身份和不可变性；架构导入检查通过。

### 阶段 2：运行恢复与创建链接入

1. `ChatRunStreamRequest` 增加内部必填 `workspace_name`；
2. `_request_for_run()` 只从数据库中的不可变 Identity Binding 生成该值；
3. `_open_or_reuse_conversation()` 使用该值作为 `context_name`；
4. 主 Thread 命名保持 `thread-{conversation_id}`；
5. 已持久化 `workspace_ref + thread_ref` 的旧会话直接复用引用，不重命名；
6. 不修改 SQLite Schema，不把名称增加到公开请求或响应。

门禁：执行器 Fake 精确断言两类名称；按 `run_id` 重建得到相同名称；旧引用零创建；既有运行、
文档范围和终态测试全部通过。

### 阶段 3：AnythingLLM Gateway 安全加固

当本地没有远端引用而进入 `open_conversation()` 时：

1. 同名 Workspace 为 0 个：创建新 Workspace；
2. 同名 Workspace 为 1 个或多个：抛出内部资源名称冲突，不认领、不创建 Thread、不删除未知资源；
3. 供应商返回的 Workspace `name` 必须与请求值完全一致；不一致时补偿删除本次新建资源；
4. Thread 创建失败继续执行现有新 Workspace 补偿；补偿失败继续返回精确资源引用供租约清理；
5. 日志按第 3 节输出必要业务身份、Workspace 名称、内部关联和补偿结果，绝不输出 secret 或正文。

门禁：创建、同名冲突、重复同名、返回名称漂移、Thread 失败补偿、补偿失败和日志内容均有离线
测试；通用 AnythingLLM 原子客户端保持供应商协议职责，不吸收 Chat 业务命名规则。

### 阶段 4：兼容、删除重建与并发

1. 历史旧命名 Workspace 只要本地引用完整，续聊和删除继续成功；
2. Weaponry 删除成功后相同业务身份可以创建新 Workspace，名称相同但文档范围必须是新世代快照；
3. 删除清理失败时身份不释放，不能创建第二个同名 Workspace；
4. File 删除后永久墓碑继续禁止相同 `chatId` 重建；
5. 50 个不同身份并发生成唯一目标名称且资源、运行和消息不串扰；
6. 当前结论只证明 SQLite 单实例离线行为，不宣称分布式唯一性或可靠队列。

门禁：删除/重试/重建、旧会话、日志、50 并发和公开路由回归全部通过。

### 阶段 5：文档与全面离线关闭

1. 更新根 README、Chat 模块 README、Adapter README 和测试索引；
2. 新增更新记录，写明代码事实、测试数量、接口文档修改范围和证据边界；
3. 运行 Chat、Gateway、Container、架构和合同资产测试；
4. 运行当前安全全仓套件、`compileall`、`git diff --check` 和静态边界检查；
5. 核对 `run.py` 零修改，未连接真实后台服务。

门禁：发现/排除/执行/失败/错误/跳过数量完整；预期故障注入日志不误报为失败；无未归属文件。

### 阶段 6：隔离 AnythingLLM 真实验收

该阶段必须使用非生产、可清理的虚拟业务 ID，不启动 `run.py`：

1. 写入前盘点目标名称和 Workspace 基线；
2. 直接通过任务级 Chat 组合创建 File/Weaponry Workspace；
3. 核对 Workspace `name` 精确值、Thread 隔离和最小问答链；
4. 删除所有临时资源并确认 Workspace 回到基线；
5. 若真实服务不可用、名称已碰撞或缺少写入授权，停止并向负责人确认，不能把离线 Fake 当作
   真实验收。

## 6. 文件范围

计划内生产代码：

- `app/modules/chat/domain/workspace_naming.py`；
- `app/modules/chat/domain/__init__.py`、`app/modules/chat/__init__.py`；
- `app/modules/chat/application/run_executor.py`；
- `app/modules/chat/ports/conversations.py`；
- `app/modules/chat/adapters/anythingllm_gateway.py`。

计划内测试：

- `tests/test_chat_workspace_naming.py`；
- `tests/fakes/chat.py`；
- `tests/test_chat_run_executor.py`；
- `tests/test_anythingllm_chat_gateway.py`；
- `tests/test_chat_delete_service.py`；
- `tests/test_weaponry_chat_routes.py`；
- 既有 Chat 合同、Container 与架构门禁。

明确不应修改：

- Web Blueprint 的请求字段解析和 Presenter；
- `app/container.py` 的对象所有权；
- Chat SQLite Schema；
- `run.py`；
- 除日志治理说明外的公开接口合同。

## 7. 回滚与停止条件

本改造没有数据库迁移。代码回滚后，已经持久化远端引用的会话仍按引用工作；新旧 Workspace
名称都不是本地聚合主键。禁止通过批量重命名或删除真实 Workspace 作为代码回滚手段。

出现以下情况立即停止：

- AnythingLLM 截断、改写或拒绝目标名称；
- 本地无远端引用但发现同名 Workspace；
- 必须新增公开参数、状态码或错误结构才能继续；
- 必须修改数据库 Schema 才能证明资源所有权；
- 测试发现旧会话、删除重建或日志 secret 防护发生回归；
- 工作区出现来源不明且与计划文件重叠的修改。

## 8. 完成定义

1. 新 File Workspace 精确命名为 `chat-id{chatId}`；
2. 新 Weaponry Workspace 精确命名为 `wChat-user{userId}-arch{architectureId}`；
3. 旧会话不重命名，按已存引用继续工作；
4. 同名未知资源失败关闭，不发生跨世代复用；
5. 应用日志具备业务身份和资源排障能力，同时不泄露凭据、消息、Prompt、Chunk 或文件正文；
6. 前后端接口参数和公开响应协议零变化；
7. 离线验收与真实供应商验收分开报告，不把单实例结果外推到可靠队列、多实例或生产容量。

## 9. 实际执行状态（2026-08-02）

- 阶段 0～4 均在各自定向门禁通过、且没有遗留商讨事项后才进入下一阶段。
- 阶段 5 已完成 README、测试索引和更新记录收口；182 项定向组合、281 项 Chat 动态发现及
  3 项阶段 1G 所有权门禁通过。
- 安全全仓最终发现 2,183 项，精确排除既有 13 项后执行 2,170 项；失败 0、错误 0、跳过 3、
  预期失败 0、意外成功 0。`compileall`、架构边界、遗留引用盘点和 `git diff --check` 通过。
- `run.py` 零修改、零执行；除已批准的日志条款外，公开接口合同零变化。
- 阶段 6 已在负责人确认授权后完成：写入前 Workspace 基线为 4、目标碰撞为 0；File 与
  Weaponry 精确名称均由供应商完整保存，各完成 23 个文本 Chunk 和 1 个来源的最小 Query；
  Workspace/Thread 引用各自唯一。Thread、Workspace、临时全局文档均已删除，目标残留为 0，
  Workspace 总数恢复为 4。
- 真实证据仅覆盖本机回环 AnythingLLM 的任务级 Gateway，不替代 Flask/浏览器、多实例、可靠
  队列、共享数据库或容量验收。
