#!/usr/bin/env python3
"""HTTP server for the OpenClaw configuration panel.

This module serves the panel SPA, persists single-user auth/session state,
bridges panel actions into OpenClaw config writes, and optionally exposes the
embedded Codex console endpoints used by the companion UI.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import copy
import hashlib
import hmac
import io
import json
import mimetypes
import os
import pty
import re
import select
import secrets
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import traceback
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from urllib.parse import parse_qs, quote, urlparse

from openclaw_config_manager import (
    ConfigError,
    DEFAULT_STATE_DIR,
    OPENAI_CODEX_PROVIDER,
    apply_config,
    apply_agent_config,
    apply_store_config,
    build_openclaw_env,
    delete_preset,
    derive_openclaw_home,
    find_openclaw_bin,
    get_openclaw_status,
    get_runtime_auth_status,
    load_current_config,
    load_panel_store,
    load_presets,
    refresh_provider_available_models,
    reorder_provider_records,
    restart_openclaw,
    save_runtime_auth_config,
    save_channel_record,
    save_model_catalog,
    save_preset,
    save_agent_record,
    save_provider_record,
    delete_agent_record,
    delete_channel_record,
    delete_provider_record,
    set_selected_provider,
)
from ccswitch_import import (
    apply_ccswitch_provider_import,
    preview_ccswitch_provider_import,
)
from panel_codex_runtime import (
    codex_job_dir,
    codex_job_request_path,
    codex_job_unit_name,
    is_systemd_unit_running,
    load_codex_store as runtime_load_codex_store,
    reconcile_codex_store as runtime_reconcile_codex_store,
    save_codex_store as runtime_save_codex_store,
    stat_codex_store_mtime_ns,
)


STATIC_DIR = Path(__file__).resolve().parent / "static"
SESSION_COOKIE_NAME = "openclaw_panel_session"
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
CODEX_SESSION_COOKIE_NAME = "openclaw_panel_codex_session"
CODEX_SESSION_TTL_SECONDS = 6 * 60 * 60
CAPTCHA_TTL_SECONDS = 5 * 60
AUTH_CONFIG_FILENAME = "config-panel-auth.json"
CODEX_AUTH_CONFIG_FILENAME = "config-panel-codex-auth.json"
CODEX_STORE_FILENAME = "config-panel-codex-store.json"
SESSION_STORE_FILENAME = "config-panel-sessions.json"
CODEX_THREAD_PAGE_SIZE = 100
CODEX_MAX_THREAD_PAGE_SIZE = 200
CODEX_MAX_COMMAND_OUTPUT = 12000
CODEX_MAX_ATTACHMENTS = 6
CODEX_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
OAUTH_LOGIN_OUTPUT_MAX_CHARS = 32000
# Allow open-source users to keep Codex history outside the default root-owned
# location when they deploy the optional console under another account.
CODEX_HISTORY_ROOT = Path(str(os.environ.get("CODEX_HISTORY_ROOT") or "/root/.codex/sessions"))
CODEX_ORPHANED_JOB_NOTICE = "上一次 Codex 任务在面板重启或进程退出时状态丢失，若未看到完整回复，请重新发送。"
CODEX_CANCELLED_JOB_NOTICE = "已手动中断当前 Codex 任务。"
CODEX_FORCE_CANCELLED_JOB_NOTICE = "已强制停止当前 Codex 任务。"
PANEL_PIDFILE_TEMPLATE = "openclaw-config-panel-{port}.pid"


def hash_password(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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


def _is_same_path(left: str, right: Path) -> bool:
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
    server_script = Path(__file__).resolve()
    if not any(_is_same_path(item, server_script) for item in argv):
        return False
    port_value = _extract_cmd_option(argv, "--port")
    if port_value and port_value != str(expected_port):
        return False
    state_dir_value = _extract_cmd_option(argv, "--state-dir")
    if state_dir_value and not _is_same_path(state_dir_value, expected_state_dir):
        return False
    return True


def _cleanup_panel_pidfile(pidfile_path: Path) -> None:
    try:
        if not pidfile_path.exists():
            return
        raw = pidfile_path.read_text(encoding="utf-8").strip()
        if raw == str(os.getpid()):
            pidfile_path.unlink(missing_ok=True)
    except OSError:
        pass


def _prepare_panel_pidfile(state_dir: Path, port: int) -> Path:
    pidfile_path = _panel_pidfile_path(state_dir, port)
    if pidfile_path.exists():
        raw = ""
        try:
            raw = pidfile_path.read_text(encoding="utf-8").strip()
        except OSError:
            raw = ""
        existing_pid = int(raw) if raw.isdigit() else 0
        if existing_pid > 0 and _is_matching_panel_process(existing_pid, expected_state_dir=state_dir, expected_port=port):
            raise SystemExit(
                f"检测到面板已在运行（pid={existing_pid}, pidfile={pidfile_path}），请先停止旧进程后再启动。"
            )
        try:
            pidfile_path.unlink(missing_ok=True)
        except OSError:
            pass
    pidfile_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    return pidfile_path

def _auth_config_path(state_dir: Path) -> Path:
    return state_dir / AUTH_CONFIG_FILENAME


def _codex_auth_config_path(state_dir: Path) -> Path:
    return state_dir / CODEX_AUTH_CONFIG_FILENAME


def _codex_store_path(state_dir: Path) -> Path:
    return state_dir / CODEX_STORE_FILENAME


def _session_store_path(state_dir: Path) -> Path:
    return state_dir / SESSION_STORE_FILENAME


def _codex_upload_dir(state_dir: Path) -> Path:
    return state_dir / "panel-codex-uploads"


def load_auth_config(state_dir: Path) -> dict:
    path = _auth_config_path(state_dir)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"鉴权配置损坏：{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("鉴权配置格式错误。")
    username = validate_auth_username(str(raw.get("username") or ""))
    password_hash = str(raw.get("password_hash") or "").strip().lower()
    if not password_hash:
        raise ConfigError("鉴权配置缺少密码哈希。")
    return {
        "version": int(raw.get("version") or 1),
        "username": username,
        "password_hash": password_hash,
        "created_at": raw.get("created_at") or "",
    }


def save_auth_config(state_dir: Path, username: str, password_hash: str) -> dict:
    record = {
        "version": 1,
        "username": username,
        "password_hash": password_hash,
        "created_at": int(time.time()),
    }
    _auth_config_path(state_dir).write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return record


def load_codex_auth_config(state_dir: Path) -> dict:
    path = _codex_auth_config_path(state_dir)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Codex 鉴权配置损坏：{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("Codex 鉴权配置格式错误。")
    password_hash = str(raw.get("password_hash") or "").strip().lower()
    if not password_hash:
        raise ConfigError("Codex 鉴权配置缺少密码哈希。")
    return {
        "version": int(raw.get("version") or 1),
        "password_hash": password_hash,
        "created_at": raw.get("created_at") or "",
    }


def save_codex_auth_config(state_dir: Path, password_hash: str) -> dict:
    record = {
        "version": 1,
        "password_hash": password_hash,
        "created_at": int(time.time()),
    }
    _codex_auth_config_path(state_dir).write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return record


def load_codex_store(state_dir: Path) -> dict:
    try:
        return runtime_load_codex_store(state_dir)
    except RuntimeError as exc:
        raise ConfigError(str(exc)) from exc


def save_codex_store(state_dir: Path, payload: dict) -> None:
    runtime_save_codex_store(state_dir, payload)


def reconcile_codex_store(payload: dict) -> tuple[dict, bool]:
    return runtime_reconcile_codex_store(
        payload,
        interrupted_notice_text=CODEX_ORPHANED_JOB_NOTICE,
        job_running_checker=lambda job: is_systemd_unit_running(str(job.get("runner_unit") or "")),
    )


def _normalize_session_records(raw: object) -> dict[str, dict]:
    if not isinstance(raw, dict):
        return {}
    records: dict[str, dict] = {}
    for session_id, session in raw.items():
        if not isinstance(session_id, str) or not session_id.strip() or not isinstance(session, dict):
            continue
        records[session_id] = {
            "username": str(session.get("username") or "").strip(),
            "created_at": float(session.get("created_at") or 0),
            "last_seen_at": float(session.get("last_seen_at") or 0),
            "expires_at": float(session.get("expires_at") or 0),
            "remote_addr": str(session.get("remote_addr") or "").strip(),
            "user_agent": str(session.get("user_agent") or "").strip()[:320],
        }
    return records


def _normalize_codex_session_records(raw: object) -> dict[str, dict]:
    if not isinstance(raw, dict):
        return {}
    records: dict[str, dict] = {}
    for session_id, session in raw.items():
        if not isinstance(session_id, str) or not session_id.strip() or not isinstance(session, dict):
            continue
        records[session_id] = {
            "created_at": float(session.get("created_at") or 0),
            "last_seen_at": float(session.get("last_seen_at") or 0),
            "expires_at": float(session.get("expires_at") or 0),
            "remote_addr": str(session.get("remote_addr") or "").strip(),
            "user_agent": str(session.get("user_agent") or "").strip()[:320],
        }
    return records


def load_session_store(state_dir: Path) -> dict:
    path = _session_store_path(state_dir)
    if not path.exists():
        return {"version": 1, "sessions": {}, "codex_sessions": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"会话存储损坏：{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("会话存储格式错误。")
    return {
        "version": 1,
        "sessions": _normalize_session_records(raw.get("sessions")),
        "codex_sessions": _normalize_codex_session_records(raw.get("codex_sessions")),
    }


def save_session_store(state_dir: Path, sessions: dict, codex_sessions: dict) -> None:
    payload = {
        "version": 1,
        "sessions": sessions,
        "codex_sessions": codex_sessions,
    }
    _session_store_path(state_dir).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def validate_auth_username(value: str) -> str:
    username = str(value or "").strip()
    if not username:
        raise ConfigError("用户名不能为空。")
    if len(username) < 3 or len(username) > 32:
        raise ConfigError("用户名长度需在 3 到 32 个字符之间。")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-@")
    if any(char not in allowed for char in username):
        raise ConfigError("用户名只允许字母、数字、点、下划线、中划线和 @。")
    return username


def validate_auth_password(value: str) -> str:
    password = str(value or "")
    if len(password) < 8:
        raise ConfigError("密码至少 8 位。")
    if len(password) > 128:
        raise ConfigError("密码不能超过 128 位。")
    return password


def generate_captcha_code(length: int = 5) -> str:
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def build_captcha_svg(code: str) -> str:
    width = 132
    height = 44
    palette = ("#60a5fa", "#34d399", "#f472b6", "#fbbf24", "#a78bfa")
    noise_lines = []
    for _ in range(6):
        x1 = secrets.randbelow(width)
        y1 = secrets.randbelow(height)
        x2 = secrets.randbelow(width)
        y2 = secrets.randbelow(height)
        color = secrets.choice(palette)
        noise_lines.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="{color}" stroke-opacity="0.28" stroke-width="1.4" />'
        )
    chars = []
    for index, char in enumerate(code):
        x = 18 + index * 22
        y = 29 + secrets.randbelow(7) - 3
        rotate = secrets.randbelow(25) - 12
        color = secrets.choice(palette)
        chars.append(
            f'<text x="{x}" y="{y}" fill="{color}" font-size="24" font-weight="700" '
            f'font-family="ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'transform="rotate({rotate} {x} {y})">{char}</text>'
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        '<rect width="100%" height="100%" rx="12" fill="#0f172a" />'
        '<rect x="0.5" y="0.5" width="131" height="43" rx="11.5" fill="none" stroke="rgba(148,163,184,0.28)" />'
        + "".join(noise_lines)
        + "".join(chars)
        + "</svg>"
    )


def normalize_base_path(value: str) -> str:
    raw = (value or "").strip()
    if not raw or raw == "/":
        return ""
    if not raw.startswith("/"):
        raw = f"/{raw}"
    return raw.rstrip("/")


def parse_timestamp(value: str | int | float | None) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def trim_output_text(value: str, limit: int = OAUTH_LOGIN_OUTPUT_MAX_CHARS) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[-limit:]


class PanelHandler(BaseHTTPRequestHandler):
    """Request handler for auth, store CRUD, apply actions, and static assets."""
    server_version = "OpenClawConfigPanel/1.0"

    def _cookie_path(self) -> str:
        return self.server.base_path or "/"

    def _parse_cookies(self) -> SimpleCookie:
        cookie = SimpleCookie()
        raw = self.headers.get("Cookie")
        if raw:
            cookie.load(raw)
        return cookie

    def _send_cookie(
        self,
        name: str,
        value: str,
        *,
        path: str,
        max_age: int | None = None,
        expires: str | None = None,
        http_only: bool = True,
    ) -> None:
        cookie = SimpleCookie()
        cookie[name] = value
        morsel = cookie[name]
        morsel["path"] = path
        morsel["samesite"] = "Lax"
        if http_only:
            morsel["httponly"] = True
        if max_age is not None:
            morsel["max-age"] = str(max_age)
        if expires is not None:
            morsel["expires"] = expires
        self.send_header("Set-Cookie", morsel.OutputString())

    def _cleanup_auth_state(self) -> None:
        now = time.time()
        changed = False
        with self.server.auth_lock:
            expired_sessions = [key for key, value in self.server.sessions.items() if value.get("expires_at", 0) <= now]
            for key in expired_sessions:
                self.server.sessions.pop(key, None)
                changed = True
            expired_codex_sessions = [key for key, value in self.server.codex_sessions.items() if value.get("expires_at", 0) <= now]
            for key in expired_codex_sessions:
                self.server.codex_sessions.pop(key, None)
                changed = True
            expired_captchas = [key for key, value in self.server.captchas.items() if value.get("expires_at", 0) <= now]
            for key in expired_captchas:
                self.server.captchas.pop(key, None)
        if changed:
            self._persist_session_store()

    def _auth_config(self) -> dict:
        return load_auth_config(self.server.state_dir)

    def _is_auth_initialized(self) -> bool:
        return _auth_config_path(self.server.state_dir).exists()

    def _create_session(self, username: str) -> str:
        session_id = secrets.token_urlsafe(32)
        now = time.time()
        with self.server.auth_lock:
            self.server.sessions[session_id] = {
                "username": username,
                "created_at": now,
                "last_seen_at": now,
                "expires_at": now + SESSION_TTL_SECONDS,
                "remote_addr": self._client_ip(),
                "user_agent": str(self.headers.get("User-Agent") or "").strip()[:320],
            }
        self._persist_session_store()
        return session_id

    def _clear_session(self) -> None:
        session_id = self._current_session_id()
        if not session_id:
            return
        changed = False
        with self.server.auth_lock:
            if self.server.sessions.pop(session_id, None) is not None:
                changed = True
        if changed:
            self._persist_session_store()

    def _current_session_id(self) -> str:
        cookies = self._parse_cookies()
        morsel = cookies.get(SESSION_COOKIE_NAME)
        return morsel.value if morsel else ""

    def _client_ip(self) -> str:
        forwarded_for = str(self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if forwarded_for:
            return forwarded_for
        return str((self.client_address or ("", 0))[0] or "").strip()

    def _mask_session_id(self, session_id: str) -> str:
        value = str(session_id or "")
        if len(value) <= 14:
            return value
        return f"{value[:8]}…{value[-6:]}"

    def _session_list_payload(self) -> list[dict]:
        self._cleanup_auth_state()
        current_session_id = self._current_session_id()
        sessions: list[dict] = []
        with self.server.auth_lock:
            for session_id, session in self.server.sessions.items():
                sessions.append(
                    {
                        "id": self._mask_session_id(session_id),
                        "is_current": session_id == current_session_id,
                        "username": str(session.get("username") or ""),
                        "created_at": float(session.get("created_at") or 0),
                        "last_seen_at": float(session.get("last_seen_at") or 0),
                        "expires_at": float(session.get("expires_at") or 0),
                        "remote_addr": str(session.get("remote_addr") or ""),
                        "user_agent": str(session.get("user_agent") or ""),
                    }
                )
        sessions.sort(key=lambda item: (item.get("is_current", False), item.get("last_seen_at", 0)), reverse=True)
        return sessions

    def _codex_auth_config(self) -> dict:
        return load_codex_auth_config(self.server.state_dir)

    def _is_codex_auth_initialized(self) -> bool:
        return _codex_auth_config_path(self.server.state_dir).exists()

    def _create_codex_session(self) -> str:
        session_id = secrets.token_urlsafe(32)
        now = time.time()
        with self.server.auth_lock:
            self.server.codex_sessions[session_id] = {
                "created_at": now,
                "last_seen_at": now,
                "expires_at": now + CODEX_SESSION_TTL_SECONDS,
                "remote_addr": self._client_ip(),
                "user_agent": str(self.headers.get("User-Agent") or "").strip()[:320],
            }
        self._persist_session_store()
        return session_id

    def _clear_codex_session(self) -> None:
        session_id = self._current_codex_session_id()
        if not session_id:
            return
        changed = False
        with self.server.auth_lock:
            if self.server.codex_sessions.pop(session_id, None) is not None:
                changed = True
        if changed:
            self._persist_session_store()

    def _current_codex_session_id(self) -> str:
        cookies = self._parse_cookies()
        morsel = cookies.get(CODEX_SESSION_COOKIE_NAME)
        return morsel.value if morsel else ""

    def _is_codex_authenticated(self) -> bool:
        self._cleanup_auth_state()
        if not self._is_codex_auth_initialized():
            return False
        session_id = self._current_codex_session_id()
        if not session_id:
            return False
        changed = False
        should_persist = False
        with self.server.auth_lock:
            session = self.server.codex_sessions.get(session_id)
            if not session:
                return False
            if session.get("expires_at", 0) <= time.time():
                self.server.codex_sessions.pop(session_id, None)
                changed = True
            else:
                now = time.time()
                should_persist = now - float(session.get("last_seen_at") or 0) >= 60
                session["last_seen_at"] = now
                session["expires_at"] = now + CODEX_SESSION_TTL_SECONDS
        if changed:
            self._persist_session_store()
            return False
        if should_persist:
            self._persist_session_store()
        return True

    def _codex_auth_state_payload(self) -> dict:
        logged_in = self._is_codex_authenticated()
        expires_at = 0.0
        if logged_in:
            session_id = self._current_codex_session_id()
            with self.server.auth_lock:
                expires_at = float((self.server.codex_sessions.get(session_id) or {}).get("expires_at") or 0)
        return {
            "initialized": self._is_codex_auth_initialized(),
            "logged_in": logged_in,
            "expires_at": expires_at or None,
        }

    def _query_params(self) -> dict[str, list[str]]:
        return parse_qs(urlparse(self.path).query, keep_blank_values=True)

    def _query_value(self, key: str) -> str:
        return str((self._query_params().get(key) or [""])[0] or "").strip()

    def _query_int_value(self, key: str, default: int | None = None) -> int | None:
        raw = self._query_value(key)
        if raw == "":
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(f"参数 {key} 必须是整数。") from exc

    def _persist_codex_store(self) -> None:
        save_codex_store(self.server.state_dir, self.server.codex_store)
        self.server.codex_store_mtime_ns = stat_codex_store_mtime_ns(self.server.state_dir)

    def _reload_codex_store(self, *, force: bool = False) -> None:
        latest_mtime_ns = stat_codex_store_mtime_ns(self.server.state_dir)
        current_deleted_threads = getattr(self.server, "codex_store", {}).get("deleted_threads") or {}
        if (
            not force
            and latest_mtime_ns
            and latest_mtime_ns <= int(getattr(self.server, "codex_store_mtime_ns", 0) or 0)
            and not current_deleted_threads
        ):
            return
        payload = load_codex_store(self.server.state_dir)
        normalized, changed = reconcile_codex_store(payload)
        if changed:
            save_codex_store(self.server.state_dir, normalized)
            latest_mtime_ns = stat_codex_store_mtime_ns(self.server.state_dir)
        with self.server.codex_lock:
            self.server.codex_store = normalized
            self.server.codex_jobs = self.server.codex_store.setdefault("jobs", {})
            self.server.codex_store_mtime_ns = latest_mtime_ns
        for deleted_thread_id in list((normalized.get("deleted_threads") or {}).keys()):
            try:
                self._delete_codex_thread_permanently(str(deleted_thread_id))
            except ConfigError:
                with self.server.codex_lock:
                    self._codex_deleted_threads_map().pop(str(deleted_thread_id), None)
                    self._persist_codex_store()

    def _persist_session_store(self) -> None:
        with self.server.auth_lock:
            save_session_store(
                self.server.state_dir,
                copy.deepcopy(self.server.sessions),
                copy.deepcopy(self.server.codex_sessions),
            )

    def _trim_codex_text(self, value: str, limit: int = CODEX_MAX_COMMAND_OUTPUT) -> str:
        text = str(value or "")
        if len(text) <= limit:
            return text
        return f"{text[:limit]}\n…[已截断 {len(text) - limit} 字符]"

    def _normalize_codex_message_text(self, value: str) -> str:
        text = str(value or "")
        text = re.sub(r"<image\b[^>]*>\s*</image>\s*", "", text, flags=re.IGNORECASE | re.MULTILINE)
        lines = [line for line in text.splitlines() if not re.match(r"^\s*</?image\b", line, flags=re.IGNORECASE)]
        return " ".join(" ".join(lines).split()).strip()

    def _message_has_image_markup(self, message: dict) -> bool:
        return bool(re.search(r"<image\b", str(message.get("text") or ""), flags=re.IGNORECASE))

    def _message_attachment_count(self, message: dict) -> int:
        return len(list(message.get("attachments") or []))

    def _message_id_looks_like_history(self, message: dict) -> bool:
        return str(message.get("id") or "").startswith("hist_")

    def _messages_are_equivalent(self, left: dict, right: dict) -> bool:
        if str(left.get("role") or "") != str(right.get("role") or ""):
            return False
        if str(left.get("event_type") or "") != str(right.get("event_type") or ""):
            return False
        left_role = str(left.get("role") or "")
        if left_role == "event" and str(left.get("event_type") or "") == "command":
            return False
        left_text = self._normalize_codex_message_text(str(left.get("text") or ""))
        right_text = self._normalize_codex_message_text(str(right.get("text") or ""))
        if not left_text or left_text != right_text:
            return False
        left_created = float(left.get("created_at") or 0)
        right_created = float(right.get("created_at") or 0)
        looks_like_store_history_mirror = self._message_id_looks_like_history(left) != self._message_id_looks_like_history(right)
        looks_like_image_mirror = (
            self._message_has_image_markup(left)
            or self._message_has_image_markup(right)
            or bool(self._message_attachment_count(left)) != bool(self._message_attachment_count(right))
        )
        allowed_delta = 15 if looks_like_store_history_mirror or looks_like_image_mirror else 3
        if left_created and right_created and abs(left_created - right_created) > allowed_delta:
            return False
        return True

    def _message_equivalence_signature(self, message: dict) -> tuple[str, str, str] | None:
        role = str(message.get("role") or "")
        event_type = str(message.get("event_type") or "")
        if role == "event" and event_type == "command":
            return None
        text = self._normalize_codex_message_text(str(message.get("text") or ""))
        if not text:
            return None
        return (role, event_type, text)

    def _merge_equivalent_messages(self, base_message: dict, overlay_message: dict) -> dict:
        merged = copy.deepcopy(base_message)
        overlay = copy.deepcopy(overlay_message)
        merged.update(overlay)
        base_attachments = list(base_message.get("attachments") or [])
        overlay_attachments = list(overlay_message.get("attachments") or [])
        if base_attachments or overlay_attachments:
            deduped: list[dict] = []
            seen: set[tuple[str, str, str]] = set()
            for item in [*base_attachments, *overlay_attachments]:
                key = (
                    str(item.get("kind") or ""),
                    str(item.get("name") or ""),
                    str(item.get("path") or ""),
                )
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(copy.deepcopy(item))
            merged["attachments"] = deduped
        base_text = self._normalize_codex_message_text(str(base_message.get("text") or ""))
        overlay_text = self._normalize_codex_message_text(str(overlay_message.get("text") or ""))
        if base_text and overlay_text == base_text:
            base_raw_text = str(base_message.get("text") or "")
            overlay_raw_text = str(overlay_message.get("text") or "")
            base_has_image_markup = self._message_has_image_markup(base_message)
            overlay_has_image_markup = self._message_has_image_markup(overlay_message)
            if base_has_image_markup and not overlay_has_image_markup:
                merged["text"] = overlay_raw_text or base_raw_text
            elif overlay_has_image_markup and not base_has_image_markup:
                merged["text"] = base_raw_text or overlay_raw_text
            else:
                merged["text"] = overlay_raw_text or base_raw_text
        merged["created_at"] = min(
            float(base_message.get("created_at") or 0) or float(overlay_message.get("created_at") or 0),
            float(overlay_message.get("created_at") or 0) or float(base_message.get("created_at") or 0),
        )
        return merged

    def _strip_history_messages(self, thread: dict) -> dict:
        sanitized = copy.deepcopy(thread)
        sanitized["messages"] = [
            copy.deepcopy(message)
            for message in list(sanitized.get("messages") or [])
            if not str(message.get("id") or "").startswith("hist_")
        ]
        return sanitized

    def _resolve_codex_managed_path(self, raw_path: str, root: Path) -> Path | None:
        normalized_path = str(raw_path or "").strip()
        if not normalized_path:
            return None
        try:
            resolved = Path(normalized_path).expanduser().resolve()
            root_resolved = root.expanduser().resolve()
            resolved.relative_to(root_resolved)
        except (OSError, ValueError):
            return None
        return resolved

    def _collect_codex_attachment_paths(self, threads: list[dict]) -> list[Path]:
        upload_root = _codex_upload_dir(self.server.state_dir)
        resolved_paths: list[Path] = []
        seen: set[str] = set()
        for thread in threads:
            for message in list(thread.get("messages") or []):
                for attachment in list(message.get("attachments") or []):
                    resolved = self._resolve_codex_managed_path(str((attachment or {}).get("path") or ""), upload_root)
                    if not resolved:
                        continue
                    key = str(resolved)
                    if key in seen:
                        continue
                    seen.add(key)
                    resolved_paths.append(resolved)
        return resolved_paths

    def _find_codex_history_paths(self, thread_ids: set[str]) -> list[Path]:
        normalized_thread_ids = {str(item or "").strip() for item in thread_ids if str(item or "").strip()}
        if not normalized_thread_ids or not CODEX_HISTORY_ROOT.exists():
            return []
        matched_paths: list[Path] = []
        seen_paths: set[str] = set()
        for session_path in CODEX_HISTORY_ROOT.rglob("rollout-*.jsonl"):
            thread = self._parse_real_codex_thread(session_path)
            if not thread or str(thread.get("id") or "").strip() not in normalized_thread_ids:
                continue
            resolved = session_path.resolve()
            key = str(resolved)
            if key in seen_paths:
                continue
            seen_paths.add(key)
            matched_paths.append(resolved)
        return matched_paths

    def _delete_codex_history_paths(self, paths: list[Path]) -> None:
        history_root = CODEX_HISTORY_ROOT.expanduser().resolve()
        for path in paths:
            resolved = self._resolve_codex_managed_path(str(path), history_root)
            if not resolved:
                continue
            try:
                resolved.unlink(missing_ok=True)
            except OSError:
                continue
            parent = resolved.parent
            while True:
                try:
                    parent.relative_to(history_root)
                except ValueError:
                    break
                if parent == history_root:
                    break
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent

    def _delete_codex_thread_permanently(self, thread_id: str) -> dict:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            raise ConfigError("缺少 thread_id。")

        history_thread = self._load_real_codex_thread(normalized_thread_id) or {}
        matched_threads: list[dict] = []
        matched_thread_ids: set[str] = set()
        history_thread_ids: set[str] = set()
        job_ids: set[str] = set()
        thread_previously_deleted = False

        with self.server.codex_lock:
            threads = self.server.codex_store.setdefault("threads", {})
            jobs = self.server.codex_store.setdefault("jobs", {})
            deleted_threads = self._codex_deleted_threads_map()
            thread_previously_deleted = normalized_thread_id in deleted_threads
            for store_thread_id, raw_thread in list(threads.items()):
                if not isinstance(raw_thread, dict):
                    continue
                raw_codex_thread_id = str(raw_thread.get("codex_thread_id") or "").strip()
                if store_thread_id != normalized_thread_id and raw_codex_thread_id != normalized_thread_id:
                    continue
                active_job_id = str(raw_thread.get("active_job_id") or "")
                if active_job_id:
                    active_job = jobs.get(active_job_id)
                    if active_job and str(active_job.get("status") or "") == "running":
                        raise ConfigError("当前会话有进行中的任务，不能删除。")
                    job_ids.add(active_job_id)
                matched_threads.append(copy.deepcopy(raw_thread))
                matched_thread_ids.add(str(store_thread_id))
                if raw_codex_thread_id:
                    history_thread_ids.add(raw_codex_thread_id)
            for job_id, raw_job in list(jobs.items()):
                if not isinstance(raw_job, dict):
                    continue
                if str(raw_job.get("thread_id") or "").strip() in matched_thread_ids:
                    job_ids.add(str(job_id))
            if history_thread:
                history_thread_ids.add(str(history_thread.get("id") or "").strip())
                history_thread_ids.add(str(history_thread.get("codex_thread_id") or "").strip())
        target_history_ids = {item for item in {normalized_thread_id, *history_thread_ids} if item}
        history_paths = self._find_codex_history_paths(target_history_ids)
        if not matched_threads and not history_thread and not history_paths and not thread_previously_deleted:
            raise ConfigError("Codex 会话不存在。")

        attachment_paths = self._collect_codex_attachment_paths(matched_threads)
        for attachment_path in attachment_paths:
            try:
                attachment_path.unlink(missing_ok=True)
            except OSError:
                continue

        for job_id in job_ids:
            if not job_id:
                continue
            try:
                shutil.rmtree(codex_job_dir(self.server.state_dir, job_id), ignore_errors=True)
            except OSError:
                pass

        self._delete_codex_history_paths(history_paths)

        with self.server.codex_lock:
            threads = self.server.codex_store.setdefault("threads", {})
            jobs = self.server.codex_store.setdefault("jobs", {})
            deleted_threads = self._codex_deleted_threads_map()
            for job_id in job_ids:
                if job_id:
                    jobs.pop(job_id, None)
            for matched_thread_key in matched_thread_ids:
                threads.pop(matched_thread_key, None)
            for candidate_thread_id in {normalized_thread_id, *matched_thread_ids, *history_thread_ids}:
                if candidate_thread_id:
                    deleted_threads.pop(candidate_thread_id, None)
                    self.server.codex_history_index.pop(candidate_thread_id, None)
            for deleted_path in history_paths:
                self.server.codex_history_cache.pop(str(deleted_path), None)
            self.server.codex_jobs = jobs
            self._persist_codex_store()

        return {
            "thread_id": normalized_thread_id,
            "deleted_store_threads": len(matched_thread_ids),
            "deleted_history_files": len(history_paths),
            "deleted_jobs": len([job_id for job_id in job_ids if job_id]),
            "deleted_attachments": len(attachment_paths),
        }

    def _codex_deleted_threads_map(self) -> dict[str, float]:
        deleted_threads = self.server.codex_store.setdefault("deleted_threads", {})
        if isinstance(deleted_threads, dict):
            return deleted_threads
        normalized: dict[str, float] = {}
        if isinstance(deleted_threads, list):
            for thread_id in deleted_threads:
                normalized_thread_id = str(thread_id or "").strip()
                if normalized_thread_id:
                    normalized[normalized_thread_id] = 0.0
        self.server.codex_store["deleted_threads"] = normalized
        return self.server.codex_store["deleted_threads"]

    def _codex_thread_is_deleted(self, *thread_ids: str) -> bool:
        deleted_threads = self._codex_deleted_threads_map()
        for thread_id in thread_ids:
            normalized_thread_id = str(thread_id or "").strip()
            if normalized_thread_id and normalized_thread_id in deleted_threads:
                return True
        return False

    def _overlay_thread_from_history(self, history_thread: dict, fallback_cwd: str = "") -> dict:
        return {
            "id": str(history_thread.get("id") or ""),
            "codex_thread_id": str(history_thread.get("codex_thread_id") or history_thread.get("id") or ""),
            "title": str(history_thread.get("title") or "未命名会话"),
            "cwd": str(fallback_cwd or history_thread.get("cwd") or "/root"),
            "created_at": float(history_thread.get("created_at") or 0) or time.time(),
            "updated_at": float(history_thread.get("updated_at") or 0) or time.time(),
            "messages": [],
            "active_job_id": "",
        }

    def _dedupe_codex_messages(self, messages: list[dict]) -> list[dict]:
        deduped: list[dict] = []
        signature_index: dict[tuple[str, str, str], list[int]] = {}
        for message in sorted([copy.deepcopy(item) for item in list(messages or [])], key=lambda item: float(item.get("created_at") or 0)):
            duplicate_index = -1
            signature = self._message_equivalence_signature(message)
            if signature:
                for idx in reversed(signature_index.get(signature) or []):
                    existing = deduped[idx]
                    if self._messages_are_equivalent(existing, message):
                        duplicate_index = idx
                        break
            if duplicate_index >= 0:
                deduped[duplicate_index] = self._merge_equivalent_messages(deduped[duplicate_index], message)
                continue
            deduped.append(message)
            if signature:
                signature_index.setdefault(signature, []).append(len(deduped) - 1)
        return deduped

    def _merge_codex_thread_with_history(self, thread: dict, history_thread: dict | None) -> dict:
        if not history_thread:
            return copy.deepcopy(thread)
        base = copy.deepcopy(history_thread)
        overlay = copy.deepcopy(thread)
        merged = copy.deepcopy(base)
        for key, value in overlay.items():
            if key == "messages":
                continue
            if key in {"title", "cwd", "codex_thread_id", "active_job_id"}:
                if value not in (None, "", []):
                    merged[key] = value
                continue
            if key in {"updated_at", "created_at"}:
                merged[key] = max(float(base.get(key) or 0), float(value or 0))
                continue
            merged[key] = value
        merged_messages: list[dict] = []
        index_by_id: dict[str, int] = {}
        for message in list(base.get("messages") or []):
            message_copy = copy.deepcopy(message)
            merged_messages.append(message_copy)
            message_id = str(message_copy.get("id") or "")
            if message_id:
                index_by_id[message_id] = len(merged_messages) - 1
        for message in list(overlay.get("messages") or []):
            message_copy = copy.deepcopy(message)
            message_id = str(message_copy.get("id") or "")
            if message_id and message_id in index_by_id:
                merged_messages[index_by_id[message_id]].update(message_copy)
            else:
                merged_messages.append(message_copy)
                if message_id:
                    index_by_id[message_id] = len(merged_messages) - 1
        merged_messages.sort(key=lambda item: float(item.get("created_at") or 0))
        merged["messages"] = self._dedupe_codex_messages(merged_messages)
        merged["created_at"] = min(
            float(base.get("created_at") or 0) or float(overlay.get("created_at") or 0),
            float(overlay.get("created_at") or 0) or float(base.get("created_at") or 0),
        )
        merged["updated_at"] = max(float(base.get("updated_at") or 0), float(overlay.get("updated_at") or 0))
        return merged

    def _codex_thread_summary(self, thread: dict) -> dict:
        messages = self._dedupe_codex_messages(list(thread.get("messages") or []))
        last_message = messages[-1] if messages else {}
        preview = str(last_message.get("text") or "").strip().replace("\n", " ")
        if len(preview) > 96:
            preview = f"{preview[:96]}…"
        active_job_id = str(thread.get("active_job_id") or "")
        active_job = self.server.codex_jobs.get(active_job_id) if active_job_id else None
        return {
            "id": str(thread.get("id") or ""),
            "codex_thread_id": str(thread.get("codex_thread_id") or ""),
            "title": str(thread.get("title") or "未命名会话"),
            "cwd": str(thread.get("cwd") or ""),
            "created_at": float(thread.get("created_at") or 0),
            "updated_at": float(thread.get("updated_at") or 0),
            "last_message_role": str(last_message.get("role") or ""),
            "last_message_preview": preview,
            "message_count": len(messages),
            "active_job": self._codex_job_payload(active_job) if active_job else None,
        }

    def _codex_job_payload(self, job: dict | None) -> dict | None:
        if not job:
            return None
        return {
            "id": str(job.get("id") or ""),
            "thread_id": str(job.get("thread_id") or ""),
            "status": str(job.get("status") or ""),
            "created_at": float(job.get("created_at") or 0),
            "updated_at": float(job.get("updated_at") or 0),
            "error": str(job.get("error") or ""),
            "assistant_text": str(job.get("assistant_text") or ""),
            "last_event_text": str(job.get("last_event_text") or ""),
            "commands": copy.deepcopy(job.get("commands") or []),
        }

    def _extract_codex_message_text(self, content: object) -> str:
        parts: list[str] = []
        if not isinstance(content, list):
            return ""
        for item in content:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if text:
                parts.append(text)
        return "\n".join(parts).strip()

    def _parse_real_codex_thread(self, session_path: Path) -> dict | None:
        try:
            stat = session_path.stat()
        except OSError:
            return None
        cache_key = str(session_path.resolve())
        fingerprint = (int(stat.st_mtime_ns), int(stat.st_size))
        with self.server.codex_lock:
            cached = copy.deepcopy((self.server.codex_history_cache.get(cache_key) or {}))
        if cached.get("fingerprint") == fingerprint and cached.get("thread"):
            return copy.deepcopy(cached.get("thread"))
        try:
            with session_path.open("r", encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError:
            return None
        thread_id = ""
        cwd = "/root"
        created_at = 0.0
        updated_at = 0.0
        title = ""
        messages: list[dict] = []
        for index, line in enumerate(lines):
            raw = line.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            timestamp = parse_timestamp(event.get("timestamp"))
            event_type = str(event.get("type") or "")
            payload = event.get("payload") or {}
            if event_type == "session_meta" and isinstance(payload, dict):
                thread_id = str(payload.get("id") or "").strip() or thread_id
                cwd = str(payload.get("cwd") or "").strip() or cwd
                created_at = parse_timestamp(payload.get("timestamp")) or timestamp or created_at
                updated_at = max(updated_at, timestamp, created_at)
                continue
            if event_type != "response_item" or not isinstance(payload, dict):
                continue
            if str(payload.get("type") or "") != "message":
                continue
            role = str(payload.get("role") or "").strip()
            if role not in {"user", "assistant"}:
                continue
            text = self._extract_codex_message_text(payload.get("content"))
            if not text:
                continue
            if role == "user" and text.startswith("<environment_context>"):
                continue
            if role == "assistant" and text.startswith("<environment_context>"):
                continue
            message_created_at = timestamp or updated_at or created_at or time.time()
            messages.append(
                {
                    "id": f"hist_{index}",
                    "role": role,
                    "text": text,
                    "created_at": message_created_at,
                }
            )
            updated_at = max(updated_at, message_created_at)
            if not title and role == "user":
                title = self._codex_default_title(text)
        if not thread_id:
            return None
        if not title:
            title = session_path.stem
        thread = {
            "id": thread_id,
            "codex_thread_id": thread_id,
            "title": title,
            "cwd": cwd,
            "created_at": created_at or updated_at or parse_timestamp(stat.st_mtime),
            "updated_at": updated_at or created_at or parse_timestamp(stat.st_mtime),
            "messages": messages,
            "active_job_id": "",
            "source": "history",
        }
        with self.server.codex_lock:
            self.server.codex_history_cache[cache_key] = {
                "fingerprint": fingerprint,
                "thread": copy.deepcopy(thread),
            }
        return thread

    def _load_real_codex_threads(self) -> dict[str, dict]:
        if not CODEX_HISTORY_ROOT.exists():
            return {}
        threads: dict[str, dict] = {}
        thread_index: dict[str, str] = {}
        existing_paths: set[str] = set()
        for session_path in sorted(CODEX_HISTORY_ROOT.rglob("rollout-*.jsonl"), reverse=True):
            existing_paths.add(str(session_path.resolve()))
            thread = self._parse_real_codex_thread(session_path)
            if not thread:
                continue
            thread_id = str(thread.get("id") or "")
            if not thread_id:
                continue
            current = threads.get(thread_id)
            if not current or float(thread.get("updated_at") or 0) >= float(current.get("updated_at") or 0):
                threads[thread_id] = thread
                thread_index[thread_id] = str(session_path.resolve())
        with self.server.codex_lock:
            self.server.codex_history_index = thread_index
            stale_paths = [path for path in self.server.codex_history_cache.keys() if path not in existing_paths]
            for path in stale_paths:
                self.server.codex_history_cache.pop(path, None)
        return threads

    def _load_real_codex_thread(self, thread_id: str) -> dict | None:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return {}
        with self.server.codex_lock:
            indexed_path = str((self.server.codex_history_index.get(normalized_thread_id) or "")).strip()
        if indexed_path:
            thread = self._parse_real_codex_thread(Path(indexed_path))
            if thread and str(thread.get("id") or "") == normalized_thread_id:
                return copy.deepcopy(thread)
        return copy.deepcopy(self._load_real_codex_threads().get(normalized_thread_id) or {})

    def _paginate_codex_messages(
        self,
        messages: list[dict],
        *,
        limit: int = CODEX_THREAD_PAGE_SIZE,
        before: int | None = None,
    ) -> dict:
        normalized_limit = max(1, min(int(limit or CODEX_THREAD_PAGE_SIZE), CODEX_MAX_THREAD_PAGE_SIZE))
        ordered = self._dedupe_codex_messages(list(messages or []))
        total = len(ordered)
        end = total if before is None else max(0, min(int(before), total))
        start = max(0, end - normalized_limit)
        window = ordered[start:end]
        return {
            "messages": window,
            "message_total": total,
            "message_loaded_count": len(window),
            "message_window_start": start,
            "message_window_end": end,
            "has_older_messages": start > 0,
            "has_newer_messages": end < total,
            "message_page_size": normalized_limit,
        }

    def _codex_threads_payload(self) -> dict:
        self._reload_codex_store()
        with self.server.codex_lock:
            deleted_thread_ids = set(self._codex_deleted_threads_map().keys())
        history_threads = self._load_real_codex_threads()
        with self.server.codex_lock:
            store_threads = [self._strip_history_messages(copy.deepcopy(item)) for item in (self.server.codex_store.get("threads") or {}).values()]
        merged: dict[str, dict] = {
            thread_id: copy.deepcopy(thread)
            for thread_id, thread in history_threads.items()
            if thread_id not in deleted_thread_ids
        }
        for thread in store_threads:
            thread_id = str(thread.get("id") or "").strip()
            codex_thread_id = str(thread.get("codex_thread_id") or "").strip()
            if thread_id in deleted_thread_ids or (codex_thread_id and codex_thread_id in deleted_thread_ids):
                continue
            if codex_thread_id and codex_thread_id in history_threads:
                merged[thread_id] = self._merge_codex_thread_with_history(thread, history_threads.get(codex_thread_id))
                if codex_thread_id != thread_id:
                    merged.pop(codex_thread_id, None)
                continue
            merged[thread_id] = thread
        threads = list(merged.values())
        threads.sort(key=lambda item: float(item.get("updated_at") or 0), reverse=True)
        return {"threads": [self._codex_thread_summary(thread) for thread in threads]}

    def _codex_thread_payload(
        self,
        thread_id: str,
        *,
        limit: int = CODEX_THREAD_PAGE_SIZE,
        before: int | None = None,
    ) -> dict:
        self._reload_codex_store()
        if self._codex_thread_is_deleted(thread_id):
            raise ConfigError("Codex 会话不存在。")
        with self.server.codex_lock:
            thread = self._strip_history_messages(copy.deepcopy((self.server.codex_store.get("threads") or {}).get(thread_id) or {}))
            active_job_id = str(thread.get("active_job_id") or "")
            active_job = self.server.codex_jobs.get(active_job_id) if active_job_id else None
        if not thread:
            thread = self._load_real_codex_thread(thread_id) or {}
            if not thread:
                raise ConfigError("Codex 会话不存在。")
        else:
            codex_thread_id = str(thread.get("codex_thread_id") or "").strip()
            if codex_thread_id:
                thread = self._merge_codex_thread_with_history(thread, self._load_real_codex_thread(codex_thread_id))
        if self._codex_thread_is_deleted(str(thread.get("id") or ""), str(thread.get("codex_thread_id") or "")):
            raise ConfigError("Codex 会话不存在。")
        payload = self._codex_thread_summary(thread)
        page = self._paginate_codex_messages(thread.get("messages") or [], limit=limit, before=before)
        payload.update(page)
        payload["messages"] = page["messages"]
        payload["active_job"] = self._codex_job_payload(active_job) if active_job else None
        return payload

    def _codex_job_status_payload(self, job_id: str) -> dict:
        self._reload_codex_store()
        with self.server.codex_lock:
            job = self.server.codex_jobs.get(job_id)
            if not job:
                raise ConfigError("Codex 任务不存在。")
            return self._codex_job_payload(copy.deepcopy(job)) or {}

    def _codex_default_title(self, prompt: str) -> str:
        cleaned = " ".join(str(prompt or "").strip().split())
        if not cleaned:
            return "新会话"
        if len(cleaned) <= 28:
            return cleaned
        return f"{cleaned[:28]}…"

    def _normalize_codex_cwd(self, value: str) -> str:
        raw = str(value or "").strip() or "/root"
        path = Path(raw).expanduser()
        if not path.exists() or not path.is_dir():
            raise ConfigError(f"工作目录不存在：{raw}")
        return str(path.resolve())

    def _create_codex_thread(self, prompt: str, cwd: str) -> dict:
        thread_id = f"thread_{secrets.token_hex(8)}"
        now = time.time()
        return {
            "id": thread_id,
            "codex_thread_id": "",
            "title": self._codex_default_title(prompt),
            "cwd": cwd,
            "created_at": now,
            "updated_at": now,
            "messages": [],
            "active_job_id": "",
        }

    def _append_codex_message(self, thread: dict, message: dict) -> None:
        messages = list(thread.get("messages") or [])
        messages.append(message)
        thread["messages"] = messages
        thread["updated_at"] = time.time()

    def _upsert_codex_message(self, thread: dict, message: dict) -> None:
        messages = list(thread.get("messages") or [])
        message_id = str(message.get("id") or "").strip()
        updated = False
        if message_id:
            for index, item in enumerate(messages):
                if str(item.get("id") or "") == message_id:
                    merged = copy.deepcopy(item)
                    merged.update(message)
                    messages[index] = merged
                    updated = True
                    break
        if not updated:
            messages.append(message)
        thread["messages"] = messages
        thread["updated_at"] = time.time()

    def _save_codex_uploaded_attachments(self, attachments: object) -> tuple[list[str], list[dict], str]:
        if not attachments:
            return [], [], ""
        if not isinstance(attachments, list):
            raise ConfigError("附件参数格式错误。")
        if len(attachments) > CODEX_MAX_ATTACHMENTS:
            raise ConfigError(f"单次最多上传 {CODEX_MAX_ATTACHMENTS} 个附件。")
        upload_dir = _codex_upload_dir(self.server.state_dir)
        upload_dir.mkdir(parents=True, exist_ok=True)
        image_paths: list[str] = []
        attachment_records: list[dict] = []
        file_prompt_lines: list[str] = []
        for index, attachment in enumerate(attachments, 1):
            if not isinstance(attachment, dict):
                raise ConfigError("附件参数格式错误。")
            name = str(attachment.get("name") or f"attachment-{index}").strip() or f"attachment-{index}"
            mime_type = str(attachment.get("type") or attachment.get("mime") or "application/octet-stream").strip().lower()
            data = str(attachment.get("data") or "").strip()
            if not data:
                raise ConfigError(f"附件内容为空：{name}")
            try:
                payload = base64.b64decode(data, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ConfigError(f"附件编码非法：{name}") from exc
            if not payload:
                raise ConfigError(f"附件内容为空：{name}")
            if len(payload) > CODEX_MAX_ATTACHMENT_BYTES:
                raise ConfigError(f"附件过大：{name}，单个不能超过 {CODEX_MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB。")
            suffix = Path(name).suffix.lower()
            if not suffix:
                suffix = mimetypes.guess_extension(mime_type) or ".img"
            file_path = upload_dir / f"{int(time.time())}-{secrets.token_hex(8)}{suffix}"
            file_path.write_bytes(payload)
            is_image = mime_type.startswith("image/")
            attachment_records.append(
                {
                    "name": name,
                    "type": mime_type,
                    "kind": "image" if is_image else "file",
                    "path": str(file_path),
                }
            )
            if is_image:
                image_paths.append(str(file_path))
            else:
                file_prompt_lines.append(f"- {name}: {file_path}")
        file_prompt = ""
        if file_prompt_lines:
            file_prompt = "\n\n已上传以下本地文件，你可以直接读取这些路径：\n" + "\n".join(file_prompt_lines)
        return image_paths, attachment_records, file_prompt

    def _launch_codex_job_runner(self, request: dict) -> None:
        job_id = str(request.get("job_id") or "").strip()
        cwd = str(request.get("cwd") or "/root").strip() or "/root"
        unit_name = str(request.get("runner_unit") or codex_job_unit_name(job_id)).strip() or codex_job_unit_name(job_id)
        job_dir_path = codex_job_dir(self.server.state_dir, job_id)
        job_dir_path.mkdir(parents=True, exist_ok=True)
        request_path = codex_job_request_path(self.server.state_dir, job_id)
        request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        runner_script = Path(__file__).resolve().parent / "codex_job_runner.py"
        env = os.environ.copy()
        command = [
            "systemd-run",
            "--quiet",
            "--collect",
            "--no-block",
            "--service-type=exec",
            f"--unit={unit_name[:-8] if unit_name.endswith('.service') else unit_name}",
            f"--property=WorkingDirectory={cwd}",
            f"--description=OpenClaw Panel Codex Job {job_id}",
            f"--setenv=HOME={env.get('HOME') or '/root'}",
            f"--setenv=PATH={env.get('PATH') or ''}",
            sys.executable,
            str(runner_script),
            "--state-dir",
            str(self.server.state_dir),
            "--job-id",
            job_id,
        ]
        proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if proc.returncode != 0:
            raise ConfigError(proc.stderr.strip() or proc.stdout.strip() or "启动 Codex 独立任务失败。")

    def _start_codex_job(self, payload: dict) -> dict:
        self._reload_codex_store(force=True)
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            raise ConfigError("消息不能为空。")
        cwd = self._normalize_codex_cwd(str(payload.get("cwd") or ""))
        model = str(payload.get("model") or "").strip()
        image_paths, attachment_records, file_prompt = self._save_codex_uploaded_attachments(
            payload.get("attachments") or payload.get("images") or []
        )
        effective_prompt = prompt + file_prompt
        requested_thread_id = str(payload.get("thread_id") or "").strip()
        history_thread = {}
        if requested_thread_id and self._codex_thread_is_deleted(requested_thread_id):
            raise ConfigError("Codex 会话不存在。")

        with self.server.codex_lock:
            threads = self.server.codex_store.setdefault("threads", {})
            thread = self._strip_history_messages(copy.deepcopy(threads.get(requested_thread_id) or {})) if requested_thread_id else {}
        if requested_thread_id and not thread:
            history_thread = self._load_real_codex_thread(requested_thread_id) or {}
            thread = self._overlay_thread_from_history(history_thread, cwd) if history_thread else {}
            if not thread:
                raise ConfigError("Codex 会话不存在。")

        with self.server.codex_lock:
            threads = self.server.codex_store.setdefault("threads", {})
            deleted_threads = self._codex_deleted_threads_map()
            if not thread:
                thread = self._create_codex_thread(prompt, cwd)
            active_job_id = str(thread.get("active_job_id") or "")
            if active_job_id:
                active_job = self.server.codex_jobs.get(active_job_id)
                if active_job and str(active_job.get("status") or "") == "running":
                    raise ConfigError("当前会话还有进行中的 Codex 任务。")
                thread["active_job_id"] = ""
            thread["cwd"] = cwd
            deleted_threads.pop(str(thread.get("id") or "").strip(), None)
            deleted_threads.pop(str(thread.get("codex_thread_id") or "").strip(), None)
            self._append_codex_message(
                thread,
                {
                    "id": f"msg_{secrets.token_hex(6)}",
                    "role": "user",
                    "text": prompt,
                    "attachments": attachment_records,
                    "created_at": time.time(),
                },
            )
            job_id = f"job_{secrets.token_hex(8)}"
            runner_unit = codex_job_unit_name(job_id)
            job = {
                "id": job_id,
                "thread_id": thread["id"],
                "status": "running",
                "created_at": time.time(),
                "updated_at": time.time(),
                "error": "",
                "assistant_text": "",
                "commands": [],
                "last_event_text": "等待调度",
                "runner_unit": runner_unit,
            }
            thread["active_job_id"] = job_id
            threads[thread["id"]] = thread
            self.server.codex_jobs = self.server.codex_store.setdefault("jobs", {})
            self.server.codex_jobs[job_id] = job
            self._persist_codex_store()
        request = {
            "job_id": job_id,
            "thread_id": thread["id"],
            "prompt": effective_prompt,
            "cwd": cwd,
            "model": model,
            "image_paths": image_paths,
            "codex_thread_id": str(thread.get("codex_thread_id") or ""),
            "codex_bin": shutil.which("codex") or "codex",
            "runner_unit": runner_unit,
            "env": {
                "HOME": os.environ.get("HOME") or "/root",
                "PATH": os.environ.get("PATH") or "",
            },
        }
        try:
            self._launch_codex_job_runner(request)
        except Exception as exc:
            with self.server.codex_lock:
                threads = self.server.codex_store.setdefault("threads", {})
                jobs = self.server.codex_store.setdefault("jobs", {})
                failed_thread = threads.get(thread["id"])
                failed_job = jobs.get(job_id)
                if isinstance(failed_thread, dict) and str(failed_thread.get("active_job_id") or "") == job_id:
                    failed_thread["active_job_id"] = ""
                    failed_thread["updated_at"] = time.time()
                    threads[thread["id"]] = failed_thread
                if isinstance(failed_job, dict):
                    failed_job["status"] = "failed"
                    failed_job["updated_at"] = time.time()
                    failed_job["error"] = str(exc)
                    failed_job["last_event_text"] = "启动失败"
                    jobs[job_id] = failed_job
                self.server.codex_jobs = jobs
                self._persist_codex_store()
            raise ConfigError(f"启动 Codex 任务失败：{exc}") from exc
        return {
            "thread_id": thread["id"],
            "job": self._codex_job_payload(job),
        }

    def _cancel_codex_job(self, payload: dict) -> dict:
        self._reload_codex_store(force=True)
        requested_job_id = str(payload.get("job_id") or "").strip()
        requested_thread_id = str(payload.get("thread_id") or "").strip()
        force_stop = payload.get("force")
        if isinstance(force_stop, bool):
            force_stop = force_stop
        elif isinstance(force_stop, (int, float)):
            force_stop = bool(force_stop)
        else:
            force_stop = str(force_stop or "").strip().lower() not in {"", "0", "false", "off", "no"}
        if not requested_job_id and not requested_thread_id:
            raise ConfigError("缺少 job_id 或 thread_id。")

        runner_unit = ""
        resolved_job_id = requested_job_id
        resolved_thread_id = requested_thread_id
        with self.server.codex_lock:
            threads = self.server.codex_store.setdefault("threads", {})
            jobs = self.server.codex_store.setdefault("jobs", {})

            thread = threads.get(requested_thread_id) if requested_thread_id else None
            job = jobs.get(requested_job_id) if requested_job_id else None

            if thread and not resolved_job_id:
                resolved_job_id = str(thread.get("active_job_id") or "").strip()
                job = jobs.get(resolved_job_id) if resolved_job_id else None
            if job and not resolved_thread_id:
                resolved_thread_id = str(job.get("thread_id") or "").strip()
                thread = threads.get(resolved_thread_id) if resolved_thread_id else None

            if not isinstance(thread, dict) or not thread:
                raise ConfigError("Codex 会话不存在。")
            if not isinstance(job, dict) or not job:
                raise ConfigError("当前会话没有可中断的 Codex 任务。")

            active_job_id = str(thread.get("active_job_id") or "").strip()
            job_status = str(job.get("status") or "").strip().lower()
            if active_job_id != resolved_job_id and job_status != "running":
                raise ConfigError("当前任务已结束，无需中断。")

            runner_unit = str(job.get("runner_unit") or codex_job_unit_name(resolved_job_id)).strip()

        unit_was_running = bool(runner_unit and is_systemd_unit_running(runner_unit))
        forced_used = False
        if runner_unit and unit_was_running:
            proc = subprocess.run(
                ["systemctl", "stop", runner_unit],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            for _ in range(8):
                if not is_systemd_unit_running(runner_unit):
                    break
                time.sleep(0.2)
            if is_systemd_unit_running(runner_unit) and force_stop:
                forced_used = True
                subprocess.run(
                    ["systemctl", "kill", "--kill-who=all", "--signal=SIGKILL", runner_unit],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                subprocess.run(
                    ["systemctl", "stop", runner_unit],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                for _ in range(12):
                    if not is_systemd_unit_running(runner_unit):
                        break
                    time.sleep(0.2)
                subprocess.run(
                    ["systemctl", "reset-failed", runner_unit],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
            if proc.returncode != 0 and is_systemd_unit_running(runner_unit):
                detail = proc.stderr.strip() or proc.stdout.strip() or "未知错误"
                prefix = "强制停止" if force_stop else "中断"
                raise ConfigError(f"{prefix} Codex 任务失败：{detail}")
            if is_systemd_unit_running(runner_unit):
                prefix = "强制停止" if force_stop else "中断"
                raise ConfigError(f"{prefix} Codex 任务失败：systemd 未能停止任务进程。")

        now = time.time()
        notice_text = CODEX_FORCE_CANCELLED_JOB_NOTICE if force_stop else CODEX_CANCELLED_JOB_NOTICE
        last_event_text = "已强制停止" if force_stop else "已手动中断"
        with self.server.codex_lock:
            threads = self.server.codex_store.setdefault("threads", {})
            jobs = self.server.codex_store.setdefault("jobs", {})
            thread = threads.get(resolved_thread_id)
            job = jobs.get(resolved_job_id)
            if not isinstance(thread, dict) or not isinstance(job, dict):
                raise ConfigError("Codex 任务不存在。")

            thread_messages = [copy.deepcopy(item) for item in list(thread.get("messages") or []) if isinstance(item, dict)]
            notice_id = f"sys_cancelled_{resolved_job_id}"
            if not any(str(item.get("id") or "").strip() == notice_id for item in thread_messages):
                thread_messages.append(
                    {
                        "id": notice_id,
                        "role": "event",
                        "event_type": "status",
                        "status": "cancelled",
                        "text": notice_text,
                        "created_at": now,
                    }
                )
                thread["messages"] = thread_messages

            if str(thread.get("active_job_id") or "").strip() == resolved_job_id:
                thread["active_job_id"] = ""
            thread["updated_at"] = max(float(thread.get("updated_at") or 0), now)

            job["status"] = "cancelled"
            job["updated_at"] = now
            job["error"] = notice_text
            job["last_event_text"] = last_event_text
            job["runner_unit"] = runner_unit

            threads[resolved_thread_id] = thread
            jobs[resolved_job_id] = job
            self.server.codex_jobs = jobs
            self._persist_codex_store()

        return {
            "thread_id": resolved_thread_id,
            "job": self._codex_job_payload(job),
            "unit_was_running": unit_was_running,
            "forced": forced_used or force_stop,
        }

    def _run_codex_job(self, panel_thread_id: str, job_id: str, prompt: str, cwd: str, model: str, image_paths: list[str]) -> None:
        thread_snapshot = None
        with self.server.codex_lock:
            thread_snapshot = copy.deepcopy((self.server.codex_store.get("threads") or {}).get(panel_thread_id) or {})
        codex_thread_id = str(thread_snapshot.get("codex_thread_id") or "")

        command = ["codex", "exec"]
        if codex_thread_id:
            command.extend(["resume", codex_thread_id])
        for image_path in image_paths:
            command.extend(["-i", image_path])
        command.extend(["--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "-"])
        if model:
            if codex_thread_id:
                command = ["codex", "exec", "resume", codex_thread_id]
                for image_path in image_paths:
                    command.extend(["-i", image_path])
                command.extend(["--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "-m", model, "-"])
            else:
                command = ["codex", "exec"]
                for image_path in image_paths:
                    command.extend(["-i", image_path])
                command.extend(["--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "-m", model, "-"])

        env = os.environ.copy()
        env.setdefault("HOME", "/root")
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=env,
            bufsize=1,
        )
        if process.stdin:
            process.stdin.write(prompt)
            process.stdin.close()

        next_codex_thread_id = codex_thread_id
        assistant_messages: list[str] = []
        commands: list[dict] = []
        parse_errors: list[str] = []
        try:
            for line in process.stdout or []:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    parse_errors.append(raw)
                    continue
                event_type = str(event.get("type") or "")
                if event_type == "thread.started" and str(event.get("thread_id") or "").strip():
                    next_codex_thread_id = str(event.get("thread_id") or "").strip()
                    with self.server.codex_lock:
                        threads = self.server.codex_store.setdefault("threads", {})
                        thread = threads.get(panel_thread_id)
                        job = self.server.codex_jobs.get(job_id)
                        if thread:
                            thread["codex_thread_id"] = next_codex_thread_id
                            thread["updated_at"] = time.time()
                            threads[panel_thread_id] = thread
                        if job:
                            job["updated_at"] = time.time()
                            job["last_event_text"] = "已连接 Codex 会话"
                    continue
                if event_type not in {"item.started", "item.completed"}:
                    continue
                item = event.get("item") or {}
                item_type = str(item.get("type") or "")
                item_id = str(item.get("id") or f"evt_{secrets.token_hex(4)}")
                if item_type == "agent_message":
                    text = str(item.get("text") or "").strip()
                    if not text:
                        continue
                    assistant_messages.append(text)
                    with self.server.codex_lock:
                        threads = self.server.codex_store.setdefault("threads", {})
                        thread = threads.get(panel_thread_id)
                        job = self.server.codex_jobs.get(job_id)
                        if thread:
                            self._append_codex_message(
                                thread,
                                {
                                    "id": f"msg_{item_id}",
                                    "role": "assistant",
                                    "text": text,
                                    "created_at": time.time(),
                                },
                            )
                            threads[panel_thread_id] = thread
                        if job:
                            job["updated_at"] = time.time()
                            job["assistant_text"] = "\n\n".join(part for part in assistant_messages if part).strip()
                            job["last_event_text"] = "Codex 正在回复"
                    continue
                if item_type != "command_execution":
                    continue
                command_entry = {
                    "id": item_id,
                    "command": str(item.get("command") or ""),
                    "output": self._trim_codex_text(str(item.get("aggregated_output") or "")),
                    "exit_code": item.get("exit_code"),
                    "status": "running" if event_type == "item.started" else str(item.get("status") or "completed"),
                }
                existing = next((entry for entry in commands if str(entry.get("id") or "") == item_id), None)
                if existing:
                    existing.update(command_entry)
                else:
                    commands.append(command_entry)
                status_text = "命令执行中" if event_type == "item.started" else "命令已完成"
                with self.server.codex_lock:
                    threads = self.server.codex_store.setdefault("threads", {})
                    thread = threads.get(panel_thread_id)
                    job = self.server.codex_jobs.get(job_id)
                    if thread:
                        self._upsert_codex_message(
                            thread,
                            {
                                "id": f"cmd_{item_id}",
                                "role": "event",
                                "event_type": "command",
                                "text": f"{status_text}：{command_entry['command'] or '-'}",
                                "command": command_entry["command"],
                                "output": command_entry["output"],
                                "exit_code": command_entry["exit_code"],
                                "status": command_entry["status"],
                                "created_at": time.time(),
                            },
                        )
                        threads[panel_thread_id] = thread
                    if job:
                        job["updated_at"] = time.time()
                        job["commands"] = copy.deepcopy(commands)
                        job["last_event_text"] = status_text
        finally:
            stderr_output = process.stderr.read() if process.stderr else ""
            process.wait()

        assistant_text = "\n\n".join(part for part in assistant_messages if part).strip()
        error_text = ""
        if process.returncode != 0:
            error_text = str(stderr_output or "Codex 执行失败。").strip()
        elif not assistant_text and parse_errors:
            error_text = self._trim_codex_text("\n".join(parse_errors))

        with self.server.codex_lock:
            threads = self.server.codex_store.setdefault("threads", {})
            thread = threads.get(panel_thread_id)
            job = self.server.codex_jobs.get(job_id)
            if not thread or not job:
                return
            thread["codex_thread_id"] = next_codex_thread_id or thread.get("codex_thread_id") or ""
            thread["cwd"] = cwd
            thread["active_job_id"] = ""
            job["status"] = "failed" if error_text else "completed"
            job["updated_at"] = time.time()
            job["error"] = self._trim_codex_text(error_text)
            job["assistant_text"] = assistant_text
            job["commands"] = commands
            job["last_event_text"] = "执行失败" if error_text else "执行完成"
            threads[panel_thread_id] = thread
            self._persist_codex_store()

    def _authenticated_username(self) -> str:
        self._cleanup_auth_state()
        auth_config = self._auth_config()
        if not auth_config:
            return ""
        session_id = self._current_session_id()
        if not session_id:
            return ""
        changed = False
        should_persist = False
        username = ""
        with self.server.auth_lock:
            session = self.server.sessions.get(session_id)
            if not session:
                return ""
            if session.get("expires_at", 0) <= time.time():
                self.server.sessions.pop(session_id, None)
                changed = True
            elif not hmac.compare_digest(str(session.get("username") or ""), str(auth_config.get("username") or "")):
                self.server.sessions.pop(session_id, None)
                changed = True
            else:
                now = time.time()
                should_persist = now - float(session.get("last_seen_at") or 0) >= 60
                session["last_seen_at"] = now
                session["expires_at"] = now + SESSION_TTL_SECONDS
                username = str(session.get("username") or "")
        if changed:
            self._persist_session_store()
            return ""
        if should_persist:
            self._persist_session_store()
        return username

    def _is_authenticated(self) -> bool:
        return bool(self._authenticated_username())

    def _public_get_routes(self) -> set[str]:
        return {
            "/healthz",
            "/login",
            "/bootstrap.sh",
            "/install/archive.tar.gz",
            "/api/auth/state",
            "/api/auth/captcha",
        }

    def _public_post_routes(self) -> set[str]:
        return {
            "/api/auth/setup",
            "/api/auth/login",
            "/api/auth/logout",
        }

    def _known_get_routes(self, route_path: str) -> bool:
        return route_path in {
            "/",
            "/healthz",
            "/login",
            "/bootstrap.sh",
            "/install/archive.tar.gz",
            "/api/auth/state",
            "/api/auth/captcha",
            "/api/auth/sessions",
            "/api/config",
            "/api/status",
            "/api/presets",
            "/api/store",
            "/api/store/runtime-auth",
            "/api/store/runtime-auth/openai-codex/login",
        } or route_path.startswith("/static/")

    def _known_post_routes(self, route_path: str) -> bool:
        return route_path in {
            "/api/auth/setup",
            "/api/auth/login",
            "/api/auth/logout",
            "/api/codex/job/cancel",
            "/api/config",
            "/api/restart",
            "/api/presets/save",
            "/api/presets/delete",
            "/api/store/models",
            "/api/store/providers/save",
            "/api/store/providers/delete",
            "/api/store/providers/select",
            "/api/store/providers/reorder",
            "/api/store/providers/refresh-models",
            "/api/store/providers/import/ccswitch/preview",
            "/api/store/providers/import/ccswitch/apply",
            "/api/store/channels/save",
            "/api/store/channels/delete",
            "/api/store/agents/save",
            "/api/store/agents/delete",
            "/api/store/agents/apply",
            "/api/store/runtime-auth/mode",
            "/api/store/runtime-auth/openai-codex/login/start",
            "/api/store/runtime-auth/openai-codex/login/submit",
            "/api/store/runtime-auth/openai-codex/login/cancel",
            "/api/store/apply",
        }

    def _login_url(self) -> str:
        return f"{self.server.base_path}/login" if self.server.base_path else "/login"

    def _app_url(self) -> str:
        return f"{self.server.base_path}/" if self.server.base_path else "/"

    def _request_base_url(self) -> str:
        forwarded_proto = str(self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip()
        forwarded_host = str(self.headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
        host = forwarded_host or str(self.headers.get("Host") or "").strip()
        if not host:
            host = f"{self.server.server_address[0]}:{self.server.server_address[1]}"
        proto = forwarded_proto or ("https" if str(self.headers.get("X-Forwarded-Ssl") or "").lower() == "on" else "http")
        return f"{proto}://{host}{self.server.base_path}"

    def _is_public_route(self, route_path: str, method: str) -> bool:
        if method == "GET":
            return route_path in self._public_get_routes()
        if method == "POST":
            return route_path in self._public_post_routes()
        if method == "HEAD":
            return route_path in self._public_get_routes()
        return False

    def _auth_state_payload(self) -> dict:
        username = self._authenticated_username()
        return {
            "initialized": self._is_auth_initialized(),
            "logged_in": bool(username),
            "username": username or None,
        }

    def _handle_unauthorized(self, route_path: str, method: str) -> bool:
        if self._is_public_route(route_path, method) or self._is_authenticated():
            return False
        message = "未登录或登录已失效。"
        if not self._is_auth_initialized():
            message = "请先完成初始化。"
        if route_path.startswith("/api/"):
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            if method != "HEAD":
                body = json.dumps({"ok": False, "error": message}, ensure_ascii=False).encode("utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_header("Content-Length", "0")
                self.end_headers()
            return True
        target = quote(urlparse(self.path).path, safe="/")
        location = f"{self._login_url()}?next={target}"
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return True

    def _is_codex_public_route(self, route_path: str, method: str) -> bool:
        if method == "GET":
            return route_path == "/api/codex/auth/state"
        if method == "POST":
            return route_path in {"/api/codex/auth/setup", "/api/codex/auth/login", "/api/codex/auth/logout"}
        return False

    def _handle_codex_unauthorized(self, route_path: str, method: str) -> bool:
        if not route_path.startswith("/api/codex/"):
            return False
        if self._is_codex_public_route(route_path, method):
            return False
        if self._is_codex_authenticated():
            return False
        self._json_response(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Codex 未解锁或会话已失效。"})
        return True

    def _json_response(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text_response(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(content_length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError("请求体必须是 JSON 对象。")
        return data

    def _strip_ansi_text(self, value: str) -> str:
        return re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", str(value or ""))

    def _oauth_login_session_payload(self) -> dict:
        with self.server.oauth_login_lock:
            session = self.server.oauth_login_session or {}
        if not session:
            return {
                "active": False,
                "provider": OPENAI_CODEX_PROVIDER,
                "status": "idle",
                "stage": "idle",
                "oauth_url": "",
                "output": "",
                "created_at": None,
                "updated_at": None,
                "completed_at": None,
                "exit_code": None,
            }
        return {
            "active": str(session.get("status") or "") in {"running", "cancelling"},
            "id": str(session.get("id") or ""),
            "provider": str(session.get("provider") or OPENAI_CODEX_PROVIDER),
            "status": str(session.get("status") or ""),
            "stage": str(session.get("stage") or ""),
            "oauth_url": str(session.get("oauth_url") or ""),
            "output": str(session.get("output") or ""),
            "created_at": float(session.get("created_at") or 0) or None,
            "updated_at": float(session.get("updated_at") or 0) or None,
            "completed_at": float(session.get("completed_at") or 0) or None,
            "exit_code": session.get("exit_code"),
        }

    def _apply_oauth_login_output(self, session: dict, text: str) -> None:
        chunk = str(text or "")
        if not chunk:
            return
        session["output"] = trim_output_text(f"{session.get('output') or ''}{chunk}")
        session["updated_at"] = time.time()
        sanitized = self._strip_ansi_text(session["output"])
        if not session.get("oauth_url"):
            match = re.search(
                r"Open this URL in your LOCAL browser:\s*(https?://\S+)",
                sanitized,
                flags=re.IGNORECASE,
            )
            if not match:
                match = re.search(r"(https://auth\.openai\.com/\S+)", sanitized, flags=re.IGNORECASE)
            if match:
                session["oauth_url"] = str(match.group(1) or "").strip()
        if "Paste the redirect URL" in sanitized:
            session["stage"] = "await_redirect"
        elif session.get("oauth_url") and str(session.get("stage") or "") == "starting":
            session["stage"] = "open_url"

    def _finalize_oauth_login_session(self, session_id: str, exit_code: int | None) -> None:
        completed_at = time.time()
        with self.server.oauth_login_lock:
            session = self.server.oauth_login_session
            if not isinstance(session, dict) or str(session.get("id") or "") != session_id:
                return
            sanitized = self._strip_ansi_text(str(session.get("output") or ""))
            prior_status = str(session.get("status") or "")
            if exit_code == 0:
                session["status"] = "completed"
                session["stage"] = "completed"
            elif prior_status in {"cancelling", "cancelled"} or "Setup cancelled" in sanitized:
                session["status"] = "cancelled"
                session["stage"] = "cancelled"
            else:
                session["status"] = "failed"
                session["stage"] = "failed"
            session["completed_at"] = completed_at
            session["updated_at"] = completed_at
            session["exit_code"] = exit_code
            session["process"] = None
            master_fd = session.pop("master_fd", None)
        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass

    def _oauth_login_reader_loop(self, session_id: str) -> None:
        while True:
            with self.server.oauth_login_lock:
                session = self.server.oauth_login_session
                if not isinstance(session, dict) or str(session.get("id") or "") != session_id:
                    return
                process = session.get("process")
                master_fd = session.get("master_fd")
            if process is None or master_fd is None:
                return

            try:
                ready, _, _ = select.select([master_fd], [], [], 0.25)
            except OSError:
                ready = []
            if ready:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    decoded = chunk.decode("utf-8", errors="replace")
                    with self.server.oauth_login_lock:
                        session = self.server.oauth_login_session
                        if isinstance(session, dict) and str(session.get("id") or "") == session_id:
                            self._apply_oauth_login_output(session, decoded)
                    continue

            exit_code = process.poll()
            if exit_code is None:
                continue

            while True:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                decoded = chunk.decode("utf-8", errors="replace")
                with self.server.oauth_login_lock:
                    session = self.server.oauth_login_session
                    if isinstance(session, dict) and str(session.get("id") or "") == session_id:
                        self._apply_oauth_login_output(session, decoded)
            self._finalize_oauth_login_session(session_id, exit_code)
            return

    def _start_oauth_login_session(self) -> dict:
        with self.server.oauth_login_lock:
            existing = self.server.oauth_login_session
            if isinstance(existing, dict) and str(existing.get("status") or "") in {"running", "cancelling"}:
                raise ConfigError("当前已有进行中的 OpenAI OAuth 登录流程。")

        openclaw_bin = find_openclaw_bin(self.server.state_dir)
        env = build_openclaw_env(self.server.state_dir)
        home_dir = derive_openclaw_home(self.server.state_dir)
        master_fd, slave_fd = pty.openpty()
        try:
            process = subprocess.Popen(
                [
                    openclaw_bin,
                    "models",
                    "auth",
                    "login",
                    "--provider",
                    OPENAI_CODEX_PROVIDER,
                    "--method",
                    "oauth",
                ],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=str(home_dir),
                env=env,
                close_fds=True,
            )
        except Exception:
            try:
                os.close(master_fd)
            except OSError:
                pass
            try:
                os.close(slave_fd)
            except OSError:
                pass
            raise
        finally:
            try:
                os.close(slave_fd)
            except OSError:
                pass

        session_id = f"oauth_{secrets.token_hex(8)}"
        now = time.time()
        with self.server.oauth_login_lock:
            self.server.oauth_login_session = {
                "id": session_id,
                "provider": OPENAI_CODEX_PROVIDER,
                "status": "running",
                "stage": "starting",
                "oauth_url": "",
                "output": "",
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
                "exit_code": None,
                "process": process,
                "master_fd": master_fd,
            }

        Thread(target=self._oauth_login_reader_loop, args=(session_id,), daemon=True).start()
        return self._oauth_login_session_payload()

    def _submit_oauth_login_redirect(self, payload: dict) -> dict:
        redirect_url = str(payload.get("redirect_url") or payload.get("redirectUrl") or "").strip()
        if not redirect_url:
            raise ConfigError("缺少 redirect_url。")
        if not redirect_url.startswith(("http://", "https://")):
            raise ConfigError("redirect_url 格式不合法。")

        with self.server.oauth_login_lock:
            session = self.server.oauth_login_session
            if not isinstance(session, dict) or str(session.get("status") or "") not in {"running", "cancelling"}:
                raise ConfigError("当前没有进行中的 OAuth 登录流程。")
            if str(session.get("provider") or "") != OPENAI_CODEX_PROVIDER:
                raise ConfigError("当前 OAuth 登录流程的 Provider 不匹配。")
            master_fd = session.get("master_fd")
            if master_fd is None:
                raise ConfigError("OAuth 登录流程已失效，请重新开始。")
            session["stage"] = "submitting"
            session["updated_at"] = time.time()
        try:
            os.write(master_fd, redirect_url.encode("utf-8"))
            os.write(master_fd, b"\r")
        except OSError as exc:
            raise ConfigError(f"提交回调地址失败：{exc}") from exc
        return self._oauth_login_session_payload()

    def _cancel_oauth_login_session(self) -> dict:
        with self.server.oauth_login_lock:
            session = self.server.oauth_login_session
            if not isinstance(session, dict) or str(session.get("status") or "") not in {"running", "cancelling"}:
                raise ConfigError("当前没有进行中的 OAuth 登录流程。")
            master_fd = session.get("master_fd")
            process = session.get("process")
            session["status"] = "cancelling"
            session["stage"] = "cancelling"
            session["updated_at"] = time.time()
        if master_fd is not None:
            try:
                os.write(master_fd, b"\x03")
            except OSError:
                pass
        if process is not None:
            try:
                process.send_signal(signal.SIGINT)
            except Exception:
                pass
        return self._oauth_login_session_payload()

    def _route_path(self) -> str | None:
        parsed = urlparse(self.path)
        base_path = self.server.base_path
        path = parsed.path
        if base_path:
            if path == base_path:
                return "/"
            if path == f"{base_path}/":
                return "/"
            if path.startswith(f"{base_path}/"):
                return path[len(base_path):]
            return None
        return path

    def _serve_static(self, path: str) -> None:
        safe_path = (STATIC_DIR / path.lstrip("/")).resolve()
        if not str(safe_path).startswith(str(STATIC_DIR.resolve())) or not safe_path.exists():
            self._json_response(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})
            return
        if safe_path.name in {"index.html", "login.html", "bootstrap.sh"}:
            content = (
                safe_path.read_text(encoding="utf-8")
                .replace("__BASE_PATH__", self.server.base_path)
                .replace("__PANEL_URL__", self._request_base_url())
            )
            if safe_path.suffix == ".html":
                self._text_response(HTTPStatus.OK, content.encode("utf-8"), "text/html; charset=utf-8")
            else:
                self._text_response(HTTPStatus.OK, content.encode("utf-8"), "text/x-shellscript; charset=utf-8")
            return
        content_type = mimetypes.guess_type(str(safe_path))[0] or "application/octet-stream"
        self._text_response(HTTPStatus.OK, safe_path.read_bytes(), content_type)

    def _serve_codex_attachment(self) -> None:
        raw_path = self._query_value("path")
        if not raw_path:
            raise ConfigError("缺少附件路径。")
        upload_root = _codex_upload_dir(self.server.state_dir).resolve()
        safe_path = Path(raw_path).expanduser().resolve()
        try:
            safe_path.relative_to(upload_root)
        except ValueError as exc:
            raise ConfigError("附件路径非法。") from exc
        if not safe_path.exists() or not safe_path.is_file():
            self._json_response(HTTPStatus.NOT_FOUND, {"ok": False, "error": "附件不存在"})
            return
        stat = safe_path.stat()
        content_type = mimetypes.guess_type(str(safe_path))[0] or "application/octet-stream"
        body = safe_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("ETag", f"\"{int(stat.st_mtime_ns)}-{int(stat.st_size)}\"")
        self.end_headers()
        self.wfile.write(body)

    def _serve_install_archive(self) -> None:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            root_name = "openclaw-config-panel"
            for path in sorted(Path(__file__).resolve().parent.iterdir()):
                if path.name in {"__pycache__", ".git"}:
                    continue
                if path.is_dir():
                    for child in sorted(path.rglob("*")):
                        if "__pycache__" in child.parts or child.name.endswith(".pyc"):
                            continue
                        archive.add(child, arcname=f"{root_name}/{child.relative_to(path.parent)}", recursive=False)
                else:
                    archive.add(path, arcname=f"{root_name}/{path.name}", recursive=False)
        payload = buffer.getvalue()
        self._text_response(HTTPStatus.OK, payload, "application/gzip")

    def _issue_captcha(self) -> tuple[str, str]:
        captcha_id = secrets.token_urlsafe(18)
        code = generate_captcha_code()
        with self.server.auth_lock:
            self.server.captchas[captcha_id] = {
                "code": code,
                "expires_at": time.time() + CAPTCHA_TTL_SECONDS,
            }
        return captcha_id, build_captcha_svg(code)

    def _verify_captcha(self, captcha_id: str, captcha_code: str) -> bool:
        normalized_id = (captcha_id or "").strip()
        normalized_code = (captcha_code or "").strip().upper()
        if not normalized_id or not normalized_code:
            return False
        self._cleanup_auth_state()
        with self.server.auth_lock:
            captcha = self.server.captchas.pop(normalized_id, None)
        if not captcha:
            return False
        if captcha.get("expires_at", 0) <= time.time():
            return False
        return hmac.compare_digest(str(captcha.get("code") or "").upper(), normalized_code)

    def log_message(self, format: str, *args) -> None:
        return

    def do_HEAD(self) -> None:
        route_path = self._route_path()
        if route_path is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not self._known_get_routes(route_path) and not self._known_post_routes(route_path):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if self._handle_unauthorized(route_path, "HEAD"):
            return
        if self._handle_codex_unauthorized(route_path, "HEAD"):
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return

    def do_GET(self) -> None:
        route_path = self._route_path()
        if route_path is None:
            self._json_response(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})
            return
        if not self._known_get_routes(route_path):
            self._json_response(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})
            return
        if self._handle_unauthorized(route_path, "GET"):
            return
        if self._handle_codex_unauthorized(route_path, "GET"):
            return
        if route_path == "/":
            self._serve_static("index.html")
            return
        if route_path == "/login":
            if self._is_auth_initialized() and self._is_authenticated():
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", self._app_url())
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            self._serve_static("login.html")
            return
        if route_path == "/bootstrap.sh":
            self._serve_static("bootstrap.sh")
            return
        if route_path == "/install/archive.tar.gz":
            self._serve_install_archive()
            return
        if route_path.startswith("/static/"):
            self._serve_static(route_path[len("/static/"):])
            return
        if route_path == "/api/codex/attachment":
            try:
                self._serve_codex_attachment()
            except ConfigError as exc:
                self._json_response(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        if route_path == "/healthz":
            self._json_response(HTTPStatus.OK, {"ok": True})
            return
        if route_path == "/api/auth/state":
            self._json_response(HTTPStatus.OK, {"ok": True, "data": self._auth_state_payload()})
            return
        if route_path == "/api/auth/captcha":
            captcha_id, svg = self._issue_captcha()
            self._json_response(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "data": {
                        "captcha_id": captcha_id,
                        "svg": svg,
                        "expires_in": CAPTCHA_TTL_SECONDS,
                    },
                },
            )
            return
        if route_path == "/api/auth/sessions":
            self._json_response(HTTPStatus.OK, {"ok": True, "data": {"sessions": self._session_list_payload()}})
            return
        if route_path == "/api/codex/auth/state":
            self._json_response(HTTPStatus.OK, {"ok": True, "data": self._codex_auth_state_payload()})
            return
        if route_path == "/api/codex/threads":
            try:
                self._json_response(HTTPStatus.OK, {"ok": True, "data": self._codex_threads_payload()})
            except Exception as exc:
                self._json_response(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
            return
        if route_path == "/api/codex/thread":
            try:
                thread_id = self._query_value("thread_id")
                if not thread_id:
                    raise ConfigError("缺少 thread_id。")
                limit = self._query_int_value("limit", CODEX_THREAD_PAGE_SIZE)
                before = self._query_int_value("before", None)
                self._json_response(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "data": self._codex_thread_payload(thread_id, limit=limit or CODEX_THREAD_PAGE_SIZE, before=before),
                    },
                )
            except ConfigError as exc:
                self._json_response(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            except Exception as exc:
                self._json_response(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
            return
        if route_path == "/api/codex/job":
            try:
                job_id = self._query_value("job_id")
                if not job_id:
                    raise ConfigError("缺少 job_id。")
                self._json_response(HTTPStatus.OK, {"ok": True, "data": self._codex_job_status_payload(job_id)})
            except ConfigError as exc:
                self._json_response(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            except Exception as exc:
                self._json_response(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
            return
        if route_path == "/api/config":
            try:
                payload = load_current_config(self.server.state_dir)
                self._json_response(HTTPStatus.OK, {"ok": True, "data": payload})
            except Exception as exc:
                self._json_response(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
            return
        if route_path == "/api/status":
            try:
                status = get_openclaw_status(self.server.state_dir)
                self._json_response(HTTPStatus.OK, {"ok": True, "data": status})
            except Exception as exc:
                self._json_response(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
            return
        if route_path == "/api/presets":
            try:
                presets = load_presets(self.server.state_dir)
                self._json_response(HTTPStatus.OK, {"ok": True, "data": presets})
            except Exception as exc:
                self._json_response(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
            return
        if route_path == "/api/store":
            try:
                store = load_panel_store(self.server.state_dir)
                store["runtimeAuthStatus"] = get_runtime_auth_status(self.server.state_dir)
                store["oauthLogin"] = self._oauth_login_session_payload()
                self._json_response(HTTPStatus.OK, {"ok": True, "data": store})
            except Exception as exc:
                self._json_response(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
            return
        if route_path == "/api/store/runtime-auth":
            try:
                store = load_panel_store(self.server.state_dir)
                self._json_response(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "data": {
                            "runtimeAuth": copy.deepcopy(store.get("runtimeAuth") or {}),
                            "runtimeAuthStatus": get_runtime_auth_status(self.server.state_dir),
                            "oauthLogin": self._oauth_login_session_payload(),
                        },
                    },
                )
            except Exception as exc:
                self._json_response(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
            return
        if route_path == "/api/store/runtime-auth/openai-codex/login":
            self._json_response(HTTPStatus.OK, {"ok": True, "data": self._oauth_login_session_payload()})
            return
        self._json_response(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})

    def do_POST(self) -> None:
        route_path = self._route_path()
        if route_path is None or not self._known_post_routes(route_path):
            self._json_response(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})
            return
        if self._handle_unauthorized(route_path, "POST"):
            return
        if self._handle_codex_unauthorized(route_path, "POST"):
            return
        try:
            payload = self._read_json_body()
            if route_path == "/api/auth/setup":
                if self._is_auth_initialized():
                    self._json_response(HTTPStatus.CONFLICT, {"ok": False, "error": "初始化已完成。"})
                    return
                username = validate_auth_username(str(payload.get("username") or ""))
                password = validate_auth_password(str(payload.get("password") or ""))
                confirm_password = str(payload.get("confirm_password") or "")
                if password != confirm_password:
                    raise ConfigError("两次输入的密码不一致。")
                save_auth_config(self.server.state_dir, username, hash_password(password))
                session_id = self._create_session(username)
                self.send_response(HTTPStatus.OK)
                self._send_cookie(SESSION_COOKIE_NAME, session_id, path=self._cookie_path(), max_age=SESSION_TTL_SECONDS)
                response = json.dumps(
                    {"ok": True, "data": {"redirect_to": self._app_url(), "username": username, "initialized": True}},
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(response)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(response)
                return
            if route_path == "/api/auth/login":
                auth_config = self._auth_config()
                if not auth_config:
                    raise ConfigError("请先完成初始化。")
                username = validate_auth_username(str(payload.get("username") or ""))
                password = str(payload.get("password") or "")
                captcha_id = str(payload.get("captcha_id") or "")
                captcha_code = str(payload.get("captcha_code") or "")
                if not self._verify_captcha(captcha_id, captcha_code):
                    raise ConfigError("验证码错误或已过期。")
                if not hmac.compare_digest(username, str(auth_config.get("username") or "")):
                    raise ConfigError("用户名或密码错误。")
                if not hmac.compare_digest(hash_password(password), str(auth_config.get("password_hash") or "")):
                    raise ConfigError("用户名或密码错误。")
                session_id = self._create_session(username)
                next_path = str(payload.get("next") or "").strip()
                if not next_path.startswith("/"):
                    next_path = self._app_url()
                self.send_response(HTTPStatus.OK)
                self._send_cookie(SESSION_COOKIE_NAME, session_id, path=self._cookie_path(), max_age=SESSION_TTL_SECONDS)
                response = json.dumps(
                    {"ok": True, "data": {"redirect_to": next_path, "username": username}},
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(response)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(response)
                return
            if route_path == "/api/auth/logout":
                self._clear_session()
                self.send_response(HTTPStatus.OK)
                self._send_cookie(
                    SESSION_COOKIE_NAME,
                    "",
                    path=self._cookie_path(),
                    max_age=0,
                    expires="Thu, 01 Jan 1970 00:00:00 GMT",
                )
                response = json.dumps({"ok": True, "data": {"logged_out": True}}, ensure_ascii=False).encode("utf-8")
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(response)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(response)
                return
            if route_path == "/api/codex/auth/setup":
                if self._is_codex_auth_initialized():
                    self._json_response(HTTPStatus.CONFLICT, {"ok": False, "error": "Codex 二次鉴权已初始化。"})
                    return
                password = validate_auth_password(str(payload.get("password") or ""))
                confirm_password = str(payload.get("confirm_password") or "")
                if password != confirm_password:
                    raise ConfigError("两次输入的密码不一致。")
                save_codex_auth_config(self.server.state_dir, hash_password(password))
                session_id = self._create_codex_session()
                self.send_response(HTTPStatus.OK)
                self._send_cookie(CODEX_SESSION_COOKIE_NAME, session_id, path=self._cookie_path(), max_age=CODEX_SESSION_TTL_SECONDS)
                response = json.dumps({"ok": True, "data": self._codex_auth_state_payload()}, ensure_ascii=False).encode("utf-8")
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(response)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(response)
                return
            if route_path == "/api/codex/auth/login":
                auth_config = self._codex_auth_config()
                if not auth_config:
                    raise ConfigError("请先初始化 Codex 二次鉴权。")
                password = str(payload.get("password") or "")
                if not hmac.compare_digest(hash_password(password), str(auth_config.get("password_hash") or "")):
                    raise ConfigError("Codex 二次鉴权密码错误。")
                session_id = self._create_codex_session()
                self.send_response(HTTPStatus.OK)
                self._send_cookie(CODEX_SESSION_COOKIE_NAME, session_id, path=self._cookie_path(), max_age=CODEX_SESSION_TTL_SECONDS)
                response = json.dumps({"ok": True, "data": self._codex_auth_state_payload()}, ensure_ascii=False).encode("utf-8")
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(response)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(response)
                return
            if route_path == "/api/codex/auth/logout":
                self._clear_codex_session()
                self.send_response(HTTPStatus.OK)
                self._send_cookie(
                    CODEX_SESSION_COOKIE_NAME,
                    "",
                    path=self._cookie_path(),
                    max_age=0,
                    expires="Thu, 01 Jan 1970 00:00:00 GMT",
                )
                response = json.dumps({"ok": True, "data": {"logged_out": True}}, ensure_ascii=False).encode("utf-8")
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(response)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(response)
                return
            if route_path == "/api/codex/chat":
                result = self._start_codex_job(payload)
                self._json_response(HTTPStatus.OK, {"ok": True, "data": result})
                return
            if route_path == "/api/codex/job/cancel":
                result = self._cancel_codex_job(payload)
                self._json_response(HTTPStatus.OK, {"ok": True, "data": result})
                return
            if route_path == "/api/codex/thread/delete":
                thread_id = str(payload.get("thread_id") or "").strip()
                if not thread_id:
                    raise ConfigError("缺少 thread_id。")
                self._reload_codex_store(force=True)
                result = self._delete_codex_thread_permanently(thread_id)
                self._json_response(HTTPStatus.OK, {"ok": True, "data": result})
                return
            if route_path == "/api/config":
                if payload.get("apply_store"):
                    result = apply_store_config(self.server.state_dir, restart_gateway=bool(payload.get("restart_gateway", True)))
                else:
                    result = apply_config(payload, self.server.state_dir)
            elif route_path == "/api/restart":
                result = restart_openclaw(self.server.state_dir)
            elif route_path == "/api/presets/save":
                result = save_preset(str(payload.get("name") or ""), payload.get("config") or {}, self.server.state_dir)
            elif route_path == "/api/presets/delete":
                result = delete_preset(str(payload.get("name") or ""), self.server.state_dir)
            elif route_path == "/api/store/models":
                result = save_model_catalog(payload.get("models") or [], self.server.state_dir)
            elif route_path == "/api/store/providers/save":
                result = save_provider_record(payload, self.server.state_dir)
            elif route_path == "/api/store/providers/delete":
                result = delete_provider_record(str(payload.get("provider") or ""), self.server.state_dir)
            elif route_path == "/api/store/providers/reorder":
                result = reorder_provider_records(payload.get("provider_ids") or [], self.server.state_dir)
            elif route_path == "/api/store/providers/refresh-models":
                result = refresh_provider_available_models(payload.get("provider_ids") or [], self.server.state_dir)
            elif route_path == "/api/store/providers/import/ccswitch/preview":
                result = preview_ccswitch_provider_import(
                    str(payload.get("sql_text") or ""),
                    self.server.state_dir,
                    file_name=str(payload.get("file_name") or ""),
                )
            elif route_path == "/api/store/providers/import/ccswitch/apply":
                result = apply_ccswitch_provider_import(payload.get("items") or [], self.server.state_dir)
            elif route_path == "/api/store/channels/save":
                result = save_channel_record(payload, self.server.state_dir)
            elif route_path == "/api/store/channels/delete":
                result = delete_channel_record(payload, self.server.state_dir)
            elif route_path == "/api/store/agents/save":
                result = save_agent_record(payload, self.server.state_dir)
            elif route_path == "/api/store/agents/delete":
                result = delete_agent_record(str(payload.get("agent") or ""), self.server.state_dir)
            elif route_path == "/api/store/agents/apply":
                result = apply_agent_config(str(payload.get("agent") or ""), self.server.state_dir, restart_gateway=bool(payload.get("restart_gateway", True)))
            elif route_path == "/api/store/runtime-auth/mode":
                result = save_runtime_auth_config(payload, self.server.state_dir)
            elif route_path == "/api/store/runtime-auth/openai-codex/login/start":
                result = self._start_oauth_login_session()
            elif route_path == "/api/store/runtime-auth/openai-codex/login/submit":
                result = self._submit_oauth_login_redirect(payload)
            elif route_path == "/api/store/runtime-auth/openai-codex/login/cancel":
                result = self._cancel_oauth_login_session()
            elif route_path == "/api/store/apply":
                result = apply_store_config(self.server.state_dir, restart_gateway=bool(payload.get("restart_gateway", True)))
            else:
                result = set_selected_provider(str(payload.get("provider") or ""), self.server.state_dir)
            self._json_response(HTTPStatus.OK, {"ok": True, "data": result})
        except ConfigError as exc:
            self._json_response(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "ok": False,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenClaw provider control panel")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host, default 127.0.0.1")
    parser.add_argument("--port", type=int, default=5711, help="Bind port, default 5711")
    parser.add_argument("--base-path", default="", help="Optional base path, e.g. /xyz/api/config")
    parser.add_argument(
        "--state-dir",
        default=str(DEFAULT_STATE_DIR),
        help=f"OpenClaw state dir, default {DEFAULT_STATE_DIR}",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    state_dir = Path(args.state_dir).expanduser().resolve()
    base_path = normalize_base_path(args.base_path)
    pidfile_path = _prepare_panel_pidfile(state_dir, args.port)
    session_store = load_session_store(state_dir)
    server = ThreadingHTTPServer((args.host, args.port), PanelHandler)
    server.state_dir = state_dir
    server.base_path = base_path
    server.auth_lock = Lock()
    server.codex_lock = Lock()
    server.sessions = session_store.get("sessions") or {}
    server.codex_sessions = session_store.get("codex_sessions") or {}
    server.captchas = {}
    server.codex_store, codex_store_changed = reconcile_codex_store(load_codex_store(state_dir))
    server.codex_jobs = server.codex_store.setdefault("jobs", {})
    if codex_store_changed:
        save_codex_store(state_dir, server.codex_store)
    server.codex_store_mtime_ns = stat_codex_store_mtime_ns(state_dir)
    server.codex_history_cache = {}
    server.codex_history_index = {}
    server.oauth_login_lock = Lock()
    server.oauth_login_session = None
    stop_requested = {"value": False}

    def _request_shutdown(signum: int, _frame) -> None:
        if stop_requested["value"]:
            return
        stop_requested["value"] = True
        Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _request_shutdown)

    display_url = f"http://{args.host}:{args.port}{base_path or '/'}"
    print(f"[openclaw-config-panel] serving {display_url}")
    print(f"[openclaw-config-panel] state dir: {state_dir}")
    print(f"[openclaw-config-panel] pidfile: {pidfile_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        _cleanup_panel_pidfile(pidfile_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
