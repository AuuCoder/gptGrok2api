"""Flatten / unflatten helpers shared by all backends."""

import json
import logging
from typing import Any

_log = logging.getLogger(__name__)


def flatten(nested: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Recursively flatten *nested* to ``{"section.key": json_value, ...}``."""
    out: dict[str, str] = {}
    for k, v in nested.items():
        full = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(flatten(v, full))
        else:
            out[full] = json.dumps(v, ensure_ascii=False)
    return out


def unflatten(flat: dict[str, str]) -> dict[str, Any]:
    """Rebuild a nested dict from ``{"section.key": json_value, ...}``."""
    result: dict[str, Any] = {}
    for dotted, json_val in flat.items():
        parts = dotted.split(".")
        node = result
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        try:
            node[parts[-1]] = json.loads(json_val)
        except (json.JSONDecodeError, ValueError):
            _log.warning("config: failed to deserialize key %r, treating as string", dotted)
            node[parts[-1]] = json_val
    return result
