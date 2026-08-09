# Legacy Office 主线集成整合执行记录

## 1. 整合说明

本文整合 `main` 与 `refactor/file-analysis` 的 Legacy Office 集成 M0～M10 及首次 M10 准备检查。原记录将每个 Git/功能/验证里程碑拆成独立文档；本文保留最终处置、平台证据、验证数字和发布边界。

- 集成状态：分支集成与本机 Windows 启动门禁已完成。
- 公开契约：Legacy Office 支持按已批准合同接入，未擅自增删接口字段。
- 生产状态：本机验证、分支合入和目标生产发布是三个不同边界；本文不宣称已完成目标环境发布。

## 2. 里程碑

| 里程碑 | 主要结果 |
| --- | --- |
| M0 | 冻结 Git 基线、文件处置矩阵、契约摘要和离线回归基线。 |
| M1 | 保留双父历史完成非快进合并，人工处置 Analysis 等冲突并补充质量修复。 |
| M2 | 验收唯一 LibreOffice 转换内核、任务目录所有权和跨平台离线审计。 |
| M3 | 验收 AnythingLLM 单 Sheet 协议、多 Sheet 拒绝和 Folder 安全清理。 |
| M4 | 验收 Report Legacy 来源、单 Sheet、Artifact 和失败关闭。 |
| M5 | 将 Legacy Office 接入 Stage 1F Analysis 唯一生产链并冻结 V2 策略。 |
| M6 | 验收配置默认值、共享 Preparer、Preflight 和启动失败资源回收。 |
| M7 | 完成共享业务回归及无删除所有权的 XLSX Folder 只读库存治理。 |
| M8 | 同步接口语义、README、索引和契约资产，不新增公开字段。 |
| M9 | 完成九组离线关闭门禁和安全全仓验收。 |
| M10 准备 | 首次因真实平台、离线包、LibreOffice 和发布窗口不足而停止，没有伪造完成结论。 |
| M10 完成 | 记录负责人外部实机事实、本机 LibreOffice 26.2.5.2 启动门禁、远端漂移检查和最终快进回归。 |

## 3. 最终实现事实

1. Legacy Word/Excel/PowerPoint 先经唯一 LibreOffice/OOXML 安全内核转换；转换失败时 fail-closed，不把旧格式原文件直接上传。
2. XLSX 只支持单 Sheet 业务语义；多 Sheet 明确拒绝，不能静默选择第一张表。
3. 转换输出进入通用 Artifact/Profile/Lineage 流水线，任务工作目录有明确所有权标记和回收规则。
4. Analysis 与 Report 复用同一准备能力，不复制 LibreOffice 调用或格式判断逻辑。
5. 远端 XLSX Folder 库存是只读观测，不代表删除所有权，也不签发 Cleanup Token。
6. Preflight 检查可执行文件、版本系列、运行目录和最小转换能力；启动失败必须回收已创建资源。

## 4. Windows 平台结论

- 标准 Windows 安装默认发现应选择 `soffice.com`，而不是 GUI 入口 `soffice.exe`。
- 本机验证版本为 LibreOffice `26.2.5.2`，默认发现、版本门禁和最小启动检查通过。
- 一次将 `soffice.exe` 显式配置为可执行路径的诊断因超时失败，并留下短暂进程；恢复默认发现后使用 `soffice.com` 通过，最终 `soffice*` 进程数为 0。
- 非标准安装应显式配置 `soffice.com` 路径，并在目标环境重新执行 Preflight。

## 5. 验证与 Git 证据

- 定向 Legacy Office、容器、架构和契约门禁 128 项通过；
- 安全全仓动态发现 1,986 项，精确排除 13 项后执行 1,973 项；失败 0、错误 0、跳过 2；
- 集成分支保持预期双父历史，最终通过 `git merge --ff-only` 回归到目标分支；
- 临时目录和日志已清理，最终无残留 LibreOffice 进程。

跳过项和排除项为既有本地服务/`run.py`、个人联调夹具及平台权限差异。历史外部实机验收事实由负责人提供；未获得的包路径、哈希和原始日志没有被补造。

## 6. 发布与回滚边界

- 目标生产环境仍需确认数据库、运行目录、LibreOffice 安装/版本、停服排空窗口、回滚负责人和资源上限。
- 本机单实例转换成功不证明生产并发容量；LibreOffice 重型许可仍需结合 CPU、内存和真实文档压测校准。
- 外部转换结果未知时保留任务目录和审计，不盲目重跑；回滚前先判断资源所有权和业务终态。

