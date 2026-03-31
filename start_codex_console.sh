#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-5712}"
STATE_DIR="${STATE_DIR:-/root/.openclaw}"
BASE_PATH="${BASE_PATH:-/xyz/codex}"

exec python3 "${ROOT_DIR}/codex_console_server.py" \
  --host "${HOST}" \
  --port "${PORT}" \
  --base-path "${BASE_PATH}" \
  --state-dir "${STATE_DIR}"
