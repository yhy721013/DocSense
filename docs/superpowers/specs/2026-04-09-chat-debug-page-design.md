# 文件对话调试页设计

## 1. 背景

DocSense 当前已经有本地调试页 `GET /debug/callback`，用于展示最近一次落盘到 `.runtime/call_back.json` 的回调结果。该页面已经覆盖：

1. `businessType=file`
2. `businessType=report`
3. `businessType=weaponry`

但文件对话模块 `chat` 的运行模型与上述三类业务不同：

1. `POST /llm/chat` 通过 SSE 返回流式事件。
2. `GET /llm/chat/history` 读取指定 `chatId` 的历史消息。
3. `POST /llm/chat/delete` 删除指定会话及其底层资源。
4. `chat` 不参与甲方回调，也不会写入 `.runtime/call_back.json`。

因此，本期不能把 `chat` 强行塞进现有 callback 调试页的数据模型中，而应补一个独立的 `/debug/chat` 本地联调页，专门覆盖 chat 三个接口的结果展示和联调操作。

## 2. 目标

本期需要完成以下目标：

1. 新增独立页面 `GET /debug/chat`，用于本地联调文件对话模块。
2. 页面覆盖 chat 模块的 3 个正式接口：
   - `POST /llm/chat`
   - `GET /llm/chat/history`
   - `POST /llm/chat/delete`
3. 页面首页采用混合入口：
   - 左侧展示本地已存在会话列表
   - 顶部保留新建/手工发起会话区域
4. 新建或续聊时，引用文件只能从本地“已解析文件列表”中选择，不允许自由输入不存在的 `fileName`。
5. 主展示区采用双视图：
   - 聊天记录视图
   - SSE 事件流视图
6. 页面必须支持以下完整联调动作：
   - 发起流式对话
   - 查看会话历史
   - 删除会话
7. 所有新增调试能力必须与甲方真实回调链路隔离，不能影响现有对外回调服务。

## 3. 非目标

以下内容明确不在本期范围内：

1. 不把 `chat` 结果写入 `.runtime/call_back.json`。
2. 不修改 `file`、`report`、`weaponry` 的回调发送、回调补发、回调落盘和调试页行为。
3. 不增加 chat 结果的“伪回调”或额外回调协议。
4. 不新增 React、Vue、Vite、Next.js 等独立前端工程。
5. 不新增浏览器级 E2E 自动化。
6. 不修改 `AnythingLLM` 交互协议、线程模型或真实 `/llm/chat*` 对外契约。
7. 不实现跨会话搜索、导出、批量删除、自动轮询等增强功能。

## 4. 约束与前提

1. 当前项目仍采用 Flask 模板 + 原生 JavaScript 的本地调试页结构。
2. chat 模块的正式联调入口已经存在于 `app/blueprints/llm.py` 中，不需要新增业务接口。
3. 本地会话信息已经持久化在 `.runtime/chat_sessions.sqlite3`。
4. 已解析文件信息已经持久化在 `.runtime/knowledge_base.sqlite3` 的 `documents` 表中。
5. `chat` 的历史消息真实来源仍是 `AnythingLLM` Thread，不在 DocSense 侧冗余存储消息正文。
6. 本期设计必须把“新增调试能力”和“甲方真实回调链路”完全隔离。
7. 隔离要求是硬约束，不是建议：
   - 不修改 `callback_client.py` 的回调发送行为
   - 不修改 `.runtime/call_back.json` 的写盘行为
   - 不修改 `/debug/callback` 和 `/debug/api/callback` 的既有语义
   - 不让 `/debug/chat` 参与任何甲方回调动作

## 5. 方案选择

### 5.1 备选方案

#### 方案 A：独立 `/debug/chat`，页面直接调用正式 `/llm/chat*`

新增独立页面和少量 debug 辅助接口，页面初始化时从本地数据库读取会话列表与已解析文件列表；发消息、查历史、删会话时直接调用现有正式接口。

#### 方案 B：新增 `/debug/api/chat/*` 全代理层

页面只调用 `/debug/api/chat/*`，由 debug 层内部再转发到 `/llm/chat*`。

#### 方案 C：把 chat 结果伪装成 callback 数据

新增本地落盘格式，让 `chat` 结果也走 callback 风格调试展示。

### 5.2 结论

本期采用方案 A。

原因如下：

1. `chat` 的真实协议已经稳定存在，调试页不应该复制一份代理协议。
2. 页面直接调正式接口，最能反映真实联调行为，避免 debug 层与正式接口漂移。
3. 只为页面初始化补本地只读辅助接口，后端改动边界最小。
4. 该方案最容易满足“不能影响甲方真实回调服务”的硬约束。

## 6. 架构与改动边界

### 6.1 保持不变的部分

以下部分在本期保持不变：

1. `/llm/chat`、`/llm/chat/history`、`/llm/chat/delete` 的对外协议。
2. `app/services/llm_service/chat_service.py` 的业务主流程。
3. `app/services/utils/callback_client.py` 的回调发送与回调落盘逻辑。
4. `app/services/utils/callback_preview.py` 的 callback 读取逻辑。
5. `/debug/callback` 与 `/debug/api/callback` 的页面和数据语义。
6. `.runtime/call_back.json` 的写入时机、路径和格式。

### 6.2 新增与调整的部分

本期只补充 chat 调试所需的局部能力：

1. 在 `app/blueprints/debug.py` 中新增：
   - `GET /debug/chat`
   - `GET /debug/api/chat/bootstrap`
2. 在 `app/templates/debug/` 下新增 `chat.html`。
3. 在 `app/services/utils/` 下新增只读聚合层 `chat_debug_preview.py`。
4. 在 `app/services/core/database.py` 中补充只读查询方法：
   - `ChatDatabaseService.list_chats()`
   - `DatabaseService.list_document_records()`
5. 在测试中新增 `/debug/chat` 页面与 bootstrap 数据接口覆盖。

### 6.3 分层职责

新增职责边界如下：

1. `debug blueprint`
   - 负责返回调试页模板
   - 负责返回本地 bootstrap 只读数据
2. `chat_debug_preview`
   - 聚合本地数据库中的会话与文件列表
   - 统一封装返回结构与错误处理
3. `database services`
   - 只增加只读列表查询，不引入新的业务状态变更
4. `chat.html`
   - 负责页面交互、SSE 解析、聊天展示和本地列表刷新
5. 真实 chat 接口
   - 继续承担对话创建、流式回复、历史查询、资源删除

## 7. 数据接口设计

### 7.1 `GET /debug/api/chat/bootstrap`

该接口只返回调试页初始化所需的本地数据，不代理真实 chat 行为。

返回结构建议如下：

```json
{
  "ok": true,
  "message": "读取成功",
  "data": {
    "sessions": [
      {
        "chatId": "conv-001",
        "fileNames": ["a.pdf", "b.docx"],
        "createdAt": "2026-04-09T10:00:00+00:00",
        "updatedAt": "2026-04-09T10:15:00+00:00"
      }
    ],
    "availableFiles": [
      {
        "fileName": "a.pdf",
        "architectureId": 12
      }
    ]
  }
}
```

字段说明：

1. `sessions`
   - 来自本地 `chat_sessions.sqlite3`
   - 用于渲染左侧会话列表
2. `availableFiles`
   - 来自本地 `knowledge_base.sqlite3` 的 `documents`
   - 用于新建/续聊时的文件选择器

错误场景约定：

1. 读库成功但为空，返回 `ok=true` 与空数组。
2. 读库失败，返回 `ok=false`，并将 `sessions`、`availableFiles` 置为空数组。
3. 接口不暴露真实 chat 消息正文，也不访问 AnythingLLM。

### 7.2 页面调用的正式接口

`/debug/chat` 页面中的联调动作直接调用以下正式接口：

1. 发消息：`POST /llm/chat`
2. 查历史：`GET /llm/chat/history`
3. 删会话：`POST /llm/chat/delete`

设计原则：

1. 不新增 `/debug/api/chat/send`
2. 不新增 `/debug/api/chat/history`
3. 不新增 `/debug/api/chat/delete`
4. 避免 debug 层重新定义 chat 协议

## 8. 页面信息架构

`/debug/chat` 采用单页面板结构，风格延续现有 `callback.html` 的本地调试视觉语言。

### 8.1 左侧：本地会话列表

用于展示和切换本地会话。

每条会话展示：

1. `chatId`
2. 当前引用文件数
3. `updatedAt`

交互规则：

1. 点击某条会话时，将其 `chatId` 与 `fileNames` 回填到主区域表单。
2. 点击会话后自动触发一次历史查询。
3. 切换会话时清空上一轮 SSE 事件流展示，避免误读。

### 8.2 顶部：新建/续聊表单

表单包含：

1. `chatId` 输入框
2. 已解析文件多选区
3. `message` 输入框
4. `发送消息` 按钮
5. `加载历史` 按钮
6. `删除当前会话` 按钮

规则如下：

1. `chatId` 可手工输入，用于新建会话或续接已有会话。
2. 文件选择只能从 `availableFiles` 中勾选。
3. 当本地无已解析文件时，文件选择区显示空状态并禁用发送动作。
4. 页面不允许输入任意不存在的 `fileName`。

### 8.3 主区域：双视图结果区

主区域分为两部分：

1. 聊天记录视图
2. SSE 事件流视图

#### 8.3.1 聊天记录视图

展示：

1. 历史 `user` 消息
2. 历史 `assistant` 消息
3. 当前流式生成中的 assistant 回复

展示原则：

1. 优先保证聊天内容可读性。
2. 角色区分清晰。
3. 当前流式回复与历史消息在视觉上可区分。

#### 8.3.2 SSE 事件流视图

展示：

1. `chatInfo`
2. `textChunk`
3. `done`
4. `error`

展示原则：

1. 保留事件顺序，便于定位流式联调问题。
2. 同步展示解析后的事件块和原始事件文本。
3. 出现 `error` 事件时，在该区域明显提示。

## 9. 交互流设计

### 9.1 页面初始化

1. 页面加载时请求 `GET /debug/api/chat/bootstrap`。
2. 渲染本地会话列表与已解析文件列表。
3. 首屏默认不自动加载任意会话历史。

### 9.2 选择已有会话

1. 用户点击左侧会话。
2. 页面把该会话的 `chatId` 和 `fileNames` 填入表单。
3. 页面自动请求 `GET /llm/chat/history?chatId=...`。
4. 聊天记录区展示完整历史。
5. SSE 事件区清空上一轮事件。

### 9.3 发起新对话或继续对话

1. 用户填写 `chatId`、勾选文件、输入消息。
2. 页面直接调用 `POST /llm/chat`。
3. 页面按事件流实时处理：
   - `chatInfo`：记录本轮会话元信息
   - `textChunk`：持续追加到当前 assistant 回复
   - `done`：标记本轮请求完成
   - `error`：标记失败并停止流式状态
4. 本轮流结束后，页面重新请求一次 `GET /debug/api/chat/bootstrap`，刷新左侧本地会话列表。

### 9.4 单独查看历史

1. 用户仅输入或选择 `chatId`。
2. 点击 `加载历史`。
3. 页面调用 `GET /llm/chat/history`。
4. 成功后仅刷新聊天记录区，不触发 SSE。

### 9.5 删除会话

1. 用户点击 `删除当前会话`。
2. 页面调用 `POST /llm/chat/delete`。
3. 删除成功后：
   - 清空当前聊天记录
   - 清空当前 SSE 事件区
   - 重新请求 `GET /debug/api/chat/bootstrap`
4. 若删除的是当前选中会话，页面回到空状态。

## 10. 状态管理设计

前端状态拆分为四组，避免互相污染：

1. `bootstrapState`
   - 本地会话列表
   - 已解析文件列表
   - bootstrap 加载状态与错误
2. `activeChatState`
   - 当前 `chatId`
   - 当前选中文件
   - 当前是否为已有会话
3. `historyState`
   - 当前会话历史消息
   - 历史加载状态
4. `streamState`
   - 当前请求的 SSE 事件数组
   - 当前拼接中的 assistant 文本
   - 流式请求中状态
   - 流式错误信息

状态原则：

1. 切换会话不覆盖 bootstrap 数据。
2. 刷新 bootstrap 不重置当前正在编辑的表单内容。
3. 一次只允许一个活动中的 SSE 请求，避免事件流串线。

## 11. 错误处理与边界场景

### 11.1 Bootstrap 错误

若 `GET /debug/api/chat/bootstrap` 失败：

1. 页面顶部展示错误 banner。
2. 左侧会话列表与文件列表显示为空状态。
3. 不影响 `/debug/callback` 及任何真实回调服务。

### 11.2 正式接口错误

#### `POST /llm/chat`

1. 建连前 `400/404` 使用 JSON 错误提示展示在表单区。
2. 建连后 `error` 事件展示在 SSE 区域。
3. 发生流式错误时不写任何本地 callback 文件。

#### `GET /llm/chat/history`

1. `404` 显示“对话不存在”。
2. 用户可随后刷新 bootstrap，确认本地列表是否仍保留该会话。

#### `POST /llm/chat/delete`

1. `404` 视为本地与远端状态不一致。
2. 页面提示后允许用户刷新 bootstrap。

### 11.3 边界场景

需要明确处理以下场景：

1. 本地没有任何会话。
2. 本地没有任何已解析文件。
3. 本地会话存在，但远端历史不存在。
4. 用户尝试在 SSE 未结束时切换会话。
5. 用户快速重复点击发送按钮。

处理策略：

1. 无会话或无文件时展示空状态，不报错。
2. SSE 进行中禁止再次发送。
3. SSE 进行中切换会话时，前端直接阻止切换，并提示“当前流式响应尚未结束”。
4. 不自动中断已经发出的正式 chat 请求。

## 12. 数据库只读查询设计

### 12.1 `ChatDatabaseService.list_chats()`

建议返回：

1. `chat_id`
2. `file_names`
3. `workspace_slug`
4. `thread_slug`
5. `created_at`
6. `updated_at`

页面聚合层最终可根据需要裁剪字段，但数据库层建议一次性提供完整只读信息，便于调试扩展。

排序规则：

1. 默认按 `updated_at DESC` 返回，便于本地联调时优先看到最近活动的会话。

### 12.2 `DatabaseService.list_document_records()`

建议返回：

1. `file_name`
2. `architecture_id`
3. `anything_doc_id`
4. `doc_path`

排序规则：

1. 默认按 `file_name ASC` 返回，便于文件选择器展示。

注意：

1. 这些方法只读，不新增写操作。
2. 不改动既有 `save/get/update/delete` 语义。

## 13. 测试与验证策略

### 13.1 自动化测试

新增或扩展以下测试：

1. `tests/test_callback_debug_routes.py`
   - 保持现有 callback 调试页测试继续通过
   - 确认 chat 设计没有影响既有 callback 行为
2. 新增 chat debug 路由测试
   - `GET /debug/chat` 页面可访问
   - `GET /debug/api/chat/bootstrap` 在正常、空数据、异常场景下结构正确
3. 扩展数据库层测试
   - `ChatDatabaseService.list_chats()` 的排序与字段结构
   - `DatabaseService.list_document_records()` 的空库与正常返回
4. 如已有 `tests/test_chat.py`
   - 保证正式 `/llm/chat*` 测试继续通过，不因 debug 页面新增逻辑而变化

### 13.2 手工验证

本机 macOS 环境下的验证步骤建议为：

1. 先完成至少一个文件解析，确保 `documents` 表中有可选文件。
2. 打开 `/debug/chat`。
3. 选择文件并发起一轮新对话。
4. 确认聊天记录区能实时看到 assistant 回复。
5. 确认 SSE 区能看到 `chatInfo -> textChunk -> done`。
6. 重新加载该会话历史，确认与流式结果一致。
7. 删除该会话，确认本地列表刷新。
8. 回归打开 `/debug/callback`，确认 callback 页面行为不变。

### 13.3 核心验收标准

以下条件全部满足，视为本期设计达成：

1. `/debug/chat` 能独立完成 chat 三个接口的本地联调。
2. 页面能展示本地会话列表与已解析文件列表。
3. 页面能同时展示聊天记录与 SSE 事件流。
4. 新增能力不修改 callback 调试页语义。
5. 新增能力不影响甲方真实回调链路。
