[CmdletBinding()]
param(
    [string]$ExecutablePath = "",
    [string]$SamplesDirectory = "",
    [ValidateRange(1, 3600)]
    [int]$TimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Test-BundleChecksums {
    param([string]$Root)

    $ChecksumPath = Join-Path $Root "SHA256SUMS"
    if (-not (Test-Path -LiteralPath $ChecksumPath -PathType Leaf)) {
        throw "缺少 SHA256SUMS，拒绝执行未校验的离线包"
    }
    $ChecksumLines = @(Get-Content -LiteralPath $ChecksumPath)
    if ($ChecksumLines.Count -eq 0) {
        throw "SHA256SUMS 为空，拒绝执行未校验的离线包"
    }
    $RootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    foreach ($Line in $ChecksumLines) {
        if ($Line -notmatch '^([0-9a-f]{64})  (.+)$') {
            throw "SHA256SUMS 包含非法行：$Line"
        }
        $Expected = $Matches[1]
        $Relative = $Matches[2].Replace('/', '\')
        $Candidate = [IO.Path]::GetFullPath((Join-Path $Root $Relative))
        if (-not $Candidate.StartsWith($RootFull, [StringComparison]::OrdinalIgnoreCase)) {
            throw "SHA256SUMS 路径越界：$Relative"
        }
        if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
            throw "SHA256SUMS 中的文件不存在：$Relative"
        }
        $Actual = (Get-FileHash -LiteralPath $Candidate -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($Actual -ne $Expected) {
            throw "文件 SHA-256 校验失败：$Relative"
        }
    }
}

function Quote-ProcessArgument {
    param([string]$Value)
    if ($Value.Contains('"')) {
        throw "进程参数路径不能包含双引号"
    }
    return '"' + $Value + '"'
}

function Stop-ProcessTree {
    param([int]$ProcessId)
    & "$env:SystemRoot\System32\taskkill.exe" /PID $ProcessId /T /F 2>$null |
        Out-Null
}

function Get-ProfileProcessIds {
    param([string]$ProfileUri)

    if ([string]::IsNullOrWhiteSpace($ProfileUri)) {
        return @()
    }
    return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($ProfileUri) } |
        ForEach-Object { [int]$_.ProcessId })
}

function Stop-ProfileProcesses {
    param([string]$ProfileUri)

    foreach ($ProcessId in @(Get-ProfileProcessIds -ProfileUri $ProfileUri)) {
        Stop-ProcessTree -ProcessId $ProcessId
    }
}

function Invoke-BoundedProcess {
    param(
        [string]$Executable,
        [string[]]$Arguments,
        [int]$Timeout,
        [string]$ProfileUri,
        [string]$Operation
    )

    $StartInfo = [Diagnostics.ProcessStartInfo]::new()
    $StartInfo.FileName = $Executable
    $StartInfo.Arguments = (($Arguments | ForEach-Object {
        Quote-ProcessArgument $_
    }) -join " ")
    $StartInfo.UseShellExecute = $false
    $StartInfo.CreateNoWindow = $true
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    $Process = [Diagnostics.Process]::new()
    $Process.StartInfo = $StartInfo

    try {
        if (-not $Process.Start()) {
            throw "无法启动 $Operation"
        }
        # 两个异步读取必须和父进程退出共享同一个总 deadline。父进程先退出并
        # 不代表成功：继承管道句柄的子进程仍可能让 ReadToEndAsync 永久不 EOF。
        $StandardOutputTask = $Process.StandardOutput.ReadToEndAsync()
        $StandardErrorTask = $Process.StandardError.ReadToEndAsync()
        $Clock = [Diagnostics.Stopwatch]::StartNew()
        $TimeoutMilliseconds = [int64]$Timeout * 1000
        $TimedOut = $false

        while ($true) {
            $Remaining = $TimeoutMilliseconds - $Clock.ElapsedMilliseconds
            if ($Remaining -le 0) {
                $TimedOut = $true
                break
            }
            if (-not $Process.HasExited) {
                $WaitSlice = [int][Math]::Min(100, $Remaining)
                [void]$Process.WaitForExit($WaitSlice)
            }
            if ($Process.HasExited -and
                $StandardOutputTask.IsCompleted -and
                $StandardErrorTask.IsCompleted) {
                break
            }
            Start-Sleep -Milliseconds ([int][Math]::Min(25, $Remaining))
        }

        if ($TimedOut) {
            $ParentExited = $Process.HasExited
            Stop-ProcessTree -ProcessId $Process.Id
            Stop-ProfileProcesses -ProfileUri $ProfileUri
            $CleanupClock = [Diagnostics.Stopwatch]::StartNew()
            while ($CleanupClock.ElapsedMilliseconds -lt 5000 -and
                ((-not $Process.HasExited) -or
                    (-not $StandardOutputTask.IsCompleted) -or
                    (-not $StandardErrorTask.IsCompleted))) {
                Start-Sleep -Milliseconds 25
            }
            if ($ParentExited) {
                throw "$Operation 超时：父进程已退出，但输出管道未在总时限内 EOF；已按 PID/profile 清理"
            }
            throw "$Operation 超时：进程或输出管道未在总时限内完成；已按 PID/profile 清理"
        }

        $StandardOutput = $StandardOutputTask.GetAwaiter().GetResult()
        $StandardError = $StandardErrorTask.GetAwaiter().GetResult()
        return [pscustomobject]@{
            ExitCode = $Process.ExitCode
            StandardOutput = $StandardOutput
            StandardError = $StandardError
        }
    } finally {
        $Process.Dispose()
    }
}

function Test-Ole2 {
    param([string]$Path)
    $Expected = [byte[]](0xD0, 0xCF, 0x11, 0xE0, 0xA1, 0xB1, 0x1A, 0xE1)
    $Stream = [IO.File]::OpenRead($Path)
    try {
        if ($Stream.Length -le $Expected.Length) {
            throw "样本为空或过短：$Path"
        }
        $Header = New-Object byte[] $Expected.Length
        [void]$Stream.Read($Header, 0, $Header.Length)
        for ($Index = 0; $Index -lt $Expected.Length; $Index++) {
            if ($Header[$Index] -ne $Expected[$Index]) {
                throw "样本不是 Office OLE2 文件：$Path"
            }
        }
    } finally {
        $Stream.Dispose()
    }
}

function Test-Ooxml {
    param(
        [string]$Path,
        [string]$RequiredMember
    )
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "未生成预期 OOXML：$Path"
    }
    if ((Get-Item -LiteralPath $Path).Length -eq 0) {
        throw "OOXML 输出为空：$Path"
    }
    $Archive = [IO.Compression.ZipFile]::OpenRead($Path)
    try {
        $Names = @($Archive.Entries | ForEach-Object { $_.FullName })
        if ($Names -notcontains $RequiredMember) {
            throw "OOXML 缺少结构成员：$RequiredMember"
        }
        foreach ($Entry in $Archive.Entries) {
            $EntryStream = $Entry.Open()
            try {
                $Buffer = New-Object byte[] 65536
                while ($EntryStream.Read($Buffer, 0, $Buffer.Length) -gt 0) {
                }
            } finally {
                $EntryStream.Dispose()
            }
        }
    } finally {
        $Archive.Dispose()
    }
}

function Invoke-SmokeConversion {
    param(
        [string]$Executable,
        [string]$Sample,
        [string]$TargetExtension,
        [string]$FilterName,
        [string]$RequiredMember,
        [int]$Timeout
    )

    Test-Ole2 -Path $Sample
    $TemporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ("docsense-lo-" + [guid]::NewGuid().ToString("N"))
    $InputDirectory = Join-Path $TemporaryRoot "input"
    $OutputDirectory = Join-Path $TemporaryRoot "output"
    $ProfileDirectory = Join-Path $TemporaryRoot "profile"
    [void](New-Item -ItemType Directory -Path $InputDirectory, $OutputDirectory, $ProfileDirectory)
    $FixedInput = Join-Path $InputDirectory ("input" + [IO.Path]::GetExtension($Sample).ToLowerInvariant())
    Copy-Item -LiteralPath $Sample -Destination $FixedInput
    $ProfileUri = ([Uri]([IO.Path]::GetFullPath($ProfileDirectory))).AbsoluteUri

    try {
        $Arguments = @(
            "--headless",
            "--nologo",
            "--nodefault",
            "--nolockcheck",
            "--norestore",
            "-env:UserInstallation=$ProfileUri",
            "--convert-to",
            $FilterName,
            "--outdir",
            $OutputDirectory,
            $FixedInput
        )
        $Result = Invoke-BoundedProcess `
            -Executable $Executable `
            -Arguments $Arguments `
            -Timeout $Timeout `
            -ProfileUri $ProfileUri `
            -Operation "LibreOffice smoke 转换"
        if ($Result.ExitCode -ne 0) {
            throw "LibreOffice 转换失败，退出码 $($Result.ExitCode)"
        }

        $ExpectedOutput = Join-Path $OutputDirectory "input.$TargetExtension"
        $Outputs = @(Get-ChildItem -LiteralPath $OutputDirectory -File)
        if ($Outputs.Count -ne 1 -or $Outputs[0].FullName -ne $ExpectedOutput) {
            throw "LibreOffice 产物不唯一或名称不符"
        }
        Test-Ooxml -Path $ExpectedOutput -RequiredMember $RequiredMember

        $Residual = @(Get-ProfileProcessIds -ProfileUri $ProfileUri)
        if ($Residual.Count -gt 0) {
            Stop-ProfileProcesses -ProfileUri $ProfileUri
            throw "转换后发现残留 LibreOffice 进程，已清理"
        }
        Write-Host "转换结构验证通过：$([IO.Path]::GetFileName($Sample)) -> input.$TargetExtension"
    } finally {
        if (Test-Path -LiteralPath $TemporaryRoot) {
            Remove-Item -LiteralPath $TemporaryRoot -Recurse -Force
        }
    }
}

$NativeArchitecture = if ($env:PROCESSOR_ARCHITEW6432) {
    $env:PROCESSOR_ARCHITEW6432
} else {
    $env:PROCESSOR_ARCHITECTURE
}
if ($NativeArchitecture -notin @("AMD64", "x86_64")) {
    throw "此 preflight 只支持 Windows x64；当前架构：$NativeArchitecture"
}

Test-BundleChecksums -Root $PSScriptRoot

if ([string]::IsNullOrWhiteSpace($ExecutablePath)) {
    $Candidates = @(
        (Join-Path $env:ProgramFiles "LibreOffice\program\soffice.com"),
        (Join-Path $env:ProgramFiles "LibreOffice\program\soffice.exe")
    )
    $ExecutablePath = $Candidates |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
        Select-Object -First 1
}
if ([string]::IsNullOrWhiteSpace($ExecutablePath) -or
    -not [IO.Path]::IsPathRooted($ExecutablePath) -or
    -not (Test-Path -LiteralPath $ExecutablePath -PathType Leaf)) {
    throw "找不到可执行 LibreOffice；可用 -ExecutablePath 指定绝对路径"
}

$VersionTemporaryRoot = Join-Path ([IO.Path]::GetTempPath()) (
    "docsense-lo-version-" + [guid]::NewGuid().ToString("N")
)
$VersionProfileDirectory = Join-Path $VersionTemporaryRoot "profile"
[void](New-Item -ItemType Directory -Path $VersionProfileDirectory)
$VersionProfileUri = (
    [Uri]([IO.Path]::GetFullPath($VersionProfileDirectory))
).AbsoluteUri
try {
    $VersionTimeout = [int][Math]::Min($TimeoutSeconds, 20)
    $VersionResult = Invoke-BoundedProcess `
        -Executable $ExecutablePath `
        -Arguments @("-env:UserInstallation=$VersionProfileUri", "--version") `
        -Timeout $VersionTimeout `
        -ProfileUri $VersionProfileUri `
        -Operation "LibreOffice --version"
    if ($VersionResult.ExitCode -ne 0) {
        throw "LibreOffice --version 执行失败"
    }
    $VersionResidual = @(
        Get-ProfileProcessIds -ProfileUri $VersionProfileUri
    )
    if ($VersionResidual.Count -gt 0) {
        Stop-ProfileProcesses -ProfileUri $VersionProfileUri
        throw "LibreOffice --version 结束后仍有残留进程，已清理"
    }
    $VersionOutput = (
        "$($VersionResult.StandardOutput)`n$($VersionResult.StandardError)"
    ).Trim()
} finally {
    Stop-ProfileProcesses -ProfileUri $VersionProfileUri
    if (Test-Path -LiteralPath $VersionTemporaryRoot) {
        Remove-Item -LiteralPath $VersionTemporaryRoot -Recurse -Force
    }
}
if ($VersionOutput -match "(?i)LibreOfficeDev" -or
    $VersionOutput -match "(?i)(?:^|[^A-Za-z])(?:alpha|beta|rc|nightly|development)(?:[0-9._-]*)(?:[^A-Za-z]|$)") {
    throw "拒绝 LibreOffice 开发版或预发布版本"
}
if ($VersionOutput -notmatch "\bLibreOffice\s+26\.2\.5(?:\.\d+)*\b") {
    throw "版本不符合锁定的稳定 26.2.5.x：$VersionOutput"
}
Write-Host "版本门禁通过：$VersionOutput"

if ([string]::IsNullOrWhiteSpace($SamplesDirectory)) {
    $SamplesDirectory = Join-Path $PSScriptRoot "samples"
}
$Cases = @(
    @{
        Name = "word-sample.doc"
        Target = "docx"
        Filter = "docx:Office Open XML Text"
        Member = "word/document.xml"
    },
    @{
        Name = "powerpoint-2002-apache-poi.ppt"
        Target = "pptx"
        Filter = "pptx:Impress MS PowerPoint 2007 XML"
        Member = "ppt/presentation.xml"
    },
    @{
        Name = "excel-sample.xls"
        Target = "xlsx"
        Filter = "xlsx:Calc Office Open XML"
        Member = "xl/workbook.xml"
    }
)
foreach ($Case in $Cases) {
    $Sample = Join-Path $SamplesDirectory $Case.Name
    if (-not (Test-Path -LiteralPath $Sample -PathType Leaf)) {
        throw "缺少 smoke 样本：$Sample"
    }
    Invoke-SmokeConversion -Executable $ExecutablePath -Sample $Sample `
        -TargetExtension $Case.Target -FilterName $Case.Filter `
        -RequiredMember $Case.Member -Timeout $TimeoutSeconds
}

Write-Host "Windows x64 离线依赖 preflight 通过。"
Write-Warning "本轮 Windows 脚本和包结构仅完成静态/mock 验证，尚未完成 Windows x64 实机认证。"
Write-Host "DocSense 配置未被修改；可由运维人员设置 DOCSENSE_LEGACY_OFFICE_ENABLED=true。"
