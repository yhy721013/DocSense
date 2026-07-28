# `main` 与 `refactor/file-analysis` Legacy Office 集成 M10 准备检查记录

## 1. 检查结论

M10“真实平台认证与发布”尚未执行，当前因真实环境和交付资产不足而暂停。M0～M9 的代码与
离线验收结果保持有效，但不得据此宣称 Windows x64、macOS Apple Silicon、真实 AnythingLLM
或生产发布已经认证。

本次只进行了只读环境盘点，没有下载或安装软件，没有执行 `run.py`，没有连接真实 AnythingLLM，
没有停止任务受理、排空队列、修改生产配置、数据库或远端资源。

## 2. 已确认环境事实

| 检查项 | 当前结果 | M10 影响 |
| --- | --- | --- |
| 当前主机 | Windows AMD64 | 只能承载 Windows x64 认证，不能替代 macOS Apple Silicon 实机 |
| `soffice` PATH | 不存在 | 无法执行版本、三格式 Smoke、连续转换、超时和残留进程门禁 |
| Windows 标准安装路径 | 两个候选路径均不存在 | 当前主机没有可用于认证的 LibreOffice 安装 |
| 工作树 `dist/legacy-office` | 不存在 | 没有离线安装包、最终 zip 或包内 `SHA256SUMS` 可核对 |
| 根仓库 `dist/legacy-office` | 不存在 | 没有可从 Git 忽略目录复用的交付资产 |
| `artifacts.lock.json` | 存在，锁定 LibreOffice 26.2.5 | 仅有预期哈希和构建输入，不能代替实际文件哈希核验 |
| macOS Apple Silicon 目标机 | 当前环境不具备 | macOS 真实 Smoke 与残留进程证明无法执行 |
| 根仓库 `.env` | AnythingLLM 连接键存在；Legacy Office 三项配置键不存在 | 不能把当前配置当作发布候选配置；未读取或输出密钥值 |

## 3. 恢复 M10 所需输入

继续 M10 前至少需要：

1. 提供或允许获取锁定的 Windows x64 与 macOS ARM64 官方安装包、许可证和三份固定 Smoke 样本，
   并生成两个平台的最终离线 zip 与 `SHA256SUMS`；
2. 提供 Windows x64 与 macOS Apple Silicon 目标机或等价受控执行入口；
3. 明确是否允许安装 LibreOffice 26.2.5，以及安装目录和可接受的主机变更窗口；
4. 提供独立的真实 AnythingLLM 测试 workspace/账号和可删除的测试数据边界；
5. 明确停服、停止新受理、Accepted/Running 排空、发布、观察和回滚窗口；
6. 明确目标任务数据库与运行目录，只允许先做只读 Preflight 和诊断，禁止盲目重放或删除
   无所有权证明的 Folder。

## 4. 恢复后的强制顺序

1. 对安装包、Smoke 样本、最终 zip 和包内文件逐层核对 SHA-256；
2. 在两个目标平台分别安装并执行 `soffice --version`；
3. 执行 `.doc/.ppt/.xls` 三格式 Smoke、连续转换、超时、进程树和残留 profile 检查；
4. 使用隔离测试数据验证 Analysis、Report、AnythingLLM 单 Sheet 与失败关闭；
5. 停止新受理并排空 Legacy Accepted/Running 任务；
6. 发布后先执行 Preflight 和只读 XLSX Folder 库存，再启动正常服务；
7. 观察转换耗时、Folder 增长、清理 unknown、Callback 和 Dispatcher 状态；
8. 任一硬门禁失败即关闭新受理并回滚配置，不删除任务数据库，不盲目重放远端上传。

## 5. 当前状态声明

M10 保持“等待外部条件”，集成分支当前只达到代码与单实例临时 SQLite/Fake 的离线关闭状态。
需要负责人确认上述环境、资产和操作授权后，才能恢复真实认证与发布阶段。
