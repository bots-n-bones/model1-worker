#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if ! curl -fsS http://127.0.0.1:8765/healthz >/dev/null; then
  echo "Сначала запустите локальный сайт: ./start_site.sh" >&2
  exit 1
fi
if [[ ! -f site/local_data/access.json ]]; then
  echo "Сначала создайте пароль: ./venv/bin/python site/set_password.py" >&2
  exit 1
fi
exec /usr/bin/ssh -T -o BatchMode=yes -o ConnectTimeout=8 \
  -o StrictHostKeyChecking=accept-new -o ForwardAgent=no -o IdentityAgent=none \
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=20 -o ServerAliveCountMax=3 \
  -R 80:127.0.0.1:8765 nokey@localhost.run -- --inject-http-proxy-headers "$@"
