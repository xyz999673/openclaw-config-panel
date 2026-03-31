#!/usr/bin/env python3
"""Installer for the OpenClaw configuration panel.

It auto-detects the target OpenClaw state directory, syncs the panel source
into `<state-dir>/workspace/tools/openclaw-config-panel`, and registers a
systemd unit so the panel can be restarted independently of the gateway.
"""
from __future__ import annotations

import argparse
import os
import pwd
import shutil
import subprocess
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_SERVICE_NAME = "openclaw-config-panel"


def detect_state_dir(explicit: str) -> Path:
    """Return the OpenClaw state directory by CLI input, env, or common paths."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    for env_key in ("OPENCLAW_STATE_DIR", "STATE_DIR"):
        value = str(os.environ.get(env_key) or "").strip()
        if value:
            candidates.append(Path(value).expanduser())
    candidates.extend(
        [
            Path.home() / ".openclaw",
            Path("/root/.openclaw"),
        ]
    )
    seen: set[str] = set()
    for candidate in candidates:
        resolved = candidate.expanduser()
        marker = str(resolved)
        if marker in seen:
            continue
        seen.add(marker)
        if (resolved / "openclaw.json").exists():
            return resolved.resolve()
    raise SystemExit("未找到 OpenClaw 状态目录，请使用 --state-dir 指定包含 openclaw.json 的目录。")


def detect_user_and_home(state_dir: Path, explicit_user: str) -> tuple[str, Path]:
    """Resolve the service user and HOME used for runtime/systemd execution."""
    if explicit_user:
        user = explicit_user
        home = Path(pwd.getpwnam(user).pw_dir).expanduser().resolve()
        return user, home
    stat_info = state_dir.stat()
    user = pwd.getpwuid(stat_info.st_uid).pw_name
    home = Path(pwd.getpwnam(user).pw_dir).expanduser().resolve()
    return user, home


def sync_tree(src: Path, dst: Path) -> None:
    """Copy the panel source tree into the managed install directory."""
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name in {"__pycache__", ".git"}:
            continue
        target = dst / item.name
        if item.resolve() == target.resolve():
            continue
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(item, target)


def render_service_file(
    *,
    service_name: str,
    user: str,
    install_dir: Path,
    state_dir: Path,
    home_dir: Path,
    host: str,
    port: int,
    base_path: str,
) -> str:
    path_value = os.environ.get("PATH") or "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/bin"
    lines = [
        "[Unit]",
        "Description=OpenClaw Config Panel",
        "After=network.target",
        "",
        "[Service]",
        "Type=simple",
        f"User={user}",
        f"WorkingDirectory={install_dir}",
        f"Environment=HOST={host}",
        f"Environment=PORT={port}",
        f"Environment=STATE_DIR={state_dir}",
        f"Environment=BASE_PATH={base_path}",
        f"Environment=OPENCLAW_HOME={home_dir}",
        f"Environment=OPENCLAW_STATE_DIR={state_dir}",
        f"Environment=PATH={path_value}",
        f"ExecStart={install_dir / 'start.sh'}",
        "Restart=always",
        "RestartSec=3",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ]
    return "\n".join(lines)


def write_service(service_name: str, content: str) -> Path:
    service_path = Path("/etc/systemd/system") / f"{service_name}.service"
    service_path.write_text(content, encoding="utf-8")
    return service_path


def run(*args: str) -> None:
    subprocess.run(list(args), check=True)


def normalize_base_path(value: str) -> str:
    raw = (value or "").strip()
    if not raw or raw == "/":
        return ""
    if not raw.startswith("/"):
        raw = f"/{raw}"
    return raw.rstrip("/")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install OpenClaw config panel")
    parser.add_argument("--state-dir", default="", help="OpenClaw 状态目录，需包含 openclaw.json")
    parser.add_argument("--install-dir", default="", help="面板安装目录，默认 <state-dir>/workspace/tools/openclaw-config-panel")
    parser.add_argument("--service-name", default=DEFAULT_SERVICE_NAME, help="systemd 服务名，默认 openclaw-config-panel")
    parser.add_argument("--user", default="", help="运行面板的系统用户，默认取 state-dir 所有者")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=5711, help="监听端口，默认 5711")
    parser.add_argument("--base-path", default="", help="可选 base path，例如 /xyz/api/config")
    parser.add_argument("--reset-auth", action="store_true", help="删除旧的面板登录配置，强制重新初始化")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    state_dir = detect_state_dir(args.state_dir)
    user, home_dir = detect_user_and_home(state_dir, args.user)
    install_dir = Path(args.install_dir).expanduser().resolve() if args.install_dir else (state_dir / "workspace" / "tools" / "openclaw-config-panel").resolve()
    install_dir.parent.mkdir(parents=True, exist_ok=True)
    sync_tree(PROJECT_DIR, install_dir)
    for script_name in ("start.sh", "restart.sh", "install.sh", "install_panel.py", "restart_panel.py", "codex_job_runner.py", "server.py"):
        script_path = install_dir / script_name
        if script_path.exists():
            script_path.chmod(0o755)
    service_content = render_service_file(
        service_name=args.service_name,
        user=user,
        install_dir=install_dir,
        state_dir=state_dir,
        home_dir=home_dir,
        host=args.host.strip() or "127.0.0.1",
        port=args.port,
        base_path=normalize_base_path(args.base_path),
    )
    service_path = write_service(args.service_name, service_content)
    if args.reset_auth:
        auth_path = state_dir / "config-panel-auth.json"
        if auth_path.exists():
            auth_path.unlink()
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", f"{args.service_name}.service")
    run("systemctl", "restart", f"{args.service_name}.service")
    print(f"installed_dir={install_dir}")
    print(f"service={service_path}")
    print(f"state_dir={state_dir}")
    print(f"user={user}")
    print(f"url=http://{args.host}:{args.port}{normalize_base_path(args.base_path) or '/'}")
    if args.reset_auth:
        print("auth=reset")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
