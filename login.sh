#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export GFLOW_CLI_HOME="$PWD/gflow_home"
export GFLOW_CLI_FLOW_HOST=flow.google.com
export PYTHONUTF8=1
if [[ ! -d '/Applications/Google Chrome.app' && ! -d "$HOME/Applications/Google Chrome.app" ]]; then
  echo "Google Chrome is required for sign-in. Install it, then run ./login.sh again." >&2
  exit 1
fi
exec ./venv/bin/python gflow_ext.py auth login --browser chrome
