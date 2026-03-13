param(
    [string]$BaseUrl = "http://127.0.0.1:5001",
    [string]$PayloadPath = "tests/fixtures/llm/check_task_file_request.json"
)

$body = Get-Content -Path $PayloadPath -Raw -Encoding utf8
Invoke-RestMethod -Uri "$BaseUrl/llm/check-task" -Method Post -ContentType "application/json; charset=utf-8" -Body $body
