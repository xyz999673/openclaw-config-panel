"""Shared storage and systemd helpers for the optional Codex console."""
from __future__ import annotations

import copy
import fcntl
import json
import os
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable


CODEX_STORE_FILENAME = "config-panel-codex-store.json"
CODEX_STORE_LOCK_FILENAME = "config-panel-codex-store.lock"
CODEX_JOB_ROOT_DIRNAME = "panel-codex-jobs"
CODEX_JOB_REQUEST_FILENAME = "request.json"
CODEX_STORE_VERSION = 3


def codex_store_path(state_dir: Path) -> Path:
    return Path(state_dir).expanduser().resolve() / CODEX_STORE_FILENAME


def codex_store_lock_path(state_dir: Path) -> Path:
    return Path(state_dir).expanduser().resolve() / CODEX_STORE_LOCK_FILENAME


def codex_jobs_root(state_dir: Path) -> Path:
    return Path(state_dir).expanduser().resolve() / CODEX_JOB_ROOT_DIRNAME


def codex_job_dir(state_dir: Path, job_id: str) -> Path:
    return codex_jobs_root(state_dir) / str(job_id or "").strip()


def codex_job_request_path(state_dir: Path, job_id: str) -> Path:
    return codex_job_dir(state_dir, job_id) / CODEX_JOB_REQUEST_FILENAME


def codex_job_unit_name(job_id: str) -> str:
    raw = str(job_id or "").strip().lower()
    suffix = "".join(ch if ch.isalnum() else "-" for ch in raw).strip("-") or "job"
    return f"openclaw-panel-codex-{suffix}.service"


def stat_codex_store_mtime_ns(state_dir: Path) -> int:
    try:
        return codex_store_path(state_dir).stat().st_mtime_ns
    except OSError:
        return 0


def normalize_codex_store(payload: dict | None) -> dict[str, Any]:
    raw = payload if isinstance(payload, dict) else {}
    threads = raw.get("threads")
    jobs = raw.get("jobs")
    deleted_threads = raw.get("deleted_threads")
    if not isinstance(threads, dict):
        threads = {}
    if not isinstance(jobs, dict):
        jobs = {}
    if isinstance(deleted_threads, list):
        deleted_threads = {
            str(item).strip(): 0.0
            for item in deleted_threads
            if str(item).strip()
        }
    elif not isinstance(deleted_threads, dict):
        deleted_threads = {}
    normalized_deleted_threads: dict[str, float] = {}
    for thread_id, deleted_at in deleted_threads.items():
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            continue
        try:
            normalized_deleted_threads[normalized_thread_id] = float(deleted_at or 0)
        except (TypeError, ValueError):
            normalized_deleted_threads[normalized_thread_id] = 0.0
    return {
        "version": CODEX_STORE_VERSION,
        "threads": copy.deepcopy(threads),
        "jobs": copy.deepcopy(jobs),
        "deleted_threads": normalized_deleted_threads,
    }


def _save_codex_store_unlocked(state_dir: Path, payload: dict[str, Any]) -> None:
    path = codex_store_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = normalize_codex_store(payload)
    text = json.dumps(normalized, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as handle:
        handle.write(text)
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def _load_codex_store_unlocked(state_dir: Path) -> dict[str, Any]:
    path = codex_store_path(state_dir)
    if not path.exists():
        return normalize_codex_store({})
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Codex 会话存储损坏：{exc}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("Codex 会话存储格式错误。")
    return normalize_codex_store(raw)


@contextmanager
def _codex_store_file_lock(state_dir: Path):
    """Serialize store access across the panel HTTP process and job workers."""
    lock_path = codex_store_lock_path(state_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_codex_store(state_dir: Path) -> dict[str, Any]:
    with _codex_store_file_lock(state_dir):
        return _load_codex_store_unlocked(state_dir)


def save_codex_store(state_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_codex_store(payload)
    with _codex_store_file_lock(state_dir):
        _save_codex_store_unlocked(state_dir, normalized)
    return normalized


def update_codex_store(
    state_dir: Path,
    mutator: Callable[[dict[str, Any]], Any],
) -> tuple[dict[str, Any], Any]:
    with _codex_store_file_lock(state_dir):
        payload = _load_codex_store_unlocked(state_dir)
        result = mutator(payload)
        normalized = normalize_codex_store(payload)
        _save_codex_store_unlocked(state_dir, normalized)
    return normalized, result


def systemd_unit_state(unit_name: str) -> dict[str, str]:
    unit = str(unit_name or "").strip()
    if not unit:
        return {}
    proc = subprocess.run(
        [
            "systemctl",
            "show",
            unit,
            "--property=Id",
            "--property=LoadState",
            "--property=ActiveState",
            "--property=SubState",
            "--property=MainPID",
            "--property=ExecMainStatus",
            "--property=Result",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return {}
    data: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def is_systemd_unit_running(unit_name: str) -> bool:
    state = systemd_unit_state(unit_name)
    active_state = str(state.get("ActiveState") or "").strip().lower()
    sub_state = str(state.get("SubState") or "").strip().lower()
    return active_state in {"active", "activating", "reloading"} and sub_state not in {"dead", "failed", "exited"}


def reconcile_codex_store(
    payload: dict | None,
    *,
    interrupted_notice_text: str,
    job_running_checker: Callable[[dict[str, Any]], bool] | None = None,
) -> tuple[dict[str, Any], bool]:
    normalized = normalize_codex_store(payload)
    threads = normalized["threads"]
    jobs = normalized["jobs"]
    changed = False
    now = float(time.time())
    for thread_id, raw_thread in list(threads.items()):
        if not isinstance(raw_thread, dict):
            threads[thread_id] = {}
            raw_thread = threads[thread_id]
            changed = True
        active_job_id = str(raw_thread.get("active_job_id") or "").strip()
        if not active_job_id:
            continue
        job = jobs.get(active_job_id) if isinstance(jobs.get(active_job_id), dict) else {}
        job_status = str(job.get("status") or "").strip().lower()
        if job_status == "running" and job and job_running_checker and job_running_checker(job):
            continue
        if job_status in {"completed", "failed", "cancelled"}:
            raw_thread["active_job_id"] = ""
            raw_thread["updated_at"] = max(float(raw_thread.get("updated_at") or 0), float(job.get("updated_at") or 0), now)
            changed = True
            continue
        messages = [copy.deepcopy(item) for item in list(raw_thread.get("messages") or []) if isinstance(item, dict)]
        notice_id = f"sys_interrupted_{active_job_id}"
        if not any(str(item.get("id") or "") == notice_id for item in messages):
            created_at = max(
                float(raw_thread.get("updated_at") or 0),
                max((float(item.get("created_at") or 0) for item in messages), default=0.0),
                float(job.get("updated_at") or 0),
                now,
            )
            messages.append(
                {
                    "id": notice_id,
                    "role": "event",
                    "event_type": "status",
                    "status": "interrupted",
                    "text": interrupted_notice_text,
                    "created_at": created_at,
                }
            )
            raw_thread["messages"] = messages
            raw_thread["updated_at"] = max(float(raw_thread.get("updated_at") or 0), created_at)
            changed = True
        if raw_thread.get("active_job_id"):
            raw_thread["active_job_id"] = ""
            changed = True
        if job:
            if job_status == "running":
                job["status"] = "failed"
                changed = True
            if not str(job.get("error") or "").strip():
                job["error"] = interrupted_notice_text
                changed = True
            if str(job.get("last_event_text") or "").strip() != "执行中断":
                job["last_event_text"] = "执行中断"
                changed = True
            job["updated_at"] = max(float(job.get("updated_at") or 0), now)
    return normalized, changed
