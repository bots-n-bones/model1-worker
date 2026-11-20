#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This installer is for macOS." >&2
  exit 1
fi

if [[ -n "${PYTHON:-}" ]]; then
  python="$PYTHON"
else
  python=""
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
      python="$(command -v "$candidate")"
      break
    fi
  done
fi
if [[ -z "$python" ]] || ! "$python" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  echo "Python 3.11+ is required. Install it with: brew install python@3.12" >&2
  exit 1
fi

if [[ ! -x venv/bin/python ]]; then
  "$python" -m venv venv
fi
venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install 'gflow-cli==0.103.0' httpx pillow

if [[ ! -f worker_config.json ]]; then
  venv/bin/python - <<'PY'
import json
import secrets
from pathlib import Path

config = json.loads(Path('worker_config.example.json').read_text(encoding='utf-8'))
config['worker_key'] = 'm1w_' + secrets.token_urlsafe(32)
Path('worker_config.json').write_text(json.dumps(config, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
PY
  chmod 600 worker_config.json
fi

echo "Installed. Edit worker_config.json and set your site's HTTPS endpoint."
echo "Then run ./login.sh and ./test_pro.sh."
