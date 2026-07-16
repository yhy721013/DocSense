# Web 适配器目录说明

本目录按 Web 框架隔离请求解析和连接生命周期。Flask 与未来 FastAPI Adapter 必须调用同一应用用例和 Presenter，不能复制业务编排。

公开路径、参数、状态码、SSE/WebSocket 字段和关闭行为由 `docs/接口文档/` 决定；框架迁移不得借此增删接口参数。
