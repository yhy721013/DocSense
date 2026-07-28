# `main` 与 `refactor/file-analysis` Legacy Office 集成 M10 执行记录

## 1. 阶段结论

M10 的分支集成与发布准备门禁已完成，可以进入最终回归和 `refactor/file-analysis` 快进回归。
负责人于 2026-07-28 明确确认：Windows 离线包已经验证，macOS Apple Silicon Preflight 已在
其他 Mac 主机完成；负责人同时确认无需向本仓库提供安装包绝对路径和 macOS Preflight 原始日志。

上述两项属于负责人提供的外部实机验收事实，本执行记录不伪造未获取的包路径、哈希明细或原始
日志。本轮本机验证、分支集成关闭和生产发布是三个不同边界：本轮没有执行 `run.py`，没有连接
真实 AnythingLLM、模型或 Callback，没有操作生产数据库，也没有宣称已经完成实际生产部署。

## 2. 本机 Windows 启动门禁

当前 Windows AMD64 主机的标准安装信息和 DocSense 启动门禁结果如下：

| 检查项 | 结果 |
| --- | --- |
| LibreOffice 标准路径 | `C:\Program Files\LibreOffice\program` |
| 文件产品版本 | `26.2.5.2` |
| 功能开关 | `true` |
| 允许版本系列 | `26.2` |
| 显式可执行路径 | 留空，使用生产默认发现 |
| 实际选择入口 | `C:\Program Files\LibreOffice\program\soffice.com` |
| `LibreOfficeLegacyOfficePreparer.preflight()` | 返回 `26.2.5.2`，退出码 0 |
| Preflight 后残留 `soffice*` 进程 | 0 |
| 随机临时运行目录 | 已校验并删除 |

诊断期间曾显式把 Windows GUI 入口 `soffice.exe` 当作配置值，版本探测按超时规则失败并产生一个
残留子进程；该进程已按 PID 和标准安装路径双重确认后清理。随后按真实标准部署配置留空显式路径，
默认发现正确选择 `soffice.com` 并通过门禁。项目代码原本已经把 `.com` 放在 Windows 标准路径
候选首位；本阶段只补充部署文档，明确非标准 Windows 安装也必须配置控制台入口 `.com`。

## 3. Git 与发布准备检查

1. 已从 `origin` 获取最新远端引用；`origin/main` 仍为 `fb758cda...`，没有新漂移；
2. `origin/refactor/file-analysis` 仍为 `9776f711...`，本地 `refactor/file-analysis` 仅包含 M0
   基线提交，且是 `merge/file-analysis` 的严格祖先；
3. 原工作树与集成工作树均无未提交修改；
4. `merge/file-analysis` 可以通过 `git merge --ff-only` 回归到 `refactor/file-analysis`，
   不需要额外 merge commit，不改写双父历史；
5. 实际生产发布仍必须由目标环境确认数据库、运行目录、停服排空窗口和回滚负责人，并执行只读
   cutover 检查；本次分支回归不代替生产发布编排。

## 4. 契约与架构边界

- 不增加、删除、重命名任何 HTTP、Callback、Progress、SSE 或 WebSocket 参数；
- 不修改接口文档；
- `/llm/analysis` 继续只经过 Stage 1F Parser、批量原子受理、Dispatcher、
  `RunAnalysisTask(TaskId)`、资源审计和 Callback 恢复链；
- XLS/XLSX 继续只允许一个可解析 Sheet；
- Legacy Office 转换失败时继续 fail-closed，不把旧格式原文件直接上传；
- 当前证据仍不能推出多实例、可靠外部队列或生产数据库一致性已经完成。

## 5. 完成条件

最终定向回归和既定安全全仓门禁已经通过：

| 验收范围 | Discovered | Excluded | Executed | Failure | Error | Skip |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Legacy Office、容器、架构与契约定向门禁 | - | - | 128 | 0 | 0 | 2 |
| 安全全仓动态发现 | 1,986 | 13 | 1,973 | 0 | 0 | 2 |

安全全仓没有扩大排除范围；13 项仍为既有本地服务/`run.py`、个人联调夹具和 Windows 不稳定
POSIX 权限位测试。随机临时目录和日志均已删除，最终 `soffice*` 进程数为 0。

M10 代码、文档、外部实机验收和本地关闭门禁已经完成。剩余 Git 操作仅允许在再次确认两个工作树
干净、远端引用无漂移且祖先关系成立后，以 `git merge --ff-only merge/file-analysis` 把结果回归到
`refactor/file-analysis`；禁止用普通 merge 掩盖意外分叉。
