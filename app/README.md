# 应用层目录说明

`app/` 是 DocSense 后端的应用装配根。文件对话改造后的代码以“HTTP 入口、应用用例、供应商端口、基础设施适配器、展示层”分层组织：上层只表达业务意图，下层负责 AnythingLLM、SQLite 和 HTTP/SSE 等具体技术细节。

## 本目录文件与子目录

| 路径 | 作用 |
| --- | --- |
| `__init__.py` | 标记应用包，不承载文件对话业务逻辑。 |
| `container.py` | 组合根。创建 SQLite 单实例模式下的持久化存储、运行协调器、应用服务、AnythingLLM 工厂和内联调度器，并在启动阶段校验能力边界。 |
| `blueprints/` | Flask 路由层，负责输入校验、状态码映射和响应创建。 |
| `adapters/` | 应用边界适配器；包含正式 Flask 入站 Parser，以及模块 Application 与公开协议之间的转换。 |
| `modules/` | 按业务聚合的模块化单体根；Chat、Tasks、Analysis、Report、Weaponry、Reassign、DocumentProcessing、Translation 和 Debug 均在此拥有明确边界。 |
| `ports/` | 跨模块共享且供应商无关的窄能力协议；Chat 专属 Port 位于 `modules/chat/ports/`。 |
| `integrations/` | 对外部系统的共享原子客户端与 RAG/知识网关；Chat 专属网关位于 `modules/chat/adapters/` 并复用原子客户端。 |
| `services/` | 尚未模块化的兼容事实服务与共享核心能力；不得重新承接 Chat 状态机或业务编排。 |
| `presenters/` | 将内部领域事件转换为对外协议文本，当前负责 SSE 格式化。 |
| `templates/` | 调试页面模板；不参与文件对话核心状态机。 |

## 文件对话主流程

```text
请求进入 blueprints/llm.py
  -> adapters/web 解析文件对话或知识谱系对话的独立公开请求
  -> container.py 暴露唯一 ChatApplicationServices 组合结果
  -> modules/chat/application 受理并仅以 run_id 调度状态机
  -> modules/chat/ports <- modules/chat/adapters
  -> integrations/anythingllm 的共享原子客户端访问 AnythingLLM
  -> modules/chat/adapters/sqlite 写入身份、范围、运行、消息、Chunk、租约和清理事实
  -> presenters 输出各自冻结的 SSE/JSON 合同
```

## 维护约束

- `/llm/chat*` 与 `/llm/weaponry-chat*` 的请求字段、响应字段、HTTP 状态码、响应头和 SSE 事件格式均属于冻结契约；本目录中的重构不得擅自新增或删除接口参数。
- `container.py` 是唯一的生产装配位置。不要在蓝图、应用服务或测试之外新增模块级单例、进程内全局任务表或共享网络会话。
- 当前 SQLite 适配器仅支持单应用实例。未来接入共享数据库、可靠队列或多实例协调时，应替换端口/适配器实现，而不是把分布式语义写进路由层。
- `tests/test_architecture_boundaries.py` 静态校验新模块依赖方向；禁止通过动态导入、重新导出遗留 Service 或移动代码规避规则。确需调整边界时，应先更新重构设计并评审，而不是削弱断言。
- 各模块的 SQLite/Fake 门禁只证明单实例离线语义；不得据此宣称共享数据库、可靠队列、跨实例通知、fencing 或生产容量已经实现。
