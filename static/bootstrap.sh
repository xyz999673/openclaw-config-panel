#!/usr/bin/env bash
set -euo pipefail

PANEL_URL="${PANEL_URL:-__PANEL_URL__}"
ARCHIVE_URL="${PANEL_URL%/}/install/archive.tar.gz"
WORKDIR="$(mktemp -d)"

cleanup() {
  rm -rf "${WORKDIR}"
}
trap cleanup EXIT

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "缺少命令: $1" >&2
    exit 1
  }
}

need_cmd curl
need_cmd tar
need_cmd python3

echo "[openclaw-config-panel] downloading from ${ARCHIVE_URL}"
curl -fsSL "${ARCHIVE_URL}" -o "${WORKDIR}/panel.tar.gz"
tar -xzf "${WORKDIR}/panel.tar.gz" -C "${WORKDIR}"

PANEL_DIR="${WORKDIR}/openclaw-config-panel"
if [[ ! -x "${PANEL_DIR}/install.sh" ]]; then
  chmod +x "${PANEL_DIR}/install.sh" "${PANEL_DIR}/install_panel.py" "${PANEL_DIR}/start.sh" || true
fi

if [[ "$#" -eq 0 ]]; then
  echo "[openclaw-config-panel] installing with auto-detect"
else
  echo "[openclaw-config-panel] installing with args: $*"
fi

exec "${PANEL_DIR}/install.sh" "$@"
