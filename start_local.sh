#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec caffeinate -i ./venv/bin/python studio_launcher.py --local
