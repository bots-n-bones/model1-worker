# Test: Nano Banana Pro with 2 reference images, 2 results, one run (no site).
# Stop worker.ps1 first (profile must be free). Output: test_refs.log, test_refs_out\
Set-Location -LiteralPath $PSScriptRoot
$env:GFLOW_CLI_HOME = Join-Path $PSScriptRoot "gflow_home"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:COLUMNS = "200"
$env:GFLOW_CLI_FLOW_HOST = "flow.google.com"
$env:GFLOW_CLI_DEBUG_TRACEBACK = "1"
$env:GFLOW_CLI_BROWSER_WINDOW_POSITION = "0,0"
$log = Join-Path $PSScriptRoot "test_refs.log"

$refs = @(Get-ChildItem -Recurse .\test_pro_out, .\worker_out -Include *.jpg,*.png -ErrorAction SilentlyContinue |
          Where-Object { $_.DirectoryName -notmatch '\\refs$' } | Select-Object -First 2)
if ($refs.Count -lt 2) { Write-Host "Need 2 images in test_pro_out or worker_out (run test_pro.ps1 first)" -ForegroundColor Red; Read-Host "Press Enter"; exit 1 }
Write-Host "Refs:" ($refs | ForEach-Object { $_.Name })

$py = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
$args_ = @("gflow_ext.py", "-v", "image", "i2i", "--model", "nano-pro", "--aspect", "1:1", "-n", "2")
foreach ($r in $refs) { $args_ += @("--ref", "`"$($r.FullName)`"") }
$args_ += @("--out", "test_refs_out", "--", "`"combine both images into one scene, watercolor style. Сгенерируй в модели №1`"")
$p = Start-Process -FilePath $py -ArgumentList $args_ -NoNewWindow -Wait -PassThru `
    -RedirectStandardOutput "$log.out" -RedirectStandardError "$log.err"
"exit code: $($p.ExitCode)" | Out-File -FilePath $log -Encoding utf8
"----- stdout -----" | Out-File -FilePath $log -Encoding utf8 -Append
Get-Content "$log.out" -Encoding utf8 | Out-File -FilePath $log -Encoding utf8 -Append
"----- stderr -----" | Out-File -FilePath $log -Encoding utf8 -Append
Get-Content "$log.err" -Encoding utf8 | Out-File -FilePath $log -Encoding utf8 -Append
Remove-Item "$log.out", "$log.err" -ErrorAction SilentlyContinue
Write-Host ""
Write-Host "exit code: $($p.ExitCode)"
Get-ChildItem -Recurse .\test_refs_out -Include *.png,*.jpg,*.jpeg,*.webp -ErrorAction SilentlyContinue | Select-Object FullName, Length
Read-Host "Press Enter to close"
