"""Stripe custom-checkout token extraction for pure HTTP payment flows."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Callable


LogFn = Callable[[str], None]


def caesar_shift(value: str, shift: int) -> str:
    return "".join(chr((ord(char) - 32 + shift) % 95 + 32) for char in value)


def stripe_encode(value: str) -> str:
    padding = 3 - len(value) % 3
    xored = bytes(5 ^ ord(char) for char in value + " " * padding)
    return urllib.parse.quote(
        base64.b64encode(xored).decode("ascii"),
        safe="-_.!~*'()",
    )


def _js_stringify(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class StripeTokenConfig:
    bundle_hash: str
    shift: int
    rv_ts: str
    rv: str
    sv: str

    @property
    def runtime_version(self) -> str:
        return self.rv[:10]


class StripeTokenExtractError(RuntimeError):
    pass


_CAESAR_FN_RE = re.compile(
    r"\b[a-zA-Z_$][\w$]{0,3}\s*=\s*function\s*\(\s*"
    r"[a-zA-Z_$][\w$]{0,3}\s*,\s*[a-zA-Z_$][\w$]{0,3}\s*\)\s*\{"
    r"[^{}]*?charCodeAt\([^)]*?\)\s*-\s*32\s*\+\s*"
    r"[a-zA-Z_$][\w$]{0,3}\s*\)\s*%\s*95\s*\+\s*32[^{}]*?\}"
)
_JS_CHECKSUM_RE = re.compile(
    r"\b(?P<fn>[a-zA-Z_$][\w$]{0,3})\s*\(\s*"
    r"\(\s*0\s*,\s*(?P<encmod>[a-zA-Z_$][\w$]{0,3})\s*\.\s*"
    r"(?P<encfn>[a-zA-Z_$][\w$]{0,3})\s*\)\s*\(\s*JSON\s*\.\s*"
    r"stringify\s*\(\s*\{\s*id\s*:\s*[a-zA-Z_$][\w$]*\s*\}\s*\)\s*\)"
    r"\s*,\s*(?P<shift>\d+)\s*\)"
)
_RV_TIMESTAMP_RE = re.compile(
    r"rv_timestamp\s*:\s*[a-zA-Z_$][\w$]{0,3}\s*\(\s*\(\s*0\s*,\s*"
    r"[a-zA-Z_$][\w$]{0,3}\s*\.\s*[a-zA-Z_$][\w$]{0,3}\s*\)\s*\(\s*"
    r"JSON\s*\.\s*stringify\s*\(\s*\{(?P<keys>[^}]+)\}\s*\)\s*\)\s*,\s*"
    r"(?P<shift>\d+)\s*\)"
)
_WEBPACK_REQUIRE_RE = re.compile(
    r"\b(?P<lhs>[a-zA-Z_$][\w$]{0,3})\s*=\s*[a-zA-Z_$][\w$]{0,3}"
    r"\s*\(\s*(?P<id>\d+)\s*\)"
)


def _balanced_brace(source: str, open_pos: int) -> int:
    depth = 0
    quote = ""
    index = open_pos
    while index < len(source):
        char = source[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
        elif char in ("'", '"', "`"):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def _extract_webpack_module(source: str, module_id: int) -> str:
    pattern = re.compile(rf"[\s,{{(]{module_id}\s*:\s*", re.MULTILINE)
    for match in pattern.finditer(source):
        signature = re.match(
            r"\s*(?:function\s*\([^)]*\)|\([^)]*\)\s*=>|"
            r"[a-zA-Z_$][\w$]*\s*=>)\s*\{",
            source[match.end() : match.end() + 200],
        )
        if not signature:
            continue
        open_pos = match.end() + signature.end() - 1
        close_pos = _balanced_brace(source, open_pos)
        if close_pos >= 0:
            return source[match.start() : close_pos + 1]
    return ""


def _extract_constants(module_source: str) -> dict[str, str]:
    exports: dict[str, str] = {}
    for match in re.finditer(
        r"([a-zA-Z_$][\w$]{0,3})\s*:\s*function\s*\(\s*\)\s*\{\s*"
        r"return\s+([a-zA-Z_$][\w$]{0,3})\s*\}",
        module_source,
    ):
        exports[match.group(1)] = match.group(2)
    values: dict[str, str] = {}
    for match in re.finditer(
        r'\b([a-zA-Z_$][\w$]{0,3})\s*=\s*'
        r'(?:/\*[^*]*(?:\*(?!/)[^*]*)*\*/\s*)?"([^"]*)"',
        module_source,
    ):
        values.setdefault(match.group(1), match.group(2))
    return {
        export_name: values[local_name]
        for export_name, local_name in exports.items()
        if local_name in values
    }


def extract_config(
    custom_checkout_source: str,
    *,
    fallback_sources: list[str] | None = None,
) -> StripeTokenConfig:
    if not _CAESAR_FN_RE.search(custom_checkout_source):
        raise StripeTokenExtractError("Stripe Caesar encoder pattern not found")
    checksum_match = _JS_CHECKSUM_RE.search(custom_checkout_source)
    if not checksum_match:
        raise StripeTokenExtractError("Stripe js_checksum builder pattern not found")
    rv_match = _RV_TIMESTAMP_RE.search(custom_checkout_source)
    if not rv_match:
        raise StripeTokenExtractError("Stripe rv_timestamp builder pattern not found")

    shift = int(checksum_match.group("shift"))
    if int(rv_match.group("shift")) != shift:
        raise StripeTokenExtractError("Stripe token shifts do not match")
    member_refs = re.findall(
        r"(\w+)\s*:\s*([a-zA-Z_$][\w$]*)\s*\.\s*([a-zA-Z_$][\w$]*)",
        rv_match.group("keys"),
    )
    if len(member_refs) != 3:
        raise StripeTokenExtractError(f"Stripe rv_timestamp layout changed: {member_refs}")

    constants_local = member_refs[0][1]
    scope = custom_checkout_source[
        max(0, rv_match.start() - 4000) : min(len(custom_checkout_source), rv_match.start() + 4000)
    ]
    module_id = next(
        (
            int(match.group("id"))
            for match in _WEBPACK_REQUIRE_RE.finditer(scope)
            if match.group("lhs") == constants_local
        ),
        None,
    )
    if module_id is None:
        raise StripeTokenExtractError("Stripe constants module ID not found")

    module_source = _extract_webpack_module(custom_checkout_source, module_id)
    if not module_source:
        module_source = next(
            (
                candidate
                for source in fallback_sources or []
                if (candidate := _extract_webpack_module(source, module_id))
            ),
            "",
        )
    if not module_source:
        raise StripeTokenExtractError(f"Stripe constants module {module_id} not found")

    constants = _extract_constants(module_source)
    expected = {member for _, _, member in member_refs}
    if missing := expected - set(constants):
        raise StripeTokenExtractError(f"Stripe constants missing: {sorted(missing)}")
    key_to_member = {key: member for key, _, member in member_refs}
    return StripeTokenConfig(
        bundle_hash=hashlib.sha256(custom_checkout_source.encode()).hexdigest(),
        shift=shift,
        rv_ts=constants[key_to_member["rvTs"]],
        rv=constants[key_to_member["rv"]],
        sv=constants[key_to_member["sv"]],
    )


_CACHE_ROOT = Path(
    os.environ.get(
        "UPI_STRIPE_TOKEN_CACHE_DIR",
        str(Path(__file__).resolve().parents[2] / "data" / "stripe_bundles"),
    )
)
_CONFIG_LOCK = RLock()
_CONFIG_CACHE: tuple[float, StripeTokenConfig] | None = None


def _cache_path(entry_hash: str) -> Path:
    path = _CACHE_ROOT / entry_hash[:16]
    path.mkdir(parents=True, exist_ok=True)
    return path


def fetch_bundles_live(
    session: Any,
    *,
    log: LogFn,
    user_agent: str,
    accept_language: str = "en-IN,en;q=0.9",
    use_cache: bool = True,
) -> tuple[str, str]:
    headers = {
        "User-Agent": user_agent,
        "Accept": "*/*",
        "Accept-Language": accept_language,
    }
    entry_response = session.get(
        "https://js.stripe.com/v3/",
        headers={**headers, "Referer": "https://chatgpt.com/"},
        timeout=30,
    )
    if entry_response.status_code != 200:
        raise StripeTokenExtractError(f"Stripe entry HTTP {entry_response.status_code}")
    entry_source = entry_response.text or ""
    entry_hash = hashlib.sha256(entry_source.encode()).hexdigest()
    cache_dir = _cache_path(entry_hash)
    checkout_cache = cache_dir / "custom_checkout.js"
    entry_cache = cache_dir / "entry.js"
    if use_cache and checkout_cache.is_file() and entry_cache.is_file():
        log(f"Stripe token bundle cache hit: {entry_hash[:16]}")
        return checkout_cache.read_text(encoding="utf-8"), entry_cache.read_text(encoding="utf-8")

    names_match = re.search(r'"fingerprinted/js/"[^}]*?\{([^}]+)\}', entry_source)
    chunk_names = {
        int(match.group(1)): match.group(2)
        for match in re.finditer(
            r'(\d+):"([a-z][a-zA-Z0-9_-]+)"',
            names_match.group(1) if names_match else "",
        )
    }
    chunk_hashes: dict[int, str] = {}
    for map_match in re.finditer(r'\{(\d+:"[a-f0-9]{20,}",?){3,40}\}', entry_source):
        chunk_hashes.update(
            {
                int(match.group(1)): match.group(2)
                for match in re.finditer(r'(\d+):"([a-f0-9]{20,})"', map_match.group(0))
            }
        )
        if chunk_hashes:
            break
    checkout_id = next(
        (chunk_id for chunk_id, name in chunk_names.items() if name == "custom-checkout"),
        None,
    )
    checkout_hash = chunk_hashes.get(checkout_id) if checkout_id is not None else None
    if checkout_id is None or not checkout_hash:
        raise StripeTokenExtractError("Stripe custom-checkout chunk not found")

    checkout_url = (
        "https://js.stripe.com/v3/fingerprinted/js/"
        f"custom-checkout-{checkout_hash}.js"
    )
    checkout_response = session.get(
        checkout_url,
        headers={
            **headers,
            "Referer": "https://js.stripe.com/v3/",
            "Sec-Fetch-Dest": "script",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "same-origin",
        },
        timeout=60,
    )
    if checkout_response.status_code != 200:
        raise StripeTokenExtractError(f"Stripe custom-checkout HTTP {checkout_response.status_code}")
    checkout_source = checkout_response.text or ""
    if use_cache:
        checkout_cache.write_text(checkout_source, encoding="utf-8")
        entry_cache.write_text(entry_source, encoding="utf-8")
    log(f"Stripe token bundle loaded: {entry_hash[:16]}")
    return checkout_source, entry_source


def extract_config_live(
    session: Any,
    *,
    log: LogFn,
    user_agent: str,
    accept_language: str = "en-IN,en;q=0.9",
    use_cache: bool = True,
) -> StripeTokenConfig:
    global _CONFIG_CACHE
    ttl = max(60, int(os.environ.get("UPI_STRIPE_TOKEN_CACHE_TTL", "3600") or 3600))
    with _CONFIG_LOCK:
        if _CONFIG_CACHE and time.time() - _CONFIG_CACHE[0] < ttl:
            return _CONFIG_CACHE[1]
        checkout_source, entry_source = fetch_bundles_live(
            session,
            log=log,
            user_agent=user_agent,
            accept_language=accept_language,
            use_cache=use_cache,
        )
        config = extract_config(checkout_source, fallback_sources=[entry_source])
        _CONFIG_CACHE = (time.time(), config)
        return config


def compute_js_checksum(ppage_id: str, *, shift: int = 11) -> str:
    return caesar_shift(stripe_encode(_js_stringify({"id": ppage_id})), shift)


def compute_rv_timestamp(config: StripeTokenConfig) -> str:
    payload = _js_stringify({"rvTs": config.rv_ts, "rv": config.rv, "sv": config.sv})
    return caesar_shift(stripe_encode(payload), config.shift)


def build_token_fields(
    *,
    ppage_id: str,
    config: StripeTokenConfig,
) -> dict[str, str]:
    if not ppage_id:
        raise StripeTokenExtractError("Stripe payment page ID is empty")
    return {
        "js_checksum": compute_js_checksum(ppage_id, shift=config.shift),
        "rv_timestamp": compute_rv_timestamp(config),
    }
