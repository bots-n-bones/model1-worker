#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export GFLOW_CLI_HOME="$PWD/gflow_home"
export GFLOW_CLI_FLOW_HOST=flow.google.com
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
job_id="${1:-}"
index="${2:-0}"
if [[ -z "$job_id" ]]; then
  job_id="$(ls -td worker_out/m1_*/run.log 2>/dev/null | head -1 | xargs -I '{}' dirname '{}' | xargs -I '{}' basename '{}' || true)"
fi
if [[ -z "$job_id" ]]; then
  echo "No previous worker job found; pass a job ID." >&2
  exit 1
fi
./venv/bin/python worker.py upscale-test "$job_id" "$index" 2>&1 | tee test_2k.log
