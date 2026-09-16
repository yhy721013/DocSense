# `main` 与 `refactor/file-analysis` Legacy Office 集成 M6 执行记录

## 1. 阶段结论

M6“容器、默认开关和生命周期”已完成，可以进入 M7。生产组合根只构造一个
`LibreOfficeLegacyOfficePreparer`，Report 与 Analysis 共享其进程级转换容量；部署样例默认开启，
环境变量缺失时的代码安全默认值仍为关闭。Preflight、版本门禁、所有权清扫和后台服务失败回滚
均已通过离线门禁。

## 2. 已完成实现与核验

1. `.env.example` 显式设置 `DOCSENSE_LEGACY_OFFICE_ENABLED=true`，并新增自动化门禁防止部署
   默认值意外回退；
2. `load_legacy_office_config()` 在环境变量缺失时继续返回 `enabled=false`，非法布尔值、相对可执行
   路径、空版本系列、非有限超时和非法容量均启动期失败；
3. 生产 `create_application_services()` 只构造一个 Preparer，并把同一实例注入 Report、Analysis
   和 `ApplicationServices`；新增 AST 架构门禁防止未来重复构造导致容量隔离失效；
4. Preparer 的 Preflight 和陈旧目录清扫发生在 AnythingLLM 配置、Dispatcher 构造及后台线程启动
   前；显式关闭时不会探测主机软件或启动子进程；
5. 陈旧目录清扫继续只删除直属 `job-*` 且带兼容所有权标记的目录，符号链接、无标记目录和根外
   路径均拒绝清理；
6. Preparer 的 `BoundedSemaphore` 覆盖转换、OOXML 校验和产物发布完整处理单元，默认全局并发为 1；
7. `ApplicationServices.start_background_services()` 已有“本轮实际启动组件逆序停止”语义，各本地
   Dispatcher 在自身启动异常路径释放进程锁；
8. 新增应用工厂最终兜底：生产容器的后台启动抛出异常或中断时统一调用 `close()`；关闭异常只记
   CRITICAL 日志，不覆盖原始启动错误，也不注册失效的 `atexit` 钩子。

## 3. 门禁证据

使用项目 `/venv` 执行 Legacy Office Config、Conversion、Dependency Container、Analysis
Composition 和 Report Runtime Adapter 共 68 项测试，全部适用测试通过；其中 1 项按设计仅在
macOS 实机运行的真实 OOXML 校验超时进程回收测试，在当前 Windows 环境预期 Skip。随后单独重跑
Dependency Container 24 项全部通过，并完成改动模块语法编译。

门禁覆盖代码缺省关闭、部署样例开启、显式关闭、非法配置、可执行文件缺失、开发版/版本系列
冲突、Fake 注入、共享 Preparer、全局并发 1、转换/校验异常归还许可、所有权标记清扫、Dispatcher
中途失败逆序停机、生产启动失败最终关闭及关闭异常不遮蔽原始错误。

`git diff --check` 通过，`docs/接口文档/` 没有修改。没有执行 `run.py`，没有探测或启动本机真实
LibreOffice，也没有连接真实 AnythingLLM、模型、Callback 或生产数据库。

## 4. 阶段后商讨项检查

M6 未改变公开接口、任务策略、支持平台或多实例能力声明。macOS 实机专属门禁仍按计划留待 M10
真实环境阶段，不构成当前离线阶段阻塞；没有新增需要确认的语义，可以进入 M7。
