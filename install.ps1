# Step 1: create venv, install packages, create worker_config.json with a new worker key.
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

if (-not (Test-Path ".\venv\Scripts\python.exe")) {
    Write-Host "== Creating virtual environment" -ForegroundColor Cyan
    python -m venv venv
    if ($LASTEXITCODE -ne 0) { throw "python -m venv failed (is Python 3.11+ installed and in PATH?)" }
}

Write-Host "== Installing packages (1-3 minutes)" -ForegroundColor Cyan
.\venv\Scripts\python.exe -m pip install --upgrade pip
.\venv\Scripts\python.exe -m pip install "gflow-cli==0.103.0" httpx pillow
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

if (-not (Test-Path ".\worker_config.json")) {
    .\venv\Scripts\python.exe -c "import json,secrets; c=json.load(open('worker_config.example.json',encoding='utf-8')); c['worker_key']='m1w_'+secrets.token_urlsafe(32); json.dump(c,open('worker_config.json','w',encoding='utf-8'),indent=2,ensure_ascii=False)"
}

Write-Host ""
Write-Host "Done. Worker key (put it into your site's secrets as MODEL1_WORKER_KEY):" -ForegroundColor Green
(Get-Content .\worker_config.json -Raw | ConvertFrom-Json).worker_key
Write-Host ""
Write-Host "Edit worker_config.json: set endpoint to your site's backend URL." -ForegroundColor Yellow
Write-Host "Next step: .\login.ps1" -ForegroundColor Green
Read-Host "Press Enter to close"
