"""CCSwitch SQL import helpers for panel-side Provider records."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from openclaw_config_manager import (
    ConfigError,
    DEFAULT_STATE_DIR,
    _dedupe_provider_name,
    _ensure_unique_provider_base_url,
    _normalize_model_entry,
    _provider_name_from_base_url,
    _sanitize_provider_record,
    _write_panel_store,
    infer_api_type,
    load_panel_store,
    mask_key,
    normalize_panel_base_url,
)

_SUPPORTED_PROVIDER_APP_TYPES = {"codex", "opencode", "claude", "gemini"}
_APP_TYPE_PRIORITY = {
    "codex": 0,
    "opencode": 1,
    "claude": 2,
    "gemini": 3,
}
_WIRE_API_MAP = {
    "responses": "openai-responses",
    "response": "openai-responses",
    "chat_completions": "openai-completions",
    "chat-completions": "openai-completions",
    "completions": "openai-completions",
    "messages": "anthropic-messages",
}
_DEFAULT_SORT_INDEX = 10**9


def _dedupe_strings(values: list[Any]) -> list[str]:
    items: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in items:
            items.append(normalized)
    return items


def _extract_config_value(config_text: str, key: str) -> str:
    pattern = re.compile(
        rf"(?mi)^\s*{re.escape(key)}\s*=\s*(?:\"([^\"]+)\"|'([^']+)')"
    )
    match = pattern.search(config_text or "")
    if not match:
        return ""
    return str(match.group(1) or match.group(2) or "").strip()


def _extract_json(raw: Any, warnings: list[str], provider_id: str) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not str(raw or "").strip():
        return {}
    try:
        payload = json.loads(str(raw))
    except json.JSONDecodeError:
        warnings.append(f"来源 {provider_id} 的 settings_config 不是合法 JSON，已跳过该来源。")
        return {}
    if not isinstance(payload, dict):
        warnings.append(f"来源 {provider_id} 的 settings_config 不是对象，已跳过该来源。")
        return {}
    return payload


def _collect_candidate_urls(
    row: sqlite3.Row,
    settings: dict[str, Any],
    endpoints: dict[tuple[str, str], list[str]],
    config_text: str,
) -> list[str]:
    values: list[Any] = []
    options = settings.get("options") or {}
    env = settings.get("env") or {}
    auth = settings.get("auth") or {}

    if isinstance(settings, dict):
        for key in ("baseURL", "baseUrl", "base_url", "website_url"):
            values.append(settings.get(key))
    if isinstance(options, dict):
        for key in ("baseURL", "baseUrl", "base_url"):
            values.append(options.get(key))
    for mapping in (auth, env):
        if not isinstance(mapping, dict):
            continue
        for key, value in mapping.items():
            if "BASE_URL" in str(key).upper() or str(key).lower() in {"baseurl", "base_url"}:
                values.append(value)

    values.append(_extract_config_value(config_text, "base_url"))
    values.append(row["website_url"])
    values.extend(endpoints.get((str(row["id"]), str(row["app_type"])), []))
    name = str(row["name"] or "").strip()
    if name.startswith("http://") or name.startswith("https://"):
        values.append(name)

    normalized: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        try:
            text = normalize_panel_base_url(text)
        except ConfigError:
            continue
        if text not in normalized:
            normalized.append(text)
    return normalized


def _collect_candidate_api_keys(settings: dict[str, Any]) -> list[str]:
    values: list[str] = []
    candidate_keys = [
        "OPENAI_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "apiKey",
        "api_key",
        "API_KEY",
    ]
    containers = [
        settings,
        settings.get("auth") if isinstance(settings, dict) else {},
        settings.get("options") if isinstance(settings, dict) else {},
        settings.get("env") if isinstance(settings, dict) else {},
    ]
    for mapping in containers:
        if not isinstance(mapping, dict):
            continue
        for key, value in mapping.items():
            normalized_key = str(key or "").strip()
            upper_key = normalized_key.upper()
            if normalized_key in candidate_keys or upper_key in {item.upper() for item in candidate_keys} or upper_key.endswith("API_KEY"):
                candidate_value = str(value or "").strip()
                if candidate_value and candidate_value not in values:
                    values.append(candidate_value)
    return values


def _collect_candidate_model_ids(settings: dict[str, Any], config_text: str) -> tuple[str, list[str]]:
    values: list[str] = []

    def add(value: Any) -> None:
        model_id = str(value or "").strip()
        if model_id and model_id not in values:
            values.append(model_id)

    default_model = _extract_config_value(config_text, "model")
    if default_model:
        add(default_model)

    models = settings.get("models")
    if isinstance(models, dict):
        for model_id in models.keys():
            add(model_id)

    model_entry = settings.get("model")
    if isinstance(model_entry, dict):
        add(model_entry.get("id") or model_entry.get("name"))
    else:
        add(model_entry)

    options = settings.get("options") or {}
    if isinstance(options, dict):
        add(options.get("model"))

    env = settings.get("env") or {}
    if isinstance(env, dict):
        for key, value in env.items():
            upper_key = str(key or "").upper()
            if upper_key.endswith("_MODEL") or "_DEFAULT_" in upper_key and upper_key.endswith("MODEL"):
                add(value)

    return default_model, values


def _infer_panel_api(app_type: str, wire_api: str, default_model_id: str, model_ids: list[str]) -> str:
    normalized_app_type = str(app_type or "").strip().lower()
    normalized_wire_api = str(wire_api or "").strip().lower()
    if normalized_wire_api in _WIRE_API_MAP:
        return _WIRE_API_MAP[normalized_wire_api]
    if normalized_app_type == "claude":
        return "anthropic-messages"
    if normalized_app_type == "gemini":
        return "google-generative-ai"
    if normalized_app_type in {"codex", "opencode"}:
        return "openai-completions"
    for model_id in [default_model_id, *model_ids]:
        inferred = infer_api_type(model_id)
        if inferred:
            return inferred
    return "openai-completions"


def _model_entries(model_ids: list[str], model_names: dict[str, str]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for model_id in model_ids:
        display_name = str(model_names.get(model_id) or model_id).strip() or model_id
        normalized = _normalize_model_entry({
            "id": model_id,
            "name": display_name,
            "suggested_api": infer_api_type(model_id),
        })
        entries.append(normalized)
    return entries


def _candidate_id(base_url: str) -> str:
    return hashlib.sha1(str(base_url).encode("utf-8")).hexdigest()[:12]


def _sorted_provider_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = connection.execute(
        """
        select id, app_type, name, settings_config, website_url, sort_index, is_current, in_failover_queue
        from providers
        where lower(app_type) in ('codex', 'opencode', 'claude', 'gemini')
        """
    ).fetchall()

    def sort_key(row: sqlite3.Row) -> tuple[int, int, int, str]:
        sort_index = row["sort_index"] if row["sort_index"] is not None else _DEFAULT_SORT_INDEX
        app_priority = _APP_TYPE_PRIORITY.get(str(row["app_type"] or "").lower(), 99)
        is_current = 0 if bool(row["is_current"]) else 1
        return (is_current, int(sort_index), app_priority, str(row["id"] or ""))

    return sorted(rows, key=sort_key)


def _load_ccswitch_sql(sql_text: str) -> sqlite3.Connection:
    temp_path = Path(tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False).name)
    try:
        connection = sqlite3.connect(str(temp_path))
        connection.row_factory = sqlite3.Row
        connection.executescript(sql_text)
        connection.execute("select 1 from providers limit 1")
        return connection
    except sqlite3.Error as exc:
        temp_path.unlink(missing_ok=True)
        raise ConfigError(f"CCSwitch SQL 解析失败：{exc}") from exc


def preview_ccswitch_provider_import(sql_text: str, state_dir: Path = DEFAULT_STATE_DIR, file_name: str = "") -> dict[str, Any]:
    content = str(sql_text or "")
    if not content.strip():
        raise ConfigError("SQL 内容不能为空。")

    connection = _load_ccswitch_sql(content)
    try:
        endpoints: dict[tuple[str, str], list[str]] = {}
        try:
            for row in connection.execute("select provider_id, app_type, url from provider_endpoints"):
                key = (str(row["provider_id"] or ""), str(row["app_type"] or ""))
                endpoints.setdefault(key, [])
                url = str(row["url"] or "").strip()
                if url and url not in endpoints[key]:
                    endpoints[key].append(url)
        except sqlite3.Error:
            endpoints = {}

        model_names: dict[str, str] = {}
        try:
            for row in connection.execute("select model_id, display_name from model_pricing"):
                model_id = str(row["model_id"] or "").strip()
                if not model_id:
                    continue
                model_names[model_id] = str(row["display_name"] or model_id).strip() or model_id
        except sqlite3.Error:
            model_names = {}

        store = load_panel_store(state_dir)
        existing_by_base_url: dict[str, str] = {}
        for provider_id, record in (store.get("providers") or {}).items():
            if not isinstance(record, dict):
                continue
            base_url = str(record.get("base_url") or "").strip()
            if not base_url:
                continue
            try:
                normalized_base_url = normalize_panel_base_url(base_url)
            except ConfigError:
                continue
            existing_by_base_url[normalized_base_url] = str(provider_id)

        groups: dict[str, dict[str, Any]] = {}
        skipped: list[dict[str, str]] = []

        for row in _sorted_provider_rows(connection):
            provider_id = str(row["id"] or "").strip()
            app_type = str(row["app_type"] or "").strip().lower()
            if app_type not in _SUPPORTED_PROVIDER_APP_TYPES:
                continue
            row_warnings: list[str] = []
            settings = _extract_json(row["settings_config"], row_warnings, provider_id)
            config_text = str(settings.get("config") or "") if isinstance(settings, dict) else ""
            base_urls = _collect_candidate_urls(row, settings, endpoints, config_text)
            api_keys = _collect_candidate_api_keys(settings)
            default_model_id, model_ids = _collect_candidate_model_ids(settings, config_text)
            wire_api = _extract_config_value(config_text, "wire_api")
            api = _infer_panel_api(app_type, wire_api, default_model_id, model_ids)

            if not base_urls:
                skipped.append({
                    "source_provider_id": provider_id,
                    "app_type": app_type,
                    "reason": "未识别到 Base URL",
                })
                continue
            if not api_keys:
                skipped.append({
                    "source_provider_id": provider_id,
                    "app_type": app_type,
                    "reason": "未识别到 API Key",
                })
                continue

            base_url = base_urls[0]
            group = groups.setdefault(
                base_url,
                {
                    "base_url": base_url,
                    "source_provider_ids": [],
                    "source_app_types": [],
                    "row_labels": [],
                    "api_keys": [],
                    "model_ids": [],
                    "default_model_candidates": [],
                    "api_candidates": [],
                    "warnings": [],
                    "sort_index": row["sort_index"] if row["sort_index"] is not None else _DEFAULT_SORT_INDEX,
                    "is_current": bool(row["is_current"]),
                },
            )

            if provider_id not in group["source_provider_ids"]:
                group["source_provider_ids"].append(provider_id)
            if app_type not in group["source_app_types"]:
                group["source_app_types"].append(app_type)
            row_label = str(row["name"] or provider_id).strip() or provider_id
            if row_label not in group["row_labels"]:
                group["row_labels"].append(row_label)
            if bool(row["is_current"]):
                group["is_current"] = True
            group["sort_index"] = min(int(group["sort_index"]), int(row["sort_index"] or _DEFAULT_SORT_INDEX))
            group["api_keys"] = _dedupe_strings([*group["api_keys"], *api_keys])
            group["model_ids"] = _dedupe_strings([*group["model_ids"], *model_ids])
            if default_model_id:
                group["default_model_candidates"] = _dedupe_strings([default_model_id, *group["default_model_candidates"]])
            group["api_candidates"] = _dedupe_strings([api, *group["api_candidates"]])
            group["warnings"] = _dedupe_strings([*group["warnings"], *row_warnings])
            if wire_api and wire_api not in group["warnings"] and api == "openai-responses":
                # no-op, preserve explicit responses mapping without adding extra noise
                pass

        ordered_groups = sorted(
            groups.values(),
            key=lambda item: (
                0 if bool(item.get("is_current")) else 1,
                int(item.get("sort_index") or _DEFAULT_SORT_INDEX),
                str(item.get("base_url") or ""),
            ),
        )

        preview_items: list[dict[str, Any]] = []
        reserved_names = set(str(key) for key in (store.get("providers") or {}).keys())
        for group in ordered_groups:
            base_url = str(group.get("base_url") or "").strip()
            api_candidates = _dedupe_strings(group.get("api_candidates") or [])
            default_candidates = _dedupe_strings(group.get("default_model_candidates") or [])
            model_ids = _dedupe_strings(group.get("model_ids") or [])
            default_model_id = default_candidates[0] if default_candidates else (model_ids[0] if model_ids else "")
            if default_model_id and default_model_id not in model_ids:
                model_ids.insert(0, default_model_id)

            warnings = _dedupe_strings(group.get("warnings") or [])
            if len(api_candidates) > 1:
                warnings.append(f"同一 Base URL 在 CCSwitch 中存在多种 API 协议，已按优先级使用 {api_candidates[0]}。")
            if len(default_candidates) > 1:
                warnings.append(f"同一 Base URL 在 CCSwitch 中存在多个默认模型，已优先使用 {default_candidates[0]}。")

            preview_provider_name = _dedupe_provider_name(_provider_name_from_base_url(base_url), reserved_names)
            reserved_names.add(preview_provider_name)
            existing_provider = existing_by_base_url.get(base_url, "")
            can_import = not bool(existing_provider)
            if existing_provider:
                warnings.append(f"当前面板已存在相同 Base URL：{existing_provider}。")

            preview_items.append(
                {
                    "id": _candidate_id(base_url),
                    "provider": preview_provider_name,
                    "base_url": base_url,
                    "api": api_candidates[0] if api_candidates else "openai-completions",
                    "api_keys": copy.deepcopy(group.get("api_keys") or []),
                    "api_key_count": len(group.get("api_keys") or []),
                    "api_key_masks": [mask_key(value) for value in (group.get("api_keys") or [])],
                    "model_ids": model_ids,
                    "model_entries": _model_entries(model_ids, model_names),
                    "default_model_id": default_model_id,
                    "source_provider_ids": copy.deepcopy(group.get("source_provider_ids") or []),
                    "source_app_types": copy.deepcopy(group.get("source_app_types") or []),
                    "source_labels": copy.deepcopy(group.get("row_labels") or []),
                    "warnings": warnings,
                    "existing_provider": existing_provider,
                    "can_import": can_import,
                }
            )

        return {
            "file_name": str(file_name or "").strip(),
            "items": preview_items,
            "skipped": skipped,
            "summary": {
                "recognized": len(preview_items),
                "importable": sum(1 for item in preview_items if item.get("can_import")),
                "skipped": len(skipped),
            },
        }
    finally:
        path = Path(connection.execute("pragma database_list").fetchone()[2])
        connection.close()
        if path.exists():
            path.unlink(missing_ok=True)


def apply_ccswitch_provider_import(items: list[Any], state_dir: Path = DEFAULT_STATE_DIR) -> dict[str, Any]:
    if not isinstance(items, list) or not items:
        raise ConfigError("请选择至少一个 Provider 再导入。")

    store = load_panel_store(state_dir)
    providers = store.setdefault("providers", {})
    provider_order = list(store.get("providerOrder") or [])
    existing_names = set(str(key) for key in providers.keys())
    existing_catalog = {str(item.get("id") or "").strip(): copy.deepcopy(item) for item in (store.get("modelCatalog") or []) if isinstance(item, dict)}

    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    added_model_ids: list[str] = []

    for raw_item in items:
        if not isinstance(raw_item, dict):
            skipped.append({"provider": "", "base_url": "", "reason": "导入项格式错误"})
            continue
        provider_preview_name = str(raw_item.get("provider") or "").strip()
        base_url = str(raw_item.get("base_url") or "").strip()
        if not base_url:
            skipped.append({"provider": provider_preview_name, "base_url": "", "reason": "缺少 Base URL"})
            continue
        try:
            base_url = normalize_panel_base_url(base_url)
        except ConfigError as exc:
            skipped.append({"provider": provider_preview_name, "base_url": base_url, "reason": str(exc)})
            continue

        try:
            _ensure_unique_provider_base_url(providers, base_url)
        except ConfigError as exc:
            skipped.append({"provider": provider_preview_name, "base_url": base_url, "reason": str(exc)})
            continue

        payload = {
            "provider": provider_preview_name,
            "base_url": base_url,
            "api": str(raw_item.get("api") or "openai-completions").strip() or "openai-completions",
            "api_keys": _dedupe_strings(raw_item.get("api_keys") or []),
            "model_ids": _dedupe_strings(raw_item.get("model_ids") or []),
            "default_model_id": str(raw_item.get("default_model_id") or "").strip(),
            "enabled": True,
            "keep_other_providers": True,
        }
        if not payload["api_keys"]:
            skipped.append({"provider": provider_preview_name, "base_url": base_url, "reason": "缺少 API Key"})
            continue

        record = _sanitize_provider_record(payload.get("provider") or "", payload)
        resolved_name = _dedupe_provider_name(record["provider"], existing_names)
        record["provider"] = resolved_name
        providers[resolved_name] = record
        existing_names.add(resolved_name)
        if resolved_name not in provider_order:
            provider_order.append(resolved_name)

        model_entries = raw_item.get("model_entries") or []
        if isinstance(model_entries, list):
            for entry in model_entries:
                if not isinstance(entry, dict):
                    continue
                model_id = str(entry.get("id") or "").strip()
                if not model_id or model_id in existing_catalog:
                    continue
                existing_catalog[model_id] = _normalize_model_entry(entry)
                added_model_ids.append(model_id)
        imported.append(
            {
                "provider": resolved_name,
                "base_url": base_url,
                "api": record.get("api") or "",
                "default_model_id": record.get("default_model_id") or "",
            }
        )

    if not imported:
        raise ConfigError(skipped[0]["reason"] if skipped else "没有可导入的 Provider。")

    store["providerOrder"] = provider_order
    if not store.get("selectedProvider") and provider_order:
        store["selectedProvider"] = provider_order[0]
    store["modelCatalog"] = list(existing_catalog.values())
    _write_panel_store(store, state_dir)

    return {
        "imported": imported,
        "skipped": skipped,
        "added_model_ids": _dedupe_strings(added_model_ids),
        "provider_count": len(providers),
        "model_count": len(store.get("modelCatalog") or []),
        "path": store.get("path") or str(state_dir / "config-panel-store.json"),
    }
