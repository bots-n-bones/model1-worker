# Step 2: one-time Google sign-in for the worker's own Chrome profile.
# A Chrome window opens: sign in to the Google account with the AI subscription.
# The window closes by itself once the Flow session is detected.
# The region block (RU/BY) is bypassed automatically by gflow_ext.py - no extension needed.
Set-Location -LiteralPath $PSScriptRoot
$env:GFLOW_CLI_HOME = Join-Path $PSScriptRoot "gflow_home"
$env:GFLOW_CLI_FLOW_HOST = "flow.google.com"
$env:PYTHONUTF8 = "1"
.\venv\Scripts\python.exe gflow_ext.py auth login --browser chrome
Read-Host "Press Enter to close"
