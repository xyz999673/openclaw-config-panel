#!/usr/bin/env python3
"""Safe restart helper for the configuration panel systemd or standalone process."""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
SERVER_SCRIPT = (ROOT_DIR / "server.py").resolve()
START_SCRIPT = (ROOT_DIR / "start.sh").resolve()
DEFAULT_STATE_DIR = Path("/root/.openclaw")
PANEL_PIDFILE_TEMPLATE = "openclaw-config-panel-{port}.pid"


def _panel_pidfile_path(state_dir: Path, port: int) -> Path:
    return state_dir / PANEL_PIDFILE_TEMPLATE.format(port=port)


def _read_process_cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [item for item in raw.decode("utf-8", errors="ignore").split("\x00") if item]


def _extract_cmd_option(argv: list[str], name: str) -> str:
    prefix = f"{name}="
    for index, item in enumerate(argv):
        if item == name and index + 1 < len(argv):
            return str(argv[index + 1] or "").strip()
        if item.startswith(prefix):
            return str(item[len(prefix):] or "").strip()
    return ""


def _same_path(left: str, right: Path) -> bool:
    if not left:
        return False
    try:
        return Path(left).expanduser().resolve() == right.expanduser().resolve()
    except OSError:
        return False


def _is_matching_panel_process(pid: int, *, expected_state_dir: Path, expected_port: int) -> bool:
    argv = _read_process_cmdline(pid)
    if not argv:
        return False
    if not any(_same_path(item, SERVER_SCRIPT) for item in argv):
        return False
    port_value = _extract_cmd_option(argv, "--port")
    if port_value and port_value != str(expected_port):
        return False
    state_dir_value = _extract_cmd_option(argv, "--state-dir")
    if state_dir_value and not _same_path(state_dir_value, expected_state_dir):
        return False
    return True


def _read_pidfile_pid(state_dir: Path, port: int) -> int:
    pidfile_path = _panel_pidfile_path(state_dir, port)
    if not pidfile_path.exists():
        return 0
    try:
        raw = pidfile_path.read_text(encoding="utf-8").strip()
    except OSError:
        return 0
    return int(raw) if raw.isdigit() else 0


def _iter_listening_socket_inodes(port: int) -> set[str]:
    inodes: set[str] = set()
    for proc_path in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        try:
            lines = proc_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines[1:]:
            parts = line.split()
            if len(parts) < 10:
                continue
            local_address = parts[1]
            state = parts[3]
            inode = parts[9]
            if state != "0A":
                continue
            try:
                local_port = int(local_address.rsplit(":", 1)[1], 16)
            except (IndexError, ValueError):
                continue
            if local_port == port:
                inodes.add(inode)
    return inodes


def _find_pids_by_socket_inodes(inodes: set[str]) -> list[int]:
    if not inodes:
        return []
    results: list[int] = []
    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.is_dir() or not proc_dir.name.isdigit():
            continue
        pid = int(proc_dir.name)
        fd_dir = proc_dir / "fd"
        try:
            fd_entries = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd_entry in fd_entries:
            try:
                target = os.readlink(fd_entry)
            except OSError:
                continue
            if not target.startswith("socket:["):
                continue
            inode = target[8:-1]
            if inode in inodes:
                results.append(pid)
                break
    return sorted(set(results))


def _find_panel_pids_by_port(port: int, state_dir: Path) -> list[int]:
    candidates = _find_pids_by_socket_inodes(_iter_listening_socket_inodes(port))
    return [pid for pid in candidates if _is_matching_panel_process(pid, expected_state_dir=state_dir, expected_port=port)]


def _wait_for_pid_exit(pid: int, timeout_seconds: float) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.1)
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    return False


def _wait_for_port_listener(port: int, state_dir: Path, timeout_seconds: float) -> int:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        pids = _find_panel_pids_by_port(port, state_dir)
        if pids:
            return pids[0]
        time.sleep(0.15)
    pids = _find_panel_pids_by_port(port, state_dir)
    return pids[0] if pids else 0


def _terminate_existing_panel(state_dir: Path, port: int, timeout_seconds: float) -> int:
    pid = _read_pidfile_pid(state_dir, port)
    if pid <= 0 or not _is_matching_panel_process(pid, expected_state_dir=state_dir, expected_port=port):
        candidates = _find_panel_pids_by_port(port, state_dir)
        pid = candidates[0] if candidates else 0
    if pid <= 0:
        return 0
    os.kill(pid, signal.SIGTERM)
    if _wait_for_pid_exit(pid, timeout_seconds):
        return pid
    os.kill(pid, signal.SIGKILL)
    if not _wait_for_pid_exit(pid, timeout_seconds):
        raise RuntimeError(f"面板进程停止失败，pid={pid}")
    return pid


def _normalize_service_name(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return raw if raw.endswith(".service") else f"{raw}.service"


def _systemd_unit_loaded(unit_name: str) -> bool:
    if not unit_name:
        return False
    result = subprocess.run(
        ["systemctl", "show", unit_name, "--property=LoadState", "--value"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "loaded"


def _restart_with_systemd(unit_name: str) -> None:
    subprocess.run(["systemctl", "restart", unit_name], check=True)


def _spawn_panel(host: str, port: int, state_dir: Path, base_path: str) -> None:
    env = os.environ.copy()
    env["HOST"] = host
    env["PORT"] = str(port)
    env["STATE_DIR"] = str(state_dir)
    env["BASE_PATH"] = base_path
    subprocess.Popen(
        [str(START_SCRIPT)],
        cwd=str(ROOT_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safely restart OpenClaw config panel")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host, default 127.0.0.1")
    parser.add_argument("--port", type=int, default=5711, help="Bind port, default 5711")
    parser.add_argument("--base-path", default="", help="Optional base path")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR), help="OpenClaw state dir")
    parser.add_argument("--service-name", default="openclaw-config-panel", help="systemd service name if installed")
    parser.add_argument("--no-systemd", action="store_true", help="Do not use systemd even if service exists")
    parser.add_argument("--stop-timeout", type=float, default=8.0, help="Seconds to wait for old process to stop")
    parser.add_argument("--start-timeout", type=float, default=12.0, help="Seconds to wait for new process to listen")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    state_dir = Path(args.state_dir).expanduser().resolve()
    unit_name = _normalize_service_name(args.service_name)
    if not args.no_systemd and _systemd_unit_loaded(unit_name):
        _restart_with_systemd(unit_name)
        pid = _wait_for_port_listener(args.port, state_dir, args.start_timeout)
        if pid <= 0:
            raise SystemExit("systemd 已重启面板，但在超时时间内未等到监听端口。")
        print(f"[openclaw-config-panel] restarted via systemd: pid={pid}, unit={unit_name}")
        return 0

    old_pid = _terminate_existing_panel(state_dir, args.port, args.stop_timeout)
    _spawn_panel(args.host, args.port, state_dir, args.base_path)
    new_pid = _wait_for_port_listener(args.port, state_dir, args.start_timeout)
    if new_pid <= 0:
        raise SystemExit("面板已尝试重启，但在超时时间内未等到监听端口。")
    print(f"[openclaw-config-panel] restarted: old_pid={old_pid or '-'} new_pid={new_pid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
