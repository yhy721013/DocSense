param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RunnerArgs
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot

$callerDatabaseEnv = @{}
foreach ($name in @(
    "DOCSENSE_RUNTIME_DIR",
    "DOCSENSE_LLM_TASK_DB",
    "DOCSENSE_KNOWLEDGE_BASE_DB",
    "KNOWLEDGE_BASE_DB_PATH"
)) {
    $value = [Environment]::GetEnvironmentVariable($name, "Process")
    if ($null -ne $value) {
        $callerDatabaseEnv[$name] = $value
    }
}

$envPath = Join-Path $RootDir ".env"
if (-not (Test-Path $envPath)) { $envPath = Join-Path $RootDir ".env.example" }
if (Test-Path $envPath) {
    Get-Content $envPath -Encoding UTF8 | Where-Object { $_ -match '^\s*([^#\s][^=]*)=(.*)' } | ForEach-Object {
        [Environment]::SetEnvironmentVariable($Matches[1].Trim(), $Matches[2].Trim(), "Process")
    }
}

foreach ($entry in $callerDatabaseEnv.GetEnumerator()) {
    [Environment]::SetEnvironmentVariable(
        [string]$entry.Key,
        [string]$entry.Value,
        "Process"
    )
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
