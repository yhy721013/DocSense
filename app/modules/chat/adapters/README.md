# Chat 适配器目录说明

本目录实现 Chat Ports，并隔离具体基础设施。`anythingllm_gateway.py` 与
`anythingllm_factory.py` 管理供应商协议和任务级网络生命周期；
`knowledge_documents.py` 读取 DocSense 知识记录；`sqlite/` 保存当前单实例权威状态与
运行协调。应用层只能依赖端口，不能反向导入这些实现。

`AnythingLLMChatGateway.open_conversation()` 只负责执行应用层传入的精确名称：查找结果为
零个时创建，为一个或多个时按未知归属冲突失败关闭，不按名称复用或删除已有 Workspace。
供应商返回名称漂移或 Thread 创建失败时，仅补偿本次能够证明为己方新建的 Workspace；补偿
失败会保留精确资源引用，交给持久化租约与清理任务继续收敛。

适配器日志可以包含精确 Workspace/Thread 名称及引用，以便定位供应商资源问题，但不得包含
API Key、Authorization/Cookie、消息或模型正文、Chunk、Prompt、文件正文、原始请求响应和
SSE 帧。
