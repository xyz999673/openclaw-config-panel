#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-5711}"
STATE_DIR="${STATE_DIR:-/root/.openclaw}"
BASE_PATH="${BASE_PATH:-}"
OPENCLAW_HOME="${OPENCLAW_HOME:-$(cd "$(dirname "${STATE_DIR}")" && pwd)}"

export OPENCLAW_HOME
export OPENCLAW_STATE_DIR="${STATE_DIR}"

exec python3 "${ROOT_DIR}/server.py" \
  --host "${HOST}" \
  --port "${PORT}" \
  --base-path "${BASE_PATH}" \
  --state-dir "${STATE_DIR}"
