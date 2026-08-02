# Chat 适配器目录说明

本目录实现 Chat Ports，并隔离具体基础设施。`anythingllm_gateway.py` 与
`anythingllm_factory.py` 管理供应商协议和任务级网络生命周期；
`knowledge_documents.py` 读取 DocSense 知识记录；`sqlite/` 保存当前单实例权威状态与
运行协调。应用层只能依赖端口，不能反向导入这些实现。
