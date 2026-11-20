#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x venv/bin/python ]]; then
  echo "Сначала запустите ./install.sh" >&2
  exit 1
fi
if [[ ! -f worker_config.json ]]; then
  echo "Нет worker_config.json" >&2
  exit 1
fi
if curl -fsS http://127.0.0.1:8765/healthz >/dev/null 2>&1; then
  echo "Локальный сайт уже работает: http://127.0.0.1:8766/" >&2
  exit 1
fi
./venv/bin/python site/local_server.py &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true' EXIT INT TERM
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS http://127.0.0.1:8765/healthz >/dev/null 2>&1; then break; fi
  sleep 1
done
if ! curl -fsS http://127.0.0.1:8765/healthz >/dev/null 2>&1; then
  echo "Локальный сайт не запустился" >&2
  exit 1
fi
open 'http://127.0.0.1:8766/'
echo "Сайт открыт. Остановить сайт и исполнитель: Ctrl+C"
./worker.sh
