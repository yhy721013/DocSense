# `main` 与 `refactor/file-analysis` Legacy Office 集成 M2 执行记录

## 1. 阶段结论

M2“Legacy Office 内核和现有平台交付”已完成，可以进入 M3。本阶段继承并审查了转换 Domain、
Port、LibreOffice Adapter、OOXML 校验器，以及 Windows x64、macOS Apple Silicon 的离线资产
锁、安装、预检和打包脚本；没有扩大平台认证范围，也没有执行 `run.py` 或真实 LibreOffice。

## 2. 永久修复

### 2.1 Windows 任务目录所有权标记

主线实现使用 `Path.write_text()` 写入以 LF 结尾的任务所有权标记，同时用字符串长度校验磁盘
字节数。在 Windows 文本模式下，LF 会写成 CRLF，导致本进程刚创建的目录立即被误判为“不属于
自己”，成功或失败后的清理均被跳过，并持续累积转换工作目录。

修复后的规则为：

1. 新任务使用 ASCII 二进制固定写入 LF 标记，不再发生平台换行转换；
2. 所有权校验只接受规范 LF 字节或旧 Windows 版本确实可能写出的精确 CRLF 字节；
3. 不接受任意空白、大小写变体或附加内容，删除边界没有放宽；
4. 启动扫描可以回收修复前遗留的可信 CRLF 标记目录；
5. 无标记、名称不符合约定、符号链接或越过根目录的目标仍保持不删除。

### 2.2 跨平台离线审计

- 目标平台绝对路径改用 `ntpath` 或 `posixpath` 判断，不再错误服从执行测试的宿主平台；
- macOS 用户级 LibreOffice 候选用 POSIX 规则拼接，Windows CI 不再改写分隔符；
- macOS 进程组强杀使用 POSIX `SIGKILL=9` 的测试回退值，使 Windows CI 可以静态模拟算法；
- 回退值不改变脚本的真实支持平台，macOS 实机进程组与 OOXML 超时回收仍留给 M10。

## 3. 合并适配

- Legacy Office 容器测试显式关闭 Weaponry 术语上下文，避免裸 `MagicMock` 的真值误触发真实目录；
- 部署资产测试按已确认决策断言 `.env.example` 默认开启，同时保留“环境变量缺失时代码关闭”的
  独立配置测试；
- M2 不再要求接口文档提前包含 Legacy Office 文本，公开语义继续延迟到 M8；
- POSIX 权限位只在提供该语义的宿主机断言，Windows 仍验证私有 HOME/TEMP 目录隔离；
- macOS 与 Windows 的进程创建参数分别在可重复的 Fake 测试中验证。

## 4. 门禁证据

| 门禁 | 结果 |
| --- | --- |
| Legacy Office 配置、转换、OOXML 安全与离线交付 | 52 项通过，2 项 macOS 实机用例按计划跳过 |
| 全仓架构边界 | 23 项通过 |
| `app/modules/document_processing` 与 `scripts/legacy_office` 编译 | 通过 |
| `git diff --check` | 通过 |
| `docs/接口文档/` 工作树差异 | 0 |

上述证据覆盖 OLE2 签名、三种格式、严格输出 Filter、ZIP/ZIP64、解压上限、超时、进程树、并发
许可、敏感信息脱敏、任务目录清理、资产 Hash、Manifest 和脚本静态合同。它只证明 Windows
宿主上的离线/Fake 行为，不把 macOS 条件跳过或未执行的真实 LibreOffice 转换解释为生产认证。

## 5. 阶段后商讨项检查

Windows 标记缺陷已按 fail-closed 所有权边界永久修复；跨平台测试调整没有扩大部署范围。剩余两项
macOS 实机用例正是既定 M10 门禁，不是新增需求。当前没有接口参数、平台范围或清理所有权疑问，
可以进入 M3。
