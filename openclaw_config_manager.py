"""Core store management and OpenClaw config translation helpers.

The panel keeps its own normalized store (`config-panel-store.json`) and this
module is responsible for:

- validating and persisting panel-side Provider / Agent / Channel records
- probing upstream providers to discover usable models and route styles
- translating panel data back into OpenClaw runtime config
- restarting the OpenClaw gateway after apply operations when requested
"""
from __future__ import annotations

import copy
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


DEFAULT_STATE_DIR = Path("/root/.openclaw")
BUILTIN_PROVIDERS = {"ollama", "ollamaLocal"}
PANEL_STORE_VERSION = 5
PRESETS_FILENAME = "config-panel-presets.json"
STORE_FILENAME = "config-panel-store.json"
EXEC_APPROVALS_FILENAME = "exec-approvals.json"
OPENAI_CODEX_PROVIDER = "openai-codex"
OPENAI_CODEX_BASE_URL = "https://chatgpt.com/backend-api"
OPENAI_CODEX_API = "openai-codex-responses"
OPENAI_CODEX_DEFAULT_MODEL_ID = "gpt-5.4"
OPENAI_CODEX_MODEL_CATALOG = [
    {"id": "gpt-5.4", "name": "GPT-5.4"},
    {"id": "gpt-5.3-codex", "name": "GPT-5.3 Codex"},
    {"id": "gpt-5.3-codex-spark", "name": "GPT-5.3 Codex Spark"},
    {"id": "gpt-5.2-codex", "name": "GPT-5.2 Codex"},
    {"id": "gpt-5.1-codex", "name": "GPT-5.1 Codex"},
]
PROVIDER_MODEL_PROBE_API_TYPES = (
    "openai-completions",
    "openai-responses",
    "anthropic-messages",
    "google-generative-ai",
    "ollama",
)
PROVIDER_MODEL_PROBE_TIMEOUT = (1.5, 3.5)
PROVIDER_MODEL_PROBE_MAX_WORKERS = 12
PROVIDER_REFRESH_MAX_WORKERS = 8
PROVIDER_MODEL_RETRY_ATTEMPTS = 2
PROVIDER_MODEL_WARMUP_MAX_MODELS = 6
PROVIDER_MODEL_WARMUP_MAX_WORKERS = 4
PROVIDER_HTTP_POOL_SIZE = 96
# Probe candidates are intentionally broader than the runtime-supported subset:
# discovery needs to identify what an upstream accepts first, then the panel
# can decide whether the successful route is safe to write back into OpenClaw.
PROVIDER_MODEL_ROUTE_CANDIDATES = (
    {"api": "openai-completions", "route_suffix": "/v1/chat/completions", "runtime_supported": True},
    {"api": "openai-responses", "route_suffix": "/v1/responses", "runtime_supported": True},
    {"api": "openai-completions", "route_suffix": "/v1/v1/chat/completions", "runtime_supported": True},
    {"api": "openai-responses", "route_suffix": "/v1/v1/responses", "runtime_supported": True},
    {"api": "anthropic-messages", "route_suffix": "/v1/messages", "runtime_supported": True},
    {"api": "anthropic-messages", "route_suffix": "/v1/v1/messages", "runtime_supported": True},
    {"api": "google-generative-ai", "route_suffix": "/models/{model}:generateContent", "runtime_supported": True},
    {"api": "google-generative-ai", "route_suffix": "/v1beta/models/{model}:generateContent", "runtime_supported": True},
    {"api": "google-generative-ai", "route_suffix": "/v1/v1beta/models/{model}:generateContent", "runtime_supported": True},
    {"api": "google-generative-ai", "route_suffix": "/v1/models/{model}:generateContent", "runtime_supported": True},
    {"api": "google-generative-ai", "route_suffix": "/v1/v1/models/{model}:generateContent", "runtime_supported": True},
    {"api": "ollama", "route_suffix": "/api/chat", "runtime_supported": True},
    {"api": "ollama", "route_suffix": "/v1/chat", "runtime_supported": False},
    {"api": "ollama", "route_suffix": "/v1/v1/chat", "runtime_supported": False},
    {"api": "openai-completions", "route_suffix": "/chat/completions", "runtime_supported": True},
    {"api": "openai-responses", "route_suffix": "/responses", "runtime_supported": True},
    {"api": "anthropic-messages", "route_suffix": "/messages", "runtime_supported": True},
    {"api": "ollama", "route_suffix": "/chat", "runtime_supported": False},
)
PROVIDER_MODEL_LIST_ROUTE_SUFFIXES = (
    "/models",
    "/v1/models",
    "/v1/v1/models",
    "/v1beta/models",
    "/v1/v1beta/models",
    "/api/tags",
)
PROVIDER_API_STANDARD_SUFFIXES = {
    "openai-completions": "/v1",
    "openai-responses": "/v1",
    "anthropic-messages": "/v1",
    "google-generative-ai": "/v1beta",
    "ollama": "/api",
}
PROVIDER_BASE_URL_STANDARD_SUFFIXES = tuple(
    sorted({suffix for suffix in PROVIDER_API_STANDARD_SUFFIXES.values() if suffix}, key=len, reverse=True)
)
SUPPORTED_ELEVATED_CHANNELS = ("webchat", "qqbot")
AGENT_LEVEL_ELEVATED_CHANNELS = ("webchat",)
CHANNEL_LEVEL_ELEVATED_CHANNELS = ("qqbot",)
AGENT_EXEC_SECURITY_OPTIONS = {"", "deny", "allowlist", "full"}
AGENT_EXEC_ASK_OPTIONS = {"", "off", "on-miss", "always"}
AGENT_ELEVATED_MODE_OPTIONS = {"", "off", "on"}
CHANNEL_PLUGIN_IDS = {
    "qqbot": "openclaw-qqbot",
    "ddingtalk": "ddingtalk",
    "wecom": "wecom",
    "feishu": "feishu",
}
CHANNEL_PLUGIN_LEGACY_IDS = {
    "qqbot": ("qqbot",),
}


class ConfigError(RuntimeError):
    """User-facing configuration error returned to the panel API."""
    pass


_HTTP_SESSION_LOCAL = threading.local()


def normalize_base_url(base_url: str) -> str:
    return normalize_panel_base_url(base_url)


def normalize_panel_base_url(base_url: str) -> str:
    value = (base_url or "").strip().rstrip("/")
    if not value:
        raise ConfigError("`site/base_url` 不能为空。")
    return value


def normalize_provider_site(base_url: str) -> str:
    value = normalize_panel_base_url(base_url)
    lowered = value.lower().rstrip("/")
    for suffix in ("/v1/v1beta", "/v1beta", "/v1/v1", "/v1", "/api"):
        if lowered.endswith(suffix):
            stripped = value[: -len(suffix)].rstrip("/")
            if stripped:
                return stripped
    return value


def _http_session() -> requests.Session:
    session = getattr(_HTTP_SESSION_LOCAL, "session", None)
    if isinstance(session, requests.Session):
        return session
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=PROVIDER_HTTP_POOL_SIZE,
        pool_maxsize=PROVIDER_HTTP_POOL_SIZE,
        max_retries=0,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    _HTTP_SESSION_LOCAL.session = session
    return session


def _is_generic_provider_name(value: str) -> bool:
    return str(value or "").strip().startswith("provider-")


def _provider_name_from_base_url(base_url: str) -> str:
    parsed = urlparse(normalize_provider_site(base_url))
    host = (parsed.hostname or parsed.netloc or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", host).strip("-")
    return slug or "provider"


def _dedupe_provider_name(candidate: str, existing_names: set[str], original_name: str = "") -> str:
    base = candidate or "provider"
    if base == original_name or base not in existing_names:
        return base
    index = 1
    while f"{base}-{index}" in existing_names:
        index += 1
    return f"{base}-{index}"


def _ensure_unique_provider_base_url(
    providers: dict[str, Any],
    base_url: str,
    *,
    original_name: str = "",
) -> None:
    target = normalize_provider_site(base_url)
    for provider_id, record in (providers or {}).items():
        if provider_id == original_name or not isinstance(record, dict):
            continue
        existing = normalize_provider_site(str(record.get("site") or record.get("base_url") or ""))
        if existing == target:
            raise ConfigError(f"Site 已存在：{target}")


def mask_key(value: str) -> str:
    raw = (value or "").strip()
    if len(raw) <= 10:
        return "*" * len(raw)
    return f"{raw[:7]}...{raw[-6:]}"


def openai_codex_model_catalog() -> list[dict[str, str]]:
    return [copy.deepcopy(item) for item in OPENAI_CODEX_MODEL_CATALOG]


def _default_runtime_auth_config() -> dict[str, Any]:
    return {
        "mode": "provider",
        "oauth": {
            "provider": OPENAI_CODEX_PROVIDER,
            "default_model_id": OPENAI_CODEX_DEFAULT_MODEL_ID,
        },
    }


def _normalize_runtime_auth_config(value: Any, *, current_provider: str = "") -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    oauth_payload = payload.get("oauth") if isinstance(payload.get("oauth"), dict) else {}
    default_model_id = str(
        oauth_payload.get("default_model_id")
        or oauth_payload.get("model_id")
        or oauth_payload.get("model")
        or payload.get("oauth_model_id")
        or payload.get("oauthModelId")
        or ""
    ).strip()
    if default_model_id.startswith(f"{OPENAI_CODEX_PROVIDER}/"):
        default_model_id = default_model_id.split("/", 1)[1].strip()
    if not default_model_id:
        default_model_id = OPENAI_CODEX_DEFAULT_MODEL_ID
    mode = str(payload.get("mode") or "").strip().lower()
    if mode not in {"provider", "oauth"}:
        mode = "oauth" if str(current_provider or "").strip().lower() == OPENAI_CODEX_PROVIDER else "provider"
    return {
        "mode": mode,
        "oauth": {
            "provider": OPENAI_CODEX_PROVIDER,
            "default_model_id": default_model_id,
        },
    }


def _runtime_auth_model_ref(runtime_auth: dict[str, Any]) -> str:
    oauth = runtime_auth.get("oauth") if isinstance(runtime_auth, dict) else {}
    model_id = str((oauth or {}).get("default_model_id") or OPENAI_CODEX_DEFAULT_MODEL_ID).strip() or OPENAI_CODEX_DEFAULT_MODEL_ID
    return f"{OPENAI_CODEX_PROVIDER}/{model_id}"


def _openai_codex_provider_config() -> dict[str, Any]:
    return {
        "baseUrl": OPENAI_CODEX_BASE_URL,
        "api": OPENAI_CODEX_API,
        "models": [],
    }


def _auth_profile_matches_provider(profile_id: str, profile: Any, provider: str) -> bool:
    target = str(provider or "").strip().lower()
    if not target:
        return False
    normalized_profile_id = str(profile_id or "").strip().lower()
    provider_id = str((profile or {}).get("provider") or "").strip().lower()
    return provider_id == target or normalized_profile_id.startswith(f"{target}:")


def _preserve_unmanaged_auth_data(
    auth_data: dict[str, Any],
    managed_providers: set[str],
    *,
    include_runtime: bool,
) -> dict[str, Any]:
    profiles = auth_data.get("profiles") or {}
    preserved_profiles: dict[str, Any] = {}
    preserved_profile_ids: set[str] = set()
    normalized_managed = {str(item or "").strip().lower() for item in managed_providers if str(item or "").strip()}
    for profile_id, profile in profiles.items():
        if any(_auth_profile_matches_provider(str(profile_id), profile, provider) for provider in normalized_managed):
            continue
        preserved_profiles[str(profile_id)] = copy.deepcopy(profile)
        preserved_profile_ids.add(str(profile_id))

    preserved_order: dict[str, Any] = {}
    for provider_id, order_value in (auth_data.get("order") or {}).items():
        normalized_provider_id = str(provider_id or "").strip().lower()
        if normalized_provider_id in normalized_managed:
            continue
        preserved_order[str(provider_id)] = copy.deepcopy(order_value)

    preserved_last_good: dict[str, Any] = {}
    preserved_usage_stats: dict[str, Any] = {}
    if include_runtime:
        for profile_id, payload in (auth_data.get("lastGood") or {}).items():
            if str(profile_id) in preserved_profile_ids:
                preserved_last_good[str(profile_id)] = copy.deepcopy(payload)
        for profile_id, payload in (auth_data.get("usageStats") or {}).items():
            if str(profile_id) in preserved_profile_ids:
                preserved_usage_stats[str(profile_id)] = copy.deepcopy(payload)

    return {
        "profiles": preserved_profiles,
        "order": preserved_order,
        "lastGood": preserved_last_good,
        "usageStats": preserved_usage_stats,
    }


def _safe_load_json(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    return load_json(path)


def get_runtime_auth_status(state_dir: Path = DEFAULT_STATE_DIR) -> dict[str, Any]:
    openclaw_data = load_json(state_dir / "openclaw.json")
    current_primary_model = str((((openclaw_data.get("agents") or {}).get("defaults") or {}).get("model") or {}).get("primary") or "").strip()
    current_provider = current_primary_model.split("/", 1)[0] if "/" in current_primary_model else ""
    store = load_panel_store(state_dir)
    runtime_auth = _normalize_runtime_auth_config(store.get("runtimeAuth") or {}, current_provider=current_provider)
    auth_store_path = _main_auth_store(state_dir)
    auth_data = _safe_load_json(auth_store_path)
    oauth_profiles: list[dict[str, Any]] = []
    for profile_id, profile in (auth_data.get("profiles") or {}).items():
        if not _auth_profile_matches_provider(str(profile_id), profile, OPENAI_CODEX_PROVIDER):
            continue
        raw_type = str((profile or {}).get("type") or (profile or {}).get("mode") or "").strip().lower()
        expires = (profile or {}).get("expires")
        if expires in (None, ""):
            expires = (profile or {}).get("expires_at")
        if expires in (None, ""):
            expires = (profile or {}).get("expiresAt")
        oauth_profiles.append({
            "profile_id": str(profile_id),
            "provider": str((profile or {}).get("provider") or OPENAI_CODEX_PROVIDER).strip() or OPENAI_CODEX_PROVIDER,
            "type": raw_type or "oauth",
            "email": str((profile or {}).get("email") or "").strip(),
            "account_id": str((profile or {}).get("accountId") or (profile or {}).get("account_id") or "").strip(),
            "expires": expires if isinstance(expires, (int, float)) else None,
        })
    oauth_profiles.sort(key=lambda item: float(item.get("expires") or 0), reverse=True)
    active_oauth = oauth_profiles[0] if oauth_profiles else {}
    return {
        "mode": runtime_auth["mode"],
        "current_mode": "oauth" if current_provider.lower() == OPENAI_CODEX_PROVIDER else "provider",
        "current_model_ref": current_primary_model,
        "oauth": {
            "provider": OPENAI_CODEX_PROVIDER,
            "authenticated": bool(oauth_profiles),
            "default_model_id": str((runtime_auth.get("oauth") or {}).get("default_model_id") or OPENAI_CODEX_DEFAULT_MODEL_ID).strip() or OPENAI_CODEX_DEFAULT_MODEL_ID,
            "default_model_ref": _runtime_auth_model_ref(runtime_auth),
            "profile_count": len(oauth_profiles),
            "profiles": oauth_profiles,
            "email": str(active_oauth.get("email") or "").strip(),
            "account_id": str(active_oauth.get("account_id") or "").strip(),
            "expires": active_oauth.get("expires"),
            "available_models": openai_codex_model_catalog(),
            "auth_store": str(auth_store_path) if auth_store_path else "",
        },
    }


def save_runtime_auth_config(payload: dict[str, Any], state_dir: Path = DEFAULT_STATE_DIR) -> dict[str, Any]:
    store = load_panel_store(state_dir)
    current_status = get_runtime_auth_status(state_dir)
    runtime_auth = _normalize_runtime_auth_config(payload, current_provider=str((current_status or {}).get("current_model_ref") or "").split("/", 1)[0])
    if runtime_auth["mode"] == "oauth" and not bool(((current_status.get("oauth") or {}).get("authenticated"))):
        raise ConfigError("尚未完成 OpenAI OAuth 登录，不能切换到 OAuth 模式。")
    store["runtimeAuth"] = runtime_auth
    _write_panel_store(store, state_dir)

    apply_now = payload.get("apply")
    if apply_now is None:
        apply_now = True
    if bool(apply_now):
        result = apply_store_config(state_dir, restart_gateway=bool(payload.get("restart_gateway", True)))
        result["runtimeAuth"] = runtime_auth
        result["runtimeAuthStatus"] = get_runtime_auth_status(state_dir)
        return result

    return {
        "runtimeAuth": runtime_auth,
        "runtimeAuthStatus": get_runtime_auth_status(state_dir),
        "path": store["path"],
    }


def alias_for_model_id(model_id: str) -> str | None:
    if not model_id.startswith("gpt-"):
        return None
    return f"gpt{model_id[len('gpt-'):]}"


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"缺少配置文件：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _split_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    raw_items: list[Any]
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, str):
        raw_items = re.split(r"[\n,]+", value)
    else:
        raw_items = [value]
    items: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        normalized = str(item or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        items.append(normalized)
    return items


def _normalize_exec_security(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in AGENT_EXEC_SECURITY_OPTIONS:
        raise ConfigError(f"不支持的 exec 安全策略：{value}")
    return normalized


def _normalize_exec_ask(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in AGENT_EXEC_ASK_OPTIONS:
        raise ConfigError(f"不支持的 exec 审批策略：{value}")
    return normalized


def _normalize_elevated_mode(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in AGENT_ELEVATED_MODE_OPTIONS:
        raise ConfigError(f"不支持的提权模式：{value}")
    return normalized


def _normalize_allow_from_map(
    value: Any,
    supported_channels: tuple[str, ...] = SUPPORTED_ELEVATED_CHANNELS,
) -> dict[str, list[str]]:
    payload = value if isinstance(value, dict) else {}
    normalized: dict[str, list[str]] = {}
    for channel in supported_channels:
        items = _split_text_list(payload.get(channel))
        if items:
            normalized[channel] = items
    return normalized


def _normalize_exec_allowlist(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_items: list[Any] = value
    else:
        raw_items = _split_text_list(value)
    patterns: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        if isinstance(item, dict):
            pattern = str(item.get("pattern") or "").strip()
        else:
            pattern = str(item or "").strip()
        if not pattern or pattern in seen:
            continue
        seen.add(pattern)
        patterns.append(pattern)
    return patterns


def _normalize_agent_permissions_from_sources(
    exec_source: Any = None,
    elevated_source: Any = None,
) -> dict[str, Any]:
    exec_payload = exec_source if isinstance(exec_source, dict) else {}
    elevated_payload = elevated_source if isinstance(elevated_source, dict) else {}

    exec_security = _normalize_exec_security(exec_payload.get("security"))
    exec_ask = _normalize_exec_ask(exec_payload.get("ask"))
    exec_allowlist = _normalize_exec_allowlist(
        exec_payload.get("allowlist")
        if "allowlist" in exec_payload
        else exec_payload.get("patterns")
    )

    elevated_mode = _normalize_elevated_mode(elevated_payload.get("mode"))
    allow_from = _normalize_allow_from_map(
        elevated_payload.get("allow_from")
        if "allow_from" in elevated_payload
        else elevated_payload.get("allowFrom")
    )
    if not elevated_mode and allow_from:
        elevated_mode = "on"
    if elevated_mode == "off":
        allow_from = {}

    return {
        "exec": {
            "security": exec_security,
            "ask": exec_ask,
            "allowlist": exec_allowlist,
        },
        "elevated": {
            "mode": elevated_mode,
            "allow_from": allow_from,
        },
    }


def _empty_agent_permissions() -> dict[str, Any]:
    return _normalize_agent_permissions_from_sources({}, {})


def _normalize_channel_permissions_from_sources(source: Any = None) -> dict[str, Any]:
    payload = source if isinstance(source, dict) else {}
    elevated_payload = payload.get("elevated") if isinstance(payload.get("elevated"), dict) else payload
    allow_from = _normalize_allow_from_map(
        elevated_payload.get("allow_from")
        if isinstance(elevated_payload, dict) and "allow_from" in elevated_payload
        else elevated_payload.get("allowFrom") if isinstance(elevated_payload, dict) else {},
        CHANNEL_LEVEL_ELEVATED_CHANNELS,
    )
    return {
        "elevated": {
            "allow_from": allow_from,
        },
    }


def _empty_channel_permissions() -> dict[str, Any]:
    return _normalize_channel_permissions_from_sources({})


def _merge_allow_from_maps(*maps: dict[str, list[str]]) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    for item in maps:
        normalized = _normalize_allow_from_map(item)
        for channel, values in normalized.items():
            bucket = merged.setdefault(channel, [])
            for value in values:
                if value not in bucket:
                    bucket.append(value)
    return merged


def _strip_allow_from_channels(value: Any, channels: tuple[str, ...]) -> dict[str, list[str]]:
    allow_from = _normalize_allow_from_map(value)
    return {
        channel: values
        for channel, values in allow_from.items()
        if channel not in channels
    }


def _strip_channel_level_permissions_from_agent(permissions: Any) -> dict[str, Any]:
    payload = copy.deepcopy(permissions if isinstance(permissions, dict) else {})
    elevated = payload.get("elevated") if isinstance(payload.get("elevated"), dict) else {}
    payload["elevated"] = {
        "mode": _normalize_elevated_mode((elevated or {}).get("mode")),
        "allow_from": _normalize_allow_from_map(
            _strip_allow_from_channels((elevated or {}).get("allow_from"), CHANNEL_LEVEL_ELEVATED_CHANNELS),
            AGENT_LEVEL_ELEVATED_CHANNELS,
        ),
    }
    normalized = _normalize_agent_permissions_from_sources(payload.get("exec"), payload.get("elevated"))
    normalized["elevated"]["allow_from"] = _normalize_allow_from_map(
        normalized["elevated"].get("allow_from"),
        AGENT_LEVEL_ELEVATED_CHANNELS,
    )
    return normalized


def _channel_store_key(platform: str, account_id: str) -> str:
    normalized_platform = _normalize_channel_platform(platform)
    normalized_account_id = _normalize_channel_account_id(account_id)
    return f"{normalized_platform}:{normalized_account_id}"


def _normalize_channel_meta_record(key: str, payload: Any) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(payload, dict):
        return None
    raw_platform = str(payload.get("platform") or payload.get("channel") or "").strip()
    raw_account_id = str(payload.get("account_id") or payload.get("accountId") or "").strip()
    if not raw_platform or not raw_account_id:
        if ":" not in key:
            return None
        raw_platform, raw_account_id = key.split(":", 1)
    store_key = _channel_store_key(raw_platform, raw_account_id)
    platform, account_id = store_key.split(":", 1)
    return store_key, {
        "platform": platform,
        "account_id": account_id,
        "permissions": _normalize_channel_permissions_from_sources(payload.get("permissions")),
    }


def _migrate_channel_level_permissions_from_agents(
    agents_map: dict[str, Any],
    channels_map: dict[str, Any],
) -> bool:
    changed = False
    for record in (agents_map or {}).values():
        if not isinstance(record, dict):
            continue
        permissions = (record.get("permissions") or {}) if isinstance(record.get("permissions"), dict) else {}
        elevated = permissions.get("elevated") if isinstance(permissions, dict) else {}
        allow_from = _normalize_allow_from_map((elevated or {}).get("allow_from"))
        channel_level_allow_from = _normalize_allow_from_map(allow_from, CHANNEL_LEVEL_ELEVATED_CHANNELS)
        if not channel_level_allow_from:
            continue
        migrated = False
        for binding in record.get("bindings") or []:
            if str((binding or {}).get("channel") or "").strip() != "qqbot":
                continue
            binding_key = _binding_key(binding)
            if not binding_key:
                continue
            existing_meta = channels_map.get(binding_key) or {
                "platform": "qqbot",
                "account_id": str((binding or {}).get("account_id") or "").strip(),
                "permissions": _empty_channel_permissions(),
            }
            existing_permissions = existing_meta.get("permissions") if isinstance(existing_meta.get("permissions"), dict) else {}
            existing_allow_from = (
                _normalize_channel_permissions_from_sources(existing_permissions)
                .get("elevated", {})
                .get("allow_from") or {}
            )
            next_meta = copy.deepcopy(existing_meta)
            next_meta["permissions"] = {
                "elevated": {
                    "allow_from": _merge_allow_from_maps(existing_allow_from, channel_level_allow_from),
                },
            }
            if channels_map.get(binding_key) != next_meta:
                channels_map[binding_key] = next_meta
                changed = True
            migrated = True
        if migrated:
            next_permissions = _strip_channel_level_permissions_from_agent(permissions)
            if record.get("permissions") != next_permissions:
                record["permissions"] = next_permissions
                changed = True
    return changed


def _effective_agent_allow_from(record: dict[str, Any], channels_map: dict[str, Any]) -> dict[str, list[str]]:
    permissions = (record.get("permissions") or {}) if isinstance(record.get("permissions"), dict) else {}
    elevated = permissions.get("elevated") if isinstance(permissions, dict) else {}
    allow_from = _normalize_allow_from_map((elevated or {}).get("allow_from"), AGENT_LEVEL_ELEVATED_CHANNELS)
    for binding in record.get("bindings") or []:
        binding_key = _binding_key(binding)
        if not binding_key:
            continue
        channel_record = channels_map.get(binding_key) if isinstance(channels_map, dict) else None
        channel_permissions = (channel_record or {}).get("permissions") if isinstance(channel_record, dict) else {}
        channel_allow_from = (
            _normalize_channel_permissions_from_sources(channel_permissions)
            .get("elevated", {})
            .get("allow_from") or {}
        )
        allow_from = _merge_allow_from_maps(allow_from, channel_allow_from)
    return allow_from


def _exec_approvals_path(state_dir: Path) -> Path:
    return state_dir / EXEC_APPROVALS_FILENAME


def _default_exec_approvals_store(state_dir: Path) -> dict[str, Any]:
    return {
        "version": 1,
        "socket": {
            "path": str(state_dir / "exec-approvals.sock"),
            "token": secrets.token_urlsafe(24),
        },
        "defaults": {},
        "agents": {},
    }


def _load_exec_approvals_store(state_dir: Path = DEFAULT_STATE_DIR) -> dict[str, Any]:
    path = _exec_approvals_path(state_dir)
    if not path.exists():
        return _default_exec_approvals_store(state_dir)
    data = load_json(path)
    store = _default_exec_approvals_store(state_dir)
    if isinstance(data.get("socket"), dict):
        store["socket"]["path"] = str(data["socket"].get("path") or store["socket"]["path"]).strip() or store["socket"]["path"]
        token = str(data["socket"].get("token") or "").strip()
        if token:
            store["socket"]["token"] = token
    if isinstance(data.get("defaults"), dict):
        store["defaults"] = copy.deepcopy(data["defaults"])
    if isinstance(data.get("agents"), dict):
        store["agents"] = copy.deepcopy(data["agents"])
    return store


def _serialize_exec_allowlist_entries(patterns: list[str], existing_entries: Any = None) -> list[dict[str, Any]]:
    existing_map: dict[str, dict[str, Any]] = {}
    if isinstance(existing_entries, list):
        for item in existing_entries:
            if not isinstance(item, dict):
                continue
            pattern = str(item.get("pattern") or "").strip()
            if not pattern:
                continue
            existing_map[pattern.lower()] = copy.deepcopy(item)
    entries: list[dict[str, Any]] = []
    for pattern in patterns:
        existing = existing_map.get(pattern.lower())
        if isinstance(existing, dict):
            existing["pattern"] = pattern
            entries.append(existing)
        else:
            entries.append({"pattern": pattern})
    return entries


def _write_exec_approvals_config(state_dir: Path, agents_map: dict[str, Any]) -> str:
    approvals = _load_exec_approvals_store(state_dir)
    existing_agents = approvals.get("agents") if isinstance(approvals.get("agents"), dict) else {}
    next_agents: dict[str, Any] = {}
    for agent_id, record in existing_agents.items():
        if str(agent_id or "").strip() not in agents_map:
            next_agents[str(agent_id)] = copy.deepcopy(record)
    for agent_id, record in (agents_map or {}).items():
        permissions = (record or {}).get("permissions") or {}
        exec_config = permissions.get("exec") if isinstance(permissions, dict) else {}
        security = _normalize_exec_security((exec_config or {}).get("security"))
        ask = _normalize_exec_ask((exec_config or {}).get("ask"))
        allowlist_patterns = _normalize_exec_allowlist((exec_config or {}).get("allowlist"))
        if not security and not ask and not allowlist_patterns:
            continue
        existing_entry = existing_agents.get(agent_id) if isinstance(existing_agents, dict) else {}
        entry: dict[str, Any] = {}
        if isinstance(existing_entry, dict):
            for key in ("askFallback", "autoAllowSkills"):
                if key in existing_entry:
                    entry[key] = copy.deepcopy(existing_entry[key])
        if security:
            entry["security"] = security
        if ask:
            entry["ask"] = ask
        if allowlist_patterns:
            entry["allowlist"] = _serialize_exec_allowlist_entries(
                allowlist_patterns,
                existing_entry.get("allowlist") if isinstance(existing_entry, dict) else None,
            )
        next_agents[str(agent_id)] = entry
    approvals["agents"] = next_agents
    write_json(_exec_approvals_path(state_dir), approvals)
    return str(_exec_approvals_path(state_dir))


def _collect_elevated_allow_from_union(
    agents_map: dict[str, Any],
    channels_map: dict[str, Any],
) -> tuple[bool, dict[str, list[str]]]:
    enabled = False
    union: dict[str, list[str]] = {}
    for record in (agents_map or {}).values():
        permissions = (record or {}).get("permissions") or {}
        elevated = permissions.get("elevated") if isinstance(permissions, dict) else {}
        mode = _normalize_elevated_mode((elevated or {}).get("mode"))
        if mode != "on":
            continue
        enabled = True
        allow_from = _effective_agent_allow_from(record or {}, channels_map)
        for channel, values in allow_from.items():
            bucket = union.setdefault(channel, [])
            for value in values:
                if value not in bucket:
                    bucket.append(value)
    return enabled, union


def _apply_elevated_runtime_config(
    openclaw_data: dict[str, Any],
    agents_map: dict[str, Any],
    channels_map: dict[str, Any],
) -> None:
    tools_root = _ensure_dict(openclaw_data, "tools")
    elevated_root = _ensure_dict(tools_root, "elevated")
    enabled, allow_from_union = _collect_elevated_allow_from_union(agents_map, channels_map)
    elevated_root["enabled"] = enabled
    if allow_from_union:
        elevated_root["allowFrom"] = copy.deepcopy(allow_from_union)
    else:
        elevated_root.pop("allowFrom", None)


def derive_openclaw_home(state_dir: Path = DEFAULT_STATE_DIR) -> Path:
    expanded = Path(state_dir).expanduser().resolve()
    env_home = str(os.environ.get("OPENCLAW_HOME") or "").strip()
    if env_home:
        return Path(env_home).expanduser().resolve()
    if expanded.name == ".openclaw":
        return expanded.parent
    return expanded.parent


def _inject_user_systemd_env(env: dict[str, str]) -> None:
    runtime_dir = str(env.get("XDG_RUNTIME_DIR") or "").strip()
    if not runtime_dir:
        runtime_dir = f"/run/user/{os.getuid()}"
    runtime_path = Path(runtime_dir)
    if runtime_path.is_dir():
        env["XDG_RUNTIME_DIR"] = str(runtime_path)
        bus_path = runtime_path / "bus"
        if bus_path.exists() and not str(env.get("DBUS_SESSION_BUS_ADDRESS") or "").strip():
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus_path}"


def build_openclaw_env(state_dir: Path = DEFAULT_STATE_DIR) -> dict[str, str]:
    env = os.environ.copy()
    home_dir = derive_openclaw_home(state_dir)
    env["HOME"] = str(home_dir)
    env["OPENCLAW_HOME"] = str(home_dir)
    env["OPENCLAW_STATE_DIR"] = str(Path(state_dir).expanduser().resolve())
    _inject_user_systemd_env(env)

    path_parts = [
        env.get("PATH", ""),
        str(home_dir / ".local" / "share" / "pnpm"),
        str(home_dir / ".npm-global" / "bin"),
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    nvm_root = home_dir / ".nvm" / "versions" / "node"
    if nvm_root.exists():
        node_bins = sorted(path for path in nvm_root.glob("*/bin") if path.is_dir())
        if node_bins:
            path_parts.append(str(node_bins[-1]))
    env["PATH"] = ":".join(part for part in path_parts if part)
    return env


def find_openclaw_bin(state_dir: Path = DEFAULT_STATE_DIR) -> str:
    env = build_openclaw_env(state_dir)
    binary = shutil.which("openclaw", path=env["PATH"])
    if binary:
        return binary
    home_dir = derive_openclaw_home(state_dir)
    candidates = [
        home_dir / ".local" / "share" / "pnpm" / "openclaw",
        Path("/root/.local/share/pnpm/openclaw"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return "openclaw"


def _provider_key(mapping: dict[str, Any], provider: str) -> str | None:
    if provider in mapping:
        return provider
    wanted = provider.lower()
    for key in mapping:
        if key.lower() == wanted:
            return key
    return None


def _remove_provider_keys(mapping: dict[str, Any], provider: str) -> None:
    wanted = provider.lower()
    for key in list(mapping.keys()):
        if key.lower() == wanted:
            mapping.pop(key, None)


def _profile_ids_for_provider(profiles: dict[str, Any], provider: str) -> list[str]:
    wanted = provider.lower()
    ids: list[str] = []
    for profile_id, profile in profiles.items():
        profile_provider = str((profile or {}).get("provider") or "").lower()
        if profile_provider == wanted or profile_id.lower().startswith(f"{wanted}:"):
            ids.append(profile_id)
    return ids


def _remove_provider_profiles(auth_data: dict[str, Any], provider: str, include_runtime: bool = False) -> None:
    profiles = auth_data.setdefault("profiles", {})
    order = auth_data.setdefault("order", {})
    usage_stats = auth_data.setdefault("usageStats", {}) if include_runtime else {}
    last_good = auth_data.setdefault("lastGood", {}) if include_runtime else {}
    target_profile_ids = set(_profile_ids_for_provider(profiles, provider))

    for profile_id in list(profiles.keys()):
        if profile_id in target_profile_ids:
            profiles.pop(profile_id, None)

    if include_runtime:
        for profile_id in list(usage_stats.keys()):
            if profile_id in target_profile_ids:
                usage_stats.pop(profile_id, None)

        for profile_id in list(last_good.keys()):
            if profile_id in target_profile_ids:
                last_good.pop(profile_id, None)

    _remove_provider_keys(order, provider)


def _set_provider_order(order_map: dict[str, Any], provider: str, profile_ids: list[str]) -> None:
    _remove_provider_keys(order_map, provider)
    order_map[provider] = profile_ids
    lower = provider.lower()
    if lower != provider:
        order_map[lower] = profile_ids


def _coerce_api_keys(payload: dict[str, Any]) -> list[str]:
    keys: list[str] = []

    api_keys = payload.get("api_keys")
    if isinstance(api_keys, list):
        for value in api_keys:
            key = str(value or "").strip()
            if key and key not in keys:
                keys.append(key)

    api_key = str(payload.get("api_key") or "").strip()
    if api_key and api_key not in keys:
        keys.append(api_key)

    return keys


def _normalize_model_entry(entry: Any) -> dict[str, Any]:
    if isinstance(entry, str):
        model_id = entry.strip()
        if not model_id:
            raise ConfigError("模型列表里有空值。")
        return {
            "id": model_id,
            "name": model_id,
            "suggested_api": infer_api_type(model_id),
            "starred": False,
            "enabled": True,
        }
    if isinstance(entry, dict):
        model_id = str(entry.get("id") or "").strip()
        model_name = str(entry.get("name") or "").strip() or model_id
        if not model_id:
            raise ConfigError("模型对象缺少 `id`。")
        suggested_api = str(entry.get("suggested_api") or entry.get("suggestedApi") or "").strip() or infer_api_type(model_id)
        normalized = {
            "id": model_id,
            "name": model_name,
            "suggested_api": suggested_api,
            "starred": bool(entry.get("starred") or entry.get("favorite") or entry.get("favourite") or entry.get("is_starred")),
            "enabled": entry.get("enabled") is not False and not bool(entry.get("disabled")),
        }
        api = str(entry.get("api") or "").strip()
        if api:
            normalized["api"] = api
        return normalized
    raise ConfigError("模型列表只支持字符串或对象。")


def infer_api_type(model_id: str) -> str:
    value = str(model_id or "").strip().lower()
    if not value:
        return "openai-completions"
    if value.startswith("claude") or value.startswith("anthropic/") or "claude-" in value:
        return "anthropic-messages"
    if value.startswith("gemini") or value.startswith("models/gemini") or value.startswith("google/"):
        return "google-generative-ai"
    if value.startswith("ollama/"):
        return "ollama"
    if re.search(r":[a-z0-9._-]+$", value) and any(token in value for token in ("llama", "qwen", "mistral", "gemma", "deepseek", "phi", "yi", "mixtral")):
        return "ollama"
    return "openai-completions"


def normalize_route_suffix(value: Any) -> str:
    route_suffix = str(value or "").strip()
    if not route_suffix:
        return ""
    if not route_suffix.startswith("/"):
        route_suffix = f"/{route_suffix}"
    route_suffix = re.sub(r"/{2,}", "/", route_suffix)
    return route_suffix.rstrip("/") if route_suffix != "/" else route_suffix


def default_route_suffix_for_api(api_type: str) -> str:
    normalized_api = str(api_type or "").strip()
    for item in PROVIDER_MODEL_ROUTE_CANDIDATES:
        if str(item.get("api") or "").strip() == normalized_api and bool(item.get("runtime_supported")):
            return normalize_route_suffix(item.get("route_suffix"))
    return ""


def default_route_suffix_for_model(model_id: str) -> str:
    return default_route_suffix_for_api(infer_api_type(model_id))


def route_candidates_for_model(model_id: str) -> list[dict[str, Any]]:
    preferred_api = infer_api_type(model_id)
    candidates: list[dict[str, Any]] = []
    for item in PROVIDER_MODEL_ROUTE_CANDIDATES:
        route_suffix = normalize_route_suffix(item.get("route_suffix"))
        api_type = str(item.get("api") or "").strip()
        candidate = {
            "api": api_type,
            "route_suffix": route_suffix,
            "runtime_supported": bool(item.get("runtime_supported")),
        }
        if api_type == preferred_api:
            candidates.append(candidate)
    for item in PROVIDER_MODEL_ROUTE_CANDIDATES:
        route_suffix = normalize_route_suffix(item.get("route_suffix"))
        api_type = str(item.get("api") or "").strip()
        if api_type == preferred_api:
            continue
        candidate = {
            "api": api_type,
            "route_suffix": route_suffix,
            "runtime_supported": bool(item.get("runtime_supported")),
        }
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _provider_site(record: dict[str, Any]) -> str:
    return normalize_provider_site(str(record.get("site") or record.get("base_url") or "").strip())


def _join_site_and_route(site: str, route_suffix: str, model_id: str = "") -> str:
    suffix = normalize_route_suffix(route_suffix)
    rendered_suffix = suffix.replace("{model}", requests.utils.quote(str(model_id or "").strip(), safe="")) if model_id else suffix
    return f"{normalize_panel_base_url(site).rstrip('/')}{rendered_suffix}"


def _route_suffix_runtime_supported(api_type: str, route_suffix: str) -> bool:
    normalized_api = str(api_type or "").strip()
    normalized_route = normalize_route_suffix(route_suffix)
    for item in PROVIDER_MODEL_ROUTE_CANDIDATES:
        if str(item.get("api") or "").strip() == normalized_api and normalize_route_suffix(item.get("route_suffix")) == normalized_route:
            return bool(item.get("runtime_supported"))
    return False


def _runtime_base_url_for_route(site: str, api_type: str, route_suffix: str) -> str:
    normalized_site = normalize_panel_base_url(site)
    normalized_api = str(api_type or "").strip()
    normalized_route = normalize_route_suffix(route_suffix)
    if not normalized_route:
        raise ConfigError("缺少模型路由后缀。")
    if normalized_api == "openai-completions":
        tail = "/chat/completions"
    elif normalized_api == "openai-responses":
        tail = "/responses"
    elif normalized_api == "anthropic-messages":
        tail = "/messages"
    elif normalized_api == "google-generative-ai":
        marker = "/models/{model}:generatecontent"
        lowered_route = normalized_route.lower()
        index = lowered_route.find(marker)
        if index < 0:
            raise ConfigError(f"Google 模型路由不受支持：{normalized_route}")
        prefix = normalized_route[:index] or ""
        return normalize_panel_base_url(f"{normalized_site}{prefix}")
    elif normalized_api == "ollama":
        if normalized_route.lower().endswith("/api/chat"):
            prefix = normalized_route[: -len("/api/chat")] or ""
            return normalize_panel_base_url(f"{normalized_site}{prefix}")
        raise ConfigError(f"Ollama 路由当前无法应用到 OpenClaw：{normalized_route}")
    else:
        raise ConfigError(f"不支持的 API 类型：{normalized_api or '(空)'}")

    if not normalized_route.lower().endswith(tail.lower()):
        raise ConfigError(f"模型路由与 API 类型不匹配：{normalized_api} · {normalized_route}")
    prefix = normalized_route[: -len(tail)] or ""
    return normalize_panel_base_url(f"{normalized_site}{prefix}")


def _coerce_models(payload: dict[str, Any], existing_models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    requested: list[dict[str, Any]] = []
    models = payload.get("models")
    if isinstance(models, list):
        requested.extend(_normalize_model_entry(item) for item in models)

    model = payload.get("model")
    if isinstance(model, dict):
        requested.append(_normalize_model_entry(model))
    elif isinstance(model, str) and model.strip():
        requested.append(_normalize_model_entry(model))

    if not requested:
        for item in existing_models:
            requested.append(_normalize_model_entry(item))

    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in requested:
        model_id = item["id"]
        if model_id in seen:
            continue
        seen.add(model_id)
        unique.append(item)
    return unique


def _merge_models(
    requested_models: list[dict[str, Any]],
    existing_models: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    existing_by_id = {
        str(item.get("id") or "").strip(): copy.deepcopy(item)
        for item in existing_models
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }

    merged: list[dict[str, Any]] = []
    for item in requested_models:
        model_id = item["id"]
        base = existing_by_id.get(model_id, {})
        base["id"] = model_id
        base["name"] = item.get("name") or base.get("name") or model_id
        base["compat"] = base.get("compat") or {"supportsStore": False}
        api = str(item.get("api") or "").strip()
        if api:
            base["api"] = api
        elif "api" in base and not str(base.get("api") or "").strip():
            base.pop("api", None)
        merged.append(base)
    return merged


def _normalize_provider_model_entry(entry: Any) -> dict[str, Any]:
    if isinstance(entry, str):
        model_id = entry.strip()
        if not model_id:
            raise ConfigError("Provider 模型列表里有空模型。")
        suggested_api = infer_api_type(model_id)
        return {
            "id": model_id,
            "name": model_id,
            "api": suggested_api,
            "route_suffix": default_route_suffix_for_api(suggested_api),
            "enabled": True,
        }
    if isinstance(entry, dict):
        model_id = str(entry.get("id") or "").strip()
        model_name = str(entry.get("name") or "").strip() or model_id
        if not model_id:
            raise ConfigError("Provider 模型对象缺少 `id`。")
        api_type = str(entry.get("api") or "").strip() or infer_api_type(model_id)
        route_suffix = normalize_route_suffix(entry.get("route_suffix") or entry.get("routeSuffix") or "")
        if not route_suffix:
            route_suffix = default_route_suffix_for_api(api_type)
        enabled = entry.get("enabled")
        if enabled is None:
            enabled = True
        elif isinstance(enabled, bool):
            enabled = enabled
        elif isinstance(enabled, (int, float)):
            enabled = bool(enabled)
        else:
            enabled = str(enabled).strip().lower() not in {"", "0", "false", "off", "no"}
        return {
            "id": model_id,
            "name": model_name,
            "api": api_type,
            "route_suffix": route_suffix,
            "enabled": bool(enabled),
        }
    raise ConfigError("Provider 模型只支持字符串或对象。")


def _coerce_provider_models(payload: dict[str, Any]) -> list[dict[str, Any]]:
    requested: list[dict[str, Any]] = []
    models_raw = payload.get("models")
    if isinstance(models_raw, list):
        requested.extend(_normalize_provider_model_entry(item) for item in models_raw)
    else:
        model_ids_raw = payload.get("model_ids")
        if isinstance(model_ids_raw, list):
            requested.extend(_normalize_provider_model_entry(item) for item in model_ids_raw)

    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in requested:
        model_id = item["id"]
        if model_id in seen:
            continue
        seen.add(model_id)
        unique.append(item)
    return unique


def _enabled_provider_models(record: dict[str, Any]) -> list[dict[str, Any]]:
    models = []
    for item in record.get("models") or []:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_provider_model_entry(item)
        if normalized["enabled"]:
            models.append(normalized)
    return models


def _resolved_model_api(model_entry: dict[str, Any], default_api: str = "") -> str:
    return str(model_entry.get("api") or default_api or "").strip()


def _resolved_model_route(model_entry: dict[str, Any], default_api: str = "") -> str:
    route_suffix = normalize_route_suffix(model_entry.get("route_suffix") or model_entry.get("routeSuffix") or "")
    if route_suffix:
        return route_suffix
    return default_route_suffix_for_api(_resolved_model_api(model_entry, default_api))


def _candidate_key(api_type: str, route_suffix: str) -> tuple[str, str]:
    return str(api_type or "").strip(), normalize_route_suffix(route_suffix)


def _append_unique_probe_candidate(candidates: list[dict[str, Any]], candidate: dict[str, Any] | None) -> None:
    if not isinstance(candidate, dict):
        return
    api_type, route_suffix = _candidate_key(candidate.get("api") or "", candidate.get("route_suffix") or "")
    if not api_type or not route_suffix:
        return
    normalized = {
        "api": api_type,
        "route_suffix": route_suffix,
        "runtime_supported": _route_suffix_runtime_supported(api_type, route_suffix),
    }
    existing_keys = {_candidate_key(item.get("api") or "", item.get("route_suffix") or "") for item in candidates}
    if _candidate_key(api_type, route_suffix) in existing_keys:
        return
    candidates.append(normalized)


def _collect_provider_candidate_preferences(record: dict[str, Any]) -> list[dict[str, Any]]:
    normalized = _sanitize_provider_record(str(record.get("provider") or ""), record)
    default_api = str(normalized.get("default_api") or normalized.get("api") or "").strip()
    counts: Counter[tuple[str, str]] = Counter()
    for item in _enabled_provider_models(normalized):
        api_type = _resolved_model_api(item, default_api)
        route_suffix = _resolved_model_route(item, api_type)
        if api_type and route_suffix:
            counts[_candidate_key(api_type, route_suffix)] += 1
    ordered = sorted(counts.items(), key=lambda entry: (-entry[1], entry[0][0], entry[0][1]))
    return [
        {
            "api": api_type,
            "route_suffix": route_suffix,
            "runtime_supported": _route_suffix_runtime_supported(api_type, route_suffix),
        }
        for (api_type, route_suffix), _ in ordered
    ]


def _select_provider_warmup_models(
    record: dict[str, Any],
    candidate_catalog: list[dict[str, Any]],
    existing_models_map: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if not candidate_catalog:
        return []
    by_id = {
        str(item.get("id") or "").strip(): item
        for item in candidate_catalog
        if str(item.get("id") or "").strip()
    }
    seeds: list[dict[str, Any]] = []
    seen_model_ids: set[str] = set()
    seen_api_hints: set[str] = set()

    def push(model_id: str, api_hint: str = "") -> None:
        clean_id = str(model_id or "").strip()
        if not clean_id or clean_id in seen_model_ids or clean_id not in by_id:
            return
        seeds.append(by_id[clean_id])
        seen_model_ids.add(clean_id)
        if api_hint:
            seen_api_hints.add(api_hint)

    default_model_id = str(record.get("default_model_id") or "").strip()
    if default_model_id:
        existing_default = existing_models_map.get(default_model_id) or {}
        default_api = _resolved_model_api(existing_default, str(record.get("default_api") or record.get("api") or "").strip())
        push(default_model_id, default_api or infer_api_type(default_model_id))

    for model_id, existing_model in existing_models_map.items():
        if len(seeds) >= PROVIDER_MODEL_WARMUP_MAX_MODELS:
            break
        api_hint = _resolved_model_api(existing_model, str(record.get("default_api") or record.get("api") or "").strip()) or infer_api_type(model_id)
        if api_hint in seen_api_hints:
            continue
        push(model_id, api_hint)

    for item in candidate_catalog:
        if len(seeds) >= PROVIDER_MODEL_WARMUP_MAX_MODELS:
            break
        model_id = str(item.get("id") or "").strip()
        api_hint = str(item.get("api") or "").strip() or infer_api_type(model_id)
        if api_hint in seen_api_hints and model_id in seen_model_ids:
            continue
        push(model_id, api_hint)

    return seeds


def _coerce_apply_provider_models(
    payload: dict[str, Any],
    existing_models: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    requested: list[dict[str, Any]] = []
    if isinstance(payload.get("models"), list) or isinstance(payload.get("model_ids"), list):
        requested.extend(item for item in _coerce_provider_models(payload) if item.get("enabled"))
    else:
        requested.extend(
            {
                "id": item["id"],
                "name": str(item.get("name") or item["id"]).strip() or item["id"],
                "api": str(item.get("api") or "").strip() or infer_api_type(item["id"]),
                "route_suffix": normalize_route_suffix(item.get("route_suffix") or item.get("routeSuffix") or "") or default_route_suffix_for_model(item["id"]),
                "enabled": True,
            }
            for item in _coerce_models(payload, existing_models)
        )

    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in requested:
        model_id = str(item.get("id") or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        unique.append({
            "id": model_id,
            "name": str(item.get("name") or model_id).strip() or model_id,
            "api": str(item.get("api") or "").strip() or infer_api_type(model_id),
            "route_suffix": normalize_route_suffix(item.get("route_suffix") or item.get("routeSuffix") or "") or default_route_suffix_for_model(model_id),
            "enabled": True,
        })
    return unique


def _remove_trailing_segment(base_url: str, segment: str) -> str:
    normalized = str(base_url or "").strip().rstrip("/")
    suffix = f"/{segment.strip('/')}"
    if normalized.lower().endswith(suffix.lower()):
        return normalized[: -len(suffix)]
    return normalized


def _strip_provider_base_url_suffix(base_url: str) -> str:
    normalized = normalize_panel_base_url(base_url).rstrip("/")
    lowered = normalized.lower()
    for suffix in PROVIDER_BASE_URL_STANDARD_SUFFIXES:
        if lowered.endswith(suffix.lower()):
            return normalized[: -len(suffix)].rstrip("/")
    return normalized


def _provider_probe_root(base_url: str) -> str:
    root = _strip_provider_base_url_suffix(base_url).rstrip("/")
    return root or normalize_panel_base_url(base_url).rstrip("/")


def _standard_probe_base_url(base_url: str, api_type: str) -> str:
    suffix = str(PROVIDER_API_STANDARD_SUFFIXES.get(api_type) or "").strip()
    normalized = normalize_panel_base_url(base_url).rstrip("/")
    if not suffix:
        return normalized
    root = _strip_provider_base_url_suffix(normalized)
    return f"{root}{suffix}" if root else normalized


def _default_http_headers() -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        ),
    }


def _contains_error_token(message: str, tokens: tuple[str, ...]) -> bool:
    lowered = str(message or "").strip().lower()
    return bool(lowered) and any(token in lowered for token in tokens)


def _is_provider_waf_error(message: str) -> bool:
    return _contains_error_token(
        message,
        (
            "just a moment",
            "cf-mitigated",
            "cloudflare",
            "attention required",
            "captcha",
            "challenge",
            "access denied",
            "forbidden",
            "<!doctype html",
            "<html",
        ),
    )


def _is_provider_transient_error(message: str) -> bool:
    return _contains_error_token(
        message,
        (
            "timeout",
            "timed out",
            "429",
            "too many requests",
            "502",
            "503",
            "504",
            "bad gateway",
            "upstream",
            "temporarily unavailable",
            "service unavailable",
            "no available providers",
            "system disk overloaded",
            "connection reset",
            "connection aborted",
        ),
    )


def _provider_list_header_variants(api_key: str) -> list[dict[str, str]]:
    key = str(api_key or "").strip()
    variants: list[dict[str, str]] = []
    base = _default_http_headers()
    if not key:
        return [base]
    for extra in (
        {"Authorization": f"Bearer {key}"},
        {"x-api-key": key},
        {"Authorization": f"Bearer {key}", "x-api-key": key},
        {"x-goog-api-key": key},
    ):
        headers = dict(base)
        headers.update(extra)
        if headers not in variants:
            variants.append(headers)
    return variants


def _extract_response_error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        text = response.text.strip()
        return text[:240] if text else f"HTTP {response.status_code}"
    if isinstance(payload, dict):
        error_value = payload.get("error")
        if isinstance(error_value, dict):
            for key in ("message", "detail", "code", "type"):
                value = str(error_value.get(key) or "").strip()
                if value:
                    return value
        elif error_value is not None:
            value = str(error_value).strip()
            if value:
                return value
        for key in ("message", "detail", "msg"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
    text = response.text.strip()
    return text[:240] if text else f"HTTP {response.status_code}"


def _is_probe_success(api_type: str, payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if api_type == "openai-completions":
        return isinstance(payload.get("choices"), list) or (bool(payload.get("id")) and bool(payload.get("model")))
    if api_type == "openai-responses":
        return bool(payload.get("id")) or isinstance(payload.get("output"), list) or payload.get("object") == "response"
    if api_type == "anthropic-messages":
        return isinstance(payload.get("content"), list) or payload.get("type") == "message" or (bool(payload.get("id")) and bool(payload.get("model")))
    if api_type == "google-generative-ai":
        return isinstance(payload.get("candidates"), list) or "promptFeedback" in payload or bool(payload.get("modelVersion"))
    if api_type == "ollama":
        return isinstance(payload.get("message"), dict) or bool(payload.get("done")) or bool(payload.get("model"))
    return False


def _load_jsonish_response_payload(response: requests.Response) -> Any:
    try:
        return response.json()
    except Exception:
        pass

    text = ""
    try:
        text = response.text or ""
    except Exception:
        text = ""
    stripped = text.strip()
    if not stripped:
        raise ValueError("响应体为空。")

    event_chunks: list[str] = []
    for raw_line in stripped.splitlines():
        line = str(raw_line or "").strip()
        if not line:
            continue
        if line.startswith("data:"):
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            event_chunks.append(data)
        elif line.startswith("{") or line.startswith("["):
            event_chunks.append(line)

    for chunk in event_chunks:
        try:
            return json.loads(chunk)
        except Exception:
            continue

    raise ValueError("响应不是合法 JSON。")


def _load_jsonish_stream_payload(response: requests.Response) -> Any:
    buffered_chunks: list[str] = []
    for raw_line in response.iter_lines(decode_unicode=True):
        line = str(raw_line or "").strip()
        if not line:
            continue
        if line.startswith("data:"):
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            buffered_chunks.append(data)
            try:
                return json.loads(data)
            except Exception:
                continue
        elif line.startswith("{") or line.startswith("["):
            buffered_chunks.append(line)
            try:
                return json.loads(line)
            except Exception:
                continue
    if buffered_chunks:
        for chunk in buffered_chunks:
            try:
                return json.loads(chunk)
            except Exception:
                continue
    raise ValueError("响应不是合法 JSON。")


def _probe_openai_compatible(endpoint: str, api_key: str, model_id: str, api_type: str) -> tuple[bool, str]:
    session = _http_session()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if api_type == "openai-responses":
        payload_variants = [
            {"model": model_id, "input": "Reply with exactly ok", "max_output_tokens": 1},
            {"model": model_id, "input": "Reply with exactly ok"},
        ]
    else:
        payload_variants = [
            {
                "model": model_id,
                "messages": [{"role": "user", "content": "Reply with exactly ok"}],
                "max_completion_tokens": 1,
                "stream": False,
            },
            {
                "model": model_id,
                "messages": [{"role": "user", "content": "Reply with exactly ok"}],
                "max_tokens": 1,
                "stream": False,
            },
            {
                "model": model_id,
                "messages": [{"role": "user", "content": "Reply with exactly ok"}],
                "stream": False,
            },
        ]

    last_error = "响应格式不符合预期。"
    for payload in payload_variants:
        try:
            with session.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=PROVIDER_MODEL_PROBE_TIMEOUT,
                stream=bool(payload.get("stream")),
            ) as response:
                if not response.ok:
                    last_error = _extract_response_error_message(response)
                    if any(token in last_error.lower() for token in ("model_not_found", "not found", "unknown model", "does not exist")):
                        return False, last_error
                    continue
                try:
                    body = _load_jsonish_stream_payload(response) if payload.get("stream") else _load_jsonish_response_payload(response)
                except Exception:
                    last_error = "响应不是合法 JSON。"
                    continue
                if _is_probe_success(api_type, body):
                    return True, "ok"
                last_error = "响应格式不符合预期。"
        except requests.RequestException as exc:
            last_error = str(exc)
            continue
    return False, last_error


def _probe_anthropic_messages(endpoint: str, api_key: str, model_id: str) -> tuple[bool, str]:
    response = _http_session().post(
        endpoint,
        headers={
            "x-api-key": api_key,
            "Authorization": f"Bearer {api_key}",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": model_id,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "ping"}],
        },
        timeout=PROVIDER_MODEL_PROBE_TIMEOUT,
    )
    if not response.ok:
        return False, _extract_response_error_message(response)
    try:
        body = response.json()
    except Exception:
        return False, "响应不是合法 JSON。"
    if _is_probe_success("anthropic-messages", body):
        return True, "ok"
    return False, "响应格式不符合预期。"


def _probe_google_generative(endpoint: str, api_key: str, model_id: str) -> tuple[bool, str]:
    response = _http_session().post(
        endpoint,
        params={"key": api_key},
        headers={
            "x-goog-api-key": api_key,
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "contents": [{"role": "user", "parts": [{"text": "ping"}]}],
            "generationConfig": {"maxOutputTokens": 1},
        },
        timeout=PROVIDER_MODEL_PROBE_TIMEOUT,
    )
    if not response.ok:
        return False, _extract_response_error_message(response)
    try:
        body = response.json()
    except Exception:
        return False, "响应不是合法 JSON。"
    if _is_probe_success("google-generative-ai", body):
        return True, "ok"
    return False, "响应格式不符合预期。"


def _probe_ollama_chat(endpoint: str, api_key: str, model_id: str) -> tuple[bool, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    response = _http_session().post(
        endpoint,
        headers=headers,
        json={
            "model": model_id,
            "messages": [{"role": "user", "content": "ping"}],
            "stream": False,
            "options": {"num_predict": 1},
        },
        timeout=PROVIDER_MODEL_PROBE_TIMEOUT,
    )
    if not response.ok:
        return False, _extract_response_error_message(response)
    try:
        body = response.json()
    except Exception:
        return False, "响应不是合法 JSON。"
    if _is_probe_success("ollama", body):
        return True, "ok"
    return False, "响应格式不符合预期。"


def _probe_provider_model_api(
    site: str,
    api_keys: list[str],
    model_id: str,
    api_type: str,
    route_suffix: str,
    *,
    retry_standard_base_url: bool = False,
) -> dict[str, Any]:
    probe_fn = {
        "openai-completions": lambda url, key: _probe_openai_compatible(url, key, model_id, "openai-completions"),
        "openai-responses": lambda url, key: _probe_openai_compatible(url, key, model_id, "openai-responses"),
        "anthropic-messages": lambda url, key: _probe_anthropic_messages(url, key, model_id),
        "google-generative-ai": lambda url, key: _probe_google_generative(url, key, model_id),
        "ollama": lambda url, key: _probe_ollama_chat(url, key, model_id),
    }.get(api_type)
    if probe_fn is None:
        return {"ok": False, "api": api_type, "error": "不支持的探测 API 类型。"}

    normalized_site = normalize_provider_site(site)
    normalized_route = normalize_route_suffix(route_suffix)
    endpoint = _join_site_and_route(normalized_site, normalized_route, model_id)

    last_error = "没有可用 API key。"
    for api_key in api_keys:
        try:
            ok, detail = probe_fn(endpoint, api_key)
        except requests.RequestException as exc:
            last_error = str(exc)
            continue
        except Exception as exc:
            last_error = str(exc)
            continue
        if ok:
            return {
                "ok": True,
                "api": api_type,
                "site": normalized_site,
                "route_suffix": normalized_route,
                "endpoint": endpoint,
                "runtime_supported": _route_suffix_runtime_supported(api_type, normalized_route),
            }
        last_error = detail
        lowered = detail.lower()
        if any(token in lowered for token in ("model_not_found", "not found", "unsupported", "unknown model", "does not exist")):
            break
    return {
        "ok": False,
        "api": api_type,
        "route_suffix": normalized_route,
        "endpoint": endpoint,
        "error": last_error,
    }


def _probe_provider_model_apis(
    record: dict[str, Any],
    model_item: dict[str, Any],
    existing_model: dict[str, Any] | None = None,
    *,
    provider_candidate_preferences: list[dict[str, Any]] | None = None,
    prefer_known_provider_routes: bool = False,
    retry_standard_base_url: bool = False,
) -> dict[str, Any]:
    model_id = str(model_item.get("id") or "").strip()
    if not model_id:
        return {"model_id": "", "successful_apis": [], "probes": [], "chosen_api": ""}

    existing_api = str((existing_model or {}).get("api") or "").strip()
    existing_route = normalize_route_suffix((existing_model or {}).get("route_suffix") or (existing_model or {}).get("routeSuffix") or "")
    model_api = str(model_item.get("api") or "").strip()
    model_route = normalize_route_suffix(model_item.get("route_suffix") or model_item.get("routeSuffix") or "")
    preferred_candidates: list[dict[str, Any]] = []

    for candidate in provider_candidate_preferences or []:
        _append_unique_probe_candidate(preferred_candidates, candidate)
    if existing_api and existing_route:
        _append_unique_probe_candidate(
            preferred_candidates,
            {"api": existing_api, "route_suffix": existing_route},
        )
    if model_api and model_route:
        _append_unique_probe_candidate(
            preferred_candidates,
            {"api": model_api, "route_suffix": model_route},
        )
    if not prefer_known_provider_routes or not preferred_candidates:
        for candidate in route_candidates_for_model(model_id):
            _append_unique_probe_candidate(preferred_candidates, candidate)

    probes: list[dict[str, Any]] = []
    successful_apis: list[str] = []
    chosen_route_suffix = ""
    chosen_endpoint = ""
    runtime_supported = False
    for candidate in preferred_candidates:
        api_type = str(candidate.get("api") or "").strip()
        route_suffix = normalize_route_suffix(candidate.get("route_suffix") or "")
        result = _probe_provider_model_api(
            _provider_site(record),
            record.get("api_keys") or [],
            model_id,
            api_type,
            route_suffix,
            retry_standard_base_url=retry_standard_base_url,
        )
        probes.append(result)
        if result.get("ok") and api_type not in successful_apis:
            successful_apis.append(api_type)
            chosen_route_suffix = str(result.get("route_suffix") or route_suffix).strip()
            chosen_endpoint = str(result.get("endpoint") or "").strip()
            runtime_supported = bool(result.get("runtime_supported"))
            break

    chosen_api = ""
    for candidate in preferred_candidates:
        if candidate["api"] in successful_apis:
            chosen_api = candidate["api"]
            break

    return {
        "model_id": model_id,
        "successful_apis": successful_apis,
        "probes": probes,
        "chosen_api": chosen_api,
        "chosen_route_suffix": chosen_route_suffix,
        "chosen_endpoint": chosen_endpoint,
        "runtime_supported": runtime_supported,
    }


def _warm_provider_candidate_preferences(
    record: dict[str, Any],
    candidate_catalog: list[dict[str, Any]],
    existing_models_map: dict[str, dict[str, Any]],
    base_preferences: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    warmed: list[dict[str, Any]] = []
    for candidate in base_preferences or []:
        _append_unique_probe_candidate(warmed, candidate)

    seed_models = _select_provider_warmup_models(record, candidate_catalog, existing_models_map)
    if not seed_models:
        return warmed

    max_workers = max(1, min(PROVIDER_MODEL_WARMUP_MAX_WORKERS, len(seed_models)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                _probe_provider_model_apis,
                record,
                item,
                existing_models_map.get(str(item.get("id") or "").strip()),
                provider_candidate_preferences=warmed,
            ): item
            for item in seed_models
        }
        for future in as_completed(future_map):
            try:
                result = future.result()
            except Exception:
                continue
            chosen_api = str(result.get("chosen_api") or "").strip()
            chosen_route_suffix = normalize_route_suffix(result.get("chosen_route_suffix") or "")
            if chosen_api and chosen_route_suffix:
                preferred = {
                    "api": chosen_api,
                    "route_suffix": chosen_route_suffix,
                    "runtime_supported": bool(result.get("runtime_supported")),
                }
                if _candidate_key(chosen_api, chosen_route_suffix) not in {
                    _candidate_key(item.get("api") or "", item.get("route_suffix") or "")
                    for item in warmed
                }:
                    warmed.insert(0, preferred)
    return warmed


def _provider_record_snapshot(record: dict[str, Any]) -> dict[str, Any]:
    normalized = _sanitize_provider_record(str(record.get("provider") or ""), record)
    return {
        "enabled": normalized.get("enabled") is not False,
        "base_url": str(normalized.get("site") or normalized.get("base_url") or "").strip(),
        "default_api": str(normalized.get("default_api") or normalized.get("api") or "").strip(),
        "default_model_id": str(normalized.get("default_model_id") or "").strip(),
        "models": {
            str(item.get("id") or "").strip(): {
                "api": str(item.get("api") or "").strip() or str(normalized.get("default_api") or normalized.get("api") or "").strip(),
                "route_suffix": _resolved_model_route(item, str(normalized.get("default_api") or normalized.get("api") or "").strip()),
            }
            for item in _enabled_provider_models(normalized)
            if str(item.get("id") or "").strip()
        },
    }


def _provider_snapshot_diff(provider_id: str, before_snapshot: dict[str, Any], after_snapshot: dict[str, Any]) -> dict[str, Any]:
    before_models = before_snapshot.get("models") or {}
    after_models = after_snapshot.get("models") or {}
    before_ids = set(before_models)
    after_ids = set(after_models)
    api_changed_models = []
    route_changed_models = []
    for model_id in sorted(before_ids & after_ids):
        before_entry = before_models.get(model_id) or {}
        after_entry = after_models.get(model_id) or {}
        before_api = str((before_entry or {}).get("api") or "").strip()
        after_api = str((after_entry or {}).get("api") or "").strip()
        before_route = str((before_entry or {}).get("route_suffix") or "").strip()
        after_route = str((after_entry or {}).get("route_suffix") or "").strip()
        if before_api != after_api:
            api_changed_models.append({
                "id": model_id,
                "before_api": before_api,
                "after_api": after_api,
            })
        if before_route != after_route:
            route_changed_models.append({
                "id": model_id,
                "before_route_suffix": before_route,
                "after_route_suffix": after_route,
            })
    added_models = sorted(after_ids - before_ids)
    removed_models = sorted(before_ids - after_ids)
    return {
        "provider": provider_id,
        "changed": bool(
            before_snapshot.get("enabled") != after_snapshot.get("enabled")
            or before_snapshot.get("base_url") != after_snapshot.get("base_url")
            or before_snapshot.get("default_api") != after_snapshot.get("default_api")
            or before_snapshot.get("default_model_id") != after_snapshot.get("default_model_id")
            or added_models
            or removed_models
            or api_changed_models
            or route_changed_models
        ),
        "before_enabled": before_snapshot.get("enabled"),
        "after_enabled": after_snapshot.get("enabled"),
        "before_base_url": before_snapshot.get("base_url") or "",
        "after_base_url": after_snapshot.get("base_url") or "",
        "before_default_api": before_snapshot.get("default_api") or "",
        "after_default_api": after_snapshot.get("default_api") or "",
        "before_default_model_id": before_snapshot.get("default_model_id") or "",
        "after_default_model_id": after_snapshot.get("default_model_id") or "",
        "before_model_count": len(before_models),
        "after_model_count": len(after_models),
        "added_models": added_models,
        "removed_models": removed_models,
        "api_changed_models": api_changed_models,
        "route_changed_models": route_changed_models,
    }


def _probe_provider_catalog_models(
    record: dict[str, Any],
    candidate_catalog: list[dict[str, Any]],
    existing_models_map: dict[str, dict[str, Any]],
    *,
    provider_candidate_preferences: list[dict[str, Any]] | None = None,
    prefer_known_provider_routes: bool = False,
    retry_standard_base_url: bool = False,
) -> list[dict[str, Any]]:
    model_probe_results: list[dict[str, Any]] = []
    if not candidate_catalog:
        return model_probe_results
    max_workers = max(1, min(PROVIDER_MODEL_PROBE_MAX_WORKERS, len(candidate_catalog)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                _probe_provider_model_apis,
                record,
                item,
                existing_models_map.get(str(item.get("id") or "").strip()),
                provider_candidate_preferences=provider_candidate_preferences,
                prefer_known_provider_routes=prefer_known_provider_routes,
                retry_standard_base_url=retry_standard_base_url,
            ): item
            for item in candidate_catalog
        }
        for future in as_completed(future_map):
            model_item = future_map[future]
            model_id = str(model_item.get("id") or "").strip()
            try:
                model_probe_results.append(future.result())
            except Exception as exc:
                model_probe_results.append({
                    "model_id": model_id,
                    "successful_apis": [],
                    "probes": [],
                    "chosen_api": "",
                    "chosen_route_suffix": "",
                    "chosen_endpoint": "",
                    "runtime_supported": False,
                    "error": str(exc),
                })
    return model_probe_results


def _probe_result_looks_retryable(result: dict[str, Any] | None) -> bool:
    if not isinstance(result, dict):
        return True
    if str(result.get("chosen_api") or "").strip():
        return False
    probes = result.get("probes") or []
    if not probes:
        return True
    retry_tokens = (
        "timeout",
        "timed out",
        "429",
        "too many requests",
        "502",
        "503",
        "504",
        "bad gateway",
        "upstream",
        "temporarily unavailable",
        "no available providers",
        "connection reset",
        "connection aborted",
        "response not",
        "响应不是合法 json",
        "响应格式不符合预期",
        "bad_response_body",
        "json",
    )
    hard_fail_tokens = (
        "model_not_found",
        "unknown model",
        "does not exist",
        "not found",
        "unsupported model",
        "invalid model",
    )
    for probe in probes:
        message = str(probe.get("error") or "").strip().lower()
        if not message:
            continue
        if any(token in message for token in hard_fail_tokens):
            return False
        if any(token in message for token in retry_tokens):
            return True
    return False


def _probe_error_is_site_level_retryable(message: str) -> bool:
    lowered = str(message or "").strip().lower()
    if not lowered:
        return False
    if _is_provider_waf_error(lowered):
        return True
    return any(token in lowered for token in (
        "timeout",
        "timed out",
        "429",
        "too many requests",
        "502",
        "503",
        "504",
        "bad gateway",
        "upstream",
        "temporarily unavailable",
        "service unavailable",
        "no available providers",
        "system disk overloaded",
        "connection reset",
        "connection aborted",
    ))


def _summarize_probe_errors(model_probe_results: list[dict[str, Any]], limit: int = 3) -> list[str]:
    counter: Counter[str] = Counter()
    for item in model_probe_results:
        if not isinstance(item, dict):
            continue
        for probe in item.get("probes") or []:
            if not isinstance(probe, dict):
                continue
            message = str(probe.get("error") or "").strip()
            if message:
                counter[message] += 1
    return [f"{message} ×{count}" if count > 1 else message for message, count in counter.most_common(limit)]


def _all_probe_results_retryable(model_probe_results: list[dict[str, Any]]) -> bool:
    saw_probe = False
    for item in model_probe_results:
        if not isinstance(item, dict):
            continue
        if str(item.get("chosen_api") or "").strip():
            return False
        probes = item.get("probes") or []
        if probes:
            saw_probe = True
        if not _probe_result_looks_retryable(item):
            return False
    return saw_probe


def _all_probe_results_site_level_retryable(model_probe_results: list[dict[str, Any]]) -> bool:
    saw_probe = False
    for item in model_probe_results:
        if not isinstance(item, dict):
            continue
        if str(item.get("chosen_api") or "").strip():
            return False
        probes = item.get("probes") or []
        if not probes:
            return False
        saw_probe = True
        saw_retryable_error = False
        for probe in probes:
            if not isinstance(probe, dict):
                return False
            message = str(probe.get("error") or "").strip()
            if not message:
                return False
            if not _probe_error_is_site_level_retryable(message):
                return False
            saw_retryable_error = True
        if not saw_retryable_error:
            return False
    return saw_probe


def _retry_probe_single_provider_model(
    record: dict[str, Any],
    model_item: dict[str, Any],
    existing_model: dict[str, Any] | None,
    provider_candidate_preferences: list[dict[str, Any]] | None = None,
    prefer_known_provider_routes: bool = False,
) -> tuple[str, dict[str, Any]]:
    model_id = str(model_item.get("id") or "").strip()
    best_result: dict[str, Any] = {}
    for _ in range(max(1, PROVIDER_MODEL_RETRY_ATTEMPTS)):
        retry_result = _probe_provider_model_apis(
            record,
            model_item,
            existing_model,
            provider_candidate_preferences=provider_candidate_preferences,
            prefer_known_provider_routes=prefer_known_provider_routes,
            retry_standard_base_url=False,
        )
        if str(retry_result.get("chosen_api") or "").strip():
            best_result = retry_result
            break
        if not best_result or _probe_result_looks_retryable(best_result):
            best_result = retry_result
    return model_id, best_result


def _refresh_single_provider_available_models(
    provider_id: str,
    record: dict[str, Any],
    model_catalog: list[dict[str, Any]],
) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Probe one provider and return the updated record plus diff metadata."""
    current_record = _sanitize_provider_record(provider_id, record)
    before_snapshot = _provider_record_snapshot(current_record)
    preserved_models = _enabled_provider_models(current_record)
    preserved_model_ids = [item["id"] for item in preserved_models]
    existing_models_map = {
        item["id"]: item
        for item in preserved_models
    }
    listed_model_ids, list_error = _list_provider_models(_provider_site(current_record), current_record.get("api_keys") or [])
    reported_not_in_catalog: list[str] = []
    candidate_catalog = model_catalog
    skipped_not_listed: list[str] = []
    if listed_model_ids is not None:
        catalog_ids = {
            str(item.get("id") or "").strip()
            for item in model_catalog
            if str(item.get("id") or "").strip()
        }
        reported_not_in_catalog = sorted(model_id for model_id in listed_model_ids if model_id not in catalog_ids)
        candidate_catalog = []
        for item in model_catalog:
            model_id = str(item.get("id") or "").strip()
            if model_id in listed_model_ids:
                candidate_catalog.append(item)
            else:
                skipped_not_listed.append(model_id)
    prefer_known_provider_routes = listed_model_ids is not None

    if listed_model_ids is None and list_error and (_is_provider_waf_error(list_error) or _is_provider_transient_error(list_error)):
        refreshed_summary = {
            "provider": provider_id,
            "base_url_before": _provider_site(current_record),
            "base_url": _provider_site(current_record),
            "base_url_retry_used": False,
            "enabled": current_record.get("enabled") is not False,
            "default_api": str(current_record.get("default_api") or current_record.get("api") or "").strip(),
            "default_model_id": str(current_record.get("default_model_id") or "").strip(),
            "available_model_ids": [],
            "available_count": 0,
            "preserved_available_model_ids": preserved_model_ids,
            "preserved_available_count": len(preserved_models),
            "tested_count": 0,
            "skipped_not_listed_count": 0,
            "skipped_not_listed": [],
            "list_models_error": list_error,
            "models": [],
            "probe_skipped_reason": "site_unreachable_or_blocked",
            "probe_skipped_message": list_error,
            "reported_model_count": 0,
            "reported_out_of_catalog_count": 0,
            "reported_out_of_catalog_sample": [],
            "kept_previous_due_to_transient_errors": True,
            "probe_error_summary": [list_error],
        }
        after_snapshot = _provider_record_snapshot(current_record)
        return provider_id, current_record, refreshed_summary, _provider_snapshot_diff(provider_id, before_snapshot, after_snapshot)

    provider_candidate_preferences = _collect_provider_candidate_preferences(current_record)
    provider_candidate_preferences = _warm_provider_candidate_preferences(
        current_record,
        candidate_catalog,
        existing_models_map,
        provider_candidate_preferences,
    )

    model_probe_results = _probe_provider_catalog_models(
        current_record,
        candidate_catalog,
        existing_models_map,
        provider_candidate_preferences=provider_candidate_preferences,
        prefer_known_provider_routes=prefer_known_provider_routes,
    )
    results_by_id = {str(item.get("model_id") or "").strip(): item for item in model_probe_results}
    retry_standard_base_url_used = False

    retry_candidates = [
        item
        for item in candidate_catalog
        if _probe_result_looks_retryable(results_by_id.get(str(item.get("id") or "").strip()))
    ]
    if retry_candidates:
        max_workers = max(1, min(PROVIDER_MODEL_PROBE_MAX_WORKERS, len(retry_candidates)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(
                    _retry_probe_single_provider_model,
                    current_record,
                    item,
                    existing_models_map.get(str(item.get("id") or "").strip()),
                    provider_candidate_preferences,
                    prefer_known_provider_routes,
                ): item
                for item in retry_candidates
            }
            for future in as_completed(future_map):
                try:
                    model_id, best_result = future.result()
                except Exception:
                    item = future_map[future]
                    model_id = str(item.get("id") or "").strip()
                    best_result = results_by_id.get(model_id) or {}
                results_by_id[model_id] = best_result
        model_probe_results = [results_by_id.get(str(item.get("id") or "").strip()) or {} for item in candidate_catalog]

    probe_error_summary = _summarize_probe_errors(model_probe_results)
    if candidate_catalog and not any(str(item.get("chosen_api") or "").strip() for item in model_probe_results) and _all_probe_results_site_level_retryable(model_probe_results):
        refreshed_summary = {
            "provider": provider_id,
            "base_url_before": _provider_site(current_record),
            "base_url": _provider_site(current_record),
            "base_url_retry_used": False,
            "enabled": current_record.get("enabled") is not False,
            "default_api": str(current_record.get("default_api") or current_record.get("api") or "").strip(),
            "default_model_id": str(current_record.get("default_model_id") or "").strip(),
            "available_model_ids": [],
            "available_count": 0,
            "preserved_available_model_ids": preserved_model_ids,
            "preserved_available_count": len(preserved_models),
            "tested_count": len(candidate_catalog),
            "skipped_not_listed_count": len(skipped_not_listed),
            "skipped_not_listed": skipped_not_listed,
            "list_models_error": list_error,
            "models": [
                {
                    "id": str(item.get("model_id") or ""),
                    "successful_apis": item.get("successful_apis") or [],
                    "chosen_api": str(item.get("chosen_api") or ""),
                    "chosen_route_suffix": str(item.get("chosen_route_suffix") or ""),
                    "chosen_endpoint": str(item.get("chosen_endpoint") or ""),
                    "runtime_supported": bool(item.get("runtime_supported")),
                }
                for item in model_probe_results
                if isinstance(item, dict)
            ],
            "probe_skipped_reason": "",
            "probe_skipped_message": "",
            "reported_model_count": len(listed_model_ids or []),
            "reported_out_of_catalog_count": len(reported_not_in_catalog),
            "reported_out_of_catalog_sample": reported_not_in_catalog[:20],
            "kept_previous_due_to_transient_errors": True,
            "probe_error_summary": probe_error_summary,
        }
        after_snapshot = _provider_record_snapshot(current_record)
        return provider_id, current_record, refreshed_summary, _provider_snapshot_diff(provider_id, before_snapshot, after_snapshot)

    def _build_next_models() -> list[dict[str, Any]]:
        next_items: list[dict[str, Any]] = []
        for item in model_catalog:
            model_id = str(item.get("id") or "").strip()
            if not model_id:
                continue
            probe = results_by_id.get(model_id) or {}
            chosen_api = str(probe.get("chosen_api") or "").strip()
            chosen_route_suffix = normalize_route_suffix(probe.get("chosen_route_suffix") or "")
            runtime_supported = bool(probe.get("runtime_supported"))
            if chosen_api and not runtime_supported:
                continue
            if not chosen_api:
                continue
            next_items.append({
                "id": model_id,
                "name": str(item.get("name") or model_id).strip() or model_id,
                "api": chosen_api,
                "route_suffix": chosen_route_suffix or default_route_suffix_for_api(chosen_api),
                "enabled": True,
            })
        return next_items

    next_models = _build_next_models()

    next_enabled = bool(next_models)
    default_model_id = str(current_record.get("default_model_id") or "").strip()
    next_model_ids = [item["id"] for item in next_models]
    if default_model_id not in next_model_ids:
        default_model_id = next_model_ids[0] if next_model_ids else ""

    default_model = next((item for item in next_models if item["id"] == default_model_id), next_models[0] if next_models else {})
    default_api = str(default_model.get("api") or "").strip()

    updated_record = copy.deepcopy(current_record)
    updated_record["site"] = _provider_site(current_record)
    updated_record["base_url"] = updated_record["site"]
    updated_record["default_api"] = default_api
    updated_record["api"] = default_api
    updated_record["models"] = next_models
    updated_record["model_ids"] = next_model_ids
    updated_record["default_model_id"] = default_model_id
    updated_record["enabled"] = next_enabled

    per_model_summary = []
    for item in model_catalog:
        model_id = str(item.get("id") or "").strip()
        probe = results_by_id.get(model_id)
        if probe:
            per_model_summary.append({
                "id": model_id,
                "successful_apis": probe.get("successful_apis") or [],
                "chosen_api": str(probe.get("chosen_api") or ""),
                "chosen_route_suffix": str(probe.get("chosen_route_suffix") or ""),
                "chosen_endpoint": str(probe.get("chosen_endpoint") or ""),
                "runtime_supported": bool(probe.get("runtime_supported")),
            })

    refreshed_summary = {
        "provider": provider_id,
        "base_url_before": _provider_site(current_record),
        "base_url": _provider_site(updated_record),
        "base_url_retry_used": retry_standard_base_url_used,
        "enabled": next_enabled,
        "default_api": default_api,
        "default_model_id": default_model_id,
        "available_model_ids": next_model_ids,
        "available_count": len(next_models),
        "preserved_available_model_ids": [],
        "preserved_available_count": 0,
        "tested_count": len(candidate_catalog),
        "skipped_not_listed_count": len(skipped_not_listed),
        "skipped_not_listed": skipped_not_listed,
        "list_models_error": list_error,
        "models": per_model_summary,
        "probe_skipped_reason": "",
        "probe_skipped_message": "",
        "reported_model_count": len(listed_model_ids or []),
        "reported_out_of_catalog_count": len(reported_not_in_catalog),
        "reported_out_of_catalog_sample": reported_not_in_catalog[:20],
        "kept_previous_due_to_transient_errors": False,
        "probe_error_summary": probe_error_summary,
    }
    after_snapshot = _provider_record_snapshot(updated_record)
    return provider_id, updated_record, refreshed_summary, _provider_snapshot_diff(provider_id, before_snapshot, after_snapshot)


def _list_provider_models(base_url: str, api_keys: list[str]) -> tuple[set[str] | None, str]:
    """Fetch a provider-reported model list using several common auth headers."""
    normalized_site = normalize_provider_site(base_url).rstrip("/")
    endpoints: list[str] = []
    for suffix in PROVIDER_MODEL_LIST_ROUTE_SUFFIXES:
        candidate = f"{normalized_site}{suffix}".rstrip("/")
        if candidate and candidate not in endpoints:
            endpoints.append(candidate)
    last_error = ""
    session = _http_session()
    for endpoint in endpoints:
        for api_key in api_keys:
            for headers in _provider_list_header_variants(api_key):
                try:
                    response = session.get(
                        endpoint,
                        headers=headers,
                        timeout=PROVIDER_MODEL_PROBE_TIMEOUT,
                    )
                except requests.RequestException as exc:
                    last_error = str(exc)
                    continue
                if not response.ok:
                    last_error = _extract_response_error_message(response)
                    if response.status_code in {404, 405, 501}:
                        continue
                    continue
                try:
                    payload = response.json()
                except Exception:
                    last_error = "模型列表响应不是合法 JSON。"
                    continue
                if isinstance(payload, dict) and isinstance(payload.get("data"), list):
                    listed = {
                        str(item.get("id") or "").strip()
                        for item in payload.get("data") or []
                        if isinstance(item, dict) and str(item.get("id") or "").strip()
                    }
                    if listed:
                        return listed, ""
                if isinstance(payload, dict) and isinstance(payload.get("models"), list):
                    listed: set[str] = set()
                    for item in payload.get("models") or []:
                        if not isinstance(item, dict):
                            continue
                        raw_name = str(item.get("name") or item.get("id") or "").strip()
                        if not raw_name:
                            continue
                        listed.add(raw_name)
                        if raw_name.startswith("models/"):
                            listed.add(raw_name[len("models/"):])
                    if listed:
                        return listed, ""
                last_error = "模型列表响应格式不符合预期。"
    return None, last_error


def _build_default_models_catalog(providers: dict[str, Any]) -> dict[str, Any]:
    catalog: dict[str, Any] = {}
    for provider, provider_config in providers.items():
        for item in provider_config.get("models") or []:
            model_id = str((item or {}).get("id") or "").strip()
            if not model_id:
                continue
            alias = alias_for_model_id(model_id)
            catalog[f"{provider}/{model_id}"] = {"alias": alias} if alias else {}
    return catalog


def _normalize_agent_id(value: str) -> str:
    agent_id = str(value or "").strip()
    if not agent_id:
        raise ConfigError("Agent ID 不能为空。")
    if not re.fullmatch(r"[a-z_]+", agent_id):
        raise ConfigError("Agent ID 只允许小写英文字母和下划线。")
    return agent_id


def _default_agent_workspace(state_dir: Path, agent_id: str) -> str:
    if agent_id == "main":
        return str((state_dir / "workspace").resolve())
    return str((state_dir / "workspace" / "agents" / agent_id).resolve())


def _default_agent_dir(state_dir: Path, agent_id: str) -> str:
    return str((state_dir / "agents" / agent_id / "agent").resolve())


def _normalize_agent_binding(payload: Any) -> dict[str, str] | None:
    if not isinstance(payload, dict):
        return None
    channel = str(payload.get("channel") or "").strip()
    account_id = str(payload.get("account_id") or payload.get("accountId") or "").strip()
    if not channel or not account_id:
        return None
    return {
        "channel": channel,
        "account_id": account_id,
    }


CHANNEL_PLATFORMS = {"qqbot", "ddingtalk", "wecom", "feishu"}


def _normalize_channel_platform(value: Any) -> str:
    platform = str(value or "").strip().lower()
    if platform not in CHANNEL_PLATFORMS:
        raise ConfigError(f"不支持的 Channel 平台：{platform or '(空)'}")
    return platform


def _normalize_channel_account_id(value: Any, *, default: str = "") -> str:
    account_id = str(value or default).strip()
    if not account_id:
        raise ConfigError("Channel 账号不能为空。")
    if not re.fullmatch(r"[a-z_]+", account_id):
        raise ConfigError("Channel 账号 ID 只允许小写英文字母和下划线。")
    return account_id


def _secret_input_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False)


def _text_to_secret_input(value: Any) -> Any:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw[:1] in {"{", "["}:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def _text_lines(value: Any) -> list[str]:
    parts = re.split(r"[\n,]+", str(value or ""))
    items: list[str] = []
    for part in parts:
        normalized = str(part or "").strip()
        if normalized and normalized not in items:
            items.append(normalized)
    return items


def _ensure_dict(container: dict[str, Any], key: str) -> dict[str, Any]:
    value = container.get(key)
    if isinstance(value, dict):
        return value
    container[key] = {}
    return container[key]


def _set_or_pop(target: dict[str, Any], key: str, value: Any) -> None:
    if value in ("", None, [], {}):
        target.pop(key, None)
        return
    target[key] = value


def _coerce_bool(payload: dict[str, Any], key: str, default: bool = True) -> bool:
    value = payload.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in {"", "0", "false", "off", "no"}


def _require_value(value: Any, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ConfigError(f"`{label}` 不能为空。")
    return normalized


def _coerce_optional_int(value: Any) -> int | str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    if normalized.isdigit():
        return int(normalized)
    return normalized


def _extract_channel_catalog(openclaw_data: dict[str, Any]) -> list[dict[str, Any]]:
    channels_root = openclaw_data.get("channels") or {}
    records: list[dict[str, Any]] = []

    qqbot = channels_root.get("qqbot") or {}
    if isinstance(qqbot, dict):
        qq_accounts: dict[str, dict[str, Any]] = {}
        accounts = qqbot.get("accounts")
        if isinstance(accounts, dict):
            for account_id, account in accounts.items():
                if isinstance(account, dict):
                    qq_accounts[str(account_id)] = copy.deepcopy(account)
        if str(qqbot.get("appId") or "").strip():
            qqbot_default = {
                "enabled": qqbot.get("enabled"),
                "name": qqbot.get("name"),
                "appId": qqbot.get("appId"),
                "clientSecret": qqbot.get("clientSecret"),
                "clientSecretFile": qqbot.get("clientSecretFile"),
            }
            if "default" not in qq_accounts:
                qq_accounts["default"] = qqbot_default
            else:
                merged_default = copy.deepcopy(qq_accounts["default"])
                if "enabled" not in merged_default:
                    merged_default["enabled"] = qqbot_default.get("enabled")
                for key in ("name", "appId", "clientSecret", "clientSecretFile"):
                    if not str(merged_default.get(key) or "").strip() and str(qqbot_default.get(key) or "").strip():
                        merged_default[key] = qqbot_default.get(key)
                qq_accounts["default"] = merged_default
        for account_id, account in qq_accounts.items():
            app_id = str(account.get("appId") or "").strip()
            if not app_id:
                continue
            records.append({
                "key": f"qqbot:{account_id}",
                "platform": "qqbot",
                "channel": "qqbot",
                "account_id": str(account_id),
                "name": str(account.get("name") or account_id).strip() or str(account_id),
                "enabled": account.get("enabled") is not False,
                "config_type": "qqbot",
                "config": {
                    "platform": "qqbot",
                    "account_id": str(account_id),
                    "enabled": account.get("enabled") is not False,
                    "name": str(account.get("name") or "").strip(),
                    "appId": app_id,
                    "clientSecret": str(account.get("clientSecret") or "").strip(),
                    "clientSecretFile": str(account.get("clientSecretFile") or "").strip(),
                },
            })

    dingtalk = channels_root.get("ddingtalk") or {}
    if isinstance(dingtalk, dict) and str(dingtalk.get("clientId") or "").strip():
        records.append({
            "key": "ddingtalk:default",
            "platform": "ddingtalk",
            "channel": "ddingtalk",
            "account_id": "default",
            "name": str(dingtalk.get("name") or "default").strip() or "default",
            "enabled": dingtalk.get("enabled") is not False,
            "config_type": "ddingtalk",
            "config": {
                "platform": "ddingtalk",
                "account_id": "default",
                "enabled": dingtalk.get("enabled") is not False,
                "name": str(dingtalk.get("name") or "").strip(),
                "clientId": str(dingtalk.get("clientId") or "").strip(),
                "clientSecret": str(dingtalk.get("clientSecret") or "").strip(),
            },
        })

    wecom = channels_root.get("wecom") or {}
    if isinstance(wecom, dict):
        wecom_accounts: dict[str, dict[str, Any]] = {}
        accounts = wecom.get("accounts")
        if isinstance(accounts, dict):
            for account_id, account in accounts.items():
                if isinstance(account, dict):
                    wecom_accounts[str(account_id)] = copy.deepcopy(account)
        elif any(isinstance(wecom.get(key), dict) for key in ("bot", "agent")):
            wecom_accounts["default"] = {
                "enabled": wecom.get("enabled"),
                "name": wecom.get("name"),
                "bot": copy.deepcopy(wecom.get("bot") or {}),
                "agent": copy.deepcopy(wecom.get("agent") or {}),
            }
        for account_id, account in wecom_accounts.items():
            bot = account.get("bot") or {}
            agent = account.get("agent") or {}
            if not isinstance(bot, dict):
                bot = {}
            if not isinstance(agent, dict):
                agent = {}
            mode = "agent" if agent else "bot"
            connection_mode = str(bot.get("connectionMode") or "webhook").strip() or "webhook"
            config_type = (
                "wecom-agent"
                if mode == "agent"
                else f"wecom-bot-{connection_mode}"
            )
            records.append({
                "key": f"wecom:{account_id}",
                "platform": "wecom",
                "channel": "wecom",
                "account_id": str(account_id),
                "name": str(account.get("name") or account_id).strip() or str(account_id),
                "enabled": account.get("enabled") is not False and wecom.get("enabled") is not False,
                "config_type": config_type,
                "config": {
                    "platform": "wecom",
                    "account_id": str(account_id),
                    "enabled": account.get("enabled") is not False and wecom.get("enabled") is not False,
                    "name": str(account.get("name") or "").strip(),
                    "mode": mode,
                    "connectionMode": connection_mode,
                    "aibotid": str(bot.get("aibotid") or "").strip(),
                    "token": str(bot.get("token") or agent.get("token") or "").strip(),
                    "encodingAESKey": str(bot.get("encodingAESKey") or agent.get("encodingAESKey") or "").strip(),
                    "botIds": "\n".join(str(item).strip() for item in (bot.get("botIds") or []) if str(item).strip()),
                    "receiveId": str(bot.get("receiveId") or "").strip(),
                    "botId": str(bot.get("botId") or "").strip(),
                    "secret": str(bot.get("secret") or "").strip(),
                    "corpId": str(agent.get("corpId") or "").strip(),
                    "corpSecret": str(agent.get("corpSecret") or "").strip(),
                    "agentId": "" if agent.get("agentId") in (None, "") else str(agent.get("agentId")),
                },
            })

    feishu = channels_root.get("feishu") or {}
    if isinstance(feishu, dict):
        feishu_accounts: dict[str, dict[str, Any]] = {}
        accounts = feishu.get("accounts")
        if isinstance(accounts, dict):
            for account_id, account in accounts.items():
                if isinstance(account, dict):
                    feishu_accounts[str(account_id)] = copy.deepcopy(account)
        elif str(feishu.get("appId") or "").strip():
            feishu_accounts["default"] = {
                "enabled": feishu.get("enabled"),
                "name": feishu.get("name"),
                "appId": feishu.get("appId"),
                "appSecret": copy.deepcopy(feishu.get("appSecret")),
                "encryptKey": feishu.get("encryptKey"),
                "verificationToken": copy.deepcopy(feishu.get("verificationToken")),
                "domain": feishu.get("domain"),
                "connectionMode": feishu.get("connectionMode"),
                "webhookPath": feishu.get("webhookPath"),
            }
        for account_id, account in feishu_accounts.items():
            app_id = str(account.get("appId") or "").strip()
            if not app_id:
                continue
            connection_mode = str(account.get("connectionMode") or feishu.get("connectionMode") or "websocket").strip() or "websocket"
            records.append({
                "key": f"feishu:{account_id}",
                "platform": "feishu",
                "channel": "feishu",
                "account_id": str(account_id),
                "name": str(account.get("name") or account_id).strip() or str(account_id),
                "enabled": account.get("enabled") is not False and feishu.get("enabled") is not False,
                "config_type": f"feishu-{connection_mode}",
                "config": {
                    "platform": "feishu",
                    "account_id": str(account_id),
                    "enabled": account.get("enabled") is not False and feishu.get("enabled") is not False,
                    "name": str(account.get("name") or "").strip(),
                    "appId": app_id,
                    "appSecret": _secret_input_to_text(account.get("appSecret")),
                    "encryptKey": str(account.get("encryptKey") or "").strip(),
                    "verificationToken": _secret_input_to_text(account.get("verificationToken")),
                    "domain": str(account.get("domain") or feishu.get("domain") or "feishu").strip() or "feishu",
                    "connectionMode": connection_mode,
                    "webhookPath": str(account.get("webhookPath") or feishu.get("webhookPath") or "").strip(),
                },
            })

    records.sort(key=lambda item: (str(item.get("platform") or ""), str(item.get("account_id") or "")))
    return records


def _binding_key(binding: dict[str, Any]) -> str:
    channel = str(binding.get("channel") or "").strip()
    account_id = str(binding.get("account_id") or binding.get("accountId") or "").strip()
    return f"{channel}:{account_id}" if channel and account_id else ""


def _validate_agent_bindings_unique(agents_map: dict[str, Any], target_agent_id: str, bindings: list[dict[str, str]]) -> None:
    seen_keys: set[str] = set()
    for binding in bindings:
        key = _binding_key(binding)
        if not key:
            continue
        if key in seen_keys:
            raise ConfigError(f"Agent `{target_agent_id}` 内部存在重复 Channel 绑定：{key}")
        seen_keys.add(key)

    occupied_by: dict[str, str] = {}
    for agent_id, record in (agents_map or {}).items():
        if agent_id == target_agent_id or not isinstance(record, dict):
            continue
        for binding in record.get("bindings") or []:
            key = _binding_key(binding)
            if key and key not in occupied_by:
                occupied_by[key] = str(agent_id)

    for key in seen_keys:
        owner = occupied_by.get(key)
        if owner:
            raise ConfigError(f"Channel 绑定 `{key}` 已被 Agent `{owner}` 占用，不能重复绑定。")


def _extract_agent_records(openclaw_data: dict[str, Any], state_dir: Path) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    agents_root = openclaw_data.get("agents") or {}
    defaults_root = agents_root.get("defaults") or {}
    default_model = defaults_root.get("model") or {}
    default_primary_ref = str(default_model.get("primary") or "").strip()
    default_primary_provider = default_primary_ref.split("/", 1)[0] if "/" in default_primary_ref else ""
    default_fallback_provider_ids = [
        str(item).split("/", 1)[0]
        for item in (default_model.get("fallbacks") or [])
        if isinstance(item, str) and "/" in item
    ]
    global_elevated = (((openclaw_data.get("tools") or {}).get("elevated") or {}) if isinstance((openclaw_data.get("tools") or {}).get("elevated"), dict) else {})
    global_elevated_mode = "on" if _coerce_bool(global_elevated, "enabled", False) else ""
    global_allow_from = _normalize_allow_from_map(global_elevated.get("allowFrom"))
    approvals_data = _load_exec_approvals_store(state_dir)
    approval_agents = approvals_data.get("agents") if isinstance(approvals_data.get("agents"), dict) else {}

    binding_map: dict[str, list[dict[str, str]]] = {}
    for binding in openclaw_data.get("bindings") or []:
        if not isinstance(binding, dict) or binding.get("type") != "route":
            continue
        agent_id = str(binding.get("agentId") or "").strip()
        match = binding.get("match") or {}
        normalized = _normalize_agent_binding({
            "channel": match.get("channel"),
            "account_id": match.get("accountId"),
        })
        if not agent_id or not normalized:
            continue
        binding_map.setdefault(agent_id, [])
        if normalized not in binding_map[agent_id]:
            binding_map[agent_id].append(normalized)

    records: dict[str, Any] = {}
    order: list[str] = []
    saw_agent_specific_elevated = False
    for item in agents_root.get("list") or []:
        if not isinstance(item, dict):
            continue
        agent_id = _normalize_agent_id(str(item.get("id") or ""))
        agent_model = item.get("model") or {}
        primary_ref = str(agent_model.get("primary") or default_primary_ref).strip()
        primary_provider = primary_ref.split("/", 1)[0] if "/" in primary_ref else default_primary_provider
        fallback_provider_ids = [
            str(ref).split("/", 1)[0]
            for ref in (agent_model.get("fallbacks") or default_model.get("fallbacks") or [])
            if isinstance(ref, str) and "/" in ref
        ]
        provider_order = [provider_id for provider_id in [primary_provider, *fallback_provider_ids] if provider_id]
        workspace = str(item.get("workspace") or _default_agent_workspace(state_dir, agent_id)).strip()
        agent_dir = str(item.get("agentDir") or _default_agent_dir(state_dir, agent_id)).strip()
        identity = item.get("identity") or {}
        display_name = str(identity.get("name") or item.get("name") or agent_id).strip() or agent_id
        agent_tools = item.get("tools") or {}
        agent_elevated = agent_tools.get("elevated") if isinstance(agent_tools, dict) else {}
        agent_elevated_mode = ""
        if isinstance(agent_elevated, dict):
            if "enabled" in agent_elevated:
                agent_elevated_mode = "on" if _coerce_bool(agent_elevated, "enabled", False) else "off"
            agent_allow_from = _normalize_allow_from_map(agent_elevated.get("allowFrom"))
        else:
            agent_allow_from = {}
        if agent_elevated_mode or agent_allow_from:
            saw_agent_specific_elevated = True
        approval_entry = approval_agents.get(agent_id) if isinstance(approval_agents, dict) else {}
        permissions = _normalize_agent_permissions_from_sources(
            approval_entry,
            {
                "mode": agent_elevated_mode,
                "allow_from": agent_allow_from,
            },
        )
        records[agent_id] = {
            "id": agent_id,
            "name": display_name,
            "workspace": workspace,
            "agent_dir": agent_dir,
            "provider_order": provider_order or copy.deepcopy([provider_id for provider_id in [default_primary_provider, *default_fallback_provider_ids] if provider_id]),
            "bindings": binding_map.get(agent_id, []),
            "permissions": permissions,
        }
        order.append(agent_id)

    if "main" not in records:
        records["main"] = {
            "id": "main",
            "name": "main",
            "workspace": _default_agent_workspace(state_dir, "main"),
            "agent_dir": _default_agent_dir(state_dir, "main"),
            "provider_order": copy.deepcopy([provider_id for provider_id in [default_primary_provider, *default_fallback_provider_ids] if provider_id]),
            "bindings": binding_map.get("main", []),
            "permissions": _normalize_agent_permissions_from_sources(
                approval_agents.get("main") if isinstance(approval_agents, dict) else {},
                {},
            ),
        }
        order.insert(0, "main")

    if not saw_agent_specific_elevated and global_elevated_mode and global_allow_from and "main" in records:
        main_permissions = ((records.get("main") or {}).get("permissions") or {})
        main_elevated = main_permissions.get("elevated") if isinstance(main_permissions, dict) else {}
        if not _normalize_elevated_mode((main_elevated or {}).get("mode")) and not _normalize_allow_from_map((main_elevated or {}).get("allow_from")):
            records["main"]["permissions"] = _normalize_agent_permissions_from_sources(
                (main_permissions.get("exec") if isinstance(main_permissions, dict) else {}),
                {
                    "mode": global_elevated_mode,
                    "allow_from": global_allow_from,
                },
            )

    return records, order, _extract_channel_catalog(openclaw_data)


def _agent_dirs(state_dir: Path) -> list[Path]:
    return sorted(path for path in state_dir.glob("agents/*/agent") if path.is_dir())


def _main_auth_store(state_dir: Path) -> Path | None:
    candidate = state_dir / "agents" / "main" / "agent" / "auth-profiles.json"
    if candidate.exists():
        return candidate
    for agent_dir in _agent_dirs(state_dir):
        path = agent_dir / "auth-profiles.json"
        if path.exists():
            return path
    return None


def _status_summary(status: dict[str, Any], provider: str) -> dict[str, Any]:
    providers = (status.get("auth") or {}).get("providers") or []
    provider_entry = None
    for item in providers:
        if str(item.get("provider") or "").lower() == provider.lower():
            provider_entry = item
            break

    return {
        "default_model": status.get("defaultModel"),
        "resolved_default": status.get("resolvedDefault"),
        "provider_auth": provider_entry,
        "image_model": status.get("imageModel"),
    }


def _presets_path(state_dir: Path) -> Path:
    return state_dir / PRESETS_FILENAME


def _empty_presets_store() -> dict[str, Any]:
    return {"version": 1, "lastSelected": "", "presets": {}}


def _sanitize_preset_config(payload: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}

    for key in ("provider", "base_url", "api", "default_model_id", "default_model_name"):
        value = str(payload.get(key) or "").strip()
        if value:
            clean[key] = value

    api_keys = _coerce_api_keys(payload)
    if api_keys:
        clean["api_keys"] = api_keys

    models_payload = payload.get("models")
    if isinstance(models_payload, list):
        clean["models"] = [_normalize_model_entry(item) for item in models_payload]
    elif isinstance(payload.get("model"), (dict, str)):
        clean["models"] = [_normalize_model_entry(payload["model"])]

    if "keep_other_providers" in payload:
        clean["keep_other_providers"] = bool(payload.get("keep_other_providers"))

    return clean


def load_presets(state_dir: Path = DEFAULT_STATE_DIR) -> dict[str, Any]:
    store_path = _presets_path(state_dir)
    if not store_path.exists():
        return _empty_presets_store()

    data = load_json(store_path)
    presets = data.get("presets")
    if not isinstance(presets, dict):
        presets = {}

    normalized_presets: dict[str, Any] = {}
    for name, payload in presets.items():
        if not isinstance(payload, dict):
            continue
        preset_name = str(name or "").strip()
        if not preset_name:
            continue
        normalized_presets[preset_name] = _sanitize_preset_config(payload)

    last_selected = str(data.get("lastSelected") or "").strip()
    if last_selected and last_selected not in normalized_presets:
        last_selected = ""

    return {
        "version": 1,
        "lastSelected": last_selected,
        "presets": normalized_presets,
        "path": str(store_path),
    }


def save_preset(name: str, payload: dict[str, Any], state_dir: Path = DEFAULT_STATE_DIR) -> dict[str, Any]:
    preset_name = str(name or "").strip()
    if not preset_name:
        raise ConfigError("预设名称不能为空。")

    store = load_presets(state_dir)
    store["presets"][preset_name] = _sanitize_preset_config(payload)
    store["lastSelected"] = preset_name
    write_json(_presets_path(state_dir), {k: v for k, v in store.items() if k != "path"})

    return {
        "name": preset_name,
        "preset": store["presets"][preset_name],
        "count": len(store["presets"]),
        "path": str(_presets_path(state_dir)),
    }


def delete_preset(name: str, state_dir: Path = DEFAULT_STATE_DIR) -> dict[str, Any]:
    preset_name = str(name or "").strip()
    if not preset_name:
        raise ConfigError("预设名称不能为空。")

    store = load_presets(state_dir)
    if preset_name not in store["presets"]:
        raise ConfigError(f"预设不存在：{preset_name}")

    store["presets"].pop(preset_name, None)
    if store.get("lastSelected") == preset_name:
        store["lastSelected"] = next(iter(store["presets"]), "")
    write_json(_presets_path(state_dir), {k: v for k, v in store.items() if k != "path"})

    return {
        "name": preset_name,
        "count": len(store["presets"]),
        "path": str(_presets_path(state_dir)),
        "lastSelected": store.get("lastSelected") or "",
    }


def _store_path(state_dir: Path) -> Path:
    return state_dir / STORE_FILENAME


def _empty_panel_store() -> dict[str, Any]:
    return {
        "version": PANEL_STORE_VERSION,
        "selectedProvider": "",
        "providerOrder": [],
        "modelCatalog": [],
        "providers": {},
        "channels": {},
        "agentOrder": [],
        "agents": {},
        "runtimeAuth": _default_runtime_auth_config(),
    }


def _merge_catalog(*model_groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in model_groups:
        for item in group or []:
            model = _normalize_model_entry(item)
            if model["id"] in seen:
                continue
            seen.add(model["id"])
            merged.append(model)
    return merged


def _normalize_model_catalog(models: list[Any]) -> list[dict[str, Any]]:
    return _merge_catalog([_normalize_model_entry(item) for item in models or []])


def _sanitize_provider_record(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    site = normalize_provider_site(str(payload.get("site") or payload.get("base_url") or "").strip())
    provider_name = str(payload.get("provider") or name or "").strip()
    if not provider_name or _is_generic_provider_name(provider_name):
        provider_name = _provider_name_from_base_url(site)

    api_keys = _coerce_api_keys(payload)
    requested_models = _coerce_provider_models(payload)
    requested_by_id: dict[str, dict[str, Any]] = {}
    requested_order: list[str] = []
    for item in requested_models:
        model_id = item["id"]
        requested_by_id[model_id] = {
            "id": model_id,
            "name": str(item.get("name") or model_id).strip() or model_id,
            "api": str(item.get("api") or "").strip() or infer_api_type(model_id),
            "route_suffix": normalize_route_suffix(item.get("route_suffix") or item.get("routeSuffix") or "") or default_route_suffix_for_model(model_id),
            "enabled": bool(item.get("enabled")),
        }
        if model_id not in requested_order:
            requested_order.append(model_id)

    provider_models: list[dict[str, Any]] = []
    provider_model_ids: set[str] = set()
    for model_id in requested_order:
        item = requested_by_id[model_id]
        if not item["enabled"] or model_id in provider_model_ids:
            continue
        provider_models.append({
            "id": model_id,
            "name": item["name"],
            "api": item["api"],
            "route_suffix": item["route_suffix"],
            "enabled": True,
        })
        provider_model_ids.add(model_id)

    default_model_id = str(payload.get("default_model_id") or "").strip()
    if default_model_id and default_model_id not in provider_model_ids:
        source = requested_by_id.get(default_model_id) or {
            "id": default_model_id,
            "name": default_model_id,
            "api": infer_api_type(default_model_id),
            "route_suffix": default_route_suffix_for_model(default_model_id),
            "enabled": True,
        }
        provider_models.insert(0, {
            "id": default_model_id,
            "name": str(source.get("name") or default_model_id).strip() or default_model_id,
            "api": str(source.get("api") or "").strip(),
            "route_suffix": normalize_route_suffix(source.get("route_suffix")) or default_route_suffix_for_model(default_model_id),
            "enabled": True,
        })
        provider_model_ids.add(default_model_id)
    if not default_model_id and provider_models:
        default_model_id = provider_models[0]["id"]

    model_ids = [str(item.get("id") or "").strip() for item in provider_models if str(item.get("id") or "").strip()]
    if default_model_id and default_model_id not in model_ids:
        default_model_id = model_ids[0] if model_ids else ""
    default_model = next((item for item in provider_models if item["id"] == default_model_id), provider_models[0] if provider_models else {})
    default_api = str(default_model.get("api") or "").strip()

    return {
        "provider": provider_name,
        "site": site,
        "base_url": site,
        "default_api": default_api,
        "api": default_api,
        "api_keys": api_keys,
        "models": provider_models,
        "model_ids": model_ids,
        "default_model_id": default_model_id,
        "keep_other_providers": _coerce_bool(payload, "keep_other_providers", True),
        "enabled": _coerce_bool(payload, "enabled", True),
    }


def _provider_display_order(store: dict[str, Any]) -> list[str]:
    ordered: list[str] = []
    for item in store.get("providerOrder") or []:
        provider_id = str(item or "").strip()
        if provider_id and provider_id in (store.get("providers") or {}) and provider_id not in ordered:
            ordered.append(provider_id)
    for provider_id in (store.get("providers") or {}):
        if provider_id not in ordered:
            ordered.append(provider_id)
    return ordered


def _migrate_panel_store(state_dir: Path) -> dict[str, Any]:
    current = load_current_config(state_dir)
    openclaw_data = load_json(state_dir / "openclaw.json")
    store = _empty_panel_store()
    store["runtimeAuth"] = _normalize_runtime_auth_config({}, current_provider=str(current.get("provider") or ""))
    if current.get("provider"):
        record = _sanitize_provider_record(
            current.get("provider") or "",
            {
                "provider": current["provider"],
                "base_url": current.get("base_url") or "",
                "api": current.get("api") or "",
                "api_keys": current.get("api_keys") or [],
                "models": current.get("models") or [],
                "default_model_id": current.get("default_model_id") or "",
                "keep_other_providers": True,
            },
        )
        store["providers"][record["provider"]] = record
        store["selectedProvider"] = record["provider"]
        store["modelCatalog"] = _merge_catalog(current.get("models") or [])

    legacy_path = _presets_path(state_dir)
    if legacy_path.exists():
        legacy = load_presets(state_dir)
        for _, payload in (legacy.get("presets") or {}).items():
            if not isinstance(payload, dict):
                continue
            if not str(payload.get("base_url") or "").strip():
                continue
            record = _sanitize_provider_record("", payload)
            store["providers"][record["provider"]] = record
            store["modelCatalog"] = _merge_catalog(store["modelCatalog"], payload.get("models") or [])
        last_selected = str(legacy.get("lastSelected") or "").strip()
        if last_selected:
            selected_payload = (legacy.get("presets") or {}).get(last_selected) or {}
            selected = ""
            selected_base_url = str(selected_payload.get("site") or selected_payload.get("base_url") or "").strip()
            if selected_base_url:
                for provider_id, record in store["providers"].items():
                    if _provider_site(record) == normalize_provider_site(selected_base_url):
                        selected = provider_id
                        break
            if selected and selected in store["providers"]:
                store["selectedProvider"] = selected

    agents, agent_order, _ = _extract_agent_records(openclaw_data, state_dir)
    store["agents"] = agents
    store["agentOrder"] = agent_order

    return store


def load_panel_store(state_dir: Path = DEFAULT_STATE_DIR) -> dict[str, Any]:
    """Load the persisted panel store and backfill any missing defaults."""
    store_path = _store_path(state_dir)
    should_rewrite = False
    openclaw_data = load_json(state_dir / "openclaw.json")
    if store_path.exists():
        raw = load_json(store_path)
    else:
        raw = _migrate_panel_store(state_dir)
        write_json(store_path, raw)
    if int(raw.get("version") or 0) != PANEL_STORE_VERSION:
        should_rewrite = True

    current_primary_model = str((((openclaw_data.get("agents") or {}).get("defaults") or {}).get("model") or {}).get("primary") or "").strip()
    current_runtime_provider = current_primary_model.split("/", 1)[0] if "/" in current_primary_model else ""

    providers_raw = raw.get("providers")
    providers: dict[str, Any] = {}
    existing_names: set[str] = set()
    renamed: dict[str, str] = {}
    if isinstance(providers_raw, dict):
        for name, payload in providers_raw.items():
            if not isinstance(payload, dict):
                continue
            if not str(payload.get("site") or payload.get("base_url") or "").strip():
                continue
            record = _sanitize_provider_record(str(name or payload.get("provider") or ""), payload)
            raw_site = str(payload.get("site") or payload.get("base_url") or "").strip().rstrip("/")
            if normalize_provider_site(raw_site) != str(record.get("site") or record.get("base_url") or "").strip():
                should_rewrite = True
            resolved_name = _dedupe_provider_name(record["provider"], existing_names, original_name=str(name or "").strip())
            if resolved_name != str(name or "").strip():
                should_rewrite = True
            record["provider"] = resolved_name
            providers[resolved_name] = record
            existing_names.add(resolved_name)
            renamed[str(name or "").strip()] = resolved_name

    provider_order_raw = raw.get("providerOrder")
    provider_order: list[str] = []
    if isinstance(provider_order_raw, list):
        for item in provider_order_raw:
            provider_id = str(item or "").strip()
            provider_id = renamed.get(provider_id, provider_id)
            if provider_id and provider_id in providers and provider_id not in provider_order:
                provider_order.append(provider_id)
    for provider_id in providers:
        if provider_id not in provider_order:
            provider_order.append(provider_id)
            should_rewrite = True

    model_catalog = _normalize_model_catalog(raw.get("modelCatalog") or [])
    selected_provider = str(raw.get("selectedProvider") or "").strip()
    selected_provider = renamed.get(selected_provider, selected_provider)
    if selected_provider and selected_provider not in providers:
        selected_provider = ""
    if not selected_provider and provider_order:
        selected_provider = provider_order[0]
        should_rewrite = True

    runtime_auth = _normalize_runtime_auth_config(raw.get("runtimeAuth") or {}, current_provider=current_runtime_provider)
    if runtime_auth != (raw.get("runtimeAuth") or {}):
        should_rewrite = True

    agents_raw = raw.get("agents")
    migrated_agents, migrated_agent_order, channel_catalog = _extract_agent_records(openclaw_data, state_dir)
    channels_raw = raw.get("channels")
    channels: dict[str, Any] = {}
    if isinstance(channels_raw, dict):
        for key, payload in channels_raw.items():
            normalized = _normalize_channel_meta_record(str(key or ""), payload)
            if not normalized:
                should_rewrite = True
                continue
            normalized_key, channel_meta = normalized
            channels[normalized_key] = channel_meta
    agents: dict[str, Any] = {}
    agent_order_raw = raw.get("agentOrder")
    if isinstance(agents_raw, dict):
        for agent_id, payload in agents_raw.items():
            if not isinstance(payload, dict):
                continue
            normalized_id = _normalize_agent_id(str(payload.get("id") or agent_id))
            migrated = migrated_agents.get(normalized_id) or {}
            primary_provider = renamed.get(str(payload.get("primary_provider") or "").strip(), str(payload.get("primary_provider") or "").strip())
            fallback_provider_ids: list[str] = []
            for item in payload.get("fallback_provider_ids") or []:
                provider_id = renamed.get(str(item or "").strip(), str(item or "").strip())
                if provider_id and provider_id in providers and provider_id not in fallback_provider_ids:
                    fallback_provider_ids.append(provider_id)
            legacy_provider_order = []
            if primary_provider:
                legacy_provider_order.append(primary_provider)
            for provider_id in fallback_provider_ids:
                if provider_id not in legacy_provider_order:
                    legacy_provider_order.append(provider_id)
            agent_provider_order: list[str] = []
            for item in payload.get("provider_order") or legacy_provider_order:
                provider_id = renamed.get(str(item or "").strip(), str(item or "").strip())
                if provider_id and provider_id in providers and provider_id not in agent_provider_order:
                    agent_provider_order.append(provider_id)
            bindings: list[dict[str, str]] = []
            for item in payload.get("bindings") or []:
                normalized_binding = _normalize_agent_binding(item)
                if normalized_binding and normalized_binding not in bindings:
                    bindings.append(normalized_binding)
            permissions_payload = payload.get("permissions") if isinstance(payload.get("permissions"), dict) else {}
            permissions = _normalize_agent_permissions_from_sources(
                permissions_payload.get("exec"),
                permissions_payload.get("elevated"),
            )
            agents[normalized_id] = {
                "id": normalized_id,
                "name": str(payload.get("name") or migrated.get("name") or normalized_id).strip() or normalized_id,
                "workspace": str(payload.get("workspace") or migrated.get("workspace") or _default_agent_workspace(state_dir, normalized_id)).strip(),
                "agent_dir": str(payload.get("agent_dir") or payload.get("agentDir") or migrated.get("agent_dir") or _default_agent_dir(state_dir, normalized_id)).strip(),
                "provider_order": agent_provider_order or copy.deepcopy(migrated.get("provider_order") or []),
                "bindings": bindings or copy.deepcopy(migrated.get("bindings") or []),
                "permissions": permissions if permissions != _empty_agent_permissions() else copy.deepcopy((migrated.get("permissions") or {})),
            }
    for agent_id, record in migrated_agents.items():
        if agent_id not in agents:
            agents[agent_id] = copy.deepcopy(record)
            should_rewrite = True

    if _migrate_channel_level_permissions_from_agents(agents, channels):
        should_rewrite = True

    for record in agents.values():
        if not isinstance(record, dict):
            continue
        normalized_provider_order: list[str] = []
        for provider_id in record.get("provider_order") or []:
            if provider_id in providers and provider_id not in normalized_provider_order:
                normalized_provider_order.append(provider_id)
        if record.get("provider_order") != normalized_provider_order:
            should_rewrite = True
        record["provider_order"] = normalized_provider_order
        normalized_permissions = _strip_channel_level_permissions_from_agent(_normalize_agent_permissions_from_sources(
            ((record.get("permissions") or {}).get("exec") if isinstance(record.get("permissions"), dict) else {}),
            ((record.get("permissions") or {}).get("elevated") if isinstance(record.get("permissions"), dict) else {}),
        ))
        if record.get("permissions") != normalized_permissions:
            should_rewrite = True
        record["permissions"] = normalized_permissions
        record.pop("primary_provider", None)
        record.pop("fallback_provider_ids", None)

    channel_keys = {_binding_key(item) for item in channel_catalog if _binding_key(item)}
    for key in list(channels.keys()):
        if key not in channel_keys:
            channels.pop(key, None)
            should_rewrite = True
    for item in channel_catalog:
        key = _binding_key(item)
        item["permissions"] = copy.deepcopy(
            ((channels.get(key) or {}).get("permissions") if key else None) or _empty_channel_permissions()
        )

    binding_owner_map: dict[str, str] = {}
    for agent_id, record in agents.items():
        if not isinstance(record, dict):
            continue
        for binding in record.get("bindings") or []:
            key = _binding_key(binding)
            if key and key not in binding_owner_map:
                binding_owner_map[key] = agent_id
    for item in channel_catalog:
        key = _binding_key(item)
        if key and key in binding_owner_map:
            item["bound_agent_id"] = binding_owner_map[key]

    agent_order: list[str] = []
    if isinstance(agent_order_raw, list):
        for item in agent_order_raw:
            agent_id = str(item or "").strip()
            if agent_id and agent_id in agents and agent_id not in agent_order:
                agent_order.append(agent_id)
    for agent_id in migrated_agent_order:
        if agent_id in agents and agent_id not in agent_order:
            agent_order.append(agent_id)
    for agent_id in agents:
        if agent_id not in agent_order:
            agent_order.append(agent_id)

    if should_rewrite:
        write_json(store_path, {
            "version": PANEL_STORE_VERSION,
            "selectedProvider": selected_provider,
            "providerOrder": provider_order,
            "modelCatalog": model_catalog,
            "providers": providers,
            "channels": channels,
            "agentOrder": agent_order,
            "agents": agents,
            "runtimeAuth": runtime_auth,
        })

    return {
        "version": PANEL_STORE_VERSION,
        "selectedProvider": selected_provider,
        "providerOrder": provider_order,
        "modelCatalog": model_catalog,
        "providers": providers,
        "channels": channels,
        "agentOrder": agent_order,
        "agents": agents,
        "runtimeAuth": runtime_auth,
        "channelCatalog": channel_catalog,
        "path": str(store_path),
    }


def _write_panel_store(store: dict[str, Any], state_dir: Path = DEFAULT_STATE_DIR) -> None:
    write_json(_store_path(state_dir), {k: v for k, v in store.items() if k not in {"path", "channelCatalog"}})


def _rewrite_agent_provider_refs(store: dict[str, Any], source_provider: str, target_provider: str = "") -> None:
    source = str(source_provider or "").strip()
    target = str(target_provider or "").strip()
    if not source:
        return
    for record in (store.get("agents") or {}).values():
        if not isinstance(record, dict):
            continue
        provider_order: list[str] = []
        legacy_provider_order = []
        primary_provider = str(record.get("primary_provider") or "").strip()
        if primary_provider:
            legacy_provider_order.append(primary_provider)
        for item in record.get("fallback_provider_ids") or []:
            provider_id = str(item or "").strip()
            if provider_id and provider_id not in legacy_provider_order:
                legacy_provider_order.append(provider_id)
        for item in record.get("provider_order") or legacy_provider_order:
            provider_id = str(item or "").strip()
            if not provider_id:
                continue
            if provider_id == source:
                provider_id = target
            if provider_id and provider_id not in provider_order:
                provider_order.append(provider_id)
        record["provider_order"] = provider_order
        record.pop("primary_provider", None)
        record.pop("fallback_provider_ids", None)


def save_model_catalog(models: list[Any], state_dir: Path = DEFAULT_STATE_DIR) -> dict[str, Any]:
    """Replace the model catalog and cascade model removals into providers."""
    store = load_panel_store(state_dir)
    store["modelCatalog"] = _normalize_model_catalog(models)
    catalog_map = {
        str(item.get("id") or "").strip(): item
        for item in store["modelCatalog"]
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    allowed_model_ids = {
        model_id
        for model_id, item in catalog_map.items()
        if isinstance(item, dict) and item.get("enabled") is not False
    }
    for provider_id, record in (store.get("providers") or {}).items():
        if not isinstance(record, dict):
            continue
        normalized_record = _sanitize_provider_record(provider_id, record)
        next_models: list[dict[str, Any]] = []
        seen_model_ids: set[str] = set()
        for item in normalized_record.get("models") or []:
            model = _normalize_provider_model_entry(item)
            model_id = model["id"]
            if model_id not in allowed_model_ids or model_id in seen_model_ids or not model["enabled"]:
                continue
            catalog_item = catalog_map.get(model_id) or {}
            next_models.append({
                "id": model_id,
                "name": str(catalog_item.get("name") or model.get("name") or model_id).strip() or model_id,
                "api": str(model.get("api") or "").strip(),
                "route_suffix": _resolved_model_route(model),
                "enabled": True,
            })
            seen_model_ids.add(model_id)
        normalized_record["models"] = next_models
        normalized_record["model_ids"] = [item["id"] for item in next_models]
        default_model_id = str(normalized_record.get("default_model_id") or "").strip()
        if default_model_id not in normalized_record["model_ids"]:
            normalized_record["default_model_id"] = normalized_record["model_ids"][0] if normalized_record["model_ids"] else ""
        store["providers"][provider_id] = normalized_record
    _write_panel_store(store, state_dir)
    return {
        "count": len(store["modelCatalog"]),
        "modelCatalog": store["modelCatalog"],
        "providers": store.get("providers") or {},
        "path": store["path"],
    }


def refresh_provider_available_models(
    provider_ids: list[Any],
    state_dir: Path = DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    """Probe selected providers and return a UI-friendly diff summary."""
    store = load_panel_store(state_dir)
    providers_map = store.get("providers") or {}
    provider_order = _provider_display_order(store)
    target_provider_ids: list[str] = []
    for item in provider_ids or []:
        provider_id = str(item or "").strip()
        if provider_id and provider_id in providers_map and provider_id not in target_provider_ids:
            target_provider_ids.append(provider_id)
    if not target_provider_ids:
        raise ConfigError("请先选择至少一个 Provider。")

    model_catalog = [item for item in _normalize_model_catalog(store.get("modelCatalog") or []) if item.get("enabled") is not False]
    if not model_catalog:
        raise ConfigError("模型库为空或全部已禁用，请先维护模型库。")

    agent_order_before = {
        str(agent_id): [str(item or "").strip() for item in ((record or {}).get("provider_order") or []) if str(item or "").strip()]
        for agent_id, record in (store.get("agents") or {}).items()
        if isinstance(record, dict)
    }

    refreshed_map: dict[str, dict[str, Any]] = {}
    updated_records_map: dict[str, dict[str, Any]] = {}
    diff_map: dict[str, dict[str, Any]] = {}
    max_workers = max(1, min(PROVIDER_REFRESH_MAX_WORKERS, len(target_provider_ids)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                _refresh_single_provider_available_models,
                provider_id,
                providers_map.get(provider_id) or {},
                model_catalog,
            ): provider_id
            for provider_id in target_provider_ids
        }
        for future in as_completed(future_map):
            provider_id = future_map[future]
            try:
                result_provider_id, updated_record, refreshed_summary, diff_summary = future.result()
            except Exception as exc:
                raise ConfigError(f"刷新 Provider `{provider_id}` 失败：{exc}") from exc
            updated_records_map[result_provider_id] = updated_record
            refreshed_map[result_provider_id] = refreshed_summary
            diff_map[result_provider_id] = diff_summary

    refreshed = [refreshed_map[provider_id] for provider_id in target_provider_ids if provider_id in refreshed_map]
    diffs = [diff_map[provider_id] for provider_id in target_provider_ids if provider_id in diff_map]
    for provider_id, updated_record in updated_records_map.items():
        providers_map[provider_id] = updated_record

    disabled_provider_ids = [provider_id for provider_id in target_provider_ids if (providers_map.get(provider_id) or {}).get("enabled") is False]
    total_success_models = sum(int(item.get("available_count") or 0) for item in refreshed)
    total_preserved_models = sum(int(item.get("preserved_available_count") or 0) for item in refreshed)
    impacted_agent_ids: list[str] = []
    if disabled_provider_ids:
        for record in (store.get("agents") or {}).values():
            if not isinstance(record, dict):
                continue
            original_order = [str(item or "").strip() for item in (record.get("provider_order") or []) if str(item or "").strip()]
            normalized_provider_order: list[str] = []
            for provider_id in original_order:
                if provider_id and provider_id not in disabled_provider_ids and provider_id not in normalized_provider_order:
                    normalized_provider_order.append(provider_id)
            if normalized_provider_order != original_order:
                agent_id = str(record.get("id") or "").strip()
                if agent_id and agent_id not in impacted_agent_ids:
                    impacted_agent_ids.append(agent_id)
            record["provider_order"] = normalized_provider_order

    ordered_all = provider_order + [provider_id for provider_id in providers_map if provider_id not in provider_order]
    enabled_order = [provider_id for provider_id in ordered_all if provider_id in providers_map and (providers_map.get(provider_id) or {}).get("enabled") is not False]
    disabled_order = [provider_id for provider_id in ordered_all if provider_id in providers_map and (providers_map.get(provider_id) or {}).get("enabled") is False]
    store["providerOrder"] = enabled_order + disabled_order
    store["selectedProvider"] = enabled_order[0] if enabled_order else (store["providerOrder"][0] if store["providerOrder"] else "")
    store["providers"] = providers_map
    _write_panel_store(store, state_dir)

    agent_diffs = []
    for agent_id in impacted_agent_ids:
        before_order = agent_order_before.get(agent_id) or []
        after_order = [str(item or "").strip() for item in (((store.get("agents") or {}).get(agent_id) or {}).get("provider_order") or []) if str(item or "").strip()]
        removed = [provider_id for provider_id in before_order if provider_id not in after_order]
        agent_diffs.append({
            "id": agent_id,
            "before_order": before_order,
            "after_order": after_order,
            "removed_provider_ids": removed,
        })

    return {
        "refreshed": refreshed,
        "diffs": diffs,
        "providers": store.get("providers") or {},
        "providerOrder": store.get("providerOrder") or [],
        "disabled_provider_ids": disabled_provider_ids,
        "selected_provider_ids": target_provider_ids,
        "tested_provider_count": len(target_provider_ids),
        "total_available_models": total_success_models,
        "total_preserved_models": total_preserved_models,
        "impacted_agents": agent_diffs,
        "path": store["path"],
    }


def reorder_provider_records(provider_ids: list[Any], state_dir: Path = DEFAULT_STATE_DIR) -> dict[str, Any]:
    store = load_panel_store(state_dir)
    providers = store.get("providers") or {}
    ordered: list[str] = []
    for item in provider_ids:
        provider_id = str(item or "").strip()
        if provider_id and provider_id in providers and provider_id not in ordered:
            ordered.append(provider_id)
    for provider_id in providers:
        if provider_id not in ordered:
            ordered.append(provider_id)
    store["providerOrder"] = ordered
    if ordered:
        store["selectedProvider"] = ordered[0]
    else:
        store["selectedProvider"] = ""
    _write_panel_store(store, state_dir)
    return {
        "providerOrder": ordered,
        "selectedProvider": store["selectedProvider"],
        "path": store["path"],
    }


def save_provider_record(payload: dict[str, Any], state_dir: Path = DEFAULT_STATE_DIR) -> dict[str, Any]:
    """Create or update a provider entry inside the panel store."""
    store = load_panel_store(state_dir)
    original_name = str(payload.get("original_name") or "").strip()
    previous_order = [str(item or "").strip() for item in (store.get("providerOrder") or []) if str(item or "").strip()]
    record = _sanitize_provider_record(original_name or str(payload.get("provider") or "").strip(), payload)
    _ensure_unique_provider_base_url(store.get("providers") or {}, record["base_url"], original_name=original_name)
    existing_names = {key for key in (store.get("providers") or {}).keys() if key != original_name}
    provider_name = _dedupe_provider_name(record["provider"], existing_names, original_name=original_name)
    record["provider"] = provider_name

    preserved_index: int | None = None
    if original_name and original_name in previous_order:
        preserved_index = previous_order.index(original_name)
    elif provider_name in previous_order:
        preserved_index = previous_order.index(provider_name)

    if original_name and original_name != provider_name:
        store["providers"].pop(original_name, None)
        store["providerOrder"] = [item for item in (store.get("providerOrder") or []) if item != original_name]
        _rewrite_agent_provider_refs(store, original_name, provider_name)

    store["providers"][provider_name] = record
    provider_order = [item for item in (store.get("providerOrder") or []) if item != provider_name]
    if preserved_index is not None:
        provider_order.insert(min(preserved_index, len(provider_order)), provider_name)
    else:
        provider_order.append(provider_name)
    store["providerOrder"] = provider_order
    if provider_order:
        store["selectedProvider"] = provider_order[0]
    _write_panel_store(store, state_dir)
    return {
        "provider": provider_name,
        "count": len(store["providers"]),
        "selectedProvider": store["selectedProvider"],
        "path": store["path"],
    }


def delete_provider_record(provider_name: str, state_dir: Path = DEFAULT_STATE_DIR) -> dict[str, Any]:
    target = str(provider_name or "").strip()
    if not target:
        raise ConfigError("Provider 名称不能为空。")

    store = load_panel_store(state_dir)
    if target not in store["providers"]:
        raise ConfigError(f"Provider 不存在：{target}")
    store["providers"].pop(target, None)
    store["providerOrder"] = [item for item in (store.get("providerOrder") or []) if item != target]
    _rewrite_agent_provider_refs(store, target, "")
    if store["selectedProvider"] == target:
        store["selectedProvider"] = (store.get("providerOrder") or [""])[0] if store.get("providerOrder") else ""
    _write_panel_store(store, state_dir)
    return {
        "provider": target,
        "count": len(store["providers"]),
        "selectedProvider": store["selectedProvider"],
        "path": store["path"],
    }


def set_selected_provider(provider_name: str, state_dir: Path = DEFAULT_STATE_DIR) -> dict[str, Any]:
    target = str(provider_name or "").strip()
    store = load_panel_store(state_dir)
    if target and target not in store["providers"]:
        raise ConfigError(f"Provider 不存在：{target}")
    store["selectedProvider"] = target
    _write_panel_store(store, state_dir)
    return {
        "selectedProvider": target,
        "path": store["path"],
    }


def _sanitize_agent_record(payload: dict[str, Any], state_dir: Path, existing_record: dict[str, Any] | None = None) -> dict[str, Any]:
    existing_record = existing_record or {}
    agent_id = _normalize_agent_id(str(payload.get("id") or existing_record.get("id") or ""))
    name = str(payload.get("name") or existing_record.get("name") or agent_id).strip() or agent_id
    workspace = str(existing_record.get("workspace") or _default_agent_workspace(state_dir, agent_id)).strip()
    agent_dir = str(existing_record.get("agent_dir") or existing_record.get("agentDir") or _default_agent_dir(state_dir, agent_id)).strip()
    legacy_provider_order = []
    existing_primary_provider = str(existing_record.get("primary_provider") or "").strip()
    if existing_primary_provider:
        legacy_provider_order.append(existing_primary_provider)
    for item in existing_record.get("fallback_provider_ids") or []:
        provider_id = str(item or "").strip()
        if provider_id and provider_id not in legacy_provider_order:
            legacy_provider_order.append(provider_id)
    provider_order: list[str] = []
    for item in payload.get("provider_order") or existing_record.get("provider_order") or legacy_provider_order:
        provider_id = str(item or "").strip()
        if provider_id and provider_id not in provider_order:
            provider_order.append(provider_id)

    bindings: list[dict[str, str]] = []
    for item in payload.get("bindings") or existing_record.get("bindings") or []:
        normalized_binding = _normalize_agent_binding(item)
        if normalized_binding and normalized_binding not in bindings:
            bindings.append(normalized_binding)
    permissions_payload = payload.get("permissions") if isinstance(payload.get("permissions"), dict) else None
    existing_permissions = existing_record.get("permissions") if isinstance(existing_record.get("permissions"), dict) else {}
    permissions = _strip_channel_level_permissions_from_agent(_normalize_agent_permissions_from_sources(
        (permissions_payload.get("exec") if isinstance(permissions_payload, dict) else existing_permissions.get("exec") if isinstance(existing_permissions, dict) else {}),
        (permissions_payload.get("elevated") if isinstance(permissions_payload, dict) else existing_permissions.get("elevated") if isinstance(existing_permissions, dict) else {}),
    ))

    return {
        "id": agent_id,
        "name": name,
        "workspace": workspace,
        "agent_dir": agent_dir,
        "provider_order": provider_order,
        "bindings": bindings,
        "permissions": permissions,
    }


def save_agent_record(payload: dict[str, Any], state_dir: Path = DEFAULT_STATE_DIR) -> dict[str, Any]:
    store = load_panel_store(state_dir)
    original_id = str(payload.get("original_id") or payload.get("originalId") or "").strip()
    existing_record = copy.deepcopy((store.get("agents") or {}).get(original_id or str(payload.get("id") or "").strip()) or {})
    record = _sanitize_agent_record(payload, state_dir, existing_record)
    agent_id = record["id"]

    providers_map = store.get("providers") or {}
    normalized_provider_order: list[str] = []
    for provider_id in record.get("provider_order") or []:
        if provider_id not in providers_map:
            raise ConfigError(f"Provider 不存在：{provider_id}")
        if provider_id not in normalized_provider_order:
            normalized_provider_order.append(provider_id)
    if not normalized_provider_order:
        raise ConfigError("请至少为 Agent 选择一个 Provider。")
    record["provider_order"] = normalized_provider_order

    if original_id and original_id != agent_id:
        raise ConfigError("暂不支持修改 Agent ID，请删除后重新新增。")

    store.setdefault("agents", {})
    store.setdefault("agentOrder", [])
    _validate_agent_bindings_unique(store["agents"], agent_id, record.get("bindings") or [])
    store["agents"][agent_id] = record
    if agent_id not in store["agentOrder"]:
        store["agentOrder"].append(agent_id)
    _write_panel_store(store, state_dir)
    return {
        "agent": agent_id,
        "count": len(store["agents"]),
        "path": store["path"],
    }


def delete_agent_record(agent_id: str, state_dir: Path = DEFAULT_STATE_DIR) -> dict[str, Any]:
    target = _normalize_agent_id(str(agent_id or ""))
    if target == "main":
        raise ConfigError("`main` Agent 不允许删除。")
    store = load_panel_store(state_dir)
    if target not in (store.get("agents") or {}):
        raise ConfigError(f"Agent 不存在：{target}")
    store["agents"].pop(target, None)
    store["agentOrder"] = [item for item in (store.get("agentOrder") or []) if item != target]
    _write_panel_store(store, state_dir)
    return {
        "agent": target,
        "count": len(store["agents"]),
        "path": store["path"],
    }


def _find_channel_record(
    channel_catalog: list[dict[str, Any]],
    platform: str,
    account_id: str,
) -> dict[str, Any] | None:
    for item in channel_catalog:
        if str(item.get("channel") or "").strip() == platform and str(item.get("account_id") or "").strip() == account_id:
            return item
    return None


def _channel_plugin_id(platform: str) -> str:
    return CHANNEL_PLUGIN_IDS.get(platform, platform)


def _normalize_channel_plugin_config(openclaw_data: dict[str, Any], platform: str) -> None:
    plugins_root = _ensure_dict(openclaw_data, "plugins")
    allow = plugins_root.setdefault("allow", [])
    if isinstance(allow, list):
        normalized_allow: list[str] = []
        preferred_plugin_id = _channel_plugin_id(platform)
        legacy_ids = set(CHANNEL_PLUGIN_LEGACY_IDS.get(platform, ()))
        for item in allow:
            plugin_id = str(item or "").strip()
            if not plugin_id:
                continue
            if plugin_id in legacy_ids:
                continue
            if plugin_id not in normalized_allow:
                normalized_allow.append(plugin_id)
        if preferred_plugin_id and preferred_plugin_id not in normalized_allow:
            normalized_allow.append(preferred_plugin_id)
        plugins_root["allow"] = normalized_allow

    entries = _ensure_dict(plugins_root, "entries")
    for legacy_id in CHANNEL_PLUGIN_LEGACY_IDS.get(platform, ()):
        if legacy_id == _channel_plugin_id(platform):
            continue
        entries.pop(legacy_id, None)


def _normalize_all_channel_plugin_configs(openclaw_data: dict[str, Any]) -> None:
    channels_root = openclaw_data.get("channels") or {}
    for platform, legacy_ids in CHANNEL_PLUGIN_LEGACY_IDS.items():
        if platform in channels_root:
            _normalize_channel_plugin_config(openclaw_data, platform)
            continue
        plugins_root = _ensure_dict(openclaw_data, "plugins")
        allow = plugins_root.get("allow")
        if isinstance(allow, list):
            plugins_root["allow"] = [item for item in allow if str(item or "").strip() not in legacy_ids]
        entries = plugins_root.get("entries")
        if isinstance(entries, dict):
            for legacy_id in legacy_ids:
                entries.pop(legacy_id, None)


def _ensure_channel_plugin_enabled(openclaw_data: dict[str, Any], platform: str) -> None:
    _normalize_channel_plugin_config(openclaw_data, platform)
    plugin_id = _channel_plugin_id(platform)
    plugins_root = openclaw_data.setdefault("plugins", {})
    allow = plugins_root.setdefault("allow", [])
    if isinstance(allow, list) and plugin_id not in allow:
        allow.append(plugin_id)
    entries = plugins_root.setdefault("entries", {})
    entry = entries.get(plugin_id)
    if isinstance(entry, dict):
        entry["enabled"] = True
    else:
        entries[plugin_id] = {"enabled": True}


def _upsert_qqbot_channel(openclaw_data: dict[str, Any], payload: dict[str, Any]) -> tuple[str, str]:
    channels_root = openclaw_data.setdefault("channels", {})
    qqbot = _ensure_dict(channels_root, "qqbot")
    _ensure_channel_plugin_enabled(openclaw_data, "qqbot")
    accounts = _ensure_dict(qqbot, "accounts")

    account_id = _normalize_channel_account_id(payload.get("account_id"))
    existing = copy.deepcopy(accounts.get(account_id) or {})
    app_id = _require_value(payload.get("appId") or existing.get("appId"), "appId")
    client_secret = str(payload.get("clientSecret") or existing.get("clientSecret") or "").strip()
    client_secret_file = str(payload.get("clientSecretFile") or existing.get("clientSecretFile") or "").strip()
    if not client_secret and not client_secret_file:
        raise ConfigError("QQ Channel 需要填写 `clientSecret` 或 `clientSecretFile`。")

    existing["enabled"] = _coerce_bool(payload, "enabled", existing.get("enabled") is not False)
    existing["appId"] = app_id
    _set_or_pop(existing, "name", str(payload.get("name") or existing.get("name") or "").strip())
    if client_secret:
        existing["clientSecret"] = client_secret
        existing.pop("clientSecretFile", None)
    else:
        existing["clientSecretFile"] = client_secret_file
        existing.pop("clientSecret", None)
    accounts[account_id] = existing
    qqbot["enabled"] = True
    return "qqbot", account_id


def _upsert_dingtalk_channel(openclaw_data: dict[str, Any], payload: dict[str, Any]) -> tuple[str, str]:
    channels_root = openclaw_data.setdefault("channels", {})
    dingtalk = _ensure_dict(channels_root, "ddingtalk")
    _ensure_channel_plugin_enabled(openclaw_data, "ddingtalk")

    dingtalk["enabled"] = _coerce_bool(payload, "enabled", dingtalk.get("enabled") is not False)
    dingtalk["clientId"] = _require_value(payload.get("clientId") or dingtalk.get("clientId"), "clientId")
    dingtalk["clientSecret"] = _require_value(payload.get("clientSecret") or dingtalk.get("clientSecret"), "clientSecret")
    _set_or_pop(dingtalk, "name", str(payload.get("name") or dingtalk.get("name") or "").strip())
    return "ddingtalk", "default"


def _bootstrap_wecom_accounts(wecom: dict[str, Any]) -> dict[str, Any]:
    accounts = wecom.get("accounts")
    if isinstance(accounts, dict):
        return accounts
    accounts = {}
    if any(isinstance(wecom.get(key), dict) for key in ("bot", "agent")):
        accounts["default"] = {
            "enabled": wecom.get("enabled"),
            "name": wecom.get("name"),
        }
        if isinstance(wecom.get("bot"), dict):
            accounts["default"]["bot"] = copy.deepcopy(wecom.get("bot") or {})
        if isinstance(wecom.get("agent"), dict):
            accounts["default"]["agent"] = copy.deepcopy(wecom.get("agent") or {})
    wecom["accounts"] = accounts
    wecom.pop("bot", None)
    wecom.pop("agent", None)
    wecom.pop("name", None)
    return accounts


def _upsert_wecom_channel(openclaw_data: dict[str, Any], payload: dict[str, Any]) -> tuple[str, str]:
    channels_root = openclaw_data.setdefault("channels", {})
    wecom = _ensure_dict(channels_root, "wecom")
    _ensure_channel_plugin_enabled(openclaw_data, "wecom")
    accounts = _bootstrap_wecom_accounts(wecom)

    account_id = _normalize_channel_account_id(payload.get("account_id"))
    existing = copy.deepcopy(accounts.get(account_id) or {})
    existing["enabled"] = _coerce_bool(payload, "enabled", existing.get("enabled") is not False)
    _set_or_pop(existing, "name", str(payload.get("name") or existing.get("name") or "").strip())

    mode = str(payload.get("mode") or "bot").strip().lower()
    if mode == "agent":
        agent = copy.deepcopy(existing.get("agent") or {})
        agent["corpId"] = _require_value(payload.get("corpId") or agent.get("corpId"), "corpId")
        agent["corpSecret"] = _require_value(payload.get("corpSecret") or agent.get("corpSecret"), "corpSecret")
        agent["token"] = _require_value(payload.get("token") or agent.get("token"), "token")
        agent["encodingAESKey"] = _require_value(payload.get("encodingAESKey") or agent.get("encodingAESKey"), "encodingAESKey")
        normalized_agent_id = _coerce_optional_int(payload.get("agentId"))
        if normalized_agent_id is None:
            normalized_agent_id = _coerce_optional_int(agent.get("agentId"))
        _set_or_pop(agent, "agentId", normalized_agent_id)
        existing["agent"] = agent
        existing.pop("bot", None)
    else:
        bot = copy.deepcopy(existing.get("bot") or {})
        connection_mode = str(payload.get("connectionMode") or bot.get("connectionMode") or "webhook").strip().lower() or "webhook"
        bot["connectionMode"] = connection_mode
        _set_or_pop(bot, "aibotid", str(payload.get("aibotid") or bot.get("aibotid") or "").strip())
        _set_or_pop(bot, "receiveId", str(payload.get("receiveId") or bot.get("receiveId") or "").strip())
        bot_ids = _text_lines(payload.get("botIds"))
        if not bot_ids:
            bot_ids = [str(item).strip() for item in (bot.get("botIds") or []) if str(item).strip()]
        _set_or_pop(bot, "botIds", bot_ids)
        if connection_mode == "websocket":
            bot["botId"] = _require_value(payload.get("botId") or bot.get("botId"), "botId")
            bot["secret"] = _require_value(payload.get("secret") or bot.get("secret"), "secret")
            bot.pop("token", None)
            bot.pop("encodingAESKey", None)
        else:
            bot["token"] = _require_value(payload.get("token") or bot.get("token"), "token")
            bot["encodingAESKey"] = _require_value(payload.get("encodingAESKey") or bot.get("encodingAESKey"), "encodingAESKey")
            bot.pop("botId", None)
            bot.pop("secret", None)
        existing["bot"] = bot
        existing.pop("agent", None)

    accounts[account_id] = existing
    wecom["enabled"] = True
    return "wecom", account_id


def _bootstrap_feishu_accounts(feishu: dict[str, Any]) -> dict[str, Any]:
    accounts = feishu.get("accounts")
    if isinstance(accounts, dict):
        return accounts
    accounts = {}
    if str(feishu.get("appId") or "").strip():
        accounts["default"] = {
            "enabled": feishu.get("enabled"),
            "name": feishu.get("name"),
            "appId": feishu.get("appId"),
            "appSecret": copy.deepcopy(feishu.get("appSecret")),
            "encryptKey": feishu.get("encryptKey"),
            "verificationToken": copy.deepcopy(feishu.get("verificationToken")),
            "domain": feishu.get("domain"),
            "connectionMode": feishu.get("connectionMode"),
            "webhookPath": feishu.get("webhookPath"),
        }
    feishu["accounts"] = accounts
    for key in ("appId", "appSecret", "encryptKey", "verificationToken", "name"):
        feishu.pop(key, None)
    return accounts


def _upsert_feishu_channel(openclaw_data: dict[str, Any], payload: dict[str, Any]) -> tuple[str, str]:
    channels_root = openclaw_data.setdefault("channels", {})
    feishu = _ensure_dict(channels_root, "feishu")
    _ensure_channel_plugin_enabled(openclaw_data, "feishu")
    accounts = _bootstrap_feishu_accounts(feishu)

    account_id = _normalize_channel_account_id(payload.get("account_id"))
    existing = copy.deepcopy(accounts.get(account_id) or {})
    existing["enabled"] = _coerce_bool(payload, "enabled", existing.get("enabled") is not False)
    _set_or_pop(existing, "name", str(payload.get("name") or existing.get("name") or "").strip())
    existing["appId"] = _require_value(payload.get("appId") or existing.get("appId"), "appId")
    app_secret = _text_to_secret_input(payload.get("appSecret") or existing.get("appSecret"))
    if app_secret in ("", None):
        raise ConfigError("飞书 Channel 需要填写 `appSecret`。")
    existing["appSecret"] = app_secret

    connection_mode = str(payload.get("connectionMode") or existing.get("connectionMode") or feishu.get("connectionMode") or "websocket").strip().lower() or "websocket"
    existing["connectionMode"] = connection_mode
    _set_or_pop(existing, "encryptKey", str(payload.get("encryptKey") or existing.get("encryptKey") or "").strip())
    verification_token = _text_to_secret_input(payload.get("verificationToken") or existing.get("verificationToken"))
    if connection_mode == "webhook" and verification_token in ("", None):
        raise ConfigError("飞书 webhook 模式需要填写 `verificationToken`。")
    _set_or_pop(existing, "verificationToken", verification_token)
    _set_or_pop(existing, "domain", str(payload.get("domain") or existing.get("domain") or feishu.get("domain") or "feishu").strip())
    _set_or_pop(existing, "webhookPath", str(payload.get("webhookPath") or existing.get("webhookPath") or feishu.get("webhookPath") or "").strip())

    accounts[account_id] = existing
    feishu["enabled"] = True
    return "feishu", account_id


def save_channel_record(payload: dict[str, Any], state_dir: Path = DEFAULT_STATE_DIR) -> dict[str, Any]:
    platform = _normalize_channel_platform(payload.get("platform") or payload.get("channel"))
    openclaw_path = state_dir / "openclaw.json"
    openclaw_data = load_json(openclaw_path)

    if platform == "qqbot":
        saved_platform, saved_account_id = _upsert_qqbot_channel(openclaw_data, payload)
    elif platform == "ddingtalk":
        saved_platform, saved_account_id = _upsert_dingtalk_channel(openclaw_data, payload)
    elif platform == "wecom":
        saved_platform, saved_account_id = _upsert_wecom_channel(openclaw_data, payload)
    else:
        saved_platform, saved_account_id = _upsert_feishu_channel(openclaw_data, payload)

    _normalize_all_channel_plugin_configs(openclaw_data)
    write_json(openclaw_path, openclaw_data)
    store = load_panel_store(state_dir)
    channels_map = copy.deepcopy(store.get("channels") or {})
    channel_key = _channel_store_key(saved_platform, saved_account_id)
    existing_channel_meta = channels_map.get(channel_key) or {
        "platform": saved_platform,
        "account_id": saved_account_id,
        "permissions": _empty_channel_permissions(),
    }
    if "permissions" in payload:
        next_permissions = _normalize_channel_permissions_from_sources(payload.get("permissions"))
    else:
        next_permissions = _normalize_channel_permissions_from_sources(existing_channel_meta.get("permissions"))
    channels_map[channel_key] = {
        "platform": saved_platform,
        "account_id": saved_account_id,
        "permissions": next_permissions,
    }
    store["channels"] = channels_map
    _write_panel_store(store, state_dir)
    store = load_panel_store(state_dir)
    record = _find_channel_record(store.get("channelCatalog") or [], saved_platform, saved_account_id)
    return {
        "platform": saved_platform,
        "account_id": saved_account_id,
        "channel": record,
        "openclaw_path": str(openclaw_path),
    }


def delete_channel_record(payload: dict[str, Any], state_dir: Path = DEFAULT_STATE_DIR) -> dict[str, Any]:
    platform = _normalize_channel_platform(payload.get("platform") or payload.get("channel"))
    account_id = _normalize_channel_account_id(payload.get("account_id") or payload.get("accountId") or "default")

    store = load_panel_store(state_dir)
    record = _find_channel_record(store.get("channelCatalog") or [], platform, account_id)
    if not record:
        raise ConfigError(f"Channel 不存在：{platform}/{account_id}")
    if record.get("bound_agent_id"):
        raise ConfigError(f"Channel `{platform}/{account_id}` 已绑定到 Agent `{record['bound_agent_id']}`，请先解绑。")

    openclaw_path = state_dir / "openclaw.json"
    openclaw_data = load_json(openclaw_path)
    channels_root = openclaw_data.get("channels") or {}
    channel_config = channels_root.get(platform)
    if not isinstance(channel_config, dict):
        raise ConfigError(f"Channel 不存在：{platform}/{account_id}")

    if platform == "ddingtalk":
        channels_root.pop("ddingtalk", None)
    else:
        accounts = channel_config.get("accounts")
        if isinstance(accounts, dict):
            accounts.pop(account_id, None)
            if not accounts:
                channel_config["accounts"] = {}
        elif platform == "qqbot" and account_id == "default":
            for key in ("appId", "clientSecret", "clientSecretFile", "name"):
                channel_config.pop(key, None)
        elif platform == "wecom" and account_id == "default":
            channel_config.pop("bot", None)
            channel_config.pop("agent", None)
            channel_config.pop("name", None)
        elif platform == "feishu" and account_id == "default":
            for key in ("appId", "appSecret", "encryptKey", "verificationToken", "name", "connectionMode", "webhookPath"):
                channel_config.pop(key, None)
        else:
            raise ConfigError(f"Channel `{platform}` 当前不是可删除的多账号结构。")

    _normalize_all_channel_plugin_configs(openclaw_data)
    write_json(openclaw_path, openclaw_data)
    channels_map = copy.deepcopy(store.get("channels") or {})
    channels_map.pop(_channel_store_key(platform, account_id), None)
    store["channels"] = channels_map
    _write_panel_store(store, state_dir)
    return {
        "platform": platform,
        "account_id": account_id,
        "openclaw_path": str(openclaw_path),
    }


def load_current_config(state_dir: Path = DEFAULT_STATE_DIR) -> dict[str, Any]:
    openclaw_path = state_dir / "openclaw.json"
    data = load_json(openclaw_path)

    primary_model = str((((data.get("agents") or {}).get("defaults") or {}).get("model") or {}).get("primary") or "")
    provider = primary_model.split("/", 1)[0] if "/" in primary_model else ""
    default_model_id = primary_model.split("/", 1)[1] if "/" in primary_model else ""

    providers = ((data.get("models") or {}).get("providers") or {})
    provider_key = _provider_key(providers, provider) if provider else None
    provider_config = providers.get(provider_key or "", {}) if provider_key else {}
    if not provider_config and provider.lower() == OPENAI_CODEX_PROVIDER:
        provider_config = _openai_codex_provider_config()
    provider_api = str(provider_config.get("api") or "").strip()

    models = []
    for item in provider_config.get("models") or []:
        model_id = str((item or {}).get("id") or "").strip()
        if not model_id:
            continue
        models.append({
            "id": model_id,
            "name": str((item or {}).get("name") or "").strip() or model_id,
            "api": str((item or {}).get("api") or provider_api or "").strip(),
        })

    default_model_name = default_model_id
    for item in models:
        if item["id"] == default_model_id:
            default_model_name = item["name"]
            break

    api_keys: list[str] = []
    order: list[str] = []
    auth_store = _main_auth_store(state_dir)
    if auth_store:
        auth_data = load_json(auth_store)
        profiles = auth_data.get("profiles") or {}
        order_map = auth_data.get("order") or {}
        provider_order = (
            order_map.get(provider)
            or order_map.get(provider.lower())
            or []
        )
        remaining = _profile_ids_for_provider(profiles, provider)
        ordered_profile_ids: list[str] = []
        for profile_id in provider_order:
            if profile_id in profiles and profile_id not in ordered_profile_ids:
                ordered_profile_ids.append(profile_id)
        for profile_id in remaining:
            if profile_id not in ordered_profile_ids:
                ordered_profile_ids.append(profile_id)
        order = ordered_profile_ids
        for profile_id in ordered_profile_ids:
            profile = profiles.get(profile_id) or {}
            key = str(profile.get("key") or "").strip()
            if key:
                api_keys.append(key)

    return {
        "provider": provider,
        "base_url": str(provider_config.get("baseUrl") or ""),
        "default_api": provider_api,
        "api": provider_api,
        "models": models,
        "default_model_id": default_model_id,
        "default_model_name": default_model_name,
        "api_keys": api_keys,
        "profile_ids": order,
        "agents": [path.parent.name for path in _agent_dirs(state_dir)],
        "paths": {
            "openclaw_json": str(openclaw_path),
            "auth_store": str(auth_store) if auth_store else "",
        },
    }


def _run_openclaw(*args: str, state_dir: Path = DEFAULT_STATE_DIR) -> subprocess.CompletedProcess[str]:
    env = build_openclaw_env(state_dir)
    openclaw_bin = find_openclaw_bin(state_dir)
    return subprocess.run(
        [openclaw_bin, *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def get_openclaw_status(state_dir: Path = DEFAULT_STATE_DIR) -> dict[str, Any]:
    try:
        proc = _run_openclaw("models", "status", "--json", state_dir=state_dir)
        return json.loads(proc.stdout)
    except subprocess.CalledProcessError as exc:
        raise ConfigError(exc.stderr.strip() or exc.stdout.strip() or "无法读取 OpenClaw 状态。") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError("OpenClaw 状态输出不是合法 JSON。") from exc


def _run_systemctl_user(
    args: list[str],
    *,
    env: dict[str, str],
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        timeout=timeout,
    )


def _detect_gateway_systemd_unit(env: dict[str, str]) -> str:
    candidates: list[str] = []
    configured = str(env.get("OPENCLAW_SYSTEMD_UNIT") or "").strip()
    if configured:
        candidates.append(configured)
    candidates.append("openclaw-gateway.service")
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            proc = _run_systemctl_user(["show", candidate, "--property=LoadState", "--value"], env=env, timeout=10)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
        if str(proc.stdout or "").strip() and str(proc.stdout or "").strip() != "not-found":
            return candidate
    return ""


def _gateway_systemd_main_pid(unit: str, env: dict[str, str]) -> int:
    if not unit:
        return 0
    try:
        proc = _run_systemctl_user(["show", unit, "--property=MainPID", "--value"], env=env, timeout=10)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return 0
    raw = str(proc.stdout or "").strip()
    return int(raw) if raw.isdigit() else 0


def _wait_for_gateway_systemd_restart(unit: str, env: dict[str, str], previous_pid: int, timeout: int = 60) -> int:
    deadline = time.time() + max(5, timeout)
    last_state = "unknown"
    last_pid = 0
    while time.time() < deadline:
        try:
            proc = _run_systemctl_user(
                ["show", unit, "--property=ActiveState,SubState,MainPID", "--value"],
                env=env,
                timeout=10,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            time.sleep(1)
            continue
        lines = [str(item or "").strip() for item in str(proc.stdout or "").splitlines()]
        active_state = lines[0] if len(lines) > 0 else ""
        sub_state = lines[1] if len(lines) > 1 else ""
        raw_pid = lines[2] if len(lines) > 2 else ""
        last_state = f"{active_state}/{sub_state}".strip("/")
        last_pid = int(raw_pid) if raw_pid.isdigit() else 0
        if active_state == "active" and sub_state == "running" and last_pid > 0:
            if previous_pid <= 0 or last_pid != previous_pid:
                return last_pid
        time.sleep(1)
    raise ConfigError(f"systemd 服务 `{unit}` 重启后未进入运行态（最后状态：{last_state or 'unknown'}，PID={last_pid or 0}）。")


def restart_openclaw(state_dir: Path = DEFAULT_STATE_DIR) -> dict[str, Any]:
    """Restart the OpenClaw gateway process via the detected service wrapper."""
    current = load_current_config(state_dir)
    provider = str(current.get("provider") or "").strip()
    env = build_openclaw_env(state_dir)
    systemd_unit = _detect_gateway_systemd_unit(env)
    if systemd_unit:
        previous_pid = _gateway_systemd_main_pid(systemd_unit, env)
        try:
            _run_systemctl_user(["restart", systemd_unit], env=env, timeout=60)
            current_pid = _wait_for_gateway_systemd_restart(systemd_unit, env, previous_pid, timeout=60)
            restart_stdout = f"Restarted systemd service: {systemd_unit}"
            if previous_pid > 0:
                restart_stdout += f" ({previous_pid} -> {current_pid})"
            else:
                restart_stdout += f" (pid {current_pid})"
        except subprocess.TimeoutExpired as exc:
            raise ConfigError("网关重启超时（systemd）。") from exc
        except subprocess.CalledProcessError as exc:
            raise ConfigError(exc.stderr.strip() or exc.stdout.strip() or "网关重启失败。") from exc
    else:
        openclaw_bin = find_openclaw_bin(state_dir)
        try:
            proc = subprocess.run(
                [openclaw_bin, "gateway", "restart"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                timeout=60,
            )
            restart_stdout = (proc.stdout or proc.stderr).strip()
        except subprocess.TimeoutExpired as exc:
            raise ConfigError("网关重启超时。") from exc
        except subprocess.CalledProcessError as exc:
            raise ConfigError(exc.stderr.strip() or exc.stdout.strip() or "网关重启失败。") from exc

    status = get_openclaw_status(state_dir)
    summary = _status_summary(status, provider)
    return {
        "restart_output": restart_stdout,
        "status": summary,
        "provider": provider,
    }


def _enabled_provider_ids(store: dict[str, Any]) -> list[str]:
    provider_order = _provider_display_order(store)
    providers_map = store.get("providers") or {}
    return [provider_id for provider_id in provider_order if (providers_map.get(provider_id) or {}).get("enabled") is not False]


def _runtime_group_slug(api_type: str, runtime_base_url: str) -> str:
    parsed = urlparse(str(runtime_base_url or "").strip())
    api_slug = re.sub(r"[^a-z0-9]+", "-", str(api_type or "").strip().lower()).strip("-")
    path_slug = re.sub(r"[^a-z0-9]+", "-", str(parsed.path or "").strip("/").lower()).strip("-")
    if api_slug and path_slug:
        return f"{api_slug}-{path_slug}"
    return api_slug or path_slug or "runtime"


def _runtime_group_provider_id(panel_provider_id: str, api_type: str, runtime_base_url: str, existing_ids: set[str]) -> str:
    candidate = f"{panel_provider_id}--{_runtime_group_slug(api_type, runtime_base_url)}"
    return _dedupe_provider_name(candidate, existing_ids)


def _panel_provider_default_model_ref(panel_runtime_meta: dict[str, Any], provider_id: str) -> str:
    return str(((panel_runtime_meta.get(provider_id) or {}).get("default_model_ref")) or "").strip()


def _panel_runtime_provider_ids(panel_runtime_meta: dict[str, Any], provider_id: str) -> list[str]:
    return [
        str(item or "").strip()
        for item in (((panel_runtime_meta.get(provider_id) or {}).get("runtime_provider_ids")) or [])
        if str(item or "").strip()
    ]


def _build_runtime_provider_configs(
    store: dict[str, Any],
    enabled_provider_ids: list[str],
) -> tuple[dict[str, Any], dict[str, list[str]], dict[str, Any], dict[str, str]]:
    providers_map = store.get("providers") or {}
    catalog_map = {
        str(item.get("id") or "").strip(): item
        for item in store.get("modelCatalog") or []
        if isinstance(item, dict)
    }
    runtime_providers: dict[str, Any] = {}
    profile_ids_by_runtime_provider: dict[str, list[str]] = {}
    panel_runtime_meta: dict[str, Any] = {}
    runtime_provider_owner_map: dict[str, str] = {}
    existing_runtime_ids: set[str] = set(BUILTIN_PROVIDERS)

    for provider_id in enabled_provider_ids:
        record = _sanitize_provider_record(provider_id, providers_map.get(provider_id) or {})
        api_keys = [str(item or "").strip() for item in (record.get("api_keys") or []) if str(item or "").strip()]
        if not api_keys:
            raise ConfigError(f"Provider `{provider_id}` 没有可用 API key。")
        default_model_id = str(record.get("default_model_id") or "").strip()
        if not default_model_id:
            raise ConfigError(f"Provider `{provider_id}` 缺少默认模型。")
        default_api = str(record.get("default_api") or record.get("api") or "").strip()
        provider_models = _enabled_provider_models(record)
        if not provider_models:
            raise ConfigError(f"Provider `{provider_id}` 没有可用模型。")
        if default_model_id not in {item["id"] for item in provider_models}:
            raise ConfigError(f"Provider `{provider_id}` 的默认模型 `{default_model_id}` 不在可用模型列表中。")

        group_order: list[tuple[str, str]] = []
        grouped_models: dict[tuple[str, str], list[dict[str, Any]]] = {}
        model_to_group: dict[str, tuple[str, str]] = {}
        for item in provider_models:
            model_id = item["id"]
            resolved_api = _resolved_model_api(item, default_api)
            if not resolved_api:
                raise ConfigError(f"Provider `{provider_id}` 的模型 `{model_id}` 缺少 API 类型。")
            route_suffix = _resolved_model_route(item, resolved_api)
            if not route_suffix:
                raise ConfigError(f"Provider `{provider_id}` 的模型 `{model_id}` 缺少路由后缀。")
            if not _route_suffix_runtime_supported(resolved_api, route_suffix):
                raise ConfigError(
                    f"Provider `{provider_id}` 的模型 `{model_id}` 路由当前无法应用到 OpenClaw：{route_suffix}"
                )
            runtime_base_url = _runtime_base_url_for_route(_provider_site(record), resolved_api, route_suffix)
            group_key = (resolved_api, runtime_base_url)
            if group_key not in grouped_models:
                grouped_models[group_key] = []
                group_order.append(group_key)
            grouped_models[group_key].append({
                "id": model_id,
                "name": str((catalog_map.get(model_id) or {}).get("name") or item.get("name") or model_id).strip() or model_id,
                "api": resolved_api,
            })
            model_to_group[model_id] = group_key

        default_group_key = model_to_group.get(default_model_id)
        if default_group_key is None:
            raise ConfigError(f"Provider `{provider_id}` 的默认模型 `{default_model_id}` 没有可用运行时分组。")

        ordered_group_keys = [default_group_key] + [item for item in group_order if item != default_group_key]
        runtime_provider_ids: list[str] = []
        runtime_provider_by_model_id: dict[str, str] = {}
        for group_key in ordered_group_keys:
            resolved_api, runtime_base_url = group_key
            if group_key == default_group_key:
                runtime_provider_id = provider_id
            else:
                runtime_provider_id = _runtime_group_provider_id(provider_id, resolved_api, runtime_base_url, existing_runtime_ids)
            existing_runtime_ids.add(runtime_provider_id)
            runtime_provider_owner_map[runtime_provider_id] = provider_id
            runtime_provider_ids.append(runtime_provider_id)

            provider_runtime_config = {
                "baseUrl": runtime_base_url,
                "models": _merge_models(grouped_models[group_key], []),
            }
            if resolved_api:
                provider_runtime_config["api"] = resolved_api
            runtime_providers[runtime_provider_id] = provider_runtime_config
            profile_ids_by_runtime_provider[runtime_provider_id] = [
                f"{runtime_provider_id}:acct{index}" for index in range(1, len(api_keys) + 1)
            ]
            for model_entry in grouped_models[group_key]:
                runtime_provider_by_model_id[model_entry["id"]] = runtime_provider_id

        panel_runtime_meta[provider_id] = {
            "panel_provider": provider_id,
            "default_model_ref": f"{runtime_provider_by_model_id[default_model_id]}/{default_model_id}",
            "default_runtime_provider": runtime_provider_by_model_id[default_model_id],
            "runtime_provider_ids": runtime_provider_ids,
            "runtime_provider_by_model_id": runtime_provider_by_model_id,
        }

    return runtime_providers, profile_ids_by_runtime_provider, panel_runtime_meta, runtime_provider_owner_map


def _effective_agent_provider_order(record: dict[str, Any], enabled_provider_ids: list[str]) -> list[str]:
    configured_provider_order = [str(item or "").strip() for item in (record.get("provider_order") or []) if str(item or "").strip()]
    effective_provider_order: list[str] = []
    for provider_id in configured_provider_order:
        if provider_id in enabled_provider_ids and provider_id not in effective_provider_order:
            effective_provider_order.append(provider_id)
    return effective_provider_order


def _build_agent_entry(
    agent_id: str,
    record: dict[str, Any],
    state_dir: Path,
    panel_runtime_meta: dict[str, Any],
    enabled_provider_ids: list[str],
    channels_map: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    effective_provider_order = _effective_agent_provider_order(record, enabled_provider_ids)
    if not effective_provider_order:
        raise ConfigError(f"Agent `{agent_id}` 没有可用 Provider 顺序。")

    primary_provider_id = effective_provider_order[0]
    fallback_provider_ids = effective_provider_order[1:]
    primary_model_ref = _panel_provider_default_model_ref(panel_runtime_meta, primary_provider_id)
    if not primary_model_ref:
        raise ConfigError(f"Agent `{agent_id}` 的主 Provider `{primary_provider_id}` 没有默认模型引用。")
    fallback_model_refs: list[str] = []
    for provider_id in fallback_provider_ids:
        model_ref = _panel_provider_default_model_ref(panel_runtime_meta, provider_id)
        if model_ref and model_ref not in fallback_model_refs:
            fallback_model_refs.append(model_ref)
    agent_entry: dict[str, Any] = {
        "id": agent_id,
        "name": agent_id,
        "workspace": str(record.get("workspace") or _default_agent_workspace(state_dir, agent_id)).strip(),
        "agentDir": str(record.get("agent_dir") or _default_agent_dir(state_dir, agent_id)).strip(),
        "identity": {
            "name": str(record.get("name") or agent_id).strip() or agent_id,
        },
        "model": {
            "primary": primary_model_ref,
            "fallbacks": fallback_model_refs,
        },
    }
    permissions = (record.get("permissions") or {}) if isinstance(record.get("permissions"), dict) else {}
    exec_config = permissions.get("exec") if isinstance(permissions, dict) else {}
    exec_security = _normalize_exec_security((exec_config or {}).get("security"))
    exec_ask = _normalize_exec_ask((exec_config or {}).get("ask"))
    elevated = permissions.get("elevated") if isinstance(permissions, dict) else {}
    elevated_mode = _normalize_elevated_mode((elevated or {}).get("mode"))
    elevated_allow_from = _effective_agent_allow_from(record, channels_map)
    tools_root: dict[str, Any] = {}
    if elevated_mode or elevated_allow_from:
        elevated_root: dict[str, Any] = {}
        if elevated_mode == "on":
            elevated_root["enabled"] = True
            if elevated_allow_from:
                elevated_root["allowFrom"] = copy.deepcopy(elevated_allow_from)
        elif elevated_mode == "off":
            elevated_root["enabled"] = False
        if elevated_root:
            tools_root["elevated"] = elevated_root
    if exec_security or exec_ask:
        exec_root: dict[str, Any] = {}
        if exec_security:
            exec_root["security"] = exec_security
        if exec_ask:
            exec_root["ask"] = exec_ask
        if exec_root:
            tools_root["exec"] = exec_root
    if tools_root:
        agent_entry["tools"] = tools_root
    if agent_id == "main":
        agent_entry.pop("name", None)
        if agent_entry["workspace"] == _default_agent_workspace(state_dir, "main"):
            agent_entry.pop("workspace", None)
        if agent_entry["agentDir"] == _default_agent_dir(state_dir, "main"):
            agent_entry.pop("agentDir", None)
    return agent_entry, effective_provider_order


def _write_agent_runtime_files(
    agent_id: str,
    agent_record: dict[str, Any],
    state_dir: Path,
    enabled_provider_ids: list[str],
    providers_map: dict[str, Any],
    runtime_providers: dict[str, Any],
    profile_ids_by_runtime_provider: dict[str, list[str]],
    panel_runtime_meta: dict[str, Any],
    runtime_provider_owner_map: dict[str, str],
) -> list[str]:
    updated_files: list[str] = []
    agent_dir = Path(str(agent_record.get("agent_dir") or _default_agent_dir(state_dir, agent_id))).expanduser()
    agent_dir.mkdir(parents=True, exist_ok=True)

    models_path = agent_dir / "models.json"
    agent_panel_provider_ids = _effective_agent_provider_order(agent_record, enabled_provider_ids)
    agent_runtime_provider_ids: list[str] = []
    for provider_id in agent_panel_provider_ids:
        for runtime_provider_id in _panel_runtime_provider_ids(panel_runtime_meta, provider_id):
            if runtime_provider_id in runtime_providers and runtime_provider_id not in agent_runtime_provider_ids:
                agent_runtime_provider_ids.append(runtime_provider_id)
    if models_path.exists():
        models_data = load_json(models_path)
        agent_providers = {key: value for key, value in (models_data.get("providers") or {}).items() if key in BUILTIN_PROVIDERS}
        for runtime_provider_id in agent_runtime_provider_ids:
            agent_providers[runtime_provider_id] = copy.deepcopy(runtime_providers[runtime_provider_id])
        models_data["providers"] = agent_providers
    else:
        models_data = {
            "providers": {provider_id: copy.deepcopy(runtime_providers[provider_id]) for provider_id in BUILTIN_PROVIDERS if provider_id in runtime_providers},
        }
        for runtime_provider_id in agent_runtime_provider_ids:
            models_data["providers"][runtime_provider_id] = copy.deepcopy(runtime_providers[runtime_provider_id])
    write_json(models_path, models_data)
    updated_files.append(str(models_path))

    auth_profiles_path = agent_dir / "auth-profiles.json"
    if auth_profiles_path.exists():
        auth_data = load_json(auth_profiles_path)
    else:
        auth_data = {"version": 1, "profiles": {}, "order": {}, "lastGood": {}, "usageStats": {}}
    preserved_auth = _preserve_unmanaged_auth_data(auth_data, set(agent_runtime_provider_ids), include_runtime=True)
    auth_data["version"] = 1
    auth_data["profiles"] = copy.deepcopy(preserved_auth["profiles"])
    auth_data["order"] = copy.deepcopy(preserved_auth["order"])
    auth_data["lastGood"] = copy.deepcopy(preserved_auth["lastGood"])
    auth_data["usageStats"] = copy.deepcopy(preserved_auth["usageStats"])
    for runtime_provider_id in agent_runtime_provider_ids:
        owner_provider_id = str(runtime_provider_owner_map.get(runtime_provider_id) or "").strip()
        if not owner_provider_id:
            continue
        record = providers_map.get(owner_provider_id) or {}
        api_keys = [str(item or "").strip() for item in (record.get("api_keys") or []) if str(item or "").strip()]
        profile_ids = profile_ids_by_runtime_provider[runtime_provider_id]
        for profile_id, api_key in zip(profile_ids, api_keys, strict=True):
            auth_data["profiles"][profile_id] = {"type": "api_key", "provider": runtime_provider_id, "key": api_key}
        _set_provider_order(auth_data["order"], runtime_provider_id, profile_ids)
    write_json(auth_profiles_path, auth_data)
    updated_files.append(str(auth_profiles_path))
    return updated_files


def _sync_memory_scope_config(openclaw_data: dict[str, Any], agent_ids: list[str]) -> None:
    plugin_config = (((openclaw_data.get("plugins") or {}).get("entries") or {}).get("memory-lancedb-pro") or {}).get("config") or {}
    scopes = plugin_config.get("scopes") or {}
    definitions = scopes.get("definitions") or {}
    agent_access = scopes.get("agentAccess") or {}
    keep_definitions = {key: value for key, value in definitions.items() if not str(key).startswith("agent:")}
    for agent_id in agent_ids:
        keep_definitions[f"agent:{agent_id}"] = {"description": f"{agent_id} 私有"}
    scopes["definitions"] = keep_definitions
    scopes["agentAccess"] = {agent_id: ["global", f"agent:{agent_id}"] for agent_id in agent_ids}
    plugin_config["scopes"] = scopes


def apply_store_config(state_dir: Path = DEFAULT_STATE_DIR, restart_gateway: bool = True) -> dict[str, Any]:
    """Materialize the whole panel store into OpenClaw runtime config."""
    store = load_panel_store(state_dir)
    providers_map = store.get("providers") or {}
    runtime_auth = _normalize_runtime_auth_config(store.get("runtimeAuth") or {})
    agent_order = [item for item in store.get("agentOrder") or [] if item in (store.get("agents") or {})]
    agents_map = store.get("agents") or {}
    enabled_provider_ids = _enabled_provider_ids(store)
    if not enabled_provider_ids:
        raise ConfigError("没有启用的 Provider 可用于应用配置。")
    if not agent_order:
        raise ConfigError("Agent 列表为空。")

    openclaw_path = state_dir / "openclaw.json"
    openclaw_data = load_json(openclaw_path)
    (
        runtime_providers,
        profile_ids_by_runtime_provider,
        panel_runtime_meta,
        runtime_provider_owner_map,
    ) = _build_runtime_provider_configs(store, enabled_provider_ids)

    models_root = openclaw_data.setdefault("models", {})
    models_root["mode"] = models_root.get("mode") or "merge"
    providers_root = models_root.setdefault("providers", {})
    providers_root = {key: value for key, value in providers_root.items() if key in BUILTIN_PROVIDERS}
    models_root["providers"] = providers_root

    auth_root = openclaw_data.setdefault("auth", {})
    preserved_auth_root = _preserve_unmanaged_auth_data(auth_root, set(runtime_providers.keys()), include_runtime=False)
    auth_root["profiles"] = copy.deepcopy(preserved_auth_root["profiles"])
    auth_root["order"] = copy.deepcopy(preserved_auth_root["order"])
    auth_root.pop("usageStats", None)
    auth_root.pop("lastGood", None)

    for provider_id in enabled_provider_ids:
        for runtime_provider_id in _panel_runtime_provider_ids(panel_runtime_meta, provider_id):
            providers_root[runtime_provider_id] = copy.deepcopy(runtime_providers[runtime_provider_id])
            for profile_id in profile_ids_by_runtime_provider[runtime_provider_id]:
                auth_root["profiles"][profile_id] = {"provider": runtime_provider_id, "mode": "api_key"}
            _set_provider_order(auth_root["order"], runtime_provider_id, profile_ids_by_runtime_provider[runtime_provider_id])

    agents_root = openclaw_data.setdefault("agents", {})
    defaults_root = agents_root.setdefault("defaults", {})
    model_root = defaults_root.setdefault("model", {})
    defaults_root["models"] = _build_default_models_catalog(models_root["providers"])
    _sync_memory_scope_config(openclaw_data, agent_order)
    _apply_elevated_runtime_config(openclaw_data, agents_map, store.get("channels") or {})

    next_agents_list: list[dict[str, Any]] = []
    main_agent_provider_order: list[str] = []
    for agent_id in agent_order:
        record = agents_map.get(agent_id) or {}
        agent_entry, effective_provider_order = _build_agent_entry(
            agent_id,
            record,
            state_dir,
            panel_runtime_meta,
            enabled_provider_ids,
            store.get("channels") or {},
        )
        if agent_id == "main":
            main_agent_provider_order = copy.deepcopy(effective_provider_order)
        next_agents_list.append(agent_entry)
    agents_root["list"] = next_agents_list

    if not main_agent_provider_order:
        main_agent_provider_order = copy.deepcopy(enabled_provider_ids)
    main_primary_provider = main_agent_provider_order[0]
    primary_model_ref = _panel_provider_default_model_ref(panel_runtime_meta, main_primary_provider)
    if not primary_model_ref:
        raise ConfigError(f"主 Provider `{main_primary_provider}` 没有默认模型引用。")
    global_fallbacks = []
    for provider_id in main_agent_provider_order[1:]:
        model_ref = _panel_provider_default_model_ref(panel_runtime_meta, provider_id)
        if model_ref and model_ref not in global_fallbacks:
            global_fallbacks.append(model_ref)
    if runtime_auth["mode"] == "oauth":
        oauth_status = get_runtime_auth_status(state_dir)
        if not bool(((oauth_status.get("oauth") or {}).get("authenticated"))):
            raise ConfigError("尚未完成 OpenAI OAuth 登录，不能应用 OAuth 模式。")
        primary_model_ref = _runtime_auth_model_ref(runtime_auth)
        global_fallbacks = []
        for item in next_agents_list:
            if str(item.get("id") or "") == "main":
                item_model = item.setdefault("model", {})
                item_model["primary"] = primary_model_ref
                item_model["fallbacks"] = []
                break
    model_root["primary"] = primary_model_ref
    model_root["fallbacks"] = global_fallbacks

    preserved_bindings = [binding for binding in (openclaw_data.get("bindings") or []) if not isinstance(binding, dict) or binding.get("type") != "route"]
    next_bindings = list(preserved_bindings)
    for agent_id in agent_order:
        record = agents_map.get(agent_id) or {}
        for binding in record.get("bindings") or []:
            channel = str((binding or {}).get("channel") or "").strip()
            account_id = str((binding or {}).get("account_id") or "").strip()
            if not channel or not account_id:
                continue
            next_bindings.append({
                "type": "route",
                "agentId": agent_id,
                "match": {
                    "channel": channel,
                    "accountId": account_id,
                },
            })
    openclaw_data["bindings"] = next_bindings

    _normalize_all_channel_plugin_configs(openclaw_data)
    write_json(openclaw_path, openclaw_data)
    updated_files = [str(openclaw_path)]
    updated_files.append(_write_exec_approvals_config(state_dir, agents_map))

    for agent_id in agent_order:
        updated_files.extend(
            _write_agent_runtime_files(
                agent_id,
                agents_map.get(agent_id) or {},
                state_dir,
                enabled_provider_ids,
                providers_map,
                runtime_providers,
                profile_ids_by_runtime_provider,
                panel_runtime_meta,
                runtime_provider_owner_map,
            )
        )

    restart_stdout = ""
    summary = {
        "default_model": primary_model_ref,
        "resolved_default": None,
        "provider_auth": None,
        "image_model": None,
        "restart_required": not restart_gateway,
    }
    if restart_gateway:
        restart_result = restart_openclaw(state_dir)
        restart_stdout = restart_result["restart_output"]
        summary = restart_result["status"]
        summary["restart_required"] = False
        if summary["default_model"] != primary_model_ref:
            raise ConfigError(f"校验失败：默认模型不是 `{primary_model_ref}`，当前是 `{summary['default_model']}`。")

    return {
        "provider": OPENAI_CODEX_PROVIDER if runtime_auth["mode"] == "oauth" else main_primary_provider,
        "fallbacks": global_fallbacks,
        "runtimeAuth": runtime_auth,
        "agents": [
            {
                "id": agent_id,
                "provider_order": [provider_id for provider_id in (agents_map.get(agent_id) or {}).get("provider_order") or [] if provider_id in enabled_provider_ids],
                "bindings": (agents_map.get(agent_id) or {}).get("bindings") or [],
            }
            for agent_id in agent_order
        ],
        "updated_files": updated_files,
        "restart_output": restart_stdout,
        "status": summary,
    }


def apply_agent_config(agent_id: str, state_dir: Path = DEFAULT_STATE_DIR, restart_gateway: bool = True) -> dict[str, Any]:
    """Materialize a single agent using its current provider/channel bindings."""
    target_agent_id = _normalize_agent_id(agent_id)
    store = load_panel_store(state_dir)
    providers_map = store.get("providers") or {}
    agents_map = store.get("agents") or {}
    runtime_auth = _normalize_runtime_auth_config(store.get("runtimeAuth") or {})
    if target_agent_id not in agents_map:
        raise ConfigError(f"Agent 不存在：{target_agent_id}")

    enabled_provider_ids = _enabled_provider_ids(store)
    if not enabled_provider_ids:
        raise ConfigError("没有启用的 Provider 可用于应用配置。")

    openclaw_path = state_dir / "openclaw.json"
    openclaw_data = load_json(openclaw_path)
    (
        runtime_providers,
        profile_ids_by_runtime_provider,
        panel_runtime_meta,
        runtime_provider_owner_map,
    ) = _build_runtime_provider_configs(store, enabled_provider_ids)

    target_record = agents_map.get(target_agent_id) or {}
    target_entry, effective_provider_order = _build_agent_entry(
        target_agent_id,
        target_record,
        state_dir,
        panel_runtime_meta,
        enabled_provider_ids,
        store.get("channels") or {},
    )
    target_runtime_provider_ids: list[str] = []
    for provider_id in effective_provider_order:
        for runtime_provider_id in _panel_runtime_provider_ids(panel_runtime_meta, provider_id):
            if runtime_provider_id in runtime_providers and runtime_provider_id not in target_runtime_provider_ids:
                target_runtime_provider_ids.append(runtime_provider_id)

    models_root = openclaw_data.setdefault("models", {})
    models_root["mode"] = models_root.get("mode") or "merge"
    agents_root = openclaw_data.setdefault("agents", {})
    defaults_root = agents_root.setdefault("defaults", {})
    if target_agent_id == "main":
        providers_root = models_root.setdefault("providers", {})
        providers_root = {key: value for key, value in providers_root.items() if key in BUILTIN_PROVIDERS}
        for runtime_provider_id in target_runtime_provider_ids:
            providers_root[runtime_provider_id] = copy.deepcopy(runtime_providers[runtime_provider_id])
        models_root["providers"] = providers_root
        defaults_root["models"] = _build_default_models_catalog(models_root["providers"])
    _sync_memory_scope_config(openclaw_data, [item for item in store.get("agentOrder") or [] if item in agents_map])
    _apply_elevated_runtime_config(openclaw_data, agents_map, store.get("channels") or {})

    existing_agents_list = [item for item in (agents_root.get("list") or []) if isinstance(item, dict)]
    next_agents_list: list[dict[str, Any]] = []
    replaced = False
    for item in existing_agents_list:
        existing_id = _normalize_agent_id(str(item.get("id") or ""))
        if existing_id == target_agent_id:
            next_agents_list.append(target_entry)
            replaced = True
        else:
            next_agents_list.append(item)
    if not replaced:
        next_agents_list.append(target_entry)
    agents_root["list"] = next_agents_list

    if target_agent_id == "main":
        auth_root = openclaw_data.setdefault("auth", {})
        preserved_auth_root = _preserve_unmanaged_auth_data(auth_root, set(target_runtime_provider_ids), include_runtime=False)
        auth_root["profiles"] = copy.deepcopy(preserved_auth_root["profiles"])
        auth_root["order"] = copy.deepcopy(preserved_auth_root["order"])
        auth_root.pop("usageStats", None)
        auth_root.pop("lastGood", None)
        for runtime_provider_id in target_runtime_provider_ids:
            for profile_id in profile_ids_by_runtime_provider[runtime_provider_id]:
                auth_root["profiles"][profile_id] = {"provider": runtime_provider_id, "mode": "api_key"}
            _set_provider_order(auth_root["order"], runtime_provider_id, profile_ids_by_runtime_provider[runtime_provider_id])

        model_root = defaults_root.setdefault("model", {})
        primary_model_ref = _panel_provider_default_model_ref(panel_runtime_meta, effective_provider_order[0])
        if not primary_model_ref:
            raise ConfigError(f"Agent `{target_agent_id}` 的主 Provider `{effective_provider_order[0]}` 没有默认模型引用。")
        fallback_refs = []
        for provider_id in effective_provider_order[1:]:
            model_ref = _panel_provider_default_model_ref(panel_runtime_meta, provider_id)
            if model_ref and model_ref not in fallback_refs:
                fallback_refs.append(model_ref)
        if runtime_auth["mode"] == "oauth":
            oauth_status = get_runtime_auth_status(state_dir)
            if not bool(((oauth_status.get("oauth") or {}).get("authenticated"))):
                raise ConfigError("尚未完成 OpenAI OAuth 登录，不能应用 OAuth 模式。")
            primary_model_ref = _runtime_auth_model_ref(runtime_auth)
            fallback_refs = []
            target_entry.setdefault("model", {})
            target_entry["model"]["primary"] = primary_model_ref
            target_entry["model"]["fallbacks"] = []
        model_root["primary"] = primary_model_ref
        model_root["fallbacks"] = fallback_refs

    preserved_bindings = []
    for binding in openclaw_data.get("bindings") or []:
        if not isinstance(binding, dict) or binding.get("type") != "route":
            preserved_bindings.append(binding)
            continue
        existing_agent_id = str(binding.get("agentId") or "").strip()
        if existing_agent_id != target_agent_id:
            preserved_bindings.append(binding)
    for binding in target_record.get("bindings") or []:
        channel = str((binding or {}).get("channel") or "").strip()
        account_id = str((binding or {}).get("account_id") or "").strip()
        if not channel or not account_id:
            continue
        preserved_bindings.append({
            "type": "route",
            "agentId": target_agent_id,
            "match": {
                "channel": channel,
                "accountId": account_id,
            },
        })
    openclaw_data["bindings"] = preserved_bindings

    _normalize_all_channel_plugin_configs(openclaw_data)
    write_json(openclaw_path, openclaw_data)
    updated_files = [str(openclaw_path)]
    updated_files.append(_write_exec_approvals_config(state_dir, agents_map))
    updated_files.extend(
        _write_agent_runtime_files(
            target_agent_id,
            target_record,
            state_dir,
            enabled_provider_ids,
            providers_map,
            runtime_providers,
            profile_ids_by_runtime_provider,
            panel_runtime_meta,
            runtime_provider_owner_map,
        )
    )

    restart_stdout = ""
    current = load_current_config(state_dir)
    summary = {
        "default_model": current.get("provider") and current.get("default_model_id") and f"{current['provider']}/{current['default_model_id']}" or None,
        "resolved_default": None,
        "provider_auth": None,
        "image_model": None,
        "restart_required": not restart_gateway,
    }
    if restart_gateway:
        restart_result = restart_openclaw(state_dir)
        restart_stdout = restart_result["restart_output"]
        summary = restart_result["status"]
        summary["restart_required"] = False

    return {
        "agent": target_agent_id,
        "provider_order": effective_provider_order,
        "runtimeAuth": runtime_auth,
        "bindings": target_record.get("bindings") or [],
        "updated_files": updated_files,
        "restart_output": restart_stdout,
        "status": summary,
    }


def apply_config(payload: dict[str, Any], state_dir: Path = DEFAULT_STATE_DIR) -> dict[str, Any]:
    current = load_current_config(state_dir)

    provider = str(payload.get("provider") or current.get("provider") or "").strip()
    if not provider:
        raise ConfigError("`provider` 不能为空。")

    base_url = normalize_base_url(str(payload.get("base_url") or current.get("base_url") or ""))
    default_api = str(payload.get("default_api") or payload.get("api") or current.get("default_api") or current.get("api") or "").strip()

    api_keys = _coerce_api_keys(payload)
    if not api_keys:
        api_keys = [key for key in current.get("api_keys") or [] if str(key or "").strip()]
    if not api_keys:
        raise ConfigError("至少需要 1 个 API key。")

    keep_other_providers = bool(payload.get("keep_other_providers", True))
    restart_gateway = bool(payload.get("restart_gateway", True))

    openclaw_path = state_dir / "openclaw.json"
    openclaw_data = load_json(openclaw_path)
    global_providers = ((openclaw_data.get("models") or {}).get("providers") or {})
    existing_provider_key = _provider_key(global_providers, provider)
    existing_global_provider = copy.deepcopy(global_providers.get(existing_provider_key or "", {}))
    existing_global_models = existing_global_provider.get("models") or []

    requested_models = _coerce_apply_provider_models(payload, existing_global_models)
    if not requested_models:
        raise ConfigError("至少需要 1 个模型。")

    default_model_id = str(
        payload.get("default_model_id")
        or ((payload.get("model") or {}).get("id") if isinstance(payload.get("model"), dict) else "")
        or current.get("default_model_id")
        or requested_models[0]["id"]
    ).strip()
    if not default_model_id:
        raise ConfigError("`default_model_id` 不能为空。")

    default_model_name = str(
        payload.get("default_model_name")
        or ((payload.get("model") or {}).get("name") if isinstance(payload.get("model"), dict) else "")
        or default_model_id
    ).strip()

    if not any(item["id"] == default_model_id for item in requested_models):
        requested_models.append({
            "id": default_model_id,
            "name": default_model_name or default_model_id,
            "api": "",
            "enabled": True,
        })

    runtime_requested_models: list[dict[str, Any]] = []
    for item in requested_models:
        resolved_api = _resolved_model_api(item, default_api)
        if not resolved_api:
            raise ConfigError(f"模型 `{item['id']}` 缺少 API 类型，请设置 Provider 默认 API 或模型级 API。")
        runtime_requested_models.append({
            "id": item["id"],
            "name": str(item.get("name") or item["id"]).strip() or item["id"],
            "api": resolved_api,
        })

    merged_global_models = _merge_models(runtime_requested_models, existing_global_models)
    provider_config = copy.deepcopy(existing_global_provider)
    provider_config.pop("apiKey", None)
    provider_config["baseUrl"] = base_url
    if default_api:
        provider_config["api"] = default_api
    else:
        provider_config.pop("api", None)
    provider_config["models"] = merged_global_models

    models_root = openclaw_data.setdefault("models", {})
    models_root["mode"] = models_root.get("mode") or "merge"
    providers = models_root.setdefault("providers", {})
    if not keep_other_providers:
        providers = {
            key: value
            for key, value in providers.items()
            if key in BUILTIN_PROVIDERS or key.lower() == provider.lower()
        }
        models_root["providers"] = providers
    _remove_provider_keys(providers, provider)
    providers[provider] = provider_config

    auth_root = openclaw_data.setdefault("auth", {})
    profiles_root = auth_root.setdefault("profiles", {})
    order_root = auth_root.setdefault("order", {})
    if not keep_other_providers:
        profiles_root = {}
        order_root = {}
        auth_root["profiles"] = profiles_root
        auth_root["order"] = order_root
    auth_root.pop("usageStats", None)
    auth_root.pop("lastGood", None)
    _remove_provider_profiles(auth_root, provider, include_runtime=False)
    profile_ids = [f"{provider}:acct{index}" for index in range(1, len(api_keys) + 1)]
    for profile_id in profile_ids:
        profiles_root[profile_id] = {"provider": provider, "mode": "api_key"}
    _set_provider_order(order_root, provider, profile_ids)

    agents_root = openclaw_data.setdefault("agents", {})
    defaults_root = agents_root.setdefault("defaults", {})
    model_root = defaults_root.setdefault("model", {})
    model_root["primary"] = f"{provider}/{default_model_id}"
    model_root["fallbacks"] = []
    defaults_root["models"] = _build_default_models_catalog(models_root["providers"])
    for agent in agents_root.get("list") or []:
        if isinstance(agent, dict):
            agent.pop("model", None)

    write_json(openclaw_path, openclaw_data)

    updated_files = [str(openclaw_path)]
    for agent_dir in _agent_dirs(state_dir):
        models_path = agent_dir / "models.json"
        if models_path.exists():
            models_data = load_json(models_path)
            agent_providers = models_data.setdefault("providers", {})
            existing_agent_key = _provider_key(agent_providers, provider)
            existing_agent_provider = copy.deepcopy(agent_providers.get(existing_agent_key or "", {}))
            existing_agent_models = existing_agent_provider.get("models") or []
            merged_agent_models = _merge_models(runtime_requested_models, existing_agent_models)
            agent_provider_config = copy.deepcopy(existing_agent_provider)
            agent_provider_config.pop("apiKey", None)
            agent_provider_config["baseUrl"] = base_url
            if default_api:
                agent_provider_config["api"] = default_api
            else:
                agent_provider_config.pop("api", None)
            agent_provider_config["models"] = merged_agent_models
            if not keep_other_providers:
                agent_providers = {
                    key: value
                    for key, value in agent_providers.items()
                    if key in BUILTIN_PROVIDERS or key.lower() == provider.lower()
                }
                models_data["providers"] = agent_providers
            _remove_provider_keys(agent_providers, provider)
            agent_providers[provider] = agent_provider_config
            write_json(models_path, models_data)
            updated_files.append(str(models_path))

        auth_profiles_path = agent_dir / "auth-profiles.json"
        if auth_profiles_path.exists():
            auth_data = load_json(auth_profiles_path)
        else:
            auth_data = {"version": 1, "profiles": {}, "order": {}, "lastGood": {}, "usageStats": {}}
        auth_data["version"] = 1
        profiles = auth_data.setdefault("profiles", {})
        usage_stats = auth_data.setdefault("usageStats", {})
        last_good = auth_data.setdefault("lastGood", {})
        order = auth_data.setdefault("order", {})
        if not keep_other_providers:
            profiles = {}
            usage_stats = {}
            last_good = {}
            order = {}
            auth_data["profiles"] = profiles
            auth_data["usageStats"] = usage_stats
            auth_data["lastGood"] = last_good
            auth_data["order"] = order
        _remove_provider_profiles(auth_data, provider, include_runtime=True)
        for profile_id, api_key in zip(profile_ids, api_keys, strict=True):
            profiles[profile_id] = {"type": "api_key", "provider": provider, "key": api_key}
        _set_provider_order(order, provider, profile_ids)
        write_json(auth_profiles_path, auth_data)
        updated_files.append(str(auth_profiles_path))

    restart_stdout = ""
    summary = {
        "default_model": f"{provider}/{default_model_id}",
        "resolved_default": None,
        "provider_auth": None,
        "image_model": None,
        "restart_required": not restart_gateway,
    }
    if restart_gateway:
        restart_result = restart_openclaw(state_dir)
        restart_stdout = restart_result["restart_output"]
        summary = restart_result["status"]
        summary["restart_required"] = False
        if summary["default_model"] != f"{provider}/{default_model_id}":
            raise ConfigError(
                f"校验失败：默认模型不是 `{provider}/{default_model_id}`，当前是 `{summary['default_model']}`。"
            )

    return {
        "provider": provider,
        "base_url": base_url,
        "default_api": default_api,
        "api": default_api,
        "default_model_id": default_model_id,
        "default_model_name": default_model_name,
        "api_keys": [mask_key(item) for item in api_keys],
        "profile_ids": profile_ids,
        "updated_files": updated_files,
        "restart_output": restart_stdout,
        "status": summary,
    }
