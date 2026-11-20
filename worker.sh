#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export GFLOW_CLI_HOME="$PWD/gflow_home"
export GFLOW_CLI_FLOW_HOST=flow.google.com
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
if [[ ! -x venv/bin/python ]]; then
  echo "Run ./install.sh first." >&2
  exit 1
fi
if [[ ! -f worker_config.json ]]; then
  echo "Missing worker_config.json. Run ./install.sh first." >&2
  exit 1
fi
./venv/bin/python -c 'from worker import load_config; load_config()'
while true; do
  ./venv/bin/python worker.py
  echo "Worker exited; restarting in 10 seconds. Press Ctrl+C to stop."
  sleep 10
done
