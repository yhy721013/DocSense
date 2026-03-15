param(
    [int]$Port = 8000,
    [string]$Directory = "tests/fixtures/files"
)

# 方案：使用虚拟环境中的 python，通过相对路径查找
$venvPython = ".\.venv\Scripts\python.exe"
if (-Not (Test-Path $venvPython)) {
    $venvPython = ".\venv\Scripts\python.exe"
    if (-Not (Test-Path $venvPython)) {
    # 备选：直接用 python 命令（假设已激活虚拟环境或在 PATH 中）
    $venvPython = "python"
    }
}
& $venvPython -m http.server $Port --directory $Directory
