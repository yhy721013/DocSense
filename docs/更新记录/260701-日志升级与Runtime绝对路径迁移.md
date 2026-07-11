# 日志升级与 Runtime 绝对路径迁移实施说明

## 1. 背景与目标

本次改造解决两个长期运行问题：

1. 项目运行数据原先默认保存在仓库内的 `.runtime`，代码更新、切换工作目录或重新部署时容易误删、遗漏或使用到不同的数据目录。
2. 文档解析链路中仍存在直接 `print()` 输出，且 `argostranslate` 会在 `INFO` 级别输出大段待翻译文本和分句过程，影响日志可读性。

改造后的目标是：

- 使用一个与源码目录解耦的绝对目录统一保存 Runtime 文件，并保持原有文件和子目录的相对结构。
- 应用代码和 Python 辅助脚本统一使用标准库 `logging`。
- 保留关键业务过程日志，降低逐段翻译、分句等高频细节的默认输出级别。
- 通过静态回归检查防止重新引入直接 `print()` 调用。

本地 Windows 环境的目标 Runtime 根目录为：

```text
C:/.me/envs/DocSenseEnv
```

## 2. Runtime 统一路径设计

### 2.1 统一配置入口

在项目根目录 `.env` 中配置：

```env
DOCSENSE_RUNTIME_DIR=C:/.me/envs/DocSenseEnv
```

Windows 下推荐使用正斜杠，避免反斜杠转义问题。显式配置 `DOCSENSE_RUNTIME_DIR` 时必须使用绝对路径；相对路径会在配置加载阶段直接报错。未配置该变量时，为了兼容旧环境，仍回退到仓库根目录下的 `.runtime`。

路径解析集中在 `app/services/core/settings.py`。模块加载时会创建 Runtime 根目录、必要的子目录以及数据库文件的父目录，但不会自动把旧 `.runtime` 中的数据复制到新目录。

### 2.2 目录映射

原有相对位置保持不变：

| 运行时内容 | 新路径 |
| --- | --- |
| LLM 任务库 | `${DOCSENSE_RUNTIME_DIR}/llm_tasks.sqlite3` |
| 知识库映射库 | `${DOCSENSE_RUNTIME_DIR}/knowledge_base.sqlite3` |
| 对话状态库 | `${DOCSENSE_RUNTIME_DIR}/chat_sessions.sqlite3` |
| 下载缓存 | `${DOCSENSE_RUNTIME_DIR}/llm_downloads/` |
| OCR Markdown 缓存 | `${DOCSENSE_RUNTIME_DIR}/ocr_markdown/` |
| MinerU Markdown 缓存 | `${DOCSENSE_RUNTIME_DIR}/mineru_markdown/` |
| 回调历史 | `${DOCSENSE_RUNTIME_DIR}/callback/` |
| SQLite JSON 导出 | `${DOCSENSE_RUNTIME_DIR}/sqlite/` |
| 旧版回调预览 | `${DOCSENSE_RUNTIME_DIR}/call_back.json` |
| 批量武器装备解析输出 | `${DOCSENSE_RUNTIME_DIR}/weaponry_directory_<timestamp>/` |

`app/services/core/config.py`、正式接口、回调工具、调试页和数据库导出脚本均从上述统一配置派生路径。`clean.py` 也会读取同一个根目录，因此执行清理脚本前必须确认当前 `.env` 指向的是允许清空的环境。

### 2.3 旧组件级变量的优先级

以下历史变量仍保留兼容能力，并且优先级高于 `DOCSENSE_RUNTIME_DIR`：

```text
DOCSENSE_LLM_TASK_DB
DOCSENSE_KNOWLEDGE_BASE_DB
KNOWLEDGE_BASE_DB_PATH
DOCSENSE_CHAT_DB
FILE_DOWNLOAD_DIR
DOCSENSE_OCR_CACHE_DIR
DOCSENSE_MINERU_CACHE_DIR
```

如果目标是把全部运行时内容集中到同一个绝对目录，应删除或注释这些变量，只保留 `DOCSENSE_RUNTIME_DIR`。否则相应组件仍会写入覆盖变量指定的位置，形成数据分散。

## 3. 现有数据迁移步骤

新目录为空是正常现象：配置只负责确定路径和按需建目录，不负责迁移历史文件。旧 `.runtime` 可以手动复制到新目录，建议按以下顺序操作。

1. 停止 DocSense 主进程、后台任务和可能访问 SQLite 的辅助脚本，避免复制期间数据库继续写入。
2. 备份仓库内原 `.runtime`。
3. 将 `.runtime` 内部的所有文件和子目录复制到 `C:/.me/envs/DocSenseEnv`，不要额外嵌套一层 `.runtime`。
4. 在 `.env` 中设置 `DOCSENSE_RUNTIME_DIR=C:/.me/envs/DocSenseEnv`，并清除上一节列出的组件级覆盖变量。
5. 使用项目实际运行账号确认新目录可读、可创建、可修改、可删除文件。
6. 重启应用，确认三个 SQLite 数据库、缓存目录和回调历史均从新目录读取。
7. 完成业务验证和备份后，再决定是否删除仓库内旧 `.runtime`；不要在首次启动前直接删除旧数据。

可使用 PowerShell 复制数据：

```powershell
$target = "C:\.me\envs\DocSenseEnv"
New-Item -ItemType Directory -Path $target -Force | Out-Null
Copy-Item -Path ".\.runtime\*" -Destination $target -Recurse -Force
```

“运行账号具有读写权限”是指启动 `python run.py` 或承载服务的 Windows 用户必须能在目标目录中读取已有文件，并创建、修改和删除数据库及缓存文件。SQLite 运行时还可能创建 `-wal`、`-shm`、`-journal` 等伴随文件，仅能读取数据库文件并不足够。如需授予目录修改权限，可由管理员执行：

```powershell
icacls "C:\.me\envs\DocSenseEnv" /grant "<运行账号>:(OI)(CI)M" /T
```

其中 `<运行账号>` 必须替换为实际的 Windows 用户或服务账号。

### 3.1 Docker 环境

容器内不能使用 Windows 主机路径。Docker Compose 使用以下映射：

```yaml
volumes:
  - ../data/runtime:/app/runtime
environment:
  - DOCSENSE_RUNTIME_DIR=/app/runtime
```

因此容器内仍使用绝对路径 `/app/runtime`，持久化数据实际保存在宿主机 `docker/../data/runtime`。本地直接运行与 Docker 运行的根目录配置应分别维护。

## 4. 日志升级

### 4.1 统一初始化

应用由 `app/services/core/logging.py` 的 `setup_logging()` 初始化日志：

```text
[%(asctime)s] %(levelname)s [%(name)s:%(lineno)d] %(message)s
```

时间字段使用 `%y-%m-%d %H:%M:%S`，例如 `[26-07-05 09:51:38]`。不输出世纪位和毫秒，
以降低高频后台任务日志的存储与传输开销。

默认输出到 `stderr`，全局级别通过环境变量控制：

```env
DOCSENSE_LOG_LEVEL=INFO
```

支持标准日志级别，例如 `DEBUG`、`INFO`、`WARNING`、`ERROR`。未配置时使用 `INFO`；无法识别的值也回退为 `INFO`。独立运行的 Python 辅助脚本通过各自的 `logging.basicConfig()` 提供一致的终端日志能力。

### 4.2 `print()` 替换范围

本次已替换以下范围内的直接 `print()` 输出：

- 应用入口 `run.py`。
- Markdown、MHTML、TXT、MinerU 和通用文档解析模块。
- MHTML 规范化工具。
- MinerU 示例程序。
- 任务库检查、回调模拟和批量武器装备解析脚本。

日志级别按用途划分：

- `DEBUG`：分片、段落、翻译进度等高频细节。
- `INFO`：任务启动、阶段完成、文件落盘等正常业务事件。
- `WARNING`：可恢复异常、跳过内容和清理失败。
- `ERROR`：导致当前操作失败的异常。

测试中需要把 JSON 作为子进程协议结果写入标准输出的场景使用 `sys.stdout.write()`，避免业务日志污染可机器解析的输出。

### 4.3 第三方日志降噪

以下第三方 logger 固定提高到 `WARNING`：

```python
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("argostranslate").setLevel(logging.WARNING)
logging.getLogger("argostranslate.utils").setLevel(logging.WARNING)
```

其中 `argostranslate` 和 `argostranslate.utils` 的调整会屏蔽默认 `INFO` 级别的原文、分句
模型和翻译步骤日志，但仍保留警告与错误，无需修改 Python 包内部代码。由于
`argostranslate.utils` 在延迟导入时会主动把自身 logger 重置为 `INFO`，应用必须在 Argos
导入完成后再次调用统一的 `apply_third_party_log_levels()`，不能只在进程启动时设置一次。

### 4.4 回归约束

`tests/test_no_print_calls.py` 使用 AST 扫描以下 Python 代码：

- `run.py`
- `clean.py`
- `app/**/*.py`
- `scripts/**/*.py`

发现直接 `print()` 调用时测试失败，从而阻止后续改动重新绕过标准日志系统。

## 5. 验证清单

### 5.1 Runtime 验证

- `.env` 中仅保留预期的统一根目录配置。
- 启动账号对目标目录具有修改权限。
- 新目录中可以看到历史 SQLite 文件，而不是生成全新的空库。
- 新任务能够更新 `llm_tasks.sqlite3`。
- 文件解析后能够生成下载、OCR 或 MinerU 缓存。
- 回调后能够在 `callback/` 看到新记录。
- 项目仓库内 `.runtime` 不再产生新的运行数据。

`tests/test_runtime_settings.py` 覆盖绝对根目录的组件路径派生，以及相对根目录配置必须失败的行为。

### 5.2 日志验证

- 默认 `INFO` 模式下不再出现 `argostranslate.utils` 的大段原文和分句过程。
- 关键任务阶段仍有 `INFO` 日志。
- 异常仍保留 `WARNING` 或 `ERROR` 日志。
- 全项目 Python 运行代码不存在直接 `print()` 调用。

由于主流程依赖 AnythingLLM、模型、OCR/MinerU 等后台服务，本次实施阶段只执行了 Python AST 语法检查、直接 `print()` 全局扫描和 `git diff --check`，未启动 `run.py`，也未执行依赖后台服务的测试。

## 6. 回滚方案

如需临时回滚 Runtime 路径：

1. 停止应用和后台任务。
2. 将新目录中的最新数据完整备份或同步回仓库 `.runtime`。
3. 删除 `.env` 中的 `DOCSENSE_RUNTIME_DIR`，使系统恢复默认 `.runtime`；或者将其改为另一个绝对路径。
4. 重新启动并核对数据库时间戳和任务记录。

日志升级通常无需回滚。若排障期间需要更多细节，可临时设置 `DOCSENSE_LOG_LEVEL=DEBUG`；`argostranslate` 仍保持 `WARNING`，避免再次输出完整翻译文本。

## 7. 实施记录

- Runtime 绝对路径迁移：提交 `d2b0173`。
- 日志降噪与 `print()` 标准化：提交 `765909f`。

