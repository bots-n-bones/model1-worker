# model_1 worker: polls the site for jobs and generates them. Keep this window open.
# Log: worker.log. Stop: Ctrl+C.
Set-Location -LiteralPath $PSScriptRoot
$env:GFLOW_CLI_HOME = Join-Path $PSScriptRoot "gflow_home"
$env:GFLOW_CLI_FLOW_HOST = "flow.google.com"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
while ($true) {
    .\venv\Scripts\python.exe worker.py
    Write-Host "Worker exited, restarting in 10 s (Ctrl+C to stop)..." -ForegroundColor Yellow
    Start-Sleep -Seconds 10
}
