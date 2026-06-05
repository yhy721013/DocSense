<#
.SYNOPSIS
    DocSense 离线部署 —— 镜像导入与启动脚本（在离线目标机器上运行）

.DESCRIPTION
    此脚本完成以下工作：
    1. 合并 DVD 中的分片文件（如有）
    2. 加载所有 Docker 镜像
    3. 创建并还原 Ollama 模型数据卷
    4. 启动三个服务
    5. 执行健康检查

.NOTES
    运行前请确保：
    - Docker Desktop 已安装并运行
    - 所有 DVD 的文件已复制到本脚本所在目录的 images/ 和 volumes/ 子目录
    - 项目源码已复制到指定位置

.EXAMPLE
    # 假设将所有文件复制到了 D:\DocSense\docker\deploy\
    cd D:\DocSense
    powershell -ExecutionPolicy Bypass -File docker\deploy\import-and-start.ps1
#>

param(
    # 项目根目录（包含 app/、run.py 等源码文件）
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,

    # deploy 目录（包含 images/ 和 volumes/）
    [string]$DeployDir = $PSScriptRoot
)

$ErrorActionPreference = "Stop"

# ── 颜色输出工具函数 ─────────────────────────────────────────
function Write-Step  { param([string]$Msg) Write-Host "`n==== $Msg ====" -ForegroundColor Cyan }
function Write-Ok    { param([string]$Msg) Write-Host "  ✓ $Msg" -ForegroundColor Green }
function Write-Info  { param([string]$Msg) Write-Host "  → $Msg" -ForegroundColor Yellow }
function Write-Err   { param([string]$Msg) Write-Host "  ✗ $Msg" -ForegroundColor Red }

# ── 前置检查 ─────────────────────────────────────────────────
Write-Step "前置检查"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Err "未找到 docker 命令，请先安装 Docker Desktop。"
    Write-Info "Docker Desktop 安装程序应在 DVD 中，请先安装并重启电脑。"
    exit 1
}

try {
    docker info *>$null
    Write-Ok "Docker Desktop 运行中"
} catch {
    Write-Err "Docker Desktop 未运行，请先启动 Docker Desktop 并等待初始化完成。"
    exit 1
}

$imagesDir = Join-Path $DeployDir "images"
$volumesDir = Join-Path $DeployDir "volumes"

if (-not (Test-Path $imagesDir)) {
    Write-Err "images/ 目录不存在: $imagesDir"
    Write-Info "请确保已将 DVD 中的 images/ 目录复制到此处"
    exit 1
}

# ── 1. 合并分片文件 ──────────────────────────────────────────
Write-Step "1/4 合并分片文件（如有）"

$splitPatterns = @("*.part_000")  # 查找被切割过的文件
$dirsToCheck = @($imagesDir, $volumesDir)

foreach ($dir in $dirsToCheck) {
    if (-not (Test-Path $dir)) { continue }

    $firstParts = Get-ChildItem -Path $dir -Filter "*.part_000" -ErrorAction SilentlyContinue
    foreach ($firstPart in $firstParts) {
        # 从 "ollama.part_000" 提取 "ollama"
        $baseName = $firstPart.Name -replace '\.part_\d+$', ''
        $outputFile = Join-Path $dir "$baseName.tar"

        if (Test-Path $outputFile) {
            Write-Info "$baseName.tar 已存在，跳过合并"
            continue
        }

        Write-Info "合并 $baseName 的分片文件..."
        $parts = Get-ChildItem -Path $dir -Filter "$baseName.part_*" | Sort-Object Name

        # 使用流式合并，避免内存问题
        $outStream = [System.IO.File]::Create($outputFile)
        try {
            foreach ($part in $parts) {
                Write-Info "  拼接 $($part.Name)..."
                $inStream = [System.IO.File]::OpenRead($part.FullName)
                $inStream.CopyTo($outStream)
                $inStream.Close()
            }
        } finally {
            $outStream.Close()
        }

        $sizeMB = [math]::Round((Get-Item $outputFile).Length / 1MB, 1)
        Write-Ok "$baseName.tar 合并完成 ($sizeMB MB)"

        # 可选：删除分片文件以释放空间
        # foreach ($part in $parts) { Remove-Item $part.FullName }
    }
}

# ── 2. 加载 Docker 镜像 ──────────────────────────────────────
Write-Step "2/4 加载 Docker 镜像"

$tarFiles = Get-ChildItem -Path $imagesDir -Filter "*.tar" -ErrorAction SilentlyContinue
if ($tarFiles.Count -eq 0) {
    Write-Err "images/ 目录中没有 .tar 文件"
    exit 1
}

foreach ($tar in $tarFiles) {
    Write-Info "加载 $($tar.Name)..."
    docker load -i $tar.FullName
    Write-Ok "$($tar.Name) 加载完成"
}

# ── 3. 还原 Ollama 模型数据卷 ────────────────────────────────
Write-Step "3/4 还原 Ollama 模型数据"

$ollamaTar = Join-Path $volumesDir "ollama-models.tar"
if (Test-Path $ollamaTar) {
    Write-Info "创建 Ollama 数据卷..."
    docker volume create docsense_ollama-data 2>$null

    Write-Info "还原模型数据（可能需要几分钟）..."
    # 将 Windows 路径转换为 Docker 可用的路径格式
    $volumesDirDocker = $volumesDir -replace '\\', '/'
    docker run --rm `
        -v docsense_ollama-data:/data `
        -v "${volumesDirDocker}:/backup:ro" `
        alpine sh -c "tar xf /backup/ollama-models.tar -C /data"

    Write-Ok "Ollama 模型数据还原完成"
} else {
    Write-Info "未找到 ollama-models.tar，跳过模型还原"
    Write-Info "你可以稍后手动在 Ollama 容器中拉取模型"
}

# ── 4. 启动服务 ──────────────────────────────────────────────
Write-Step "4/4 启动 DocSense 服务"

# 创建必要的数据目录
$dataRuntime = Join-Path $ProjectRoot "data\runtime"
$dataUploads = Join-Path $ProjectRoot "data\uploads"
New-Item -ItemType Directory -Force -Path $dataRuntime | Out-Null
New-Item -ItemType Directory -Force -Path $dataUploads | Out-Null

# 切换到 docker/ 目录启动 compose
$dockerDir = Join-Path $ProjectRoot "docker"
Write-Info "启动三个服务..."
Push-Location $dockerDir
try {
    docker compose up -d
} finally {
    Pop-Location
}

# ── 健康检查 ─────────────────────────────────────────────────
Write-Step "健康检查"
Write-Info "等待服务启动（30 秒）..."
Start-Sleep -Seconds 30

$checks = @(
    @{ Name = "Ollama";       Url = "http://localhost:11434/api/tags" },
    @{ Name = "AnythingLLM";  Url = "http://localhost:3001" },
    @{ Name = "DocSense";     Url = "http://localhost:5001/debug/callback" }
)

$allOk = $true
foreach ($check in $checks) {
    try {
        $response = Invoke-WebRequest -Uri $check.Url -TimeoutSec 10 -UseBasicParsing -ErrorAction Stop
        Write-Ok "$($check.Name) — 响应正常 (HTTP $($response.StatusCode))"
    } catch {
        Write-Err "$($check.Name) — 无响应，请检查日志: docker compose -f docker/docker-compose.yml logs $($check.Name.ToLower())"
        $allOk = $false
    }
}

# ── 完成提示 ─────────────────────────────────────────────────
Write-Host ""
if ($allOk) {
    Write-Host "  ════════════════════════════════════════════" -ForegroundColor Green
    Write-Host "  ✓ 所有服务启动成功！" -ForegroundColor Green
    Write-Host "  ════════════════════════════════════════════" -ForegroundColor Green
} else {
    Write-Host "  ════════════════════════════════════════════" -ForegroundColor Yellow
    Write-Host "  ⚠ 部分服务未通过健康检查，请排查" -ForegroundColor Yellow
    Write-Host "  ════════════════════════════════════════════" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "  接下来请完成以下配置：" -ForegroundColor White
Write-Host ""
Write-Host "  1. 打开浏览器访问 http://localhost:3001" -ForegroundColor White
Write-Host "     完成 AnythingLLM 初始配置向导" -ForegroundColor White
Write-Host "     → LLM Provider 选择 Ollama" -ForegroundColor White
Write-Host "     → Ollama Base URL 填写: http://ollama:11434" -ForegroundColor White
Write-Host ""
Write-Host "  2. 在 AnythingLLM 设置中获取 API Key" -ForegroundColor White
Write-Host "     将其填入 docker\.env.docker 的 ANYTHINGLLM_API_KEY" -ForegroundColor White
Write-Host ""
Write-Host "  3. 重启 DocSense 使配置生效：" -ForegroundColor White
Write-Host "     cd $dockerDir" -ForegroundColor Cyan
Write-Host "     docker compose restart docsense" -ForegroundColor Cyan
Write-Host ""
Write-Host "  4. 验证 DocSense：http://localhost:5001/debug/callback" -ForegroundColor White
Write-Host ""
