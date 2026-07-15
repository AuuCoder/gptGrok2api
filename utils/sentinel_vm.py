"""Run the official Sentinel SDK's PoW/Turnstile path in a Node VM.

The VM has no network access. Python keeps ownership of the curl_cffi session,
its TLS fingerprint, proxy, and cookies; it only gives the SDK challenge data
and receives the generated ``p``/``t`` values through stdin/stdout.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable

from utils.turnstile import solve_turnstile_token


SENTINEL_VERSION = "20260423af3c"
SENTINEL_ORIGIN = "https://chatgpt.com"
SENTINEL_BOOTSTRAP_URL = f"{SENTINEL_ORIGIN}/backend-api/sentinel/sdk.js"
SENTINEL_SDK_URL = f"{SENTINEL_ORIGIN}/sentinel/{SENTINEL_VERSION}/sdk.js"
SENTINEL_REQ_URL = f"{SENTINEL_ORIGIN}/backend-api/sentinel/req"
RUNNER_PATH = Path(__file__).with_name("openai_sentinel_vm.js")
MAX_SDK_BYTES = 4 * 1024 * 1024
MIN_TURNSTILE_TOKEN_LENGTH = 16
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)
DEFAULT_SEC_CH_UA = '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"'


_NODE_WRAPPER = r"""
const fs = require('fs');
const sdkFile = process.env.OPENAI_SENTINEL_SDK_FILE;
const runnerFile = process.env.OPENAI_SENTINEL_VM_RUNNER;
const timeoutMs = Number(process.env.OPENAI_SENTINEL_VM_TIMEOUT_MS || '30000');
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => { input += chunk; });
process.stdin.on('end', async () => {
  try {
    globalThis.__payload_json = input;
    globalThis.__sdk_source = fs.readFileSync(sdkFile, 'utf8');
    globalThis.__vm_done = false;
    globalThis.__vm_output_json = '';
    globalThis.__vm_error = '';
    eval(fs.readFileSync(runnerFile, 'utf8'));
    const started = Date.now();
    while (!globalThis.__vm_done) {
      if (Date.now() - started > timeoutMs) throw new Error('Sentinel VM timeout');
      await new Promise((resolve) => setTimeout(resolve, 1));
    }
    if (String(globalThis.__vm_error || '').trim()) throw new Error(String(globalThis.__vm_error));
    process.stdout.write(String(globalThis.__vm_output_json || ''));
  } catch (error) {
    process.stderr.write(String((error && error.stack) || error));
    process.exit(1);
  }
});
""".strip()


def _node_binary() -> str:
    configured = str(os.getenv("OPENAI_SENTINEL_NODE_PATH") or "").strip()
    candidates = [configured, shutil.which("node") or "", "/opt/homebrew/bin/node", "/usr/local/bin/node"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError("Node.js is unavailable; set OPENAI_SENTINEL_NODE_PATH")


def _cache_file(version: str) -> Path:
    folder = Path(tempfile.gettempdir()) / "chatgpt2api-sentinel" / version
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "sdk.js"


def _ensure_sdk(session: Any, *, timeout_seconds: int) -> Path:
    version = SENTINEL_VERSION
    sdk_url = SENTINEL_SDK_URL
    try:
        bootstrap = session.get(
            SENTINEL_BOOTSTRAP_URL,
            headers={
                "accept": "*/*",
                "accept-language": "pt-BR,pt;q=0.9,en;q=0.8",
                "referer": f"{SENTINEL_ORIGIN}/",
                "sec-fetch-dest": "script",
                "sec-fetch-mode": "no-cors",
                "sec-fetch-site": "same-origin",
            },
            timeout=timeout_seconds,
            verify=False,
        )
        if getattr(bootstrap, "status_code", 0) == 200:
            source = str(getattr(bootstrap, "text", "") or "")
            match = re.search(
                r"https://chatgpt\.com/sentinel/([A-Za-z0-9_-]+)/sdk\.js",
                source,
            )
            if match:
                version = match.group(1)
                sdk_url = match.group(0)
    except Exception:
        pass

    cache_file = _cache_file(version)
    if cache_file.is_file() and 0 < cache_file.stat().st_size <= MAX_SDK_BYTES:
        return cache_file
    response = session.get(
        sdk_url,
        headers={
            "accept": "*/*",
            "accept-language": "pt-BR,pt;q=0.9,en;q=0.8",
            "referer": f"{SENTINEL_ORIGIN}/backend-api/sentinel/frame.html?sv={version}",
            "sec-fetch-dest": "script",
            "sec-fetch-mode": "no-cors",
            "sec-fetch-site": "same-origin",
        },
        timeout=timeout_seconds,
        verify=False,
    )
    if getattr(response, "status_code", 0) != 200:
        raise RuntimeError(f"sentinel_sdk_http_{getattr(response, 'status_code', 'unknown')}")
    source = bytes(getattr(response, "content", b"") or b"")
    if not source:
        source = str(getattr(response, "text", "") or "").encode("utf-8")
    if not source or len(source) > MAX_SDK_BYTES:
        raise RuntimeError("invalid_sentinel_sdk_size")
    cache_file.write_bytes(source)
    return cache_file


def _run_action(*, sdk_file: Path, payload: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    if not RUNNER_PATH.is_file():
        raise RuntimeError("sentinel_vm_runner_missing")
    process = subprocess.run(
        [_node_binary(), "-e", _NODE_WRAPPER],
        input=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        text=True,
        capture_output=True,
        timeout=max(10, timeout_seconds + 5),
        env={
            **os.environ,
            "OPENAI_SENTINEL_SDK_FILE": str(sdk_file),
            "OPENAI_SENTINEL_VM_RUNNER": str(RUNNER_PATH),
            "OPENAI_SENTINEL_VM_TIMEOUT_MS": str(timeout_seconds * 1000),
            "TZ": os.environ.get("OPENAI_SENTINEL_TIMEZONE", "America/Sao_Paulo"),
        },
    )
    if process.returncode != 0:
        raise RuntimeError(f"sentinel_vm_failed: {(process.stderr or process.stdout or 'unknown')[:240]}")
    try:
        result = json.loads(str(process.stdout or ""))
    except json.JSONDecodeError as exc:
        raise RuntimeError("sentinel_vm_invalid_json") from exc
    if not isinstance(result, dict):
        raise RuntimeError("sentinel_vm_invalid_result")
    return result


def _fetch_challenge(
    session: Any,
    *,
    device_id: str,
    flow: str,
    request_p: str,
    user_agent: str,
    sec_ch_ua: str,
    sdk_version: str,
    timeout_seconds: int,
) -> tuple[dict[str, Any], str]:
    before_cookie = ""
    try:
        before_cookie = str(session.cookies.get("oai-sc") or "")
    except Exception:
        pass
    sec_ch_ua_platform = '"macOS"' if "Macintosh" in user_agent else '"Windows"'
    response = session.post(
        SENTINEL_REQ_URL,
        data=json.dumps({"p": request_p, "id": device_id, "flow": flow}, separators=(",", ":")),
        headers={
            "accept": "*/*",
            "accept-encoding": "gzip, deflate, br, zstd",
            "accept-language": "pt-BR,pt;q=0.9,en;q=0.8",
            "content-type": "text/plain;charset=UTF-8",
            "origin": SENTINEL_ORIGIN,
            "referer": f"{SENTINEL_ORIGIN}/backend-api/sentinel/frame.html?sv={sdk_version}",
            "sec-ch-ua": sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": sec_ch_ua_platform,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": user_agent,
        },
        timeout=timeout_seconds,
        verify=False,
    )
    if getattr(response, "status_code", 0) != 200:
        raise RuntimeError(f"sentinel_req_http_{getattr(response, 'status_code', 'unknown')}")
    try:
        challenge = response.json()
    except Exception as exc:
        raise RuntimeError("sentinel_req_invalid_json") from exc
    if not isinstance(challenge, dict) or not str(challenge.get("token") or "").strip():
        raise RuntimeError("sentinel_req_missing_token")
    oai_sc = _response_cookie_value(response, "oai-sc")
    if not oai_sc:
        try:
            candidate = str(session.cookies.get("oai-sc") or "")
        except Exception:
            candidate = ""
        if candidate and candidate != before_cookie:
            oai_sc = candidate
    if not oai_sc:
        raise RuntimeError("sentinel_req_missing_oai_sc")
    return challenge, oai_sc


def _response_cookie_value(response: Any, name: str) -> str:
    headers = getattr(response, "headers", None)
    raw_values: list[str] = []
    if headers is not None:
        for method_name in ("get_list", "getlist"):
            method = getattr(headers, method_name, None)
            if callable(method):
                try:
                    raw_values.extend(str(value) for value in method("set-cookie") or [])
                except Exception:
                    pass
        try:
            raw = headers.get("set-cookie") or headers.get("Set-Cookie") or ""
            if raw:
                raw_values.append(str(raw))
        except Exception:
            pass
    pattern = re.compile(rf"(?:^|[\r\n])\s*{re.escape(name)}=([^;\r\n]+)", re.IGNORECASE)
    for raw in raw_values:
        match = pattern.search(raw)
        if match:
            return match.group(1).strip()
    return ""


def get_sentinel_token_via_vm(
    session: Any,
    device_id: str,
    flow: str,
    *,
    user_agent: str = "",
    sec_ch_ua: str = "",
    timeout_seconds: int = 30,
    log: Callable[[str], None] | None = None,
) -> tuple[str, str] | None:
    """Return a real-SDK primary Sentinel token, or ``None`` on a safe fallback."""
    log = log or (lambda _message: None)
    did = str(device_id or uuid.uuid4()).strip()
    ua = str(user_agent or DEFAULT_USER_AGENT).strip()
    ch_ua = str(sec_ch_ua or DEFAULT_SEC_CH_UA).strip()
    timeout_seconds = max(10, min(45, int(timeout_seconds)))
    try:
        sdk_file = _ensure_sdk(session, timeout_seconds=timeout_seconds)
        sdk_version = sdk_file.parent.name
        sdk_url = f"{SENTINEL_ORIGIN}/sentinel/{sdk_version}/sdk.js"
        frame_url = f"{SENTINEL_ORIGIN}/backend-api/sentinel/frame.html?sv={sdk_version}"
        requirements = _run_action(
            sdk_file=sdk_file,
            payload={
                "action": "requirements",
                "device_id": did,
                "user_agent": ua,
                "sdk_url": sdk_url,
                "frame_url": frame_url,
            },
            timeout_seconds=timeout_seconds,
        )
        request_p = str(requirements.get("request_p") or "").strip()
        if not request_p:
            raise RuntimeError("sentinel_vm_missing_requirements")
        challenge, oai_sc = _fetch_challenge(
            session,
            device_id=did,
            flow=flow,
            request_p=request_p,
            user_agent=ua,
            sec_ch_ua=ch_ua,
            sdk_version=sdk_version,
            timeout_seconds=timeout_seconds,
        )
        solved = _run_action(
            sdk_file=sdk_file,
            payload={
                "action": "solve",
                "device_id": did,
                "user_agent": ua,
                "request_p": request_p,
                "challenge": challenge,
                "sdk_url": sdk_url,
                "frame_url": frame_url,
            },
            timeout_seconds=timeout_seconds,
        )
        final_p = str(solved.get("final_p") or "").strip()
        raw_turnstile = solved.get("t")
        turnstile = "" if raw_turnstile is None else str(raw_turnstile).strip()
        if len(turnstile) < MIN_TURNSTILE_TOKEN_LENGTH:
            turnstile_data = challenge.get("turnstile") or {}
            dx = str(turnstile_data.get("dx") or "") if isinstance(turnstile_data, dict) else ""
            protocol_turnstile = solve_turnstile_token(dx, request_p) if dx else None
            if protocol_turnstile:
                turnstile = str(protocol_turnstile).strip()
                log(f"Sentinel VM replaced malformed Turnstile value (t_len={len(turnstile)})")
        challenge_token = str(challenge.get("token") or "").strip()
        if not final_p or not turnstile or not challenge_token:
            raise RuntimeError("sentinel_vm_missing_solution")
        result = json.dumps(
            {"p": final_p, "t": turnstile, "c": challenge_token, "id": did, "flow": flow},
            separators=(",", ":"),
        )
        log(
            "Sentinel VM success "
            f"(sdk={sdk_version}, p_len={len(final_p)}, t_len={len(turnstile)}, "
            f"c_len={len(challenge_token)}, oai_sc_len={len(oai_sc)})"
        )
        return result, oai_sc
    except Exception as exc:
        log(f"Sentinel VM fallback: {type(exc).__name__}: {str(exc)[:160]}")
        return None
