# Test: 2K upscale of an image the worker already generated (nothing is sent to the site).
# Run while the worker is idle (the browser profile must be free).
# Usage: .\test_2k.ps1 [job_id] [index]   (default: newest job, first image)
param([string]$JobId = "", [int]$Index = 0)
Set-Location -LiteralPath $PSScriptRoot
$env:GFLOW_CLI_HOME = Join-Path $PSScriptRoot "gflow_home"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:GFLOW_CLI_FLOW_HOST = "flow.google.com"
$env:GFLOW_CLI_BROWSER_WINDOW_POSITION = "0,0"
if (-not $JobId) {
    $JobId = (Get-ChildItem .\worker_out -Directory | Where-Object { $_.Name -like "m1_*" -and (Test-Path (Join-Path $_.FullName "run.log")) } |
              Sort-Object LastWriteTime -Descending | Select-Object -First 1).Name
}
Write-Host "Job: $JobId  index: $Index"
$py = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
& $py worker.py upscale-test $JobId $Index 2>&1 | Tee-Object -FilePath (Join-Path $PSScriptRoot "test_2k.log")
Write-Host "Log of the run: worker_out\test2k_${JobId}_${Index}\run.log"
Read-Host "Press Enter"
