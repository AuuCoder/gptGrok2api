"""TOML configuration loader with environment-variable override support."""

import os
from pathlib import Path
from typing import Any

import tomllib


def _flatten(mapping: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten a nested dict into dotted keys."""
    out: dict[str, Any] = {}
    for k, v in mapping.items():
        full = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, full))
        else:
            out[full] = v
    return out


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *override* into *base* (non-destructive)."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_toml(path: Path) -> dict[str, Any]:
    """Load a TOML file and return the raw nested dict."""
    if not path.exists():
        return {}
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def load_config(
    defaults_path: Path,
    user_path: Path | None = None,
    env_prefix: str = "GROK_",
) -> dict[str, Any]:
    """Load configuration: defaults → user file → environment overrides.

    Environment variables use the format ``GROK_SECTION_KEY=value``,
    which maps to the dotted key ``section.key``.
    """
    data = load_toml(defaults_path)
    if user_path and user_path.exists():
        user = load_toml(user_path)
        data = _deep_merge(data, user)

    # Environment overrides (GROK_PROXY_BASE_PROXY_URL → proxy.base_proxy_url)
    prefix_len = len(env_prefix)
    for env_key, env_val in os.environ.items():
        if not env_key.startswith(env_prefix):
            continue
        parts = env_key[prefix_len:].lower().split("_", 1)
        if len(parts) == 2:
            section, key = parts
            data.setdefault(section, {})[key] = env_val

    return data


def get_nested(data: dict[str, Any], dotted_key: str, default: Any = None) -> Any:
    """Retrieve a value from a nested dict using a dotted key path."""
    keys = dotted_key.split(".")
    node: Any = data
    for k in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(k)
        if node is None:
            return default
    return node
