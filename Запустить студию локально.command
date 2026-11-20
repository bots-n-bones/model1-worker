#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
if [[ ! -x venv/bin/python ]]; then
  echo "Сначала запустите ./install.sh"
  read -r -p "Нажмите Enter для выхода..." _
  exit 1
fi
./start_local.sh
echo
read -r -p "Студия остановлена. Нажмите Enter, чтобы закрыть окно..." _
