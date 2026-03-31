#!/usr/bin/env python3
"""Standalone HTTP entrypoint for the optional Codex console UI."""
from __future__ import annotations

import argparse
import copy
import hmac
import json
import mimetypes
import os
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Lock

from server import (
    CAPTCHA_TTL_SECONDS,
    CODEX_HISTORY_ROOT,
    CODEX_MAX_ATTACHMENT_BYTES,
    CODEX_MAX_ATTACHMENTS,
    CODEX_MAX_COMMAND_OUTPUT,
    CODEX_MAX_THREAD_PAGE_SIZE,
    CODEX_ORPHANED_JOB_NOTICE,
    CODEX_THREAD_PAGE_SIZE,
    ConfigError,
    PanelHandler,
    _auth_config_path,
    _codex_upload_dir,
    build_captcha_svg,
    generate_captcha_code,
    hash_password,
    load_auth_config,
    load_codex_store,
    normalize_base_path,
    parse_timestamp,
    reconcile_codex_store,
    save_auth_config,
    save_codex_store,
    stat_codex_store_mtime_ns,
    validate_auth_password,
    validate_auth_username,
)
from openclaw_config_manager import DEFAULT_STATE_DIR


STATIC_DIR = Path(__file__).resolve().parent / "static"
SESSION_COOKIE_NAME = "openclaw_codex_console_session"
SESSION_STORE_FILENAME = "config-codex-console-sessions.json"
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60


def _session_store_path(state_dir: Path) -> Path:
    return state_dir / SESSION_STORE_FILENAME


def load_session_store(state_dir: Path) -> dict:
    path = _session_store_path(state_dir)
    if not path.exists():
        return {"version": 1, "sessions": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Codex 控制台会话存储损坏：{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("Codex 控制台会话存储格式错误。")
    sessions = raw.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}
    records: dict[str, dict] = {}
    for session_id, session in sessions.items():
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
    return {"version": 1, "sessions": records}


def save_session_store(state_dir: Path, sessions: dict[str, dict]) -> None:
    payload = {"version": 1, "sessions": sessions}
    _session_store_path(state_dir).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class CodexConsoleHandler(PanelHandler):
    """Specialized handler that serves the standalone Codex console endpoints."""
    server_version = "OpenClawCodexConsole/1.0"

    def _cookie_path(self) -> str:
        return self.server.base_path or "/"

    def _cleanup_auth_state(self) -> None:
        now = time.time()
        changed = False
        with self.server.auth_lock:
            expired_sessions = [key for key, value in self.server.sessions.items() if value.get("expires_at", 0) <= now]
            for key in expired_sessions:
                self.server.sessions.pop(key, None)
                changed = True
            expired_captchas = [key for key, value in self.server.captchas.items() if value.get("expires_at", 0) <= now]
            for key in expired_captchas:
                self.server.captchas.pop(key, None)
        if changed:
            self._persist_session_store()

    def _persist_session_store(self) -> None:
        with self.server.auth_lock:
            save_session_store(self.server.state_dir, copy.deepcopy(self.server.sessions))

    def _auth_config(self) -> dict:
        return load_auth_config(self.server.state_dir)

    def _is_auth_initialized(self) -> bool:
        return _auth_config_path(self.server.state_dir).exists()

    def _create_session(self, username: str) -> str:
        session_id = os.urandom(24).hex()
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
        if route_path.startswith("/static/"):
            return True
        return route_path in {
            "/",
            "/healthz",
            "/login",
            "/api/auth/state",
            "/api/auth/captcha",
            "/api/codex/threads",
            "/api/codex/thread",
            "/api/codex/job",
            "/api/codex/attachment",
        }

    def _known_post_routes(self, route_path: str) -> bool:
        return route_path in {
            "/api/auth/setup",
            "/api/auth/login",
            "/api/auth/logout",
            "/api/codex/chat",
            "/api/codex/job/cancel",
            "/api/codex/thread/delete",
        }

    def _is_codex_public_route(self, route_path: str, method: str) -> bool:
        return False

    def _handle_codex_unauthorized(self, route_path: str, method: str) -> bool:
        return False

    def _serve_static(self, path: str) -> None:
        mapped = {
            "index.html": "codex-console.html",
            "login.html": "codex-login.html",
        }.get(path.lstrip("/"), path.lstrip("/"))
        safe_path = (STATIC_DIR / mapped).resolve()
        if not str(safe_path).startswith(str(STATIC_DIR.resolve())) or not safe_path.exists():
            self._json_response(404, {"ok": False, "error": "Not found"})
            return
        if safe_path.suffix == ".html":
            content = (
                safe_path.read_text(encoding="utf-8")
                .replace("__BASE_PATH__", self.server.base_path)
                .replace("__HOME_URL__", "/xyz/home/")
                .replace("__PANEL_URL__", "/xyz/api/config/")
                .replace("__QL_URL__", "/xyz/qinglong/")
            )
            self._text_response(200, content.encode("utf-8"), "text/html; charset=utf-8")
            return
        content_type = mimetypes.guess_type(str(safe_path))[0] or "application/octet-stream"
        self._text_response(200, safe_path.read_bytes(), content_type)

    def do_POST(self) -> None:
        route_path = self._route_path()
        if route_path in {"/api/auth/setup", "/api/auth/login", "/api/auth/logout"}:
            if route_path is None or not self._known_post_routes(route_path):
                self._json_response(404, {"ok": False, "error": "Not found"})
                return
            if self._handle_unauthorized(route_path, "POST"):
                return
            try:
                payload = self._read_json_body()
                if route_path == "/api/auth/setup":
                    if self._is_auth_initialized():
                        self._json_response(409, {"ok": False, "error": "初始化已完成。"})
                        return
                    username = validate_auth_username(str(payload.get("username") or ""))
                    password = validate_auth_password(str(payload.get("password") or ""))
                    confirm_password = str(payload.get("confirm_password") or "")
                    if password != confirm_password:
                        raise ConfigError("两次输入的密码不一致。")
                    save_auth_config(self.server.state_dir, username, hash_password(password))
                    session_id = self._create_session(username)
                    self.send_response(200)
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
                    self.send_response(200)
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
                self._clear_session()
                self.send_response(200)
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
            except ConfigError as exc:
                self._json_response(400, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:
                self._json_response(500, {"ok": False, "error": str(exc)})
                return
        return super().do_POST()



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenClaw Codex Console")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host, default 127.0.0.1")
    parser.add_argument("--port", type=int, default=5712, help="Bind port, default 5712")
    parser.add_argument("--base-path", default="/xyz/codex", help="Base path, default /xyz/codex")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR), help=f"OpenClaw state dir, default {DEFAULT_STATE_DIR}")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    state_dir = Path(args.state_dir).expanduser().resolve()
    base_path = normalize_base_path(args.base_path) or "/xyz/codex"
    session_store = load_session_store(state_dir)
    server = ThreadingHTTPServer((args.host, args.port), CodexConsoleHandler)
    server.state_dir = state_dir
    server.base_path = base_path
    server.auth_lock = Lock()
    server.codex_lock = Lock()
    server.sessions = session_store.get("sessions") or {}
    server.captchas = {}
    server.codex_store, codex_store_changed = reconcile_codex_store(load_codex_store(state_dir))
    server.codex_jobs = server.codex_store.setdefault("jobs", {})
    if codex_store_changed:
        save_codex_store(state_dir, server.codex_store)
    server.codex_store_mtime_ns = stat_codex_store_mtime_ns(state_dir)
    server.codex_history_cache = {}
    server.codex_history_index = {}
    display_url = f"http://{args.host}:{args.port}{base_path or '/'}"
    print(f"[openclaw-codex-console] serving {display_url}")
    print(f"[openclaw-codex-console] state dir: {state_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
