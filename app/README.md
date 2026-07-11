# 应用层目录说明

`app/` 是 DocSense 后端的应用装配根。文件对话改造后的代码以“HTTP 入口、应用用例、供应商端口、基础设施适配器、展示层”分层组织：上层只表达业务意图，下层负责 AnythingLLM、SQLite 和 HTTP/SSE 等具体技术细节。

## 本目录文件与子目录

| 路径 | 作用 |
| --- | --- |
| `__init__.py` | 标记应用包，不承载文件对话业务逻辑。 |
| `container.py` | 组合根。创建 SQLite 单实例模式下的持久化存储、运行协调器、应用服务、AnythingLLM 工厂和内联调度器，并在启动阶段校验能力边界。 |
| `blueprints/` | Flask 路由层，负责输入校验、状态码映射和响应创建。 |
| `ports/` | 文件对话面向业务层的供应商无关能力协议与数据传输对象。 |
| `integrations/` | 对外部系统的适配层；当前文件对话的具体供应商适配位于 `anythingllm/`。 |
| `services/` | 应用服务、领域模型、持久化、锁与既有业务服务。文件对话实现唯一位于 `services/chat/`。 |
| `presenters/` | 将内部领域事件转换为对外协议文本，当前负责 SSE 格式化。 |
| `templates/` | 调试页面模板；不参与文件对话核心状态机。 |

## 文件对话主流程

```text
请求进入 blueprints/llm.py
  -> container.py 提供的 ChatRunExecutor 受理运行并持久化输入快照
  -> ChatRunDispatcher 仅以 run_id 调度执行
  -> services/chat/application/ 执行用例和状态机
  -> ports/chat.py 表达供应商无关操作
  -> integrations/anythingllm/ 调用 AnythingLLM
  -> persistence/ 写入事件、消息、租约和清理任务
  -> presenters/chat_stream.py 输出既有 SSE 格式
```

## 维护约束

- `/llm/chat*` 的请求字段、响应字段、HTTP 状态码、响应头和 SSE 事件格式属于冻结契约；本目录中的重构不得通过新增或删除接口参数实现。
- `container.py` 是唯一的生产装配位置。不要在蓝图、应用服务或测试之外新增模块级单例、进程内全局任务表或共享网络会话。
- 当前 SQLite 适配器仅支持单应用实例。未来接入共享数据库、可靠队列或多实例协调时，应替换端口/适配器实现，而不是把分布式语义写进路由层。
