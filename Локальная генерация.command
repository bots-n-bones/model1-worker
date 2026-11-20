#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
if [[ ! -x venv/bin/python ]]; then
  echo "Сначала запустите ./install.sh"
  read -r -p "Нажмите Enter для выхода..." _
  exit 1
fi
./venv/bin/python local_app.py
read -r -p "Нажмите Enter, чтобы закрыть окно..." _
