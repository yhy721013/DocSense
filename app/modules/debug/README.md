# Debug 模块

本模块负责 `/debug/*` 内部调试路由背后的只读查询。依赖方向固定为：

`Blueprint -> Query -> Port <- Adapter`，响应字段由 `app/presenters/debug.py` 投影。

- `application/`：不可变查询结果和用例级错误收敛；
- `ports/`：不暴露 `Path`、SQLite Repository 或供应商客户端的读取契约；
- `adapters/`：Callback 文件目录与本地 Chat/Knowledge Store 的只读适配；
- `composition.py`：无 Flask、无网络、无后台线程的显式装配。

Debug 查询不得发起 AnythingLLM 请求，不得记录回调正文、文件名列表、消息正文或外部引用。
本模块不是公开甲方接口，也不会替代部署侧访问控制；`/debug/*` 必须继续由网络或反向代理限制。
