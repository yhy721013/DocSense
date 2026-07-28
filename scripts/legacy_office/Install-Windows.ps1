[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ExpectedSha256 = "f15ba07bfcb0186986cf3171063506f5d207c11f8cc051ba0d135209e9e915f9"
$MsiPath = Join-Path $PSScriptRoot "installer\LibreOffice_26.2.5_Win_x86-64.msi"

$NativeArchitecture = if ($env:PROCESSOR_ARCHITEW6432) {
    $env:PROCESSOR_ARCHITEW6432
} else {
    $env:PROCESSOR_ARCHITECTURE
}
if ($NativeArchitecture -notin @("AMD64", "x86_64")) {
    throw "此离线包只支持 Windows x64；当前架构：$NativeArchitecture"
}

$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$Principal = [Security.Principal.WindowsPrincipal]::new($Identity)
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "请以管理员身份运行 PowerShell 后重新执行 Install.ps1"
}

if (-not (Test-Path -LiteralPath $MsiPath -PathType Leaf)) {
    throw "缺少官方 MSI：$MsiPath"
}
$ActualSha256 = (Get-FileHash -LiteralPath $MsiPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($ActualSha256 -ne $ExpectedSha256) {
    throw "MSI SHA-256 校验失败；期望 $ExpectedSha256，实际 $ActualSha256"
}

$Arguments = @(
    "/i",
    "`"$MsiPath`"",
    "/qn",
    "/norestart",
    "ALLUSERS=1"
)
$Process = Start-Process -FilePath "$env:SystemRoot\System32\msiexec.exe" `
    -ArgumentList $Arguments -Wait -PassThru
if ($Process.ExitCode -notin @(0, 1641, 3010)) {
    throw "LibreOffice MSI 安装失败，退出码：$($Process.ExitCode)"
}

$Executable = Join-Path $env:ProgramFiles "LibreOffice\program\soffice.com"
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    $Executable = Join-Path $env:ProgramFiles "LibreOffice\program\soffice.exe"
}
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw "MSI 返回成功，但未找到标准安装路径中的 soffice"
}

$VersionOutput = (& $Executable --version 2>&1 | Out-String).Trim()
if ($VersionOutput -match "(?i)LibreOfficeDev" -or
    $VersionOutput -match "(?i)(?:^|[^A-Za-z])(?:alpha|beta|rc|nightly|development)(?:[0-9._-]*)(?:[^A-Za-z]|$)" -or
    $VersionOutput -notmatch "\bLibreOffice\s+26\.2\.5(?:\.\d+)*\b") {
    throw "安装后的版本门禁失败：$VersionOutput"
}

Write-Host "安装完成：$VersionOutput"
Write-Host "DocSense 配置未被修改；请先运行 .\Preflight.ps1，再由运维人员启用功能开关。"
if ($Process.ExitCode -eq 3010) {
    Write-Warning "MSI 请求重启；请重启 Windows 后再执行 preflight。"
}
