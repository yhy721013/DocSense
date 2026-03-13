param(
    [int]$Port = 8000,
    [string]$Directory = "tests/fixtures/files"
)

& "E:/DocSense/.venv/Scripts/python.exe" -m http.server $Port --directory $Directory
