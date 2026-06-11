param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RunnerArgs
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot

$envPath = Join-Path $RootDir ".env"
if (-not (Test-Path $envPath)) { $envPath = Join-Path $RootDir ".env.example" }
if (Test-Path $envPath) {
    Get-Content $envPath -Encoding UTF8 | Where-Object { $_ -match '^\s*([^#\s][^=]*)=(.*)' } | ForEach-Object {
        [Environment]::SetEnvironmentVariable($Matches[1].Trim(), $Matches[2].Trim(), "Process")
    }
}

$PythonBin = Join-Path $RootDir ".venv\Scripts\python.exe"
if (-not (Test-Path $PythonBin)) {
    $PythonBin = Join-Path $RootDir ".venv/bin/python"
}
if (-not (Test-Path $PythonBin)) {
    $PythonBin = "python"
}

& $PythonBin (Join-Path $RootDir "scripts/run_llm_weaponry_directory.py") @RunnerArgs
exit $LASTEXITCODE
