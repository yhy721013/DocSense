# DocSense Docker 离线部署指南

本文档是一份完整的操作手册，涵盖从**联网开发机打包**到**离线目标机器部署**的全过程。

---

## 第一部分：在联网开发机上打包

### 1.1 安装 Docker Desktop

1. 下载 [Docker Desktop for Windows](https://docs.docker.com/desktop/install/windows-install/)
2. 双击安装程序，按默认选项安装（新版默认使用 WSL2，无需额外勾选）
3. 安装完成后重启电脑
4. 启动 Docker Desktop，等待左下角状态变为 **"Engine running"**（绿色）
5. 如果弹出"更新 WSL"窗口，以管理员身份运行 `wsl --update`，然后重启 Docker Desktop

验证安装：

```powershell
docker --version
# 输出类似: Docker version 27.x.x, build xxxxxxx
```

### 1.2 准备模型文件

在项目根目录创建 `models/` 目录，并将以下模型文件复制进去：

#### MinerU 模型

**方式一：使用项目内置下载脚本（推荐）**

在项目的 Python 环境中运行：

```powershell
python app/services/translator/test_minerU/models_download.py -s modelscope -m all
```

下载完成后模型会保存在 `C:\Users\{你的用户名}\.cache\modelscope\hub\models\OpenDataLab`。

**方式二：手动复制已有缓存**

如果本机已经使用过 MinerU 并有模型缓存：

- **源路径**：`C:\Users\{你的用户名}\.cache\modelscope\hub\models\OpenDataLab`

然后将模型复制到项目中：

- **目标路径**：`models/mineru/OpenDataLab`
- **重命名要求**：确保 OpenDataLab 内的文件夹名称为 `MinerU2.5-Pro-2605-1.2B` 和 `PDF-Extract-Kit-1.0`

```powershell
mkdir -p models\mineru
xcopy /E /I "%USERPROFILE%\.cache\modelscope\hub\models\OpenDataLab" "models\mineru\OpenDataLab"
```

#### Argos Translate 翻译包

- **源路径**：`C:\Users\{你的用户名}\.local\share\argos-translate`
- **目标路径**：`models/argos-translate`

```powershell
xcopy /E /I "%USERPROFILE%\.local\share\argos-translate" "models\argos-translate"
```

### 1.3 修正 MFR 模型元数据

由于模型配置文件中的绝对路径可能与容器环境不符，需手动修正：

- **文件路径**：`models/mineru/OpenDataLab/PDF-Extract-Kit-1.0/models/MFR/unimernet_hf_small_2503/config.json`
- **修改项**：将 `_name_or_path` 字段更新为容器内路径：

```json
"_name_or_path": "/root/.cache/modelscope/hub/models/OpenDataLab/PDF-Extract-Kit-1.0/models/MFR/unimernet_hf_small_2503"
```

### 1.4 确认 Ollama 模型名称

你需要知道 AnythingLLM 使用的 Ollama 模型名称。在开发机的 AnythingLLM 设置页面中查看：
**Settings → LLM Preference → Model** 中显示的模型名（如 `qwen2.5:7b`）。

### 1.5 运行导出脚本

```powershell
cd C:\.me\codes\DocSense

# 拉取单个模型
powershell -ExecutionPolicy Bypass -File docker/deploy/export-images.ps1

# 拉取多个模型（用英文逗号分隔）
powershell -ExecutionPolicy Bypass -File docker/deploy/export-images.ps1 -OllamaModels "qwen2.5:7b,nomic-embed-text"
```

脚本运行完成后，会在 `docker/deploy/` 下生成：

```
docker/deploy/
├── images/              # Docker 镜像（可能已被切割为分片）
│   ├── ollama.tar       # 或 ollama.part_000, ollama.part_001, ...
│   ├── anythingllm.tar
│   └── docsense.tar
├── volumes/             # 数据卷备份
│   └── ollama-models.tar
└── BURN_MANIFEST.txt    # DVD 刻录清单
```

### 1.6 下载离线安装程序

为目标离线机器下载以下安装包：

| 文件 | 下载地址 | 大小 | 说明 |
|------|---------|------|------|
| `Docker Desktop Installer.exe` | https://docs.docker.com/desktop/install/windows-install/ | ~600MB | Docker 本体 |
| `wsl_update_x64.msi` | https://wslstorestorage.blob.core.windows.net/wslblob/wsl_update_x64.msi | ~15MB | WSL2 内核更新包（离线机器必备） |
| NVIDIA 驱动 `.exe`（可选） | https://www.nvidia.com/drivers | ~700MB | 如需升级目标机 GPU 驱动 |

将这些文件保存到 `docker/deploy/` 目录。

### 1.7 按刻录清单刻录 DVD

打开 `docker/deploy/BURN_MANIFEST.txt`，按照指示将文件分配到各张 DVD 上。

**每张 DVD 上还需额外包含**（可放在任意一张盘上）：
- `Docker Desktop Installer.exe`
- `wsl_update_x64.msi`
- `docker/` 目录（包含 `docker-compose.yml`、`.env.docker` 等）
- 项目源码：`app/`、`run.py`、`clean.py`、`requirements.txt` 等

---

## 第二部分：在离线目标机器上部署

### 2.1 安装 WSL2 更新包

1. 从 DVD 中找到 `wsl_update_x64.msi`
2. 双击安装，按提示完成

### 2.2 安装 Docker Desktop

1. 从 DVD 中找到 `Docker Desktop Installer.exe`
2. 双击运行，按默认选项安装
3. 安装完成后**重启电脑**
4. 重启后，Docker Desktop 会自动启动
5. 等待 Docker Desktop 左下角状态变为绿色 **"Engine running"**

验证：

```powershell
docker --version
docker compose version
```

### 2.3 复制文件到本地

将所有 DVD 的内容复制到本地硬盘的同一个目录下。建议目录结构如下：

```
D:\DocSense\                      # 项目根目录
├── app\                          # 源码
├── run.py
├── clean.py
├── requirements.txt
├── data\                         # 运行时数据（会自动创建）
│   ├── runtime\
│   └── uploads\
└── docker\                       # Docker 配置
    ├── Dockerfile
    ├── docker-compose.yml
    ├── .env.docker
    └── deploy\
        ├── import-and-start.ps1
        ├── images\               # 从 DVD 复制的镜像文件
        │   ├── ollama.tar (或 ollama.part_000, ollama.part_001, ...)
        │   ├── anythingllm.tar
        │   └── docsense.tar
        └── volumes\              # 从 DVD 复制的数据卷
```

### 2.3 （可选）检查并配置 GPU 依赖

项目默认配置假定机器上没有 NVIDIA GPU 或尚未配置好 GPU 环境。
如果你确认这台离线机器上有 **NVIDIA 显卡** 且已经安装了相应的 **NVIDIA 驱动**（`nvidia-smi` 能够正常输出内容）：

1. 打开刚复制下来的 `docker\docker-compose.yml` 文件。
2. 找到 `ollama` 服务下被注释掉的 `deploy` 这一段：
   ```yaml
   #    deploy:
   #      resources:
   #        reservations:
   #          devices:
   #            - driver: nvidia
   #              count: all
   #              capabilities: [gpu]
   ```
3. **删掉这 7 行开头的 `#` 号**，保存文件。这样 Ollama 就能使用 GPU 进行极速推理了。

> **⚠️ 注意：**如果机器只有集显（比如 Intel/AMD 核显）或者没装驱动，**绝对不要**取消注释这些行，否则服务启动时会直接报错崩溃（`WSL environment detected but no adapters were found`）。

### 2.4 运行导入和启动脚本

```powershell
cd D:\DocSense
powershell -ExecutionPolicy Bypass -File docker\deploy\import-and-start.ps1
```

脚本会自动：
1. ✅ 合并分片文件（如有）
2. ✅ 加载三个 Docker 镜像
3. ✅ 还原 Ollama 模型数据
4. ✅ 启动三个服务
5. ✅ 执行健康检查

### 2.5 配置 AnythingLLM

1. 打开浏览器，访问 **http://localhost:3001**
2. 完成 AnythingLLM 初始配置向导：
   - **LLM Provider** → 选择 **Ollama**
   - **Ollama Base URL** → 填写 `http://ollama:11434`
   - 选择刚才导入的模型（如 `qwen2.5:7b`）
3. 进入设置页面，找到 **API Keys**
4. 生成一个新的 API Key 并复制

### 2.6 配置 DocSense

1. 编辑 `docker\.env.docker` 文件
2. 填入 AnythingLLM 的 API Key：

```
ANYTHINGLLM_API_KEY=你复制的API Key
```

3. 重新创建 DocSense 服务使环境变量配置生效：

```powershell
cd D:\DocSense\docker
docker compose up -d --force-recreate docsense
```

`.env.docker` 已显式固定 `/llm/analysis` 的生产模式：

```ini
DOCSENSE_ANALYSIS_CLASSIFICATION_MODE=topk_two_stage
DOCSENSE_ANALYSIS_FILENAME_CONSTRAINT_MODE=scope_guard
DOCSENSE_ANALYSIS_DATA_STANDARD_MODE=scope_guard
DOCSENSE_ANALYSIS_IDENTITY_RESELECT_MODE=enforce
```

- `topk_two_stage`：先在完整领域树上本地召回有界候选，再分别执行分类和字段抽取。
- 第一个 `scope_guard`：文件名可以参与召回，但不能作为普通资料最终分类的单一硬覆盖依据。
- 第二个 `scope_guard`：仅对文件名与首页共同确认的数据标准正文启用六类叶子保护。
- `enforce`：仅在原始文件名与文档开头双证据确认唯一装备身份后，对分支外或过粗的初次分类执行一次受限重选；失败时保留初次分类。

需要紧急回滚时，优先只改动出现问题的开关：

```ini
# 保留 Top-K 候选，仅回滚为单阶段分类与抽取
DOCSENSE_ANALYSIS_CLASSIFICATION_MODE=topk_single
# 恢复旧文件名硬约束
DOCSENSE_ANALYSIS_FILENAME_CONSTRAINT_MODE=legacy
# 关闭数据标准正文分类保护
DOCSENSE_ANALYSIS_DATA_STANDARD_MODE=legacy
# 关闭装备身份受限重选
DOCSENSE_ANALYSIS_IDENTITY_RESELECT_MODE=off
```

`DOCSENSE_ANALYSIS_CLASSIFICATION_MODE=legacy` 只适用于候选不超过 128 且完整 Prompt 不超过 32,000 字符的小树，不适用于正式完整领域树。修改任一环境变量后必须重新创建容器，单纯执行 `docker compose restart` 不会重新读取 `env_file`：

```powershell
cd D:\DocSense\docker
docker compose up -d --force-recreate docsense
docker compose logs --tail 100 docsense
```

文件分析 Dispatcher 的 1F-5A 内部运行参数也已显式写入 `.env.docker`：

```ini
DOCSENSE_ANALYSIS_RUNTIME_MODE=single_instance
DOCSENSE_ANALYSIS_DISPATCH_SCAN_INTERVAL_SECONDS=1
DOCSENSE_ANALYSIS_DISPATCH_BATCH_SIZE=50
DOCSENSE_ANALYSIS_DISPATCH_RETRY_BASE_SECONDS=5
DOCSENSE_ANALYSIS_DISPATCH_RETRY_MAX_SECONDS=300
DOCSENSE_ANALYSIS_RESOURCE_SWEEP_INTERVAL_SECONDS=30
DOCSENSE_ANALYSIS_RESOURCE_SWEEP_BATCH_SIZE=50
DOCSENSE_ANALYSIS_RUNNING_ALERT_SECONDS=30
DOCSENSE_ANALYSIS_STOP_TIMEOUT_SECONDS=5
DOCSENSE_ANALYSIS_CALLBACK_HTTP_TIMEOUT_SECONDS=10
DOCSENSE_ANALYSIS_CALLBACK_LEASE_SECONDS=30
```

- 该 Dispatcher 只允许 `single_instance`：它依赖本地 SQLite、进程锁和进程内线程；设置
  `distributed`、`multi_instance` 或其他值会使容器在组合根阶段拒绝启动，不能把它当作可靠队列或
  多实例开关。
- `DISPATCH_BATCH_SIZE` 与 `RESOURCE_SWEEP_BATCH_SIZE` 只限制单次扫描量，不限制 SQLite 中可持久保存的
  accepted 积压量。领取前基础设施错误按 base/max 执行持久化指数退避，避免单个坏任务热循环。
- Callback lease 必须严格大于 HTTP timeout 加连接、响应读取和安全余量；任何非法数值或超时关系都会
  fail fast，不会静默退回默认值。
- 当前公开 `/llm/analysis` 与 file 类型 `/llm/check-task` 已接入阶段 1F 唯一运行链。
  Dispatcher 只扫描带 `batch_id`/`batch_sequence` 的新 file execution，不领取旧任务，也不得
  手工制造并行双跑。任何部署阶段都不得手工制造并行双跑。

### 2.7 验证

- DocSense 调试页：http://localhost:5001/debug/callback
- AnythingLLM：http://localhost:3001
- Ollama 模型列表：http://localhost:11434/api/tags

---

## 第三部分：日常运维

### 查看服务状态

```powershell
cd D:\DocSense\docker
docker compose ps
```

### 查看服务实时日志

如果需要监控后台运行状态或排查问题，可以在命令行中查看实时滚动的日志（按 `Ctrl + C` 退出查看）：

```powershell
# 查看所有服务的混合日志
cd D:\DocSense\docker
docker compose logs -f

# 或者直接查看单个特定服务的日志：
docker compose logs -f docsense      # 核心后端业务日志（OCR 进度、API 请求等）
docker compose logs -f anythingllm   # 向量数据库与前端日志
docker compose logs -f ollama        # 大模型加载与推理日志
```

> **💡 提示：**你也可以直接打开 **Docker Desktop** 的图形界面，点击左侧的 `Containers`，展开 `docsense` 组，点击里面的任何一个容器即可直观地看到黑底白字的实时控制台输出。

### 启动/恢复服务

如果你之前使用了 `down` 停止并移除了服务，你需要使用 `up -d` 重新把它们拉起来：

```powershell
cd D:\DocSense\docker
docker compose up -d
```

### 重启服务

> **注意：** `restart` 只能用来重启**当前正在运行（或只是处于暂停状态）**的容器。如果你刚才执行了 `down` 移除了容器，那么 `restart` 是无效的，你必须使用上面的 `up -d`。

```powershell
cd D:\DocSense\docker
# 重启所有服务（常用于配置修改后使其生效）
docker compose restart

# 只重启 DocSense
docker compose restart docsense
```

### 停止/关闭所有服务

如果你需要彻底关闭后台运行的容器，释放内存：

```powershell
cd D:\DocSense\docker
docker compose down
```
*(提示：`down` 会停止并移除容器，但所有存在数据卷中的数据和模型都是绝对安全的，下次直接使用 `docker compose up -d` 即可瞬间恢复原样。)*

### 清理测试数据

```powershell
cd D:\DocSense\docker
docker compose stop docsense
docker compose run --rm --no-deps docsense python clean.py
docker compose up -d docsense
```

> 必须先停止 DocSense，且 `clean.py` 必须以退出码 0 完成后才能启动新版本。脚本会
> 删除 `DOCSENSE_RUNTIME_DIR`，并显式删除通过 `DOCSENSE_LLM_TASK_DB`、
> `DOCSENSE_KNOWLEDGE_BASE_DB`/`KNOWLEDGE_BASE_DB_PATH`、`DOCSENSE_CHAT_DB`
> 配置在运行时目录外的数据库；文件占用或删除失败会非零退出。其 API 清理功能
> 会删除 AnythingLLM 工作区；
> 物理文件清理会因为 AnythingLLM 在另一个容器中而跳过（无影响）。
>
> 当前项目约定每次代码更新均执行上述停服清库重建，因此
> `scripts/inspect_analysis_cutover.py` 不是日常发布强制步骤。只有保留存量任务库、
> 从备份恢复或怀疑清理未成功时，才在停服窗口运行该只读预检并处置全部阻断项。

### 更新代码（最常见操作）

1. 在开发机上修改代码
2. 将修改后的文件复制到 U 盘
3. 在离线机器上替换对应文件（`app/` 目录、`run.py` 等）
4. 重启 DocSense：

```powershell
cd D:\DocSense\docker
docker compose restart docsense
```

### 更新 Python 依赖（偶尔）

如果 `requirements.txt` 发生变化，需要重新构建 DocSense 镜像：

1. 在联网开发机上：

```powershell
cd C:\.me\codes\DocSense
docker compose -f docker/docker-compose.yml build docsense
docker save -o docker/deploy/images/docsense.tar docsense-app:latest
```

2. 将新的 `docsense.tar` 刻录到 DVD 带到离线机器
3. 在离线机器上：

```powershell
docker load -i docker\deploy\images\docsense.tar
cd D:\DocSense\docker
docker compose up -d docsense
```

---

## 第四部分：端口冲突处理

如果离线机器上已有非 Docker 版本的 Ollama/AnythingLLM 正在运行，会出现端口冲突。

### 方案 A：停止旧服务

停止原有的 Ollama、AnythingLLM 和 DocSense 服务后再启动 Docker。

### 方案 B：修改端口映射

编辑 `docker/.env.docker`，取消以下注释并修改端口号：

```ini
OLLAMA_HOST_PORT=21434
ANYTHINGLLM_HOST_PORT=13001
DOCSENSE_HOST_PORT=15001
```

这样 Docker 服务会使用新的端口，不影响旧服务：
- Docker Ollama：http://localhost:21434
- Docker AnythingLLM：http://localhost:13001
- Docker DocSense：http://localhost:15001

> 注意：容器**内部**通信不受端口映射影响，仍然通过服务名 + 原始端口互访。

---

## 第五部分：故障排查

### Docker Desktop 无法启动

- 确认 Windows 版本为 **专业版 / 企业版 / 教育版**（家庭版需要额外启用 WSL2）
- 在 BIOS 中确认 **虚拟化技术 (VT-x / AMD-V)** 已启用
- 以管理员身份运行 PowerShell，执行：
  ```powershell
  wsl --status
  ```
  确认 WSL2 已启用

### 容器启动失败

```powershell
# 查看具体错误日志
docker compose logs ollama
docker compose logs anythingllm
docker compose logs docsense
```

### Ollama GPU 未被识别

```powershell
# 进入 Ollama 容器检查 GPU
docker exec -it docsense-ollama nvidia-smi
```

如果 `nvidia-smi` 不可用，可能是：
- NVIDIA 驱动未安装或版本过低
- Docker Desktop 的 WSL2 后端未正确集成 GPU

解决方法：确保 Windows 上安装了最新版 NVIDIA 驱动。

### DocSense 无法连接 AnythingLLM

检查 `.env.docker` 中的 `ANYTHINGLLM_API_KEY` 是否正确填写，然后：

```powershell
# 从 DocSense 容器内部测试连通性
docker exec -it docsense-app python -c "import requests; print(requests.get('http://anythingllm:3001/api/v1/auth', headers={'Authorization': 'Bearer YOUR_API_KEY'}).status_code)"
```
