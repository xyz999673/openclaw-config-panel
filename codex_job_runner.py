#!/usr/bin/env python3
"""Background worker that executes a single Codex job and streams events back."""
from __future__ import annotations

import argparse
import copy
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

from panel_codex_runtime import codex_job_request_path, update_codex_store


CODEX_MAX_COMMAND_OUTPUT = 12000


def _trim_codex_text(value: str, limit: int = CODEX_MAX_COMMAND_OUTPUT) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n…[已截断 {len(text) - limit} 字符]"


def _append_codex_message(thread: dict, message: dict) -> None:
    messages = list(thread.get("messages") or [])
    messages.append(message)
    thread["messages"] = messages
    thread["updated_at"] = time.time()


def _upsert_codex_message(thread: dict, message: dict) -> None:
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


def _load_request(state_dir: Path, job_id: str) -> dict:
    path = codex_job_request_path(state_dir, job_id)
    if not path.exists():
        raise RuntimeError(f"缺少 Codex 任务请求文件：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Codex 任务请求文件损坏：{exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Codex 任务请求文件格式错误。")
    return payload


def _build_command(request: dict) -> list[str]:
    codex_bin = str(request.get("codex_bin") or "codex").strip() or "codex"
    codex_thread_id = str(request.get("codex_thread_id") or "").strip()
    model = str(request.get("model") or "").strip()
    image_paths = [str(item or "").strip() for item in (request.get("image_paths") or []) if str(item or "").strip()]

    command = [codex_bin, "exec"]
    if codex_thread_id:
        command.extend(["resume", codex_thread_id])
    for image_path in image_paths:
        command.extend(["-i", image_path])
    command.extend(["--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"])
    if model:
        command.extend(["-m", model])
    command.append("-")
    return command


def _job_store_update(state_dir: Path, thread_id: str, job_id: str, updater) -> None:
    def mutator(store: dict) -> None:
        threads = store.setdefault("threads", {})
        jobs = store.setdefault("jobs", {})
        thread = threads.get(thread_id)
        job = jobs.get(job_id)
        if not isinstance(thread, dict) or not isinstance(job, dict):
            return
        updater(thread, job)
        threads[thread_id] = thread
        jobs[job_id] = job

    update_codex_store(state_dir, mutator)


def _run_job(state_dir: Path, request: dict) -> int:
    job_id = str(request.get("job_id") or "").strip()
    thread_id = str(request.get("thread_id") or "").strip()
    prompt = str(request.get("prompt") or "")
    cwd = str(request.get("cwd") or "/root").strip() or "/root"
    env = os.environ.copy()
    for key, value in (request.get("env") or {}).items():
        if not isinstance(key, str):
            continue
        env[key] = str(value or "")
    env.setdefault("HOME", "/root")

    _job_store_update(
        state_dir,
        thread_id,
        job_id,
        lambda _thread, job: job.update(
            {
                "status": "running",
                "updated_at": time.time(),
                "last_event_text": "Codex 启动中",
            }
        ),
    )

    process = subprocess.Popen(
        _build_command(request),
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

    next_codex_thread_id = str(request.get("codex_thread_id") or "").strip()
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

                def mark_thread_started(thread: dict, job: dict) -> None:
                    thread["codex_thread_id"] = next_codex_thread_id
                    thread["updated_at"] = time.time()
                    job["updated_at"] = time.time()
                    job["last_event_text"] = "已连接 Codex 会话"

                _job_store_update(state_dir, thread_id, job_id, mark_thread_started)
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

                def append_assistant_message(thread: dict, job: dict) -> None:
                    _append_codex_message(
                        thread,
                        {
                            "id": f"msg_{item_id}",
                            "role": "assistant",
                            "text": text,
                            "created_at": time.time(),
                        },
                    )
                    job["updated_at"] = time.time()
                    job["assistant_text"] = "\n\n".join(part for part in assistant_messages if part).strip()
                    job["last_event_text"] = "Codex 正在回复"

                _job_store_update(state_dir, thread_id, job_id, append_assistant_message)
                continue
            if item_type != "command_execution":
                continue
            command_entry = {
                "id": item_id,
                "command": str(item.get("command") or ""),
                "output": _trim_codex_text(str(item.get("aggregated_output") or "")),
                "exit_code": item.get("exit_code"),
                "status": "running" if event_type == "item.started" else str(item.get("status") or "completed"),
            }
            existing = next((entry for entry in commands if str(entry.get("id") or "") == item_id), None)
            if existing:
                existing.update(command_entry)
            else:
                commands.append(command_entry)
            status_text = "命令执行中" if event_type == "item.started" else "命令已完成"

            def upsert_command_event(thread: dict, job: dict) -> None:
                _upsert_codex_message(
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
                job["updated_at"] = time.time()
                job["commands"] = copy.deepcopy(commands)
                job["last_event_text"] = status_text

            _job_store_update(state_dir, thread_id, job_id, upsert_command_event)
    finally:
        stderr_output = process.stderr.read() if process.stderr else ""
        process.wait()

    assistant_text = "\n\n".join(part for part in assistant_messages if part).strip()
    error_text = ""
    if process.returncode != 0:
        error_text = str(stderr_output or "Codex 执行失败。").strip()
    elif not assistant_text and parse_errors:
        error_text = _trim_codex_text("\n".join(parse_errors))

    def finalize_job(thread: dict, job: dict) -> None:
        thread["codex_thread_id"] = next_codex_thread_id or thread.get("codex_thread_id") or ""
        thread["cwd"] = cwd
        if str(thread.get("active_job_id") or "") == job_id:
            thread["active_job_id"] = ""
        thread["updated_at"] = time.time()
        job["status"] = "failed" if error_text else "completed"
        job["updated_at"] = time.time()
        job["error"] = _trim_codex_text(error_text)
        job["assistant_text"] = assistant_text
        job["commands"] = copy.deepcopy(commands)
        job["last_event_text"] = "执行失败" if error_text else "执行完成"

    _job_store_update(state_dir, thread_id, job_id, finalize_job)
    return 1 if error_text else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Detached Codex runner for OpenClaw panel")
    parser.add_argument("--state-dir", required=True, help="OpenClaw state dir")
    parser.add_argument("--job-id", required=True, help="Panel Codex job id")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    state_dir = Path(args.state_dir).expanduser().resolve()
    request = _load_request(state_dir, args.job_id)
    return _run_job(state_dir, request)


if __name__ == "__main__":
    raise SystemExit(main())
