<#
.SYNOPSIS
    DocSense 离线部署 —— 镜像导出脚本（在联网开发机上运行）

.DESCRIPTION
    此脚本完成以下工作：
    1. 拉取 Ollama 和 AnythingLLM 官方 Docker 镜像
    2. 构建 DocSense 自定义镜像
    3. 临时启动 Ollama 容器并拉取指定的 LLM 模型
    4. 将所有镜像导出为 .tar 文件
    5. 导出 Ollama 模型数据卷
    6. 将大文件切割为 ≤4GB 分片（适配 4.7GB DVD）
    7. 生成刻录清单

.NOTES
    运行前请确保：
    - Docker Desktop 已安装并运行
    - models/ 目录已准备好（MinerU 和 Argos 模型）
    - 当前目录为项目根目录（包含 docker/ 文件夹）

.EXAMPLE
    cd C:\.me\codes\DocSense
    powershell -ExecutionPolicy Bypass -File docker/deploy/export-images.ps1
    powershell -ExecutionPolicy Bypass -File docker/deploy/export-images.ps1 -OllamaModels "qwen2.5:7b,nomic-embed-text"
    powershell -ExecutionPolicy Bypass -File docker/deploy/export-images.ps1 -Proxy "http://127.0.0.1:7890"
#>

param(
    # Ollama 中需要拉取的模型名称，多个模型用英文逗号分隔
    # 例如: "qwen2.5:7b,nomic-embed-text"
    [string]$OllamaModels = "qwen2.5:7b",

    # 分片大小上限（字节），默认 4GB（适配 4.7GB DVD）
    [long]$SplitSizeBytes = 4000000000,

    # 输出目录
    [string]$OutputDir = "docker/deploy",

    # 可选：HTTP/HTTPS 代理地址（解决 docker build 超时问题）
    # 例如: "http://127.0.0.1:7890"
    [string]$Proxy = ""
)

if ($Proxy -ne "") {
    $env:HTTP_PROXY = $Proxy
    $env:HTTPS_PROXY = $Proxy
    Write-Host "已临时启用构建代理: $Proxy" -ForegroundColor Yellow
}

$ErrorActionPreference = "Stop"

# ── 颜色输出工具函数 ─────────────────────────────────────────
function Write-Step  { param([string]$Msg) Write-Host "`n==== $Msg ====" -ForegroundColor Cyan }
function Write-Ok    { param([string]$Msg) Write-Host "  ✓ $Msg" -ForegroundColor Green }
function Write-Info  { param([string]$Msg) Write-Host "  → $Msg" -ForegroundColor Yellow }
function Write-Err   { param([string]$Msg) Write-Host "  ✗ $Msg" -ForegroundColor Red }

# ── 前置检查 ─────────────────────────────────────────────────
Write-Step "前置检查"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Err "未找到 docker 命令，请先安装 Docker Desktop"
    exit 1
}

# 检查 Docker 是否正在运行
try {
    docker info *>$null
    Write-Ok "Docker Desktop 运行中"
} catch {
    Write-Err "Docker Desktop 未运行，请先启动"
    exit 1
}

# 检查 models/ 目录
if (-not (Test-Path "models/mineru/OpenDataLab")) {
    Write-Err "models/mineru/OpenDataLab 目录不存在。"
    Write-Info "请参照 docker/deploy/README-OFFLINE.md 第一部分准备模型文件。"
    exit 1
}
Write-Ok "models/ 目录检查通过"

# ── 创建输出目录 ─────────────────────────────────────────────
$imagesDir = Join-Path $OutputDir "images"
$volumesDir = Join-Path $OutputDir "volumes"
New-Item -ItemType Directory -Force -Path $imagesDir | Out-Null
New-Item -ItemType Directory -Force -Path $volumesDir | Out-Null

# ── 1. 拉取官方镜像 ──────────────────────────────────────
Write-Step "1/6 拉取 Ollama 镜像"
docker pull ollama/ollama:latest
if ($LASTEXITCODE -ne 0) { Write-Err "Ollama 镜像拉取失败"; exit 1 }
Write-Ok "ollama/ollama:latest 拉取完成"

Write-Step "2/6 拉取 AnythingLLM 镜像"
docker pull mintplexlabs/anythingllm:latest
if ($LASTEXITCODE -ne 0) { Write-Err "AnythingLLM 镜像拉取失败"; exit 1 }
Write-Ok "mintplexlabs/anythingllm:latest 拉取完成"

Write-Step "3/6 拉取构建依赖基础镜像"
Write-Info "拉取 Python 基础镜像以防构建时无法连接..."
docker pull python:3.12-slim-bookworm
if ($LASTEXITCODE -ne 0) { Write-Err "基础镜像拉取失败"; exit 1 }
Write-Ok "python:3.12-slim-bookworm 拉取完成"

# ── 2. 构建 DocSense 镜像 ────────────────────────────────────
Write-Step "4/6 构建 DocSense 镜像"
Write-Info "这一步可能需要较长时间（安装 Python 依赖 + 复制模型文件）..."
docker compose -f docker/docker-compose.yml build docsense
if ($LASTEXITCODE -ne 0) { Write-Err "DocSense 镜像构建失败"; exit 1 }
Write-Ok "docsense-app:latest 构建完成"

# ── 3. 拉取 Ollama 模型 ──────────────────────────────────────
# 将逗号分隔的字符串拆分为数组
$modelList = $OllamaModels -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' }
$modelListStr = $modelList -join ", "
Write-Step "5/6 拉取 Ollama 模型: $modelListStr"
Write-Info "临时启动 Ollama 容器..."

# 创建临时卷用于存放模型（如果不存在）
docker volume create docsense_ollama-data 2>$null

# 临时启动 Ollama（不需要 GPU，CPU 即可拉取模型）
docker run -d --name ollama-temp -v docsense_ollama-data:/root/.ollama ollama/ollama:latest
Start-Sleep -Seconds 5

foreach ($model in $modelList) {
    Write-Info "正在拉取模型: $model（可能需要几分钟）..."
    docker exec ollama-temp ollama pull $model
    if ($LASTEXITCODE -ne 0) {
        Write-Err "模型 $model 拉取失败"
        docker stop ollama-temp | Out-Null
        docker rm ollama-temp | Out-Null
        exit 1
    }
    Write-Ok "$model 拉取完成"
}

# 验证模型已下载
Write-Info "已安装的模型列表："
docker exec ollama-temp ollama list

# 停止并删除临时容器
docker stop ollama-temp | Out-Null
docker rm ollama-temp | Out-Null
Write-Ok "全部 Ollama 模型拉取完成"

# ── 4. 导出镜像为 .tar ───────────────────────────────────────
Write-Step "6/6 导出镜像文件"

$images = @(
    @{ Name = "ollama";       Image = "ollama/ollama:latest" },
    @{ Name = "anythingllm";  Image = "mintplexlabs/anythingllm:latest" },
    @{ Name = "docsense";     Image = "docsense-app:latest" }
)

foreach ($img in $images) {
    $tarPath = Join-Path $imagesDir "$($img.Name).tar"
    Write-Info "导出 $($img.Image) → $tarPath"
    docker save -o $tarPath $img.Image
    $sizeMB = [math]::Round((Get-Item $tarPath).Length / 1MB, 1)
    Write-Ok "$($img.Name).tar ($sizeMB MB)"
}

# 导出 Ollama 模型数据卷
Write-Info "导出 Ollama 模型数据卷..."
$volumeTar = Join-Path $volumesDir "ollama-models.tar"
docker run --rm -v docsense_ollama-data:/data -v "${PWD}/${volumesDir}:/backup" `
    alpine tar cf /backup/ollama-models.tar -C /data .
$sizeMB = [math]::Round((Get-Item $volumeTar).Length / 1MB, 1)
Write-Ok "ollama-models.tar ($sizeMB MB)"

# ── 5. 切割大文件为 DVD 分片 ─────────────────────────────────
Write-Step "7/7 切割大文件为 DVD 分片（≤4GB/片）"

$allTarFiles = Get-ChildItem -Path $imagesDir, $volumesDir -Filter "*.tar" -Recurse
$splitNeeded = $false

foreach ($file in $allTarFiles) {
    if ($file.Length -gt $SplitSizeBytes) {
        $splitNeeded = $true
        Write-Info "$($file.Name) ($([math]::Round($file.Length / 1GB, 2)) GB) 超过 4GB，正在切割..."

        $splitPrefix = Join-Path $file.DirectoryName "$($file.BaseName).part_"

        # 使用较小的 buffer (64MB) 逐块读取，避免内存溢出和数组大小限制
        $bufferSize = 64 * 1024 * 1024
        $buffer = New-Object byte[] $bufferSize
        $stream = [System.IO.File]::OpenRead($file.FullName)
        $partNum = 0

        try {
            while ($stream.Position -lt $stream.Length) {
                $partFile = "${splitPrefix}$('{0:D3}' -f $partNum)"
                $outStream = [System.IO.File]::Create($partFile)
                $bytesRemainingForPart = $SplitSizeBytes

                try {
                    while ($bytesRemainingForPart -gt 0 -and $stream.Position -lt $stream.Length) {
                        $bytesToRead = [math]::Min([long]$bufferSize, $bytesRemainingForPart)
                        $bytesRead = $stream.Read($buffer, 0, [int]$bytesToRead)
                        if ($bytesRead -eq 0) { break }
                        $outStream.Write($buffer, 0, $bytesRead)
                        $bytesRemainingForPart -= $bytesRead
                    }
                } finally {
                    $outStream.Close()
                }

                $partSizeMB = [math]::Round((Get-Item $partFile).Length / 1MB, 1)
                Write-Ok "  → $([System.IO.Path]::GetFileName($partFile)) ($partSizeMB MB)"
                $partNum++
            }
        } finally {
            $stream.Close()
        }

        # 删除原始大文件，保留分片
        Remove-Item $file.FullName
        Write-Ok "$($file.Name) 已切割为 $partNum 个分片"
    } else {
        Write-Ok "$($file.Name) ($([math]::Round($file.Length / 1MB, 1)) MB) — 无需切割"
    }
}

# ── 6. 生成刻录清单 ──────────────────────────────────────────
Write-Step "生成刻录清单"

$manifestPath = Join-Path $OutputDir "BURN_MANIFEST.txt"
$allFiles = Get-ChildItem -Path $imagesDir, $volumesDir -Recurse -File | Sort-Object Length -Descending

$dvdNum = 1
$dvdUsed = 0
$dvdCapacity = 4700000000  # 4.7GB DVD

$manifest = @()
$manifest += "================================================================"
$manifest += " DocSense Docker 离线部署 —— DVD 刻录清单"
$manifest += " 生成时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
$manifest += "================================================================"
$manifest += ""

# 简单的首次适应装箱算法
$dvds = @{}
foreach ($file in $allFiles) {
    $placed = $false
    foreach ($d in 1..$dvdNum) {
        $usedKey = "dvd$d"
        if (-not $dvds.ContainsKey($usedKey)) { $dvds[$usedKey] = 0 }
        if (($dvds[$usedKey] + $file.Length) -le $dvdCapacity) {
            $dvds[$usedKey] += $file.Length
            $manifest += "DVD #$d  ←  $($file.FullName.Replace($PWD.Path + '\', ''))  ($([math]::Round($file.Length / 1MB, 1)) MB)"
            $placed = $true
            break
        }
    }
    if (-not $placed) {
        $dvdNum++
        $usedKey = "dvd$dvdNum"
        $dvds[$usedKey] = $file.Length
        $manifest += "DVD #$dvdNum  ←  $($file.FullName.Replace($PWD.Path + '\', ''))  ($([math]::Round($file.Length / 1MB, 1)) MB)"
    }
}

$manifest += ""
$manifest += "----------------------------------------------------------------"
$manifest += "共需 $dvdNum 张 DVD"
$manifest += ""
$manifest += "另外请单独准备："
$manifest += "  - Docker Desktop 离线安装程序（从 https://docs.docker.com/desktop/install/windows-install/ 下载）"
$manifest += "  - 项目源码文件夹（app/、run.py、clean.py 等）"
$manifest += "  - docker/ 目录（docker-compose.yml、.env.docker 等）"
$manifest += "----------------------------------------------------------------"

$manifest | Out-File -FilePath $manifestPath -Encoding UTF8
Write-Ok "刻录清单已生成: $manifestPath"

# ── 完成 ─────────────────────────────────────────────────────
Write-Step "全部完成！"
Write-Host ""
Write-Host "  导出文件位于: $OutputDir" -ForegroundColor White
Write-Host "  请按照 $manifestPath 的指引刻录 DVD" -ForegroundColor White
Write-Host "  离线部署指南: docker/deploy/README-OFFLINE.md" -ForegroundColor White
Write-Host ""

# 显示总大小
$totalSize = ($allFiles | Measure-Object -Property Length -Sum).Sum
Write-Host "  总导出大小: $([math]::Round($totalSize / 1GB, 2)) GB  |  预计 DVD: $dvdNum 张" -ForegroundColor Cyan
Write-Host ""


