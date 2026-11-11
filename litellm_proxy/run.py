#!/usr/bin/env python3
"""Home Assistant wrapper and Ingress router manager for LiteLLM Proxy.

v0.4.4 keeps the database-free visual router manager, incorporates the mobile-responsive Ingress UI, records recent per-model test latency/status history, and protects NVIDIA NIM deployments from unsupported explicit reasoning_effort parameters. This release pins the LiteLLM image to v1.106.6. Provider secrets stay in
Home Assistant Supervisor options. Model definitions, route membership, active
switches, and fallback order are stored in /config/model_routes.json. The
wrapper generates a normal LiteLLM YAML file and runs LiteLLM as a child process.

An Advanced YAML mode remains available as an escape hatch. Existing v0.3.x
litellm_config.yaml files are imported into the visual model on first v0.4 start
when their topology is compatible with the importer; the original file is
preserved and backed up.
"""

from __future__ import annotations

import copy
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import uuid

OPTIONS_PATH = Path(os.environ.get("HA_LITELLM_OPTIONS_PATH", "/data/options.json"))
CONFIG_DIR = Path(os.environ.get("HA_LITELLM_CONFIG_DIR", "/config"))
UI_DIR = Path(os.environ.get("HA_LITELLM_UI_DIR", "/ha-addon/ui"))
API_PORT = int(os.environ.get("HA_LITELLM_API_PORT", "4000"))
UI_PORT = int(os.environ.get("HA_LITELLM_UI_PORT", "8099"))
VERSION = "0.4.4"
STATE_PATH = CONFIG_DIR / "model_routes.json"
GENERATED_PATH = CONFIG_DIR / "litellm.generated.yaml"
STATE_BACKUP_PATH = CONFIG_DIR / "model_routes.json.bak"
GENERATED_BACKUP_PATH = CONFIG_DIR / "litellm.generated.yaml.bak"
TEST_HISTORY_PATH = CONFIG_DIR / "model_test_history.json"
TEST_HISTORY_LIMIT = 20
TEST_HISTORY_UI_COUNT = 5
_TEST_HISTORY_LOCK = threading.RLock()

SECRET_ENV_MAP = {
    "groq_api_key": ("GROQ_API_KEY",),
    "gemini_api_key": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "nvidia_api_key": ("NVIDIA_NIM_API_KEY",),
    "openai_api_key": ("OPENAI_API_KEY",),
    "anthropic_api_key": ("ANTHROPIC_API_KEY",),
    "openrouter_api_key": ("OPENROUTER_API_KEY",),
    "xai_api_key": ("XAI_API_KEY",),
    "mistral_api_key": ("MISTRAL_API_KEY",),
}

PROVIDER_DEFAULT_ENV = {
    "groq": "GROQ_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "nvidia_nim": "NVIDIA_NIM_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "xai": "XAI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "ollama": "",
    "custom": "",
}

PROVIDER_PREFIX = {
    "groq": "groq/",
    "gemini": "gemini/",
    "nvidia_nim": "nvidia_nim/",
    "openai": "openai/",
    "anthropic": "anthropic/",
    "openrouter": "openrouter/",
    "xai": "xai/",
    "mistral": "mistral/",
    "ollama": "ollama/",
}

KNOWN_PROVIDER_PREFIXES = sorted(PROVIDER_PREFIX.values(), key=len, reverse=True)
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
ROUTE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
PLACEHOLDER_RE = re.compile(
    r"(?:YOUR_(?:ACTUAL_)?[A-Z0-9_]*|REPLACE[_ -]?ME|CHANGEME|<[^>]+>)",
    re.IGNORECASE,
)

_MANAGER: "LiteLLMManager | None" = None
_STOP = threading.Event()


def log(message: str) -> None:
    print(f"[LiteLLM HA] {message}", flush=True)


def warn(message: str) -> None:
    print(f"[LiteLLM HA] WARNING: {message}", file=sys.stderr, flush=True)


def die(message: str, code: int = 1) -> None:
    print(f"[LiteLLM HA] ERROR: {message}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def slugify(value: str, fallback: str = "item") -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", text(value)).strip("-._")
    return value or fallback


def load_options() -> dict[str, Any]:
    if not OPTIONS_PATH.exists():
        die(f"Home Assistant options file does not exist: {OPTIONS_PATH}")
    try:
        data = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        die(f"Could not read {OPTIONS_PATH}: {exc}")
    if not isinstance(data, dict):
        die("options.json must contain a JSON object")
    return data


def resolve_advanced_yaml_path(options: dict[str, Any]) -> Path:
    filename = text(options.get("config_filename")) or "litellm_config.yaml"
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.ya?ml", filename):
        die("config_filename must be a simple .yaml/.yml filename without directories")
    return CONFIG_DIR / filename


def export_secret_environment(options: dict[str, Any]) -> list[str]:
    master_key = text(options.get("master_key"))
    if not master_key:
        die("master_key is required. Set it in the app Configuration tab before starting.")
    os.environ["LITELLM_MASTER_KEY"] = master_key

    exported = ["LITELLM_MASTER_KEY"]
    for option_name, env_names in SECRET_ENV_MAP.items():
        value = text(options.get(option_name))
        if not value:
            continue
        for env_name in env_names:
            os.environ[env_name] = value
            exported.append(env_name)

    return exported


def yaml_module():
    try:
        import yaml  # type: ignore
    except Exception as exc:  # noqa: BLE001
        die(f"PyYAML is required by the upstream LiteLLM image but could not be imported: {exc}")
    return yaml


def yaml_dump(data: Any) -> str:
    yaml = yaml_module()
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True)


def yaml_load_text(raw: str, source: str = "YAML") -> dict[str, Any]:
    yaml = yaml_module()
    try:
        data = yaml.safe_load(raw)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Could not parse {source}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{source} must contain a YAML mapping/object at the top level")
    return data


def load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ValueError(f"YAML file does not exist: {path}")
    return yaml_load_text(path.read_text(encoding="utf-8"), str(path))


def env_references(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for child in value.values():
            yield from env_references(child)
    elif isinstance(value, list):
        for child in value:
            yield from env_references(child)
    elif isinstance(value, str):
        match = re.fullmatch(r"os\.environ/([A-Za-z_][A-Za-z0-9_]*)", value.strip())
        if match:
            yield match.group(1)


def validate_litellm_yaml(data: dict[str, Any]) -> list[str]:
    """Validate common topology errors and return non-fatal warnings."""
    warnings: list[str] = []
    model_list = data.get("model_list")
    if not isinstance(model_list, list) or not model_list:
        raise ValueError("model_list must contain at least one deployment")

    groups: set[str] = set()
    for index, deployment in enumerate(model_list):
        if not isinstance(deployment, dict):
            raise ValueError(f"model_list entry #{index + 1} must be a mapping")
        group = text(deployment.get("model_name"))
        if not group:
            raise ValueError(f"model_list entry #{index + 1} is missing model_name")
        groups.add(group)
        params = deployment.get("litellm_params")
        if not isinstance(params, dict):
            raise ValueError(f"model_list entry '{group}' is missing litellm_params")
        model = text(params.get("model"))
        if not model:
            raise ValueError(f"model_list entry '{group}' is missing litellm_params.model")
        if PLACEHOLDER_RE.search(model):
            raise ValueError(f"model_list entry '{group}' contains placeholder model value {model!r}")
        api_base = text(params.get("api_base"))
        if api_base and PLACEHOLDER_RE.search(api_base):
            raise ValueError(f"model_list entry '{group}' contains placeholder api_base {api_base!r}")

    router = data.get("router_settings") or {}
    if not isinstance(router, dict):
        raise ValueError("router_settings must be a mapping")
    raw_fallbacks = router.get("fallbacks") or []
    if not isinstance(raw_fallbacks, list):
        raise ValueError("router_settings.fallbacks must be a list")

    edges: dict[str, list[str]] = {}
    for index, entry in enumerate(raw_fallbacks):
        if not isinstance(entry, dict) or len(entry) != 1:
            raise ValueError(f"fallbacks entry #{index + 1} must contain exactly one source")
        source, targets_raw = next(iter(entry.items()))
        source = text(source)
        if source not in groups:
            raise ValueError(f"Fallback source '{source}' does not exist in model_list")
        if isinstance(targets_raw, str):
            targets = [targets_raw]
        elif isinstance(targets_raw, list):
            targets = [text(v) for v in targets_raw if text(v)]
        else:
            raise ValueError(f"Fallback targets for '{source}' must be a string or list")
        if not targets:
            raise ValueError(f"Fallback source '{source}' has no targets")
        for target in targets:
            if target not in groups:
                raise ValueError(f"Fallback target '{target}' referenced by '{source}' does not exist")
        edges[source] = targets
        if len(targets) > 1:
            warnings.append(
                f"Fallback '{source}' has multiple targets. Explicit hop-by-hop mappings are safer "
                "with the LiteLLM version pinned by this add-on."
            )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, trail: list[str]) -> None:
        if node in visiting:
            raise ValueError("Fallback cycle detected: " + " -> ".join(trail + [node]))
        if node in visited:
            return
        visiting.add(node)
        for target in edges.get(node, []):
            visit(target, trail + [node])
        visiting.remove(node)
        visited.add(node)

    for source in edges:
        visit(source, [])

    missing_env = sorted({name for name in env_references(data) if not os.environ.get(name)})
    if missing_env:
        warnings.append("Unset environment variables referenced by YAML: " + ", ".join(missing_env))
    return warnings


def normalize_provider_model(provider: str, model: str) -> str:
    provider = text(provider).lower()
    value = text(model)
    if not value:
        return ""
    if provider == "custom":
        return value
    prefix = PROVIDER_PREFIX.get(provider, "")
    if not prefix:
        return value
    if value.startswith(prefix):
        return value
    if provider == "gemini" and value.startswith("models/"):
        value = value[len("models/") :]
    return prefix + value


def infer_provider(model_value: str) -> tuple[str, str]:
    value = text(model_value)
    for prefix in KNOWN_PROVIDER_PREFIXES:
        if value.startswith(prefix):
            provider = next(k for k, v in PROVIDER_PREFIX.items() if v == prefix)
            return provider, value[len(prefix) :]
    return "custom", value


def resolve_env_ref(value: str) -> tuple[str, str]:
    raw = text(value)
    match = re.fullmatch(r"os\.environ/([A-Za-z_][A-Za-z0-9_]*)", raw)
    if match:
        env = match.group(1)
        return env, text(os.environ.get(env))
    return "", raw


def default_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "mode": "visual",
        "settings": {
            "num_retries": 0,
            "cooldown_time": 60,
            "drop_params": True,
            "telemetry": False,
        },
        "models": [],
        "routes": [],
        "migration": {},
    }


def next_unique_id(base: str, used: set[str]) -> str:
    candidate = slugify(base, "model")
    if candidate not in used:
        used.add(candidate)
        return candidate
    suffix = 2
    while f"{candidate}-{suffix}" in used:
        suffix += 1
    result = f"{candidate}-{suffix}"
    used.add(result)
    return result


def import_yaml_to_state(data: dict[str, Any], source_path: Path) -> dict[str, Any]:
    """Import a simple LiteLLM config into v0.4 visual state.

    Unique model_name groups and explicit single-target fallback mappings are
    supported. Duplicate model_name deployments (load-balancing pools) are not
    automatically converted because treating them as fallback members would
    change their semantics.
    """
    model_list = data.get("model_list")
    if not isinstance(model_list, list) or not model_list:
        raise ValueError("Existing YAML has no model_list deployments to import")

    seen_groups: set[str] = set()
    for deployment in model_list:
        if not isinstance(deployment, dict):
            raise ValueError("Existing YAML contains a non-mapping model_list entry")
        group = text(deployment.get("model_name"))
        if not group:
            raise ValueError("Existing YAML contains a deployment without model_name")
        if group in seen_groups:
            raise ValueError(
                f"Existing YAML has duplicate model_name '{group}'. v0.4 will not auto-convert "
                "a load-balancing pool because that could change routing semantics."
            )
        seen_groups.add(group)

    state = default_state()
    router = data.get("router_settings") or {}
    if isinstance(router, dict):
        state["settings"]["num_retries"] = int(router.get("num_retries") or 0)
        state["settings"]["cooldown_time"] = int(router.get("cooldown_time") or 60)
    lite = data.get("litellm_settings") or {}
    if isinstance(lite, dict):
        state["settings"]["drop_params"] = bool(lite.get("drop_params", True))
        state["settings"]["telemetry"] = bool(lite.get("telemetry", False))

    used_ids: set[str] = set()
    group_to_model_id: dict[str, str] = {}
    for deployment in model_list:
        group = text(deployment.get("model_name"))
        params = deployment.get("litellm_params") or {}
        provider, provider_model = infer_provider(text(params.get("model")))
        model_id = next_unique_id(group, used_ids)
        group_to_model_id[group] = model_id
        api_key_env, api_key_literal = resolve_env_ref(text(params.get("api_key")))
        if api_key_literal and not api_key_env:
            # Do not copy literal secrets into the visual state. Preserve the
            # model but require the user to select a Supervisor-backed env var.
            warn(f"Not importing literal api_key for model group '{group}'. Select a credential in the UI.")
        api_base_env, api_base_value = resolve_env_ref(text(params.get("api_base")))
        if api_base_env and not api_base_value:
            api_base_value = text(params.get("api_base"))
        timeout = params.get("timeout", 45)
        try:
            timeout = int(timeout)
        except Exception:  # noqa: BLE001
            timeout = 45
        zero_cost = params.get("input_cost_per_token") == 0 and params.get("output_cost_per_token") == 0
        reasoning_effort = text(params.get("reasoning_effort")).lower()
        state["models"].append(
            {
                "id": model_id,
                "name": group,
                "provider": provider,
                "model": provider_model,
                "api_key_env": api_key_env or PROVIDER_DEFAULT_ENV.get(provider, ""),
                "api_base": api_base_value,
                "timeout": timeout,
                "reasoning_effort": reasoning_effort,
                "zero_cost": bool(zero_cost or provider == "ollama"),
            }
        )

    raw_fallbacks = router.get("fallbacks") if isinstance(router, dict) else None
    edges: dict[str, str] = {}
    if raw_fallbacks:
        if not isinstance(raw_fallbacks, list):
            raise ValueError("Existing YAML router_settings.fallbacks is not a list")
        for entry in raw_fallbacks:
            if not isinstance(entry, dict) or len(entry) != 1:
                raise ValueError("Existing YAML contains an unsupported fallback entry")
            source, targets_raw = next(iter(entry.items()))
            source = text(source)
            if isinstance(targets_raw, str):
                targets = [text(targets_raw)]
            elif isinstance(targets_raw, list):
                targets = [text(x) for x in targets_raw if text(x)]
            else:
                raise ValueError(f"Unsupported fallback targets for '{source}'")
            if len(targets) != 1:
                raise ValueError(
                    f"Fallback source '{source}' has {len(targets)} targets. v0.4 auto-import supports "
                    "explicit single-hop chains only; switch to Advanced YAML mode for this topology."
                )
            edges[source] = targets[0]

    targeted = set(edges.values())
    roots = [g for g in seen_groups if g in edges and g not in targeted]
    consumed_as_root: set[str] = set()

    for root in sorted(roots, key=lambda g: list(group_to_model_id).index(g)):
        chain: list[str] = []
        cursor = root
        visited: set[str] = set()
        while cursor:
            if cursor in visited:
                raise ValueError("Fallback cycle detected while importing: " + " -> ".join(chain + [cursor]))
            visited.add(cursor)
            if cursor not in group_to_model_id:
                raise ValueError(f"Fallback group '{cursor}' does not exist in model_list")
            chain.append(cursor)
            cursor = edges.get(cursor, "")
        state["routes"].append(
            {
                "id": uuid.uuid4().hex[:10],
                "name": root,
                "enabled": True,
                "members": [
                    {"model_id": group_to_model_id[g], "active": True} for g in chain
                ],
            }
        )
        consumed_as_root.add(root)

    # Preserve every original group as a directly callable alias. Roots already
    # have their chain route; all other groups get a single-model direct route.
    for group in group_to_model_id:
        if group in consumed_as_root:
            continue
        state["routes"].append(
            {
                "id": uuid.uuid4().hex[:10],
                "name": group,
                "enabled": True,
                "members": [{"model_id": group_to_model_id[group], "active": True}],
            }
        )

    if not state["routes"]:
        for group, model_id in group_to_model_id.items():
            state["routes"].append(
                {
                    "id": uuid.uuid4().hex[:10],
                    "name": group,
                    "enabled": True,
                    "members": [{"model_id": model_id, "active": True}],
                }
            )

    state["migration"] = {
        "imported_from": str(source_path),
        "imported_at": int(time.time()),
    }
    return state


def normalize_state(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Router state must be an object")
    state = copy.deepcopy(raw)
    state["schema_version"] = 1
    mode = text(state.get("mode")) or "visual"
    if mode not in {"visual", "yaml"}:
        raise ValueError("mode must be 'visual' or 'yaml'")
    state["mode"] = mode

    settings = state.get("settings")
    if not isinstance(settings, dict):
        settings = {}
    try:
        num_retries = int(settings.get("num_retries", 0))
        cooldown = int(settings.get("cooldown_time", 60))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Router settings must be integers: {exc}") from exc
    if not 0 <= num_retries <= 5:
        raise ValueError("num_retries must be between 0 and 5")
    if not 1 <= cooldown <= 3600:
        raise ValueError("cooldown_time must be between 1 and 3600 seconds")
    state["settings"] = {
        "num_retries": num_retries,
        "cooldown_time": cooldown,
        "drop_params": bool(settings.get("drop_params", True)),
        "telemetry": bool(settings.get("telemetry", False)),
    }

    models = state.get("models")
    if not isinstance(models, list):
        raise ValueError("models must be a list")
    normalized_models: list[dict[str, Any]] = []
    model_ids: set[str] = set()
    for index, model in enumerate(models):
        if not isinstance(model, dict):
            raise ValueError(f"Model #{index + 1} must be an object")
        model_id = text(model.get("id"))
        if not MODEL_ID_RE.fullmatch(model_id):
            raise ValueError(f"Model #{index + 1} has invalid id '{model_id}'")
        if model_id.startswith("__ui_"):
            raise ValueError("Model IDs beginning with '__ui_' are reserved")
        if model_id in model_ids:
            raise ValueError(f"Duplicate model id '{model_id}'")
        model_ids.add(model_id)
        name = text(model.get("name")) or model_id
        provider = text(model.get("provider")).lower()
        if provider not in PROVIDER_DEFAULT_ENV:
            raise ValueError(f"Model '{model_id}' has unsupported provider '{provider}'")
        model_name = text(model.get("model"))
        if not model_name:
            raise ValueError(f"Model '{model_id}' has no model identifier")
        if PLACEHOLDER_RE.search(model_name):
            raise ValueError(f"Model '{model_id}' contains a placeholder model identifier")
        api_key_env = text(model.get("api_key_env")) or PROVIDER_DEFAULT_ENV.get(provider, "")
        if api_key_env and not ENV_NAME_RE.fullmatch(api_key_env):
            raise ValueError(f"Model '{model_id}' has invalid credential environment name")
        api_base = text(model.get("api_base"))
        if api_base and PLACEHOLDER_RE.search(api_base):
            raise ValueError(f"Model '{model_id}' contains a placeholder API base")
        try:
            timeout = int(model.get("timeout", 45))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Model '{model_id}' timeout must be an integer") from exc
        if not 5 <= timeout <= 900:
            raise ValueError(f"Model '{model_id}' timeout must be between 5 and 900 seconds")
        reasoning_effort = text(model.get("reasoning_effort")).lower()
        allowed_reasoning_efforts = {"", "none", "minimal", "low", "medium", "high", "xhigh", "max"}
        if reasoning_effort not in allowed_reasoning_efforts:
            raise ValueError(
                f"Model '{model_id}' reasoning_effort must be one of: default, none, minimal, low, medium, high, xhigh, max"
            )
        # The LiteLLM nvidia_nim adapter pinned by this add-on currently rejects
        # explicit reasoning_effort before the request reaches NVIDIA. Keep the
        # visual state safe by omitting this parameter for NVIDIA NIM models.
        if provider == "nvidia_nim":
            reasoning_effort = ""
        normalized_models.append(
            {
                "id": model_id,
                "name": name,
                "provider": provider,
                "model": model_name,
                "api_key_env": api_key_env,
                "api_base": api_base,
                "timeout": timeout,
                "reasoning_effort": reasoning_effort,
                "zero_cost": bool(model.get("zero_cost", provider == "ollama")),
            }
        )
    state["models"] = normalized_models

    routes = state.get("routes")
    if not isinstance(routes, list):
        raise ValueError("routes must be a list")
    route_names: set[str] = set()
    normalized_routes: list[dict[str, Any]] = []
    for index, route in enumerate(routes):
        if not isinstance(route, dict):
            raise ValueError(f"Route #{index + 1} must be an object")
        route_id = text(route.get("id")) or uuid.uuid4().hex[:10]
        route_id = slugify(route_id, uuid.uuid4().hex[:10])
        name = text(route.get("name"))
        if not ROUTE_NAME_RE.fullmatch(name):
            raise ValueError(f"Route #{index + 1} has invalid name '{name}'")
        if name.startswith("__ui_"):
            raise ValueError("Route names beginning with '__ui_' are reserved")
        if name in route_names:
            raise ValueError(f"Duplicate route name '{name}'")
        route_names.add(name)
        members = route.get("members")
        if not isinstance(members, list):
            raise ValueError(f"Route '{name}' members must be a list")
        member_ids: set[str] = set()
        normalized_members: list[dict[str, Any]] = []
        for member in members:
            if not isinstance(member, dict):
                raise ValueError(f"Route '{name}' contains an invalid member")
            model_id = text(member.get("model_id"))
            if model_id not in model_ids:
                raise ValueError(f"Route '{name}' references unknown model '{model_id}'")
            if model_id in member_ids:
                raise ValueError(f"Route '{name}' contains model '{model_id}' more than once")
            member_ids.add(model_id)
            normalized_members.append({"model_id": model_id, "active": bool(member.get("active", True))})
        # Keep models visible in every route so they can be toggled on directly.
        for model_id in [m["id"] for m in normalized_models]:
            if model_id not in member_ids:
                normalized_members.append({"model_id": model_id, "active": False})
        normalized_routes.append(
            {
                "id": route_id,
                "name": name,
                "enabled": bool(route.get("enabled", True)),
                "members": normalized_members,
            }
        )
    state["routes"] = normalized_routes
    state["migration"] = state.get("migration") if isinstance(state.get("migration"), dict) else {}

    if mode == "visual":
        enabled_routes = [r for r in normalized_routes if r["enabled"]]
        if not enabled_routes:
            raise ValueError("Visual mode requires at least one enabled route")
        for route in enabled_routes:
            if not any(m["active"] for m in route["members"]):
                raise ValueError(f"Enabled route '{route['name']}' has no active models")
    return state


def model_params(model: dict[str, Any]) -> dict[str, Any]:
    provider = model["provider"]
    params: dict[str, Any] = {
        "model": normalize_provider_model(provider, model["model"]),
        "timeout": int(model["timeout"]),
    }
    api_key_env = text(model.get("api_key_env"))
    if api_key_env:
        params["api_key"] = f"os.environ/{api_key_env}"
    api_base = text(model.get("api_base"))
    if api_base:
        params["api_base"] = api_base
    reasoning_effort = text(model.get("reasoning_effort")).lower()
    if reasoning_effort:
        params["reasoning_effort"] = reasoning_effort
    if bool(model.get("zero_cost")):
        params["input_cost_per_token"] = 0
        params["output_cost_per_token"] = 0
    return params


def generate_visual_config(state: dict[str, Any]) -> dict[str, Any]:
    state = normalize_state(state)
    models_by_id = {m["id"]: m for m in state["models"]}
    model_list: list[dict[str, Any]] = []
    fallbacks: list[dict[str, list[str]]] = []
    generated_groups: set[str] = set()

    for route in state["routes"]:
        if not route["enabled"]:
            continue
        active = [m for m in route["members"] if m["active"]]
        if not active:
            continue
        groups: list[str] = []
        for index, member in enumerate(active):
            model = models_by_id[member["model_id"]]
            if index == 0:
                group = route["name"]
            else:
                group = f"__ui_{slugify(route['id'])}_{index + 1}_{slugify(model['id'])}"
            if group in generated_groups:
                raise ValueError(f"Generated model group collision: {group}")
            generated_groups.add(group)
            groups.append(group)
            model_list.append(
                {
                    "model_name": group,
                    "litellm_params": model_params(model),
                    "model_info": {
                        "ui_model_id": model["id"],
                        "ui_model_name": model["name"],
                        "ui_provider": model["provider"],
                        "ui_route": route["name"],
                        "ui_fallback_priority": index + 1,
                    },
                }
            )
        for index in range(len(groups) - 1):
            fallbacks.append({groups[index]: [groups[index + 1]]})

    if not model_list:
        raise ValueError("No active visual route deployments were generated")

    settings = state["settings"]
    router: dict[str, Any] = {
        "num_retries": settings["num_retries"],
        "cooldown_time": settings["cooldown_time"],
    }
    if fallbacks:
        router["fallbacks"] = fallbacks

    return {
        "model_list": model_list,
        "router_settings": router,
        "litellm_settings": {
            "drop_params": settings["drop_params"],
            "telemetry": settings["telemetry"],
        },
        "general_settings": {
            "master_key": "os.environ/LITELLM_MASTER_KEY",
            "store_model_in_db": False,
        },
    }


def read_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        raise ValueError(f"Visual state file does not exist: {STATE_PATH}")
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Could not parse {STATE_PATH}: {exc}") from exc
    return normalize_state(raw)


def write_json_atomic(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_text_atomic(path: Path, data: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


def create_first_state(advanced_yaml_path: Path) -> dict[str, Any]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if advanced_yaml_path.exists():
        data = load_yaml_file(advanced_yaml_path)
        try:
            state = import_yaml_to_state(data, advanced_yaml_path)
            backup = advanced_yaml_path.with_name(advanced_yaml_path.name + ".pre-v0.4-backup")
            if not backup.exists():
                shutil.copy2(advanced_yaml_path, backup)
            log(f"Imported existing {advanced_yaml_path.name} into the v0.4 visual router manager.")
            log(f"Original YAML preserved; pre-v0.4 backup: {backup}")
            write_json_atomic(STATE_PATH, normalize_state(state))
            return normalize_state(state)
        except Exception as exc:  # noqa: BLE001
            warn(f"Could not auto-import existing YAML into visual mode: {exc}")
            warn("Starting in Advanced YAML mode so existing routing semantics are preserved.")
            state = default_state()
            state["mode"] = "yaml"
            state["migration"] = {"import_error": str(exc), "source": str(advanced_yaml_path)}
            # Visual state must be normalizable in yaml mode even with no routes.
            write_json_atomic(STATE_PATH, normalize_state(state))
            return normalize_state(state)

    # Fresh install: provide useful starter models but no secret values.
    state = default_state()
    state["models"] = [
        {
            "id": "groq-primary",
            "name": "Groq primary",
            "provider": "groq",
            "model": "openai/gpt-oss-20b",
            "api_key_env": "GROQ_API_KEY",
            "api_base": "",
            "timeout": 45,
            "zero_cost": False,
        },
        {
            "id": "gemini-flash",
            "name": "Gemini Flash Lite",
            "provider": "gemini",
            "model": "gemini-3.1-flash-lite",
            "api_key_env": "GEMINI_API_KEY",
            "api_base": "",
            "timeout": 45,
            "zero_cost": False,
        },
        {
            "id": "ollama-local",
            "name": "Local Ollama",
            "provider": "ollama",
            "model": "qwen2.5:1.5b",
            "api_key_env": "",
            "api_base": "http://YOUR_OLLAMA_HOST:11434",
            "timeout": 300,
            "zero_cost": True,
        },
    ]
    state["routes"] = [
        {
            "id": uuid.uuid4().hex[:10],
            "name": "default-router",
            "enabled": True,
            "members": [
                {"model_id": "groq-primary", "active": True},
                {"model_id": "gemini-flash", "active": True},
                {"model_id": "ollama-local", "active": False},
            ],
        }
    ]
    # Placeholder API base would fail validation, so replace it with a neutral
    # value only in the stored starter and keep the local model inactive. The UI
    # requires the user to enter a reachable address before activating it.
    state["models"][2]["api_base"] = "http://homeassistant.local:11434"
    state = normalize_state(state)
    write_json_atomic(STATE_PATH, state)
    log(f"Created starter visual router state at {STATE_PATH}")
    return state


def credential_status() -> dict[str, bool]:
    names = sorted({env for envs in SECRET_ENV_MAP.values() for env in envs} | {"LITELLM_MASTER_KEY"})
    return {name: bool(text(os.environ.get(name))) for name in names}


def sanitize_exception(exc: Exception) -> str:
    value = str(exc)
    # Avoid accidentally surfacing configured secret values in UI errors.
    for env_name in credential_status():
        secret = text(os.environ.get(env_name))
        if secret and len(secret) >= 6:
            value = value.replace(secret, "[REDACTED]")
    return value[:4000]


def _read_test_history() -> dict[str, list[dict[str, Any]]]:
    if not TEST_HISTORY_PATH.exists():
        return {}
    try:
        raw = json.loads(TEST_HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        warn(f"Could not read model test history; starting fresh: {sanitize_exception(exc)}")
        return {}
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, list[dict[str, Any]]] = {}
    for key, entries in raw.items():
        if isinstance(key, str) and isinstance(entries, list):
            cleaned[key] = [e for e in entries if isinstance(e, dict)][-TEST_HISTORY_LIMIT:]
    return cleaned


def _write_test_history(history: dict[str, list[dict[str, Any]]]) -> None:
    write_json_atomic(TEST_HISTORY_PATH, history)


def summarize_model_history(model_id: str) -> dict[str, Any]:
    with _TEST_HISTORY_LOCK:
        entries = _read_test_history().get(model_id, [])[-TEST_HISTORY_UI_COUNT:]
    latencies = [int(e.get("latency_ms")) for e in entries if isinstance(e.get("latency_ms"), (int, float))]
    median_latency = None
    if latencies:
        ordered = sorted(latencies)
        mid = len(ordered) // 2
        median_latency = ordered[mid] if len(ordered) % 2 else round((ordered[mid - 1] + ordered[mid]) / 2)
    successes = sum(1 for e in entries if e.get("ok") is True)
    return {
        "entries": entries,
        "median_latency_ms": median_latency,
        "successes": successes,
        "total": len(entries),
    }


def all_model_test_history(model_ids: Iterable[str]) -> dict[str, Any]:
    return {model_id: summarize_model_history(model_id) for model_id in model_ids}


def record_model_test(model_id: str, result: dict[str, Any]) -> dict[str, Any]:
    entry = {
        "timestamp": int(time.time()),
        "ok": bool(result.get("ok")),
        "status": result.get("status"),
        "latency_ms": result.get("latency_ms"),
        "served_model": text(result.get("served_model")) or None,
    }
    if not entry["ok"] and result.get("error"):
        entry["error"] = text(result.get("error"))[:500]
    with _TEST_HISTORY_LOCK:
        history = _read_test_history()
        values = history.setdefault(model_id, [])
        values.append(entry)
        history[model_id] = values[-TEST_HISTORY_LIMIT:]
        _write_test_history(history)
    return summarize_model_history(model_id)


class LiteLLMManager:
    def __init__(self, options: dict[str, Any], advanced_yaml_path: Path):
        self.options = options
        self.advanced_yaml_path = advanced_yaml_path
        self.lock = threading.RLock()
        self.process: subprocess.Popen[Any] | None = None
        self.active_config_path: Path | None = None
        self.last_start_error = ""
        self.executable = shutil.which("litellm")
        if not self.executable:
            die("The upstream image does not contain the 'litellm' executable in PATH.")

    def current_state(self) -> dict[str, Any]:
        return read_state()

    def config_path_for_state(self, state: dict[str, Any]) -> Path:
        return self.advanced_yaml_path if state["mode"] == "yaml" else GENERATED_PATH

    def ensure_visual_config(self, state: dict[str, Any]) -> list[str]:
        config = generate_visual_config(state)
        warnings = validate_litellm_yaml(config)
        header = (
            "# Generated by Home Assistant LiteLLM Router Manager v0.4.4.\n"
            "# Source of truth: /config/model_routes.json\n"
            "# Edit through the add-on Web UI. Manual edits to this generated file are overwritten.\n\n"
        )
        write_text_atomic(GENERATED_PATH, header + yaml_dump(config))
        return warnings

    def validate_advanced_text(self, raw: str) -> tuple[dict[str, Any], list[str]]:
        data = yaml_load_text(raw, "Advanced YAML")
        warnings = validate_litellm_yaml(data)
        return data, warnings

    def start(self, path: Path) -> None:
        with self.lock:
            if self.process and self.process.poll() is None:
                raise RuntimeError("LiteLLM child process is already running")
            command = [
                self.executable,
                "--config",
                str(path),
                "--host",
                "0.0.0.0",
                "--port",
                str(API_PORT),
            ]
            if bool(self.options.get("debug", False)):
                command.append("--detailed_debug")
            log("Starting LiteLLM with config: " + str(path))
            self.process = subprocess.Popen(command, env=os.environ.copy())
            self.active_config_path = path
            self.last_start_error = ""

    def stop(self, timeout: float = 12.0) -> None:
        with self.lock:
            proc = self.process
            self.process = None
            if not proc or proc.poll() is not None:
                return
            log("Stopping LiteLLM child process")
            proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            warn("LiteLLM did not stop gracefully; killing child process")
            proc.kill()
            proc.wait(timeout=5)

    def healthy(self) -> bool:
        try:
            with urlopen(f"http://127.0.0.1:{API_PORT}/health/liveliness", timeout=2) as response:
                return 200 <= response.status < 300
        except Exception:  # noqa: BLE001
            return False

    def wait_healthy(self, timeout: float = 30.0) -> tuple[bool, str]:
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            with self.lock:
                proc = self.process
                if proc is None:
                    return False, "LiteLLM child process is not running"
                code = proc.poll()
                if code is not None:
                    return False, f"LiteLLM exited with code {code}"
            if self.healthy():
                return True, ""
            time.sleep(0.5)
        last = f"LiteLLM did not become healthy within {int(timeout)} seconds"
        return False, last

    def runtime_status(self) -> dict[str, Any]:
        with self.lock:
            proc = self.process
            running = bool(proc and proc.poll() is None)
            pid = proc.pid if running and proc else None
            return {
                "running": running,
                "healthy": self.healthy() if running else False,
                "pid": pid,
                "active_config": str(self.active_config_path) if self.active_config_path else None,
                "last_start_error": self.last_start_error,
            }

    def initial_start(self, state: dict[str, Any]) -> None:
        if state["mode"] == "visual":
            warnings = self.ensure_visual_config(state)
            for message in warnings:
                warn(message)
        else:
            if not self.advanced_yaml_path.exists():
                raise ValueError(f"Advanced YAML file does not exist: {self.advanced_yaml_path}")
            warnings = validate_litellm_yaml(load_yaml_file(self.advanced_yaml_path))
            for message in warnings:
                warn(message)
        self.start(self.config_path_for_state(state))

    def apply(self, incoming_state: dict[str, Any], advanced_yaml_text: str | None) -> dict[str, Any]:
        """Atomically save and apply configuration, rolling back on startup failure."""
        with self.lock:
            state = normalize_state(incoming_state)
            old_state_bytes = STATE_PATH.read_bytes() if STATE_PATH.exists() else None
            old_generated_bytes = GENERATED_PATH.read_bytes() if GENERATED_PATH.exists() else None
            old_advanced_bytes = self.advanced_yaml_path.read_bytes() if self.advanced_yaml_path.exists() else None
            old_active_path = self.active_config_path

            warnings: list[str] = []
            if state["mode"] == "visual":
                config = generate_visual_config(state)
                warnings.extend(validate_litellm_yaml(config))
                generated_text = (
                    "# Generated by Home Assistant LiteLLM Router Manager v0.4.4.\n"
                    "# Source of truth: /config/model_routes.json\n"
                    "# Edit through the add-on Web UI. Manual edits to this generated file are overwritten.\n\n"
                    + yaml_dump(config)
                )
            else:
                if advanced_yaml_text is None:
                    raise ValueError("Advanced YAML mode requires YAML content")
                _, yaml_warnings = self.validate_advanced_text(advanced_yaml_text)
                warnings.extend(yaml_warnings)
                generated_text = None

            # Save backups before mutation.
            if old_state_bytes is not None:
                STATE_BACKUP_PATH.write_bytes(old_state_bytes)
            if old_generated_bytes is not None:
                GENERATED_BACKUP_PATH.write_bytes(old_generated_bytes)

            write_json_atomic(STATE_PATH, state)
            if state["mode"] == "visual":
                assert generated_text is not None
                write_text_atomic(GENERATED_PATH, generated_text)
            else:
                write_text_atomic(self.advanced_yaml_path, advanced_yaml_text or "")

            new_path = self.config_path_for_state(state)
            self.stop()
            try:
                self.start(new_path)
                ok, error = self.wait_healthy(30)
                if not ok:
                    raise RuntimeError(error)
            except Exception as exc:  # noqa: BLE001
                failure = sanitize_exception(exc)
                warn(f"New configuration failed; rolling back: {failure}")
                self.stop()
                if old_state_bytes is not None:
                    STATE_PATH.write_bytes(old_state_bytes)
                elif STATE_PATH.exists():
                    STATE_PATH.unlink()
                if old_generated_bytes is not None:
                    GENERATED_PATH.write_bytes(old_generated_bytes)
                elif GENERATED_PATH.exists():
                    GENERATED_PATH.unlink()
                if old_advanced_bytes is not None:
                    self.advanced_yaml_path.write_bytes(old_advanced_bytes)
                elif self.advanced_yaml_path.exists() and state["mode"] == "yaml":
                    self.advanced_yaml_path.unlink()
                if old_active_path and old_active_path.exists():
                    try:
                        self.start(old_active_path)
                        self.wait_healthy(30)
                    except Exception as rollback_exc:  # noqa: BLE001
                        self.last_start_error = "Rollback restart failed: " + sanitize_exception(rollback_exc)
                        warn(self.last_start_error)
                raise RuntimeError("Configuration was rejected and rolled back: " + failure) from exc

            return {"state": state, "warnings": warnings, "runtime": self.runtime_status()}

    def test_route(self, route_name: str) -> dict[str, Any]:
        route_name = text(route_name)
        if not route_name:
            raise ValueError("route_name is required")
        payload = json.dumps(
            {
                "model": route_name,
                "messages": [{"role": "user", "content": "Reply exactly with OK"}],
                "max_tokens": 12,
            }
        ).encode("utf-8")
        request = Request(
            f"http://127.0.0.1:{API_PORT}/v1/chat/completions",
            data=payload,
            headers={
                "Authorization": "Bearer " + text(os.environ.get("LITELLM_MASTER_KEY")),
                "Content-Type": "application/json",
            },
            method="POST",
        )
        started = time.monotonic()
        try:
            with urlopen(request, timeout=70) as response:
                raw = response.read().decode("utf-8", errors="replace")
                data = json.loads(raw)
                elapsed = round((time.monotonic() - started) * 1000)
                content = ""
                try:
                    content = text(data["choices"][0]["message"]["content"])
                except Exception:  # noqa: BLE001
                    pass
                return {
                    "ok": True,
                    "status": response.status,
                    "latency_ms": elapsed,
                    "served_model": data.get("model"),
                    "response": content[:300],
                }
        except HTTPError as exc:
            elapsed = round((time.monotonic() - started) * 1000)
            body = exc.read().decode("utf-8", errors="replace")[:1500]
            return {"ok": False, "status": exc.code, "latency_ms": elapsed, "error": body}
        except Exception as exc:  # noqa: BLE001
            elapsed = round((time.monotonic() - started) * 1000)
            return {"ok": False, "status": None, "latency_ms": elapsed, "error": sanitize_exception(exc)}

    def test_model(self, model_id: str) -> dict[str, Any]:
        state = self.current_state()
        model = next((m for m in state["models"] if m["id"] == model_id), None)
        if model is None:
            raise ValueError(f"Unknown model '{model_id}'")
        params = model_params(model)
        api_key = params.pop("api_key", None)
        if isinstance(api_key, str) and api_key.startswith("os.environ/"):
            env_name = api_key.split("/", 1)[1]
            api_key = text(os.environ.get(env_name)) or None
        api_base = params.get("api_base")
        if isinstance(api_base, str) and api_base.startswith("os.environ/"):
            env_name = api_base.split("/", 1)[1]
            params["api_base"] = text(os.environ.get(env_name)) or None
        # Cost metadata belongs in proxy configuration, not an individual
        # litellm.completion() request.
        params.pop("input_cost_per_token", None)
        params.pop("output_cost_per_token", None)
        started = time.monotonic()
        result: dict[str, Any]
        try:
            import litellm  # type: ignore

            response = litellm.completion(
                **params,
                api_key=api_key,
                messages=[{"role": "user", "content": "Reply exactly with OK"}],
                max_tokens=12,
            )
            elapsed = round((time.monotonic() - started) * 1000)
            content = ""
            try:
                content = text(response.choices[0].message.content)
            except Exception:  # noqa: BLE001
                pass
            served = getattr(response, "model", None)
            result = {
                "ok": True,
                "status": 200,
                "latency_ms": elapsed,
                "served_model": served,
                "response": content[:300],
            }
        except Exception as exc:  # noqa: BLE001
            elapsed = round((time.monotonic() - started) * 1000)
            status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
            result = {
                "ok": False,
                "status": status,
                "latency_ms": elapsed,
                "error": sanitize_exception(exc),
            }
        result["history"] = record_model_test(model_id, result)
        return result


def safe_json_body(handler: BaseHTTPRequestHandler, limit: int = 2_000_000) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if length <= 0:
        return {}
    if length > limit:
        raise ValueError("Request body is too large")
    raw = handler.rfile.read(length)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSON request body must be an object")
    return data


def ingress_client_allowed(address: str) -> bool:
    if os.environ.get("HA_LITELLM_UI_ALLOW_ALL") == "1":
        return True
    # Home Assistant Supervisor ingress gateway address documented by HA.
    return address in {"172.30.32.2", "::ffff:172.30.32.2"}


class UIHandler(BaseHTTPRequestHandler):
    server_version = "LiteLLMRouterManager/0.4.4"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Avoid noisy access logs; configuration actions are explicitly logged.
        if self.path.endswith("/api/apply"):
            log("UI " + (fmt % args))

    def _path(self) -> str:
        path = urlparse(self.path).path
        marker = "/api/"
        index = path.find(marker)
        if index >= 0:
            return path[index:]
        return path

    def _deny_if_not_ingress(self) -> bool:
        address = self.client_address[0]
        if ingress_client_allowed(address):
            return False
        self.send_response(403)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Ingress access only")
        return True

    def _json(self, status: int, payload: Any) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _text(self, status: int, raw: str, content_type: str) -> None:
        body = raw.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self._deny_if_not_ingress():
            return
        assert _MANAGER is not None
        path = self._path()
        try:
            if path == "/api/state":
                state = _MANAGER.current_state()
                advanced = ""
                if _MANAGER.advanced_yaml_path.exists():
                    advanced = _MANAGER.advanced_yaml_path.read_text(encoding="utf-8")
                self._json(
                    200,
                    {
                        "version": VERSION,
                        "state": state,
                        "credentials": credential_status(),
                        "test_history": all_model_test_history(m["id"] for m in state.get("models", [])),
                        "runtime": _MANAGER.runtime_status(),
                        "advanced_yaml": advanced,
                        "advanced_yaml_path": str(_MANAGER.advanced_yaml_path),
                        "generated_yaml_path": str(GENERATED_PATH),
                    },
                )
                return
            if path == "/api/generated":
                raw = GENERATED_PATH.read_text(encoding="utf-8") if GENERATED_PATH.exists() else ""
                self._json(200, {"yaml": raw})
                return
            if path == "/api/health":
                self._json(200, {"ui": True, "litellm": _MANAGER.runtime_status()})
                return
            index_path = UI_DIR / "index.html"
            if not index_path.exists():
                self._text(500, "UI file missing", "text/plain; charset=utf-8")
                return
            self._text(200, index_path.read_text(encoding="utf-8"), "text/html; charset=utf-8")
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": sanitize_exception(exc)})

    def do_POST(self) -> None:  # noqa: N802
        if self._deny_if_not_ingress():
            return
        assert _MANAGER is not None
        path = self._path()
        try:
            body = safe_json_body(self)
            if path == "/api/apply":
                incoming = body.get("state")
                if not isinstance(incoming, dict):
                    raise ValueError("state object is required")
                advanced_yaml = body.get("advanced_yaml")
                if advanced_yaml is not None and not isinstance(advanced_yaml, str):
                    raise ValueError("advanced_yaml must be a string")
                result = _MANAGER.apply(incoming, advanced_yaml)
                self._json(200, {"ok": True, **result})
                return
            if path == "/api/test-model":
                self._json(200, _MANAGER.test_model(text(body.get("model_id"))))
                return
            if path == "/api/test-route":
                self._json(200, _MANAGER.test_route(text(body.get("route_name"))))
                return
            self._json(404, {"error": "Not found"})
        except ValueError as exc:
            self._json(400, {"ok": False, "error": sanitize_exception(exc)})
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"ok": False, "error": sanitize_exception(exc)})


def start_ui_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", UI_PORT), UIHandler)
    thread = threading.Thread(target=server.serve_forever, name="litellm-ui", daemon=True)
    thread.start()
    log(f"Ingress management UI listening on 0.0.0.0:{UI_PORT}")
    return server


def signal_handler(signum: int, _frame: Any) -> None:
    log(f"Received signal {signum}; shutting down")
    _STOP.set()


def main() -> None:
    global _MANAGER
    options = load_options()
    advanced_yaml_path = resolve_advanced_yaml_path(options)
    exported = export_secret_environment(options)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if not STATE_PATH.exists():
        state = create_first_state(advanced_yaml_path)
    else:
        state = read_state()

    try:
        litellm_version = importlib.metadata.version("litellm")
    except Exception:  # noqa: BLE001
        litellm_version = "unknown"

    log(f"Add-on wrapper version: {VERSION}")
    log(f"LiteLLM version: {litellm_version}")
    log(f"Router state: {STATE_PATH}")
    log(f"Visual generated YAML: {GENERATED_PATH}")
    log(f"Advanced YAML: {advanced_yaml_path}")
    log("Secret environment exported: " + ", ".join(sorted(set(exported))))

    manager = LiteLLMManager(options, advanced_yaml_path)
    _MANAGER = manager

    if os.environ.get("HA_LITELLM_GENERATE_ONLY") == "1":
        if state["mode"] == "visual":
            manager.ensure_visual_config(state)
        return

    # Start management UI first so an ingress endpoint is available while the
    # proxy starts. Save/apply uses rollback before replacing a working config.
    ui_server = start_ui_server()

    try:
        manager.initial_start(state)
        ok, error = manager.wait_healthy(40)
        if not ok:
            manager.last_start_error = error
            warn(error)
        else:
            log(f"LiteLLM API healthy on port {API_PORT}")

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        while not _STOP.wait(2):
            with manager.lock:
                proc = manager.process
                if proc and proc.poll() is not None:
                    manager.last_start_error = f"LiteLLM child exited unexpectedly with code {proc.returncode}"
                    warn(manager.last_start_error)
                    # Keep the wrapper/UI alive briefly; Supervisor watchdog on
                    # port 4000 can restart the add-on if the API stays down.
                    manager.process = None
    finally:
        ui_server.shutdown()
        ui_server.server_close()
        manager.stop()


if __name__ == "__main__":
    main()
