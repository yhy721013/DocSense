# `main` 与 `refactor/file-analysis` Legacy Office 集成 M9 执行记录

## 1. 阶段结论

M9 离线关闭验收已完成。全部验证使用项目 `venv`、`-B`、Fake、临时目录和临时 SQLite；
未执行 `run.py`，未连接真实 AnythingLLM、LibreOffice、模型、Callback 或生产数据库。

本阶段修正一项测试架构门禁遗漏：M6 新增的“生产容器启动失败后补偿关闭”用例会有意调用
无参 `create_app()`，现已把它作为第二个精确到文件名和方法名的生产容器所有权白名单项。
白名单没有扩展到任何其他测试，生产代码和公开接口均未变化。

## 2. 分组验收结果

| 顺序 | 验收范围 | 执行 | Failure | Error | Skip | 结果 |
| ---: | --- | ---: | ---: | ---: | ---: | --- |
| 1 | Legacy Office 内核、配置、离线交付 | 53 | 0 | 0 | 2 | 通过，Skip 为平台条件用例 |
| 2 | AnythingLLM Documents、RAG、Knowledge、XLSX Folder | 154 | 0 | 0 | 0 | 通过 |
| 3 | Report 全量发现 | 194 | 0 | 0 | 0 | 通过 |
| 4 | Analysis Port/Adapter/Application/Batch/Dispatcher/Recovery 全量发现 | 318 | 0 | 0 | 0 | 通过 |
| 5 | Weaponry 术语目录、文档范围和 Workspace | 75 | 0 | 0 | 0 | 通过 |
| 6 | Chat 全量发现 | 204 | 0 | 0 | 0 | 通过 |
| 7 | Dependency Container 与架构边界 | 47 | 0 | 0 | 0 | 修正精确白名单后通过 |
| 8 | 全部 `test_*contract*.py` 契约资产 | 169 | 0 | 0 | 0 | 通过 |

普通 Markdown、PDF、DOCX 不进入 XLSX Folder 查询或清理分支的回归包含在第 2 组中。
以上分组存在有意重叠，仅用于逐层定位，不能把执行数相加当作唯一测试总数。

## 3. 安全全仓门禁

安全全仓先动态发现 `tests` 下全部 `test*.py`，再逐项核对并排除既有 13 项：7 项可能启动
本地脚本、服务或请求本机 `run.py`，1 项依赖 Windows 无法稳定表达的 POSIX 权限位，5 项依赖
被 `.gitignore` 排除的个人联调夹具。没有增加新的排除项。

| Discovered | Excluded | Executed | Failure | Error | Skip | Expected failure | Unexpected success |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1,986 | 13 | 1,973 | 0 | 0 | 2 | 0 | 0 |

集成工作树不会复制根仓库 `venv/` 目录。第一次验收时，两个可安全执行的 Weaponry 目录 dry-run
包装器用例因此回退到 PATH 中缺少 Flask 的全局 Python；这不是代码失败。最终验收在测试进程内
把 `C:\.me\codes\DocSense\venv\Scripts` 前置到 PATH，使包装器子进程与主测试进程使用同一个
项目 venv。两个用例定向复验和完整安全全仓均通过，既定排除清单保持 13 项。

所有随机临时运行目录和日志文件均在验证后按绝对路径检查并删除，未操作开发或生产运行目录。

## 4. 静态与契约审计

- `git diff --check` 通过；
- 架构边界测试通过；
- M8 更新后的接口契约资产继续通过；
- 没有修改 `docs/接口文档/`，没有增删公开请求、响应、状态码、Header 或 Callback 字段；
- 没有把内部 task、workspace、Folder 或转换文件身份暴露到公开响应。

## 5. 阶段后商讨检查

M9 没有发现需要新增产品或接口决策的事项。离线代码门禁可以关闭，但这些结果不等于真实
LibreOffice 包、真实 AnythingLLM 或生产平台认证；M10 仍须在具备指定离线包、校验和、目标平台
与受控真实服务的环境中单独执行。
