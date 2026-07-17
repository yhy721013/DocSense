# 展示层目录说明

本目录负责把领域事件或应用结果转换为对外展示协议。文件对话中，它将供应商无关的 `ChatStreamEvent` 转换为既有 SSE 文本；任务状态中，它把可靠恢复命令结果映射为已批准的空成功或既有错误语义。状态机、消息持久化和远端调用均不应放在这里。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `__init__.py` | 导出展示层函数。 |
| `chat_stream.py` | 格式化单个 SSE 事件，并包装流迭代器的关闭逻辑，确保资源释放回调在正常结束、异常和客户端断开后均可执行。 |
| `task_status.py` | 将 `RequestCallbackRecoveryResult` 映射为 HTTP 状态、零字节成功体或既有 JSON 错误体；不创建 Flask/FastAPI Response。 |
| `task_progress.py` | 将类型化当前项/快照映射为既有 Progress WebSocket 数据消息或 `error` 消息，并负责严格 JSON 序列化；不持有连接。 |
| `report_submission.py` | 将报告提交结果映射为严格 HTTP 202 零字节体、既有 400/409 单字段 JSON；隐藏 TaskId 和内部通知结果，不创建 Flask/FastAPI Response。 |

## 工作流程

1. 应用层产出 `ChatStreamEvent`。
2. 路由将事件流传给 `present_chat_stream()`。
3. 展示层按已冻结协议生成 `event:` 与 `data:` 行，不暴露内部 `run_id`、租约或数据库字段。
4. 流关闭时调用传入的清理回调；运行状态收敛由应用层和路由协作完成。

check-task Presenter 当前只作为阶段 1B-1 内部契约存在：单项/批量成功返回 200 与严格
零字节体，单项缺失返回既有 404 `error` JSON，已由 Web Adapter 判定的参数错误返回
既有 400 `error` JSON。生产路由未切换，内部 TaskId、恢复请求 ID 和 outcome 一律丢弃。

Progress Presenter 已在阶段 1B-2 接入当前 WebSocket 路由，只输出既有
`businessType/data`、缺失项 `exists=false` 或 `type/message` 错误结构。内部 TaskId、
sequence、订阅令牌和连接 ID 均不会进入公开消息。

Report Submission Presenter 已在阶段 1C-6 接入当前 Flask 报告路由：成功严格输出 202
零字节体且移除 Content-Type，活动任务、callback sending/outcome unknown 均输出既有
409 `{"error":"任务正在处理中"}`。没有新增、删除或泄漏任何接口参数。

## 维护规则

- 不得在本目录新增业务事件类型、改变事件名称、修改 `data` 结构或加入 SSE `id:` 行。
- 任何协议变更都属于接口文档变更，必须先确认；本目录只实现已确认的契约。
- Presenter 只能依赖正向白名单中的标准库、领域类型或框架无关应用结果，不得导入 Flask/FastAPI、具体数据库、`app.modules.*.adapters`、AnythingLLM Client，也不得通过动态导入绕过门禁；该约束由 `tests/test_architecture_boundaries.py` 持续校验。
