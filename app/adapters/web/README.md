# Web 适配器目录说明

本目录按 Web 框架隔离请求解析和连接生命周期。Flask 与未来 FastAPI Adapter 必须调用同一应用用例和 Presenter，不能复制业务编排。

`report_ids.py` 保存与具体框架无关的 `reportId` 入站规范化规则：接受 JSON 整数或
十进制整数字符串，不施加 32/64 位业务范围限制，并按整数值生成唯一内部业务键。
Flask、未来 FastAPI、HTTP 与 WebSocket 入口都必须复用该规则，不能各自直接
`str()`/`int()` 后形成不一致键。

公开路径、参数、状态码、SSE/WebSocket 字段和关闭行为由 `docs/接口文档/` 决定；框架迁移不得借此增删接口参数。
