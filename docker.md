# DocSense Docker 部署指南

本文档详细说明了如何在 Docker 环境下构建、配置并运行 DocSense 项目。

## 1. 前置准备：模型资源本地化

为了确保镜像能够离线运行，需将必要的模型文件复制到项目根目录的 `models` 文件夹中。

### 1.1 MinerU 模型
从本地缓存复制模型文件至 `./models/mineru/`：
*   **源路径**：`C:\Users\{用户名}\.cache\modelscope\hub\models\OpenDataLab`
*   **目标路径**：`./models/mineru/OpenDataLab`
*   **重命名要求**：确保OpenDataLab内文件夹名称规范为 `MinerU2.5-2B` 和 `PDF-Extract-Kit-1.0`。

### 1.2 Argo Translate 翻译包
复制离线翻译包至 `./models/argos-translate/`：
*   **源路径**：`C:\Users\{用户名}\.local\share\argos-translate`
*   **目标路径**：`./models/argos-translate`

## 2. 关键配置修正

### 2.1 修正 MFR 模型元数据
由于模型配置文件中的绝对路径可能与容器环境不符，需手动修正以确保本地加载成功。
*   **文件路径**：`./models/mineru/OpenDataLab/PDF-Extract-Kit-1.0/models/MFR/unimernet_hf_small_2503/config.json`
*   **修改项**：将 `_name_or_path` 字段更新为容器内的标准路径：
    ```json
    "_name_or_path": "/root/.cache/modelscope/hub/models/OpenDataLab/PDF-Extract-Kit-1.0/models/MFR/unimernet_hf_small_2503"
    ```

### 2.2 适配 Docker 网络地址
在测试或回调场景中，需将 `localhost` 替换为 Docker 内部通信地址 `host.docker.internal`。
*   **文件路径**：`tests/fixtures/llm/analysis_request.json`
*   **示例**：
    ```json
    {
      "businessType": "file",
      "params": [
        {
          "fileName": "地雷.pdf",
          "filePath": "http://host.docker.internal:8000/地雷.pdf"
        }
      ]
    }
    ```

## 3. 构建与启动流程

请按照以下步骤在四个独立的终端窗口中执行操作：

### 步骤 1：启动测试文件服务器
提供 PDF 文件的 HTTP 访问入口。
```powershell
powershell -NoLogo -Command "./scripts/start_test_file_server.ps1"
```

### 步骤 2：启动 Mock 回调服务
用于接收 LLM 分析完成后的状态回调。
```powershell
python scripts/mock_callback_server.py
```

### 步骤 3：构建并启动 DocSense 容器
```powershell
docker-compose build docsense
docker-compose up -d docsense
```

### 步骤 4：执行分析测试
触发 LLM 文件分析接口。
```powershell
powershell -NoLogo -Command "./scripts/test_llm_analysis.ps1"
```

## 4. 运维与监控

### 查看实时日志
```powershell
docker-compose logs -f docsense
```

### 结果存储位置
所有运行时产生的数据（如 SQLite 数据库、下载的文件等）均持久化存储在宿主机的 `./data/runtime/` 目录下。

## 5. 核心配置文件清单

| 文件名 | 作用说明 |
| :--- | :--- |
| `Dockerfile` | 定义镜像构建逻辑、依赖安装及环境变量 |
| `docker-compose.yml` | 编排容器服务、端口映射及卷挂载 |
| `.dockerignore` | 排除非必要文件以优化构建速度 |
| `mineru.json` | MinerU 全局配置，指定模型来源与路径 |
