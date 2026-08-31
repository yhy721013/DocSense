# 对话 Workspace 业务身份命名执行记录

## 1. 执行结论

- 执行日期：2026-08-02。
- 目标分支：`feat/weaponry-chat`。
- 阶段状态：阶段 0～6 已全部完成；离线门禁与经负责人授权的本机回环 AnythingLLM 隔离
  写入、问答和清理验收均已通过。
- 未启动 `run.py`；除阶段 6 明确授权的本机 AnythingLLM/模型链外，未连接回调或其他真实
  后台服务。
- 未增加、删除、改名或放宽任何 HTTP/SSE/Header/请求/响应字段，也未修改状态码。
- 未修改 Chat SQLite Schema；命名不是本地聚合主键，历史远端引用不迁移。

## 2. 已落地行为

### 2.1 精确命名

| 对话类型 | 新建 Workspace `name` | 主 Thread 名称 |
| --- | --- | --- |
| 文件对话 | `chat-id{chatId}` | `thread-{conversation_id}` |
| 知识谱系对话 | `wChat-user{userId}-arch{architectureId}` | `thread-{conversation_id}` |

名称只根据数据库恢复出的不可变 `ConversationIdentityBinding` 生成。Blueprint、HTTP 请求和
后台任务调用方都不能传入任意供应商名称；按 `run_id` 重启恢复仍得到同一个业务名称。

### 2.2 资源归属与失败关闭

- 已经持久化 `workspace_ref + thread_ref` 的历史会话直接复用引用，不查找或重命名旧资源。
- 本地没有远端引用时，同名 Workspace 为零个才允许创建；一个或多个都按归属不明冲突失败，
  不认领、不创建 Thread、不删除未知资源。
- AnythingLLM 返回的 Workspace 名称必须与请求完全一致；名称漂移会补偿删除本次新建资源。
- Thread 创建失败继续补偿本次新建 Workspace；补偿失败时异常携带精确资源引用，供计划租约
  与既有持久化清理流程接管。
- 供应商异常文本不拼入应用错误，避免原始响应正文、URL 或其他敏感信息进入后续日志/持久化。

### 2.3 删除、重建与并发

- File 删除后的永久墓碑语义不变，相同 `chatId` 不能创建第二代会话。
- Weaponry 只有在远端清理和本地删除全部成功后才释放复合身份；相同业务身份随后可创建新
  Conversation，Workspace 展示名称相同，内部 Thread 不同，并重新冻结当时的类别文件快照。
- 清理失败时旧身份仍绑定错误态 Conversation，不能创建第二个同名 Workspace。
- 50 个不同 File 身份在完整公开路由链中生成 50 个精确且互不重复的目标名称，远端资源、
  运行、消息和文档范围不串扰。

## 3. 日志与接口文档

负责人确认应用日志可以记录 `userId`，因此同步修改
`docs/接口文档/知识谱系对话.md` 中唯一一段日志说明：

- 可以记录规范化 `chatId`、`userId`、`architectureId`、精确 Workspace/Thread 名称、内部
  `conversation_id`/`run_id`/租约 ID、状态/计数/补偿结果及排障必需的资源引用；
- 仍禁止记录 API Key、Authorization、Cookie、Callback 凭据、消息或模型正文、Chunk、文件
  正文、Prompt、原始请求响应、SSE 帧、未脱敏异常响应和含敏感 Query 的完整 URL；
- 供应商返回名称漂移时只记录返回名称字符数，不记录未知原值。

除上述已授权日志条款外，接口文档没有其他差异。部署层访问日志、代理日志和浏览器链路不在
本次应用单元测试证明范围内。

## 4. 主要代码范围

- `app/modules/chat/domain/workspace_naming.py`：供应商无关的纯命名规则；
- `app/modules/chat/application/run_executor.py`：从持久化身份生成内部 `workspace_name`；
- `app/modules/chat/ports/conversations.py`：同名未知资源冲突类型和失败关闭契约；
- `app/modules/chat/adapters/anythingllm_gateway.py`：精确创建、冲突拒绝、名称校验、补偿和日志；
- `tests/fakes/chat.py` 及 Chat/Gateway/Delete/Weaponry 路由测试：记录并断言名称、兼容、重建与并发。

Web Blueprint、Presenter、`app/container.py`、Chat 数据库 Schema 和 `run.py` 均未修改。

## 5. 离线验收证据

所有 Python 测试均使用项目 `venv`、临时 SQLite、Fake/模拟传输，不连接真实 AnythingLLM。

| 门禁 | 结果 |
| --- | --- |
| 命名、执行器、Gateway、删除、公开路由、合同、Container、架构定向组合 | 182 项通过 |
| Chat 动态发现 `test_chat*.py` | 281 项通过 |
| 阶段 1G 所有权长期门禁 | 3 项通过 |
| 阶段 1G 遗留引用盘点 | 11/11 候选无阻塞引用，未知动态导入 0 |
| 安全全仓动态发现 | 发现 2,183、排除 13、执行 2,170 |
| 安全全仓结果 | 失败 0、错误 0、跳过 3、预期失败 0、意外成功 0 |
| 静态检查 | `compileall`、架构边界、`git diff --check` 通过 |
| 主入口检查 | `run.py` 零修改、零执行 |

全仓只精确排除既有 13 项：

1. `tests.test_local_scripts.LocalScriptTests.test_analysis_shell_script_posts_fixture_to_expected_path`
2. `tests.test_local_scripts.LocalScriptTests.test_check_task_shell_script_posts_fixture_to_expected_path`
3. `tests.test_local_scripts.LocalScriptTests.test_progress_shell_script_reads_progress_snapshot_from_local_app`
4. `tests.test_local_scripts.LocalScriptTests.test_report_shell_script_posts_fixture_to_expected_path`
5. `tests.test_local_scripts.LocalScriptTests.test_start_test_file_server_serves_fixture_file`
6. `tests.test_local_scripts.LocalScriptTests.test_weaponry_directory_script_dry_run_writes_manifest`
7. `tests.test_local_scripts.LocalScriptTests.test_weaponry_shell_script_posts_fixture_to_expected_path`
8. `tests.test_migrate_analysis_security.AnalysisSecurityMigrationTests.test_apply_is_idempotent_and_preserves_callback_metadata_and_audit`
9. `tests.test_test_assets.LLMTestAssetsTests.test_analysis_request_fixture_has_required_fields`
10. `tests.test_test_assets.LLMTestAssetsTests.test_check_task_fixture_can_query_multiple_files`
11. `tests.test_test_assets.LLMTestAssetsTests.test_check_task_weaponry_fixture_matches_request_architecture_id`
12. `tests.test_test_assets.LLMTestAssetsTests.test_report_request_fixture_has_required_fields`
13. `tests.test_test_assets.LLMTestAssetsTests.test_weaponry_request_fixture_uses_ship_fields`

前 7 项可能启动本地服务或 Shell，后 5 项依赖被 `.gitignore` 排除的本地样例，第 8 项包含
Windows 不支持的 POSIX 权限位断言。3 个跳过项是既有 macOS 真实进程组/Windows 符号链接
权限边界。测试日志中的 ERROR/CRITICAL 均来自预期故障注入，不是 unittest failure/error。

## 6. 本机 AnythingLLM 真实验收

负责人确认当前回环实例属于非生产、可清理环境并授权维护窗口后，直接使用生产任务级
`AnythingLLMChatFactory` 和 Chat Gateway 执行阶段 6，没有启动 Flask 或 `run.py`。

### 6.1 基线与隔离身份

- `.env` 目标经 URL 解析确认为回环地址，密钥只由配置加载器读取，未打印或写入记录；
- `/api/v1/health` 在当前实例返回 HTTP 404，因此不把该非合同端点用作版本证明，改用项目现行
  `GET /workspaces` 原子客户端完成连通性和基线盘点；
- 写入前 Workspace 基线为 4，两个目标名称碰撞数为 0；
- File 使用虚拟 `chatId=1785682786446`，目标名为 `chat-id1785682786446`；
- Weaponry 使用虚拟 `userId=1785682786447`、`architectureId=1785682786448`，目标名为
  `wChat-user1785682786447-arch1785682786448`。

### 6.2 创建、名称与问答结果

- 上传一份无敏感内容、带唯一验证口令和结构化 `docSource` 的临时 Markdown；
- 两个 Workspace 均由任务级 Chat Gateway 创建，随后通过供应商详情接口核对 `name` 与目标值
  逐字符一致；
- 两次运行分别使用不同 Workspace 引用和不同 Thread 引用，引用唯一数均为 2；
- 两个 Workspace 均成功绑定本次上传文档，并以生产默认 Query 流完成最小问答；每次得到
  23 个非空文本 Chunk、唯一来源终态、来源数 1，回答包含文档中的唯一验证口令；
- 未出现 Query Rejection、名称截断、名称改写、同名冲突或终态协议错误。

### 6.3 清理与复核

- 每轮按 Thread → Workspace 顺序删除，只操作本次创建回执证明归属的精确引用；
- 两轮完成后永久删除本次上传的全局文档，删除接口明确成功；
- 清理后目标名称残留数为 0，Workspace 总数从 4 恢复为 4，本地临时 Markdown 也已删除；
- 首次执行实际已完成两轮验证及清理，但最终汇总表达式因 PowerShell 传递内联 Python 时剥离
  双引号而触发 `NameError`。该轮未计为通过；只读复核确认基线 4、目标残留 0 后，仅修正汇总
  格式并完整重跑，第二轮退出码为 0。该问题不涉及项目生产代码或远端业务语义。

## 7. 证据边界

当前证据分别证明 Windows 临时 SQLite/Fake 下的单实例离线语义，以及本机回环 AnythingLLM
任务级 Gateway 的真实名称、文档绑定、Query 流和资源清理。它仍不证明：

- 合同允许的每一种超长合法 File `chatId` 都能被当前或未来 AnythingLLM 版本完整保存；
- Flask 公开路由、仓库外真实浏览器/UI、前端状态管理、生产访问日志治理或代理脱敏；
- 多实例分布式唯一性、可靠队列、共享数据库一致性、容量或 exactly-once 行为。

未来更换 AnythingLLM 版本或部署形态后仍须重新执行隔离验收。若发现同名资源、名称被截断/
改写或无法完整清理，必须停止，不得通过哈希、截断、自动认领或扩大公开参数范围绕过。
