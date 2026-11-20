# Test: Nano Banana Pro, 2 images, 16:9 in one run (no API server).
# Close any Chrome window opened by open_profile.ps1 first (profile must be free).
# Full output is saved to test_pro.log.
Set-Location -LiteralPath $PSScriptRoot
$env:GFLOW_CLI_HOME = Join-Path $PSScriptRoot "gflow_home"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:COLUMNS = "200"
$env:GFLOW_CLI_FLOW_HOST = "flow.google.com"
$env:GFLOW_CLI_DEBUG_TRACEBACK = "1"
$env:GFLOW_CLI_BROWSER_WINDOW_POSITION = "0,0"
$log = Join-Path $PSScriptRoot "test_pro.log"

$py = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
$args_ = @("gflow_ext.py", "-v", "image", "t2i", "--model", "nano-pro", "--aspect", "16:9", "-n", "2",
           "--out", "test_pro_out", "--", "`"a ginger kitten sitting on a windowsill, soft morning light`"")
$p = Start-Process -FilePath $py -ArgumentList $args_ -NoNewWindow -Wait -PassThru `
    -RedirectStandardOutput "$log.out" -RedirectStandardError "$log.err"
"exit code: $($p.ExitCode)" | Out-File -FilePath $log -Encoding utf8
"----- stdout -----" | Out-File -FilePath $log -Encoding utf8 -Append
Get-Content "$log.out" -Encoding utf8 | Out-File -FilePath $log -Encoding utf8 -Append
"----- stderr -----" | Out-File -FilePath $log -Encoding utf8 -Append
Get-Content "$log.err" -Encoding utf8 | Out-File -FilePath $log -Encoding utf8 -Append
Remove-Item "$log.out", "$log.err" -ErrorAction SilentlyContinue

Get-Content $log -Encoding utf8
Write-Host ""
Write-Host "Result files:" -ForegroundColor Green
Get-ChildItem -Recurse .\test_pro_out -Include *.png,*.jpg,*.jpeg,*.webp -ErrorAction SilentlyContinue | Select-Object FullName, Length
Write-Host ""
Write-Host "Log saved to $log" -ForegroundColor Green
Read-Host "Press Enter to close"
