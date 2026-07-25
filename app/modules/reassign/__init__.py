"""分类节点变更业务模块。

阶段 1E-6 已具备供应商无关领域模型、端口、严格 Fake、SQLite 本地事实适配器、请求级
AnythingLLM Knowledge Port Adapter、``DocumentReassignmentService`` 前向路径，以及
``RecoverReassignmentOperation`` 的过期 lease 接管、探测、补偿和人工恢复收口。模块组合根将两个
Application 用例装配为唯一外观；``/llm/reassign`` 路由只调用该外观，仍由请求级 Factory 创建
具体 Adapter。公开请求与响应仍以接口文档为唯一权威。
"""

__all__: list[str] = []
