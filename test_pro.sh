#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export GFLOW_CLI_HOME="$PWD/gflow_home"
export GFLOW_CLI_FLOW_HOST=flow.google.com
export GFLOW_CLI_DEBUG_TRACEBACK=1
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export COLUMNS=200
set +e
./venv/bin/python gflow_ext.py -v image t2i --model nano-pro --aspect 16:9 -n 2 \
  --out test_pro_out -- 'a ginger kitten sitting on a windowsill, soft morning light' 2>&1 | tee test_pro.log
result=${PIPESTATUS[0]}
set -e
echo "Exit code: $result; output: test_pro_out; log: test_pro.log"
exit "$result"
