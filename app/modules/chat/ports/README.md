# Chat 端口目录说明

本目录定义 Chat 应用层所需的供应商无关能力。`conversations.py` 描述对话供应商能力，
`coordination.py` 描述运行准入、执行租约与中断协调，`persistence.py` 描述本地权威状态、
事件账本及事务发件箱能力。端口不得包含 Flask、SQL、AnythingLLM HTTP 字段或 SSE 文本。
