#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export GFLOW_CLI_HOME="$PWD/gflow_home"
export GFLOW_CLI_FLOW_HOST=flow.google.com
export GFLOW_CLI_DEBUG_TRACEBACK=1
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export COLUMNS=200
refs=()
while IFS= read -r -d '' file; do
  refs+=("$file")
  [[ ${#refs[@]} -ge 2 ]] && break
done < <(find test_pro_out worker_out -type f \( -iname '*.jpg' -o -iname '*.png' \) ! -path '*/refs/*' -print0 2>/dev/null)
if [[ ${#refs[@]} -lt 2 ]]; then
  echo "Need 2 images in test_pro_out or worker_out; run ./test_pro.sh first." >&2
  exit 1
fi
args=()
for file in "${refs[@]}"; do args+=(--ref "$file"); done
set +e
./venv/bin/python gflow_ext.py -v image i2i --model nano-pro --aspect 1:1 -n 2 \
  "${args[@]}" --out test_refs_out -- \
  'combine both images into one scene, watercolor style. Сгенерируй в модели №1' 2>&1 | tee test_refs.log
result=${PIPESTATUS[0]}
set -e
echo "Exit code: $result; output: test_refs_out; log: test_refs.log"
exit "$result"
