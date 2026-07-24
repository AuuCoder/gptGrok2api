from __future__ import annotations

import atexit
import hashlib
import json
import math
import os
import re
import selectors
import shutil
import struct
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, replace
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlparse

from curl_cffi import requests

from services.config import DATA_DIR


DEFAULT_BASE_URL = "https://accounts.x.ai"
DEFAULT_SIGNUP_PATH = "/sign-up?redirect=grok-com"
REFERENCE_SIGNUP_PATH = "/sign-up?redirect=cloud-console"
DEFAULT_ACTION_ID = "7f50061dd2f5b389a530e4a048d5fdf0c48d1d9259"
DEFAULT_CASTLE_PK = "pk_p8GGWvD3TmFJZRsX3BQcqAv9aFVispNz"
DEFAULT_TURNSTILE_SITEKEY = "0x4AAAAAAAhr9JGVDZbrZOo0"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
REFERENCE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
DEFAULT_ROUTER_STATE_TREE = [
    "",
    {
        "children": [
            "(app)",
            {
                "children": [
                    "(auth)",
                    {
                        "children": [
                            "sign-up",
                            {"children": ["__PAGE__", {}]},
                        ]
                    },
                ]
            },
        ]
    },
]

_RUNNER_PATH = Path(__file__).with_name("grok_castle_runner.js")
_VENDOR_SDK_PATH = Path(__file__).with_name("vendor") / "castle_sdk.js"
_DISCOVERY_TTL_SECONDS = 600
_DISCOVERY_LOCK = threading.Lock()
_DISCOVERY_CACHE: dict[str, tuple[float, "SignupMetadata"]] = {}
_SESSION_REDIRECT_HOSTS = (
    "accounts.x.ai",
    "auth.x.ai",
    "console.x.ai",
    "grok.com",
    "auth.grokusercontent.com",
    "auth.grokipedia.com",
)


class GrokProtocolError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stage: str = "protocol",
        retryable: bool = False,
        mail_retryable: bool = False,
        reason_code: str = "",
        account_created: bool = False,
        partial_result: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.stage = stage
        self.retryable = bool(retryable)
        self.mail_retryable = bool(mail_retryable)
        self.reason_code = str(reason_code or "").strip()
        self.account_created = bool(account_created)
        self.partial_result = dict(partial_result or {})
        self.partial_persisted = False


class GrpcWebError(GrokProtocolError):
    def __init__(self, status: int, message: str = ""):
        detail = unquote(str(message or "")).strip()
        text = f"gRPC-Web status {status}"
        if detail:
            text = f"{text}: {detail}"
        super().__init__(text, stage="grpc", mail_retryable=_looks_like_mail_error(detail))
        self.status = int(status)
        self.grpc_message = detail


@dataclass(frozen=True)
class GrpcWebResult:
    messages: tuple[bytes, ...]
    trailers: dict[str, str]
    status: int
    message: str


@dataclass(frozen=True)
class SignupMetadata:
    signup_url: str
    action_id: str
    sitekey: str
    castle_pk: str
    router_state_tree: list[Any]
    castle_sdk_url: str
    castle_sdk_path: str


@dataclass(frozen=True)
class SessionExchangeResult:
    redirect_url: str
    final_url: str
    status_code: int
    hops: int
    reason_code: str
    sso: str = ""
    sso_rw: str = ""


def _looks_like_mail_error(message: str) -> bool:
    lowered = str(message or "").lower()
    markers = (
        "account_email_domain",
        "account_email_in_use",
        "disposable email",
        "email domain",
        "email_in_use",
        "email malformed",
        "invalid email",
        "邮箱域名",
        "邮箱已",
    )
    return any(marker in lowered for marker in markers)


def encode_varint(value: int) -> bytes:
    value = int(value)
    if value < 0:
        raise ValueError("varint 不支持负数")
    result = bytearray()
    while True:
        part = value & 0x7F
        value >>= 7
        result.append(part | (0x80 if value else 0))
        if not value:
            return bytes(result)


def decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift < 70:
        part = data[offset]
        offset += 1
        value |= (part & 0x7F) << shift
        if not part & 0x80:
            return value, offset
        shift += 7
    raise ValueError("无效或截断的 protobuf varint")


def protobuf_string(field_number: int, value: str) -> bytes:
    payload = str(value).encode("utf-8")
    return encode_varint((int(field_number) << 3) | 2) + encode_varint(len(payload)) + payload


def protobuf_bool(field_number: int, value: bool) -> bytes:
    return encode_varint(int(field_number) << 3) + encode_varint(1 if value else 0)


def parse_protobuf_fields(payload: bytes) -> dict[int, list[Any]]:
    fields: dict[int, list[Any]] = {}
    offset = 0
    while offset < len(payload):
        key, offset = decode_varint(payload, offset)
        field_number = key >> 3
        wire_type = key & 0x07
        if not field_number:
            raise ValueError("protobuf field number 不能为 0")
        if wire_type == 0:
            value, offset = decode_varint(payload, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(payload):
                raise ValueError("截断的 protobuf fixed64")
            value = payload[offset:end]
            offset = end
        elif wire_type == 2:
            size, offset = decode_varint(payload, offset)
            end = offset + size
            if end > len(payload):
                raise ValueError("截断的 protobuf bytes")
            value = payload[offset:end]
            offset = end
        elif wire_type == 5:
            end = offset + 4
            if end > len(payload):
                raise ValueError("截断的 protobuf fixed32")
            value = payload[offset:end]
            offset = end
        else:
            raise ValueError(f"不支持的 protobuf wire type: {wire_type}")
        fields.setdefault(field_number, []).append(value)
    return fields


def grpc_web_envelope(payload: bytes, flags: int = 0) -> bytes:
    return bytes([flags & 0xFF]) + struct.pack(">I", len(payload)) + payload


def parse_grpc_web_frames(payload: bytes) -> list[tuple[int, bytes]]:
    frames: list[tuple[int, bytes]] = []
    offset = 0
    while offset < len(payload):
        if offset + 5 > len(payload):
            raise ValueError("截断的 gRPC-Web frame header")
        flags = payload[offset]
        size = struct.unpack(">I", payload[offset + 1 : offset + 5])[0]
        start = offset + 5
        end = start + size
        if end > len(payload):
            raise ValueError("截断的 gRPC-Web frame body")
        frames.append((flags, payload[start:end]))
        offset = end
    return frames


def _normalized_headers(headers: Any) -> dict[str, str]:
    if not headers:
        return {}
    try:
        items = headers.items()
    except AttributeError:
        items = headers
    return {str(key).lower(): str(value) for key, value in items}


def _header_values(headers: Any, name: str) -> list[str]:
    if not headers:
        return []
    for method_name in ("get_list", "getlist"):
        method = getattr(headers, method_name, None)
        if callable(method):
            try:
                values = method(name)
            except Exception:
                values = []
            if values:
                return [str(value) for value in values if str(value).strip()]
    value = _normalized_headers(headers).get(str(name).lower(), "").strip()
    return [value] if value else []


def _parameter_summary(value: str) -> str:
    parts = []
    for name, item in parse_qsl(str(value or ""), keep_blank_values=True):
        digest = hashlib.sha256(item.encode("utf-8")).hexdigest()[:8] if item else "empty"
        parts.append(f"{name}(len={len(item)},sha256={digest})")
    return ",".join(parts)


def summarize_sensitive_url(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}" if parsed.scheme and parsed.netloc else ""
    summary = f"{origin}{parsed.path or '/'}"
    query = _parameter_summary(parsed.query)
    fragment = _parameter_summary(parsed.fragment)
    if query:
        summary += f"?{query}"
    elif parsed.query:
        summary += "?opaque"
    if fragment:
        summary += f"#{fragment}"
    elif parsed.fragment:
        summary += "#opaque"
    return summary


def _is_allowed_session_url(value: object) -> bool:
    parsed = urlparse(str(value or "").strip())
    hostname = str(parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https" or not hostname:
        return False
    return any(hostname == allowed or hostname.endswith(f".{allowed}") for allowed in _SESSION_REDIRECT_HOSTS)


def _redirect_referer(current_url: str, next_url: str) -> str:
    current = urlparse(str(current_url or ""))
    target = urlparse(str(next_url or ""))
    if (current.scheme.lower(), current.netloc.lower()) == (target.scheme.lower(), target.netloc.lower()):
        return current_url
    if current.scheme and current.netloc:
        return f"{current.scheme.lower()}://{current.netloc.lower()}/"
    return ""


def _set_cookie_names(headers: Any) -> list[str]:
    names: set[str] = set()
    for raw in _header_values(headers, "set-cookie"):
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            cookie = SimpleCookie()
        names.update(str(name) for name in cookie.keys())
        if not cookie:
            match = re.match(r"\s*([^=;,\s]+)=", raw)
            if match:
                names.add(match.group(1))
    return sorted(names)


def _response_cookie_value_for_domain(response: Any, domain: str, *names: str) -> str:
    target = str(domain or "").lower().lstrip(".")
    response_host = str(urlparse(str(getattr(response, "url", "") or "")).hostname or "").lower()
    for raw in _header_values(getattr(response, "headers", {}), "set-cookie"):
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            continue
        for name in names:
            morsel = cookie.get(name)
            if morsel is None or not str(morsel.value).strip():
                continue
            cookie_domain = str(morsel["domain"] or response_host).lower().lstrip(".")
            cookie_path = str(morsel["path"] or "")
            host_can_set_domain = response_host == cookie_domain or response_host.endswith(f".{cookie_domain}")
            if cookie_domain == target and host_can_set_domain and cookie_path in {"", "/"}:
                return str(morsel.value).strip()
    return ""


def _parse_grpc_trailer(payload: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in payload.decode("utf-8", errors="replace").replace("\r\n", "\n").split("\n"):
        key, sep, value = line.partition(":")
        if sep and key.strip():
            result[key.strip().lower()] = value.strip()
    return result


def decode_grpc_web_response(payload: bytes, headers: Any = None) -> GrpcWebResult:
    normalized = _normalized_headers(headers)
    messages: list[bytes] = []
    trailers: dict[str, str] = {}
    for flags, body in parse_grpc_web_frames(payload):
        if flags & 0x80:
            trailers.update(_parse_grpc_trailer(body))
        else:
            messages.append(body)

    raw_status = trailers.get("grpc-status", normalized.get("grpc-status", "0"))
    raw_message = trailers.get("grpc-message", normalized.get("grpc-message", ""))
    try:
        status = int(str(raw_status or "0").strip())
    except ValueError:
        status = 2
        raw_message = raw_message or f"invalid grpc-status: {raw_status}"
    message = unquote(str(raw_message or ""))
    if status != 0:
        raise GrpcWebError(status, message)
    return GrpcWebResult(tuple(messages), trailers, status, message)


def create_email_validation_request(email: str, castle_request_token: str) -> bytes:
    payload = protobuf_string(1, email)
    if castle_request_token:
        payload += protobuf_string(3, castle_request_token)
    return payload


def verify_email_validation_request(email: str, code: str) -> bytes:
    return protobuf_string(1, email) + protobuf_string(2, code)


def validate_password_request(email: str, password: str) -> bytes:
    return protobuf_string(4, email) + protobuf_string(5, password)


def parse_verification_token(message: bytes) -> str:
    fields = parse_protobuf_fields(message)
    values = fields.get(1) or []
    if not values or not isinstance(values[0], bytes):
        return ""
    return values[0].decode("utf-8", errors="replace").strip()


_NEXT_PAYLOAD_RE = re.compile(
    r"self\.__next_f\.push\(\s*\[\s*1\s*,\s*(\"(?:\\.|[^\"\\])*\")\s*\]\s*\)",
    re.S,
)


def decode_next_f_payloads(html: str) -> list[str]:
    result: list[str] = []
    for encoded in _NEXT_PAYLOAD_RE.findall(str(html or "")):
        try:
            value = json.loads(encoded)
        except json.JSONDecodeError:
            continue
        if isinstance(value, str):
            result.append(value)
    return result


def extract_script_urls(html: str, page_url: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    pattern = re.compile(r"<script\b[^>]*\bsrc\s*=\s*(['\"])(.*?)\1", re.I | re.S)
    for _quote, raw_url in pattern.findall(str(html or "")):
        url = urljoin(page_url, raw_url.strip())
        if url and url not in seen:
            seen.add(url)
            result.append(url)
    return result


def _find_router_node(value: Any) -> list[Any] | None:
    if isinstance(value, list):
        if len(value) >= 2 and value[0] == "" and isinstance(value[1], dict) and "children" in value[1]:
            return value
        for item in value:
            found = _find_router_node(item)
            if found is not None:
                return found
    elif isinstance(value, dict):
        for item in value.values():
            found = _find_router_node(item)
            if found is not None:
                return found
    return None


def _prune_router_node(value: Any) -> Any:
    if not isinstance(value, list) or len(value) < 2 or not isinstance(value[1], dict):
        return value
    segment = value[0]
    if isinstance(segment, str) and (segment.startswith("__PAGE__?") or segment.startswith("PAGE?")):
        segment = "__PAGE__"
    children: dict[str, Any] = {}
    for key, child in value[1].items():
        children[str(key)] = _prune_router_node(child)
    return [segment, children]


def extract_next_router_state_tree(html: str) -> list[Any]:
    for payload in decode_next_f_payloads(html):
        if not payload.startswith("0:"):
            continue
        try:
            root = json.loads(payload[2:])
        except json.JSONDecodeError:
            continue
        found = _find_router_node(root)
        if found is not None:
            return _prune_router_node(found)
    return json.loads(json.dumps(DEFAULT_ROUTER_STATE_TREE))


def _extract_named_value(html: str, name: str, value_pattern: str) -> str:
    haystacks = [str(html or ""), *decode_next_f_payloads(html)]
    patterns = (
        re.compile(rf"[\"']?{re.escape(name)}[\"']?\s*:\s*[\"']({value_pattern})[\"']", re.I),
        re.compile(rf"{re.escape(name)}\\?[\"']\s*:\s*\\?[\"']({value_pattern})", re.I),
    )
    for source in haystacks:
        for pattern in patterns:
            match = pattern.search(source)
            if match:
                return match.group(1)
    return ""


def extract_turnstile_sitekey(html: str) -> str:
    return _extract_named_value(html, "sitekey", r"0x[A-Za-z0-9_-]+")


def extract_castle_pk(html: str) -> str:
    return _extract_named_value(html, "castlePk", r"pk_[A-Za-z0-9_-]+")


def extract_action_id(source: str) -> str:
    text = str(source or "")
    if "createServerReference" not in text:
        return ""
    ids = re.findall(r"createServerReference\)\(\s*[\"']([0-9a-f]{40,64})[\"']", text, re.I)
    if not ids:
        ids = re.findall(r"createServerReference\(\s*[\"']([0-9a-f]{40,64})[\"']", text, re.I)
    if not ids:
        return ""
    if "emailValidationCode" in text and "createUserAndSessionRequest" in text:
        return ids[-1]
    return ids[0] if len(ids) == 1 else ""


def extract_castle_lazy_chunk(source: str) -> str:
    text = str(source or "")
    if "CastleProvider" not in text or "createRequestToken" not in text:
        return ""
    module_match = re.search(r"\.A\((\d+)\)", text)
    if not module_match:
        return ""
    module_id = module_match.group(1)
    mapping_start = text.rfind(f"{module_id},")
    if mapping_start < 0:
        mapping_start = text.find(f"{module_id},", module_match.end())
    if mapping_start < 0:
        return ""
    window = text[mapping_start : mapping_start + 4000]
    chunk_match = re.search(r"[\"'](static/chunks/[^\"']+\.js)[\"']", window)
    return chunk_match.group(1) if chunk_match else ""


def parse_flight_records(text: str | bytes) -> dict[str, str]:
    data = bytes(text) if isinstance(text, (bytes, bytearray)) else str(text or "").encode("utf-8")
    records: dict[str, str] = {}
    offset = 0
    while offset < len(data):
        while offset < len(data) and data[offset] in (10, 13):
            offset += 1
        record_start = offset
        while offset < len(data) and chr(data[offset]).lower() in "0123456789abcdef":
            offset += 1
        if offset == record_start or offset >= len(data) or data[offset] != 58:
            newline = data.find(b"\n", offset)
            offset = len(data) if newline < 0 else newline + 1
            continue
        record_id = data[record_start:offset].decode("ascii").lower()
        offset += 1

        # React Flight uses T<hex byte length>,<text> for long strings. These
        # records are length-delimited and do not need a trailing newline.
        if offset < len(data) and data[offset] == 84:
            tag_start = offset
            offset += 1
            length_start = offset
            while offset < len(data) and chr(data[offset]).lower() in "0123456789abcdef":
                offset += 1
            if length_start < offset and offset < len(data) and data[offset] == 44:
                text_size = int(data[length_start:offset].decode("ascii"), 16)
                offset += 1
                text_end = offset + text_size
                if text_end <= len(data):
                    raw = data[tag_start:offset] + data[offset:text_end]
                    records[record_id] = raw.decode("utf-8", errors="replace")
                    offset = text_end
                    continue
            offset = tag_start

        newline = data.find(b"\n", offset)
        line_end = len(data) if newline < 0 else newline
        records[record_id] = data[offset:line_end].rstrip(b"\r").decode("utf-8", errors="replace")
        offset = len(data) if newline < 0 else newline + 1
    return records


def _decode_flight_value(raw: str) -> Any:
    source = str(raw or "")
    text_match = re.fullmatch(r"T([0-9a-f]+),(.*)", source, re.I | re.S)
    if text_match:
        return text_match.group(2)
    value = source.strip()
    if not value:
        return None
    if value[0] in "IEDHL" and len(value) > 1 and value[1] in "{[\"":
        value = value[1:]
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _flight_reference_id(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    match = re.fullmatch(r"\$(?:@|L)?([0-9a-f]+)", value.strip(), re.I)
    return match.group(1).lower() if match else ""


def _resolve_flight_record(records: dict[str, str], record_id: str, seen: set[str] | None = None) -> Any:
    key = str(record_id or "").lower()
    visited = set(seen or ())
    if not key or key in visited:
        return None
    visited.add(key)
    decoded = _decode_flight_value(records.get(key, ""))
    reference_id = _flight_reference_id(decoded)
    if reference_id:
        return _resolve_flight_record(records, reference_id, visited)
    return decoded


def parse_flight_result(text: str | bytes) -> Any:
    records = parse_flight_records(text)
    root = _decode_flight_value(records.get("0", ""))
    if isinstance(root, dict):
        ref = root.get("a")
        target = _flight_reference_id(ref)
        if target:
            decoded = _resolve_flight_record(records, target)
            if decoded is not None:
                return decoded
    for record_id in records:
        decoded = _resolve_flight_record(records, record_id)
        if isinstance(decoded, dict) and any(key in decoded for key in ("error", "signInMethods")):
            return decoded
        if isinstance(decoded, str) and (decoded.startswith("http://") or decoded.startswith("https://") or decoded.startswith("/")):
            return decoded
    return None


def _summarize_server_action_response(response: Any, action_id: str = "") -> str:
    headers = _normalized_headers(getattr(response, "headers", {}))
    content = bytes(getattr(response, "content", b"") or b"")
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower() or "missing"
    body_digest = hashlib.sha256(content).hexdigest()[:12]
    parts = [f"type={content_type}", f"body={len(content)}B", f"sha256={body_digest}"]
    cf_ray = headers.get("cf-ray", "").strip()
    if cf_ray and re.fullmatch(r"[A-Za-z0-9-]{1,80}", cf_ray):
        parts.append(f"cf-ray={cf_ray}")
    if action_id:
        action_digest = hashlib.sha256(str(action_id).encode("utf-8")).hexdigest()[:8]
        parts.append(f"action-sha256={action_digest}")
    return ", ".join(parts)


def extract_action_redirect(headers: Any) -> str:
    raw = _normalized_headers(headers).get("x-action-redirect", "").strip()
    if not raw:
        return ""
    target, separator, mode = raw.rpartition(";")
    if separator and mode.strip().lower() in {"push", "replace"}:
        raw = target
    return raw.strip()


class CastleRunner:
    def __init__(self, runner_path: Path = _RUNNER_PATH):
        self.runner_path = Path(runner_path)
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._request_id = 0

    def _start(self) -> subprocess.Popen[str]:
        node = shutil.which("node")
        if not node:
            raise GrokProtocolError("Grok Castle 运行需要 Node.js", stage="castle")
        if not self.runner_path.is_file():
            raise GrokProtocolError(f"Castle runner 不存在: {self.runner_path}", stage="castle")
        process = subprocess.Popen(
            [node, str(self.runner_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._process = process
        return process

    def _stop_unlocked(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=2)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            self._stop_unlocked()

    def create_token(
        self,
        *,
        sdk_path: str,
        pk: str,
        page_url: str,
        user_agent: str,
        timeout: float,
    ) -> str:
        with self._lock:
            for attempt in range(2):
                process = self._process
                if process is None or process.poll() is not None:
                    self._stop_unlocked()
                    process = self._start()
                self._request_id += 1
                request_id = str(self._request_id)
                request = {
                    "id": request_id,
                    "sdkPath": str(sdk_path),
                    "pk": str(pk),
                    "url": str(page_url),
                    "referrer": "https://grok.com/",
                    "userAgent": str(user_agent),
                    "timeoutMs": max(1000, int(float(timeout) * 1000)),
                }
                try:
                    assert process.stdin is not None and process.stdout is not None
                    process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
                    process.stdin.flush()
                    selector = selectors.DefaultSelector()
                    selector.register(process.stdout, selectors.EVENT_READ)
                    ready = selector.select(timeout=max(1.0, float(timeout) + 2.0))
                    selector.close()
                    if not ready:
                        raise TimeoutError("Castle runner timeout")
                    line = process.stdout.readline()
                    if not line:
                        raise RuntimeError("Castle runner exited")
                    response = json.loads(line)
                    if str(response.get("id")) != request_id:
                        raise RuntimeError("Castle runner response id mismatch")
                    if response.get("ok") is not True:
                        raise RuntimeError(str(response.get("error") or "Castle token failed"))
                    token = str(response.get("token") or "").strip()
                    if not token:
                        raise RuntimeError("Castle token 为空")
                    return token
                except Exception as exc:
                    self._stop_unlocked()
                    if attempt:
                        raise GrokProtocolError(f"Castle token 生成失败: {exc}", stage="castle", retryable=True) from exc
            raise GrokProtocolError("Castle token 生成失败", stage="castle", retryable=True)


_CASTLE_RUNNER = CastleRunner()
atexit.register(_CASTLE_RUNNER.close)


class TurnstileSolver:
    def __init__(
        self,
        config: dict[str, Any],
        *,
        transport: Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]] | None = None,
    ):
        self.config = dict(config or {})
        self.provider = str(self.config.get("provider") or "yescaptcha").strip().lower()
        self.api_key = str(self.config.get("api_key") or "").strip()
        self.timeout = max(10.0, float(self.config.get("captcha_timeout") or 180))
        self.poll_interval = max(0.2, float(self.config.get("captcha_poll_interval") or 3))
        self.request_timeout = max(1.0, float(self.config.get("request_timeout") or 30))
        self.transport = transport
        self.session = requests.Session(impersonate="chrome120", trust_env=False)

    def close(self) -> None:
        self.session.close()

    def _base_url(self) -> str:
        configured = str(self.config.get("api_base") or "").strip().rstrip("/")
        if configured:
            return configured
        if self.provider == "yescaptcha":
            return "https://api.yescaptcha.com"
        if self.provider == "2captcha":
            return "https://api.2captcha.com"
        raise GrokProtocolError("自定义 Turnstile provider 缺少 api_base", stage="captcha")

    def _paths(self) -> tuple[str, str]:
        create_path = str(
            self.config.get("custom_create_path")
            or self.config.get("create_path")
            or "/createTask"
        ).strip()
        result_path = str(
            self.config.get("custom_result_path")
            or self.config.get("result_path")
            or "/getTaskResult"
        ).strip()
        return create_path if create_path.startswith("/") else f"/{create_path}", result_path if result_path.startswith("/") else f"/{result_path}"

    def _post(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        headers = {str(key): str(value) for key, value in (self.config.get("custom_headers") or {}).items()}
        if self.provider == "local" and self.api_key and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.transport is not None:
            data = self.transport(url, payload, headers)
            if not isinstance(data, dict):
                raise GrokProtocolError("Turnstile provider 返回结构不是对象", stage="captcha")
            return data
        response = self.session.post(
            url,
            json=payload,
            headers=headers or None,
            timeout=timeout or self.request_timeout,
        )
        try:
            data = response.json()
        except Exception as exc:
            raise GrokProtocolError(
                f"Turnstile provider 返回非 JSON（HTTP {response.status_code}）",
                stage="captcha",
                retryable=response.status_code >= 500,
            ) from exc
        if not isinstance(data, dict):
            raise GrokProtocolError("Turnstile provider 返回结构不是对象", stage="captcha")
        if not 200 <= response.status_code < 300:
            raise GrokProtocolError(
                f"Turnstile provider HTTP {response.status_code}: "
                f"{data.get('errorDescription') or data.get('error') or data.get('detail') or ''}",
                stage="captcha",
                retryable=response.status_code in {408, 409, 425, 429} or response.status_code >= 500,
            )
        return data

    @staticmethod
    def _provider_error(data: dict[str, Any]) -> str:
        error_id = data.get("errorId")
        if error_id not in (None, 0, "0"):
            return str(data.get("errorDescription") or data.get("errorCode") or f"errorId={error_id}")
        return str(data.get("error") or "").strip()

    def solve(self, *, website_url: str, sitekey: str, action: str = "") -> str:
        if self.provider == "local":
            base_url = str(self.config.get("api_base") or "http://127.0.0.1:8877").strip().rstrip("/")
            max_attempts = max(1, min(5, int(self.config.get("local_max_attempts") or 3)))
            attempt_timeout = max(15, min(120, int(self.config.get("local_attempt_timeout") or 60)))
            queue_timeout_raw = self.config.get("local_queue_timeout")
            queue_timeout = max(
                0,
                min(300, int(60 if queue_timeout_raw is None else queue_timeout_raw)),
            )
            concurrency_raw = self.config.get("local_concurrency")
            concurrency = max(0, min(64, int(2 if concurrency_raw is None else concurrency_raw)))
            proxy = str(self.config.get("proxy") or "").strip()
            deadline = time.monotonic() + self.timeout
            last_error = ""
            attempts = 0
            for attempt in range(1, max_attempts + 1):
                remaining = deadline - time.monotonic()
                if remaining < 10:
                    break
                attempts = attempt
                remaining_seconds = max(0, int(math.ceil(remaining)))
                current_timeout = max(10, min(attempt_timeout, remaining_seconds - 5))
                current_queue_timeout = max(
                    0,
                    min(queue_timeout, max(0, remaining_seconds - current_timeout - 5)),
                )
                if queue_timeout > 0 and current_queue_timeout == 0 and remaining_seconds > 15:
                    current_queue_timeout = min(queue_timeout, 10, remaining_seconds - 15)
                    current_timeout = max(10, remaining_seconds - current_queue_timeout - 5)
                payload: dict[str, Any] = {
                    "type": "turnstile",
                    "url": website_url,
                    "sitekey": sitekey,
                    "real_page": bool(self.config.get("local_real_page", True)),
                    "timeout_s": current_timeout,
                    "queue_timeout_s": current_queue_timeout,
                    "concurrency": concurrency,
                }
                if action:
                    payload["action"] = action
                if proxy and proxy.lower() != "direct":
                    payload["proxy"] = proxy
                try:
                    result = self._post(
                        f"{base_url}/solve",
                        payload,
                        timeout=max(
                            self.request_timeout,
                            current_timeout + current_queue_timeout + 10,
                        ),
                    )
                except GrokProtocolError as exc:
                    last_error = str(exc)
                    if not exc.retryable or attempt >= max_attempts:
                        raise
                    continue
                token = str(result.get("token") or "").strip()
                if result.get("solved") is True and token:
                    return token
                last_error = str(result.get("error") or result.get("detail") or "").strip()
            raise GrokProtocolError(
                f"本地 Turnstile 求解失败（{attempts or 1} 次尝试）: {last_error or '未返回 token'}",
                stage="captcha",
                retryable=True,
            )

        if not self.api_key:
            raise GrokProtocolError("Grok 注册缺少 Turnstile API Key", stage="captcha")
        base_url = self._base_url()
        create_path, result_path = self._paths()
        task: dict[str, Any] = {
            "type": "TurnstileTaskProxyless",
            "websiteURL": website_url,
            "websiteKey": sitekey,
        }
        if action:
            task["action"] = action
        created = self._post(f"{base_url}{create_path}", {"clientKey": self.api_key, "task": task})
        error = self._provider_error(created)
        if error:
            raise GrokProtocolError(f"Turnstile 创建任务失败: {error}", stage="captcha")
        task_id = created.get("taskId") or created.get("id") or created.get("request")
        if not task_id:
            raise GrokProtocolError("Turnstile 创建任务未返回 taskId", stage="captcha")

        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            time.sleep(min(self.poll_interval, max(0.0, remaining)))
            result = self._post(
                f"{base_url}{result_path}",
                {"clientKey": self.api_key, "taskId": task_id},
            )
            error = self._provider_error(result)
            if error:
                raise GrokProtocolError(f"Turnstile 求解失败: {error}", stage="captcha")
            status = str(result.get("status") or "").strip().lower()
            solution = result.get("solution") if isinstance(result.get("solution"), dict) else {}
            token = str(solution.get("token") or result.get("token") or "").strip()
            if status in {"ready", "success"} or token:
                if token:
                    return token
                raise GrokProtocolError("Turnstile 已完成但未返回 token", stage="captcha")
            if status and status not in {"processing", "pending", "queued"}:
                raise GrokProtocolError(f"Turnstile 未知任务状态: {status}", stage="captcha")
        raise GrokProtocolError("Turnstile 求解超时", stage="captcha", retryable=True)


def _safe_json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _router_tree_from_config(value: Any) -> list[Any] | None:
    if isinstance(value, list):
        return _prune_router_node(_safe_json_clone(value))
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("%"):
        text = unquote(text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GrokProtocolError("next_router_state_tree 不是有效 JSON", stage="bootstrap") from exc
    if not isinstance(parsed, list):
        raise GrokProtocolError("next_router_state_tree 必须是数组", stage="bootstrap")
    return _prune_router_node(parsed)


class GrokProtocolClient:
    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        proxy: str = "",
        log: Callable[[str], None] | None = None,
    ):
        self.config = dict(config or {})
        flow = str(self.config.get("signup_flow") or "legacy").strip().lower()
        self.signup_flow = flow if flow in {"legacy", "xconsole"} else "legacy"
        self.base_url = str(self.config.get("base_url") or DEFAULT_BASE_URL).strip().rstrip("/")
        default_signup_path = REFERENCE_SIGNUP_PATH if self.signup_flow == "xconsole" else DEFAULT_SIGNUP_PATH
        signup_path = str(self.config.get("signup_path") or default_signup_path).strip()
        self.signup_url = urljoin(f"{self.base_url}/", signup_path.lstrip("/"))
        self.proxy = str(proxy or "").strip()
        self.request_timeout = max(1.0, float(self.config.get("request_timeout") or 30))
        self.castle_timeout = max(1.0, float(self.config.get("castle_timeout") or 20))
        default_user_agent = REFERENCE_USER_AGENT if self.signup_flow == "xconsole" else DEFAULT_USER_AGENT
        self.user_agent = str(self.config.get("user_agent") or default_user_agent).strip()
        self.log = log
        default_impersonate = "chrome146" if self.signup_flow == "xconsole" else "chrome120"
        impersonate = str(self.config.get("impersonate") or default_impersonate).strip()
        self._reference_client: Any = None
        reference_dir = Path(str(self.config.get("xai_cli_pkce_reference_dir") or "")).expanduser()
        reference_client_file = reference_dir / "xconsole_client" / "client.py"
        if self.signup_flow == "xconsole" and reference_client_file.is_file():
            reference_path = str(reference_dir.resolve())
            if reference_path not in sys.path:
                sys.path.insert(0, reference_path)
            from xconsole_client.client import XConsoleAuthClient

            self._reference_client = XConsoleAuthClient(
                debug=False,
                signup_url=self.signup_url,
                impersonate=impersonate,
                proxy=None if self.proxy.lower() == "direct" else self.proxy,
            )
            self.session = self._reference_client._t._session
        else:
            session_options: dict[str, Any] = {"impersonate": impersonate, "trust_env": False}
            if self.signup_flow == "xconsole":
                session_options["http_version"] = "v2"
            self.session = requests.Session(**session_options)
            self.session.headers.update(
                {
                    "User-Agent": self.user_agent,
                    "Accept-Language": "zh-CN,zh;q=0.9" if self.signup_flow == "xconsole" else "en-US,en;q=0.9",
                    **(
                        {
                            "Accept-Encoding": "gzip, deflate, br, zstd",
                            "Sec-CH-UA": '"Chromium";v="146", "Google Chrome";v="146", "Not/A)Brand";v="99"',
                            "Sec-CH-UA-Mobile": "?0",
                            "Sec-CH-UA-Platform": '"Windows"',
                        }
                        if self.signup_flow == "xconsole"
                        else {}
                    ),
                }
            )
        self.metadata: SignupMetadata | None = None
        self._landing_html = ""
        self._script_sources: dict[str, str] = {}
        self._grok_session_warmed = False

    def close(self) -> None:
        if self._reference_client is not None:
            self._reference_client.close()
        else:
            self.session.close()

    def _emit(self, message: str) -> None:
        if self.log:
            self.log(str(message))

    def _request(self, method: str, url: str, **kwargs: Any):
        kwargs.setdefault("timeout", self.request_timeout)
        kwargs.setdefault("verify", False)
        if self.proxy and self.proxy.lower() != "direct":
            kwargs.setdefault("proxy", self.proxy)
        return self.session.request(method.upper(), url, **kwargs)

    def _get_landing(self) -> str:
        reference_flow = self.signup_flow == "xconsole"
        response = self._request(
            "GET",
            self.signup_url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Cache-Control": "no-cache",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-site" if reference_flow else "none",
                "Upgrade-Insecure-Requests": "1",
                **({"Referer": "https://console.x.ai/"} if reference_flow else {}),
            },
        )
        text = str(response.text or "")
        if response.status_code != 200:
            challenge = "Cloudflare challenge" if "cf-chl-" in text or "Just a moment" in text else "HTTP error"
            raise GrokProtocolError(
                f"Grok 注册页访问失败: HTTP {response.status_code} ({challenge})",
                stage="bootstrap",
                retryable=response.status_code in {403, 429, 500, 502, 503, 504},
            )
        if "<html" not in text.lower() or "sign-up" not in text:
            raise GrokProtocolError("Grok 注册页响应内容异常", stage="bootstrap", retryable=True)
        self._landing_html = text
        return text

    def _fetch_script(self, url: str) -> str:
        if url in self._script_sources:
            return self._script_sources[url]
        response = self._request(
            "GET",
            url,
            headers={
                "Accept": "*/*",
                "Referer": self.signup_url,
                "Sec-Fetch-Dest": "script",
                "Sec-Fetch-Mode": "no-cors",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        if response.status_code != 200:
            return ""
        source = str(response.text or "")
        self._script_sources[url] = source
        return source

    @staticmethod
    def _castle_chunk_url(loader_url: str, base_url: str, path: str) -> str:
        if path.startswith("static/"):
            return urljoin(f"{base_url}/", f"_next/{path}")
        return urljoin(loader_url, path)

    def _cache_castle_sdk(self, source: str, source_url: str) -> str:
        if "createRequestToken" not in source or "configure" not in source:
            raise GrokProtocolError("下载的 Castle SDK 内容无效", stage="bootstrap")
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:20]
        directory = DATA_DIR / "grok" / "castle"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"castle_sdk_{digest}.js"
        if not target.is_file() or target.stat().st_size != len(source.encode("utf-8")):
            temporary = target.with_suffix(f".tmp-{os.getpid()}-{threading.get_ident()}")
            temporary.write_text(source, encoding="utf-8")
            temporary.replace(target)
        self._emit(f"Castle SDK 已缓存: {source_url}")
        return str(target)

    def _discover(self, html: str, *, force: bool) -> SignupMetadata:
        configured_tree = _router_tree_from_config(self.config.get("next_router_state_tree"))
        action_id = str(self.config.get("action_id") or "").strip()
        sitekey = str(self.config.get("sitekey") or "").strip() or extract_turnstile_sitekey(html)
        castle_pk = str(self.config.get("castle_pk") or "").strip() or extract_castle_pk(html)
        castle_sdk_url = str(self.config.get("castle_sdk_url") or "").strip()
        router_tree = configured_tree or extract_next_router_state_tree(html)
        cache_key = json.dumps(
            {
                "base_url": self.base_url,
                "signup_url": self.signup_url,
                "signup_flow": self.signup_flow,
                "proxy": self.proxy,
                "action_id": action_id,
                "castle_sdk_url": castle_sdk_url,
            },
            sort_keys=True,
        )

        with _DISCOVERY_LOCK:
            cached = _DISCOVERY_CACHE.get(cache_key)
            if not force and cached and time.monotonic() - cached[0] < _DISCOVERY_TTL_SECONDS:
                metadata = cached[1]
                if Path(metadata.castle_sdk_path).is_file():
                    return replace(
                        metadata,
                        sitekey=sitekey or metadata.sitekey,
                        castle_pk=castle_pk or metadata.castle_pk,
                        router_state_tree=_safe_json_clone(router_tree),
                    )

            sdk_source = ""
            if castle_sdk_url:
                sdk_source = self._fetch_script(castle_sdk_url)
            script_urls = extract_script_urls(html, self.signup_url)
            for script_url in reversed(script_urls):
                if action_id and sdk_source:
                    break
                source = self._fetch_script(script_url)
                if not source:
                    continue
                if not action_id and "emailValidationCode" in source and "createUserAndSessionRequest" in source:
                    action_id = extract_action_id(source)
                if not sdk_source:
                    if "createRequestToken" in source and "configure" in source and "CastleProvider" not in source:
                        castle_sdk_url = script_url
                        sdk_source = source
                    else:
                        lazy_path = extract_castle_lazy_chunk(source)
                        if lazy_path:
                            candidate_url = self._castle_chunk_url(script_url, self.base_url, lazy_path)
                            candidate_source = self._fetch_script(candidate_url)
                            if "createRequestToken" in candidate_source and "configure" in candidate_source:
                                castle_sdk_url = candidate_url
                                sdk_source = candidate_source

            action_id = action_id or DEFAULT_ACTION_ID
            sitekey = sitekey or DEFAULT_TURNSTILE_SITEKEY
            castle_pk = castle_pk or DEFAULT_CASTLE_PK
            if not sdk_source and _VENDOR_SDK_PATH.is_file():
                fallback = _VENDOR_SDK_PATH.read_text(encoding="utf-8")
                if "createRequestToken" in fallback and "configure" in fallback:
                    castle_sdk_url = str(_VENDOR_SDK_PATH)
                    sdk_source = fallback
            if not sdk_source:
                raise GrokProtocolError("未能从注册页发现 Castle SDK", stage="bootstrap", retryable=True)
            sdk_path = self._cache_castle_sdk(sdk_source, castle_sdk_url)
            metadata = SignupMetadata(
                signup_url=self.signup_url,
                action_id=action_id,
                sitekey=sitekey,
                castle_pk=castle_pk,
                router_state_tree=_safe_json_clone(router_tree),
                castle_sdk_url=castle_sdk_url,
                castle_sdk_path=sdk_path,
            )
            _DISCOVERY_CACHE[cache_key] = (time.monotonic(), metadata)
            return metadata

    def bootstrap(self, *, force: bool = False) -> SignupMetadata:
        if self._reference_client is not None:
            del force
            status = int(self._reference_client.load_signup_page())
            if not 200 <= status < 300:
                raise GrokProtocolError(
                    f"公网版注册页访问失败: HTTP {status}",
                    stage="bootstrap",
                    retryable=status in {403, 429, 500, 502, 503, 504},
                )
            self.metadata = SignupMetadata(
                signup_url=self.signup_url,
                action_id=str(self._reference_client.next_action_id),
                sitekey=str(self.config.get("sitekey") or DEFAULT_TURNSTILE_SITEKEY),
                castle_pk="",
                router_state_tree=[],
                castle_sdk_url="",
                castle_sdk_path="",
            )
            return self.metadata
        html = self._get_landing()
        self.metadata = self._discover(html, force=force)
        return self.metadata

    def _metadata(self) -> SignupMetadata:
        return self.metadata or self.bootstrap()

    def create_castle_token(self, *, page_url: str = "") -> str:
        metadata = self._metadata()
        return _CASTLE_RUNNER.create_token(
            sdk_path=metadata.castle_sdk_path,
            pk=metadata.castle_pk,
            page_url=str(page_url or metadata.signup_url).strip(),
            user_agent=self.user_agent,
            timeout=self.castle_timeout,
        )

    def _grpc_post(self, path: str, message: bytes) -> GrpcWebResult:
        response = self._request(
            "POST",
            urljoin(f"{self.base_url}/", path.lstrip("/")),
            data=grpc_web_envelope(message),
            headers={
                "Accept": "*/*",
                "Content-Type": "application/grpc-web+proto",
                "Origin": self.base_url,
                "Referer": self.signup_url,
                "X-Grpc-Web": "1",
                "X-User-Agent": "connect-es/2.1.1",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            },
            allow_redirects=False,
        )
        if not 200 <= response.status_code < 300:
            raise GrokProtocolError(
                f"Grok gRPC 请求失败: HTTP {response.status_code}",
                stage="grpc",
                retryable=response.status_code in {429, 500, 502, 503, 504},
            )
        return decode_grpc_web_response(bytes(response.content or b""), response.headers)

    def send_email_validation_code(self, email: str) -> None:
        if self._reference_client is not None:
            result = self._reference_client.create_email_validation_code(email)
            if not bool(result.ok):
                raise GrokProtocolError(
                    f"公网版 Grok 发码失败: HTTP {result.http_status}",
                    stage="grpc",
                    retryable=int(result.http_status or 0) in {429, 500, 502, 503, 504},
                    mail_retryable=True,
                )
            return
        castle_token = "" if self.signup_flow == "xconsole" else self.create_castle_token()
        self._grpc_post(
            "/auth_mgmt.AuthManagement/CreateEmailValidationCode",
            create_email_validation_request(email, castle_token),
        )

    send_email_code = send_email_validation_code

    def validate_password(self, email: str, password: str) -> None:
        if self._reference_client is not None:
            self._reference_client.validate_password(email, password)
            return
        if self.signup_flow != "xconsole":
            return
        self._grpc_post(
            "/auth_mgmt.AuthManagement/ValidatePassword",
            validate_password_request(email, password),
        )

    def verify_email_validation_code(self, email: str, code: str) -> str:
        if self._reference_client is not None:
            result = self._reference_client.verify_email_validation_code(email, code)
            if not bool(result.ok):
                raise GrokProtocolError(
                    f"公网版 Grok 验码失败: HTTP {result.http_status}",
                    stage="grpc",
                    retryable=int(result.http_status or 0) in {429, 500, 502, 503, 504},
                )
            return ""
        result = self._grpc_post(
            "/auth_mgmt.AuthManagement/VerifyEmailValidationCode",
            verify_email_validation_request(email, code),
        )
        for message in result.messages:
            token = parse_verification_token(message)
            if token:
                return token
        return ""

    def solve_turnstile(self) -> str:
        metadata = self._metadata()
        solver_config = dict(self.config)
        if self.proxy and self.proxy.lower() != "direct":
            solver_config["proxy"] = self.proxy
        solver = TurnstileSolver(solver_config)
        try:
            return solver.solve(
                website_url=metadata.signup_url,
                sitekey=metadata.sitekey,
                action=str(self.config.get("action") or "").strip(),
            )
        finally:
            solver.close()

    def _server_action_request(self, payload: dict[str, Any]):
        metadata = self._metadata()
        router_state = quote(
            json.dumps(metadata.router_state_tree, ensure_ascii=False, separators=(",", ":")),
            safe="",
        )
        return self._request(
            "POST",
            metadata.signup_url,
            data=json.dumps([payload], ensure_ascii=False, separators=(",", ":")),
            headers={
                "Accept": "text/x-component",
                "Content-Type": "text/plain;charset=UTF-8",
                "Next-Action": metadata.action_id,
                "Next-Router-State-Tree": router_state,
                "Next-Url": urlparse(metadata.signup_url).path + "?" + (urlparse(metadata.signup_url).query or ""),
                "Origin": self.base_url,
                "Referer": metadata.signup_url,
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            },
            allow_redirects=False,
        )

    @staticmethod
    def _invalid_action_response(response: Any) -> bool:
        text = str(getattr(response, "text", "") or "").lower()
        headers = _normalized_headers(getattr(response, "headers", {}))
        return response.status_code in {404, 405} or any(
            marker in text
            for marker in (
                "failed to find server action",
                "invalid action",
                "action not found",
                "server action was not found",
            )
        ) or headers.get("x-nextjs-action-not-found") == "1"

    @staticmethod
    def _raise_action_value(value: Any) -> None:
        if isinstance(value, dict) and value.get("error"):
            message = str(value.get("error") or "Grok 注册失败").strip()
            raise GrokProtocolError(
                message,
                stage="create_account",
                mail_retryable=_looks_like_mail_error(message),
            )
        if isinstance(value, dict) and "signInMethods" in value:
            raise GrokProtocolError(
                "该邮箱已存在 Grok 账号",
                stage="create_account",
                mail_retryable=True,
            )

    def _cookie_value(self, *names: str) -> str:
        jar = getattr(self.session.cookies, "jar", None)
        if jar is not None:
            cookies = list(jar)
            for name in names:
                matches = [
                    str(cookie.value).strip()
                    for cookie in cookies
                    if cookie.name == name and str(cookie.value).strip()
                ]
                if matches:
                    return matches[-1]
        try:
            values = self.session.cookies.get_dict()
        except Exception:
            values = {}
        for name in names:
            value = str(values.get(name) or "").strip()
            if value:
                return value
        return ""

    def _cookie_value_for_domain(self, domain: str, *names: str) -> str:
        target = str(domain or "").lower().lstrip(".")
        jar = getattr(self.session.cookies, "jar", None)
        if jar is None:
            return ""
        cookies = list(jar)
        for name in names:
            matches = [
                str(cookie.value).strip()
                for cookie in cookies
                if cookie.name == name
                and str(cookie.domain or "").lower().lstrip(".") == target
                and str(cookie.path or "") in {"", "/"}
                and str(cookie.value).strip()
            ]
            if matches:
                return matches[-1]
        return ""

    def _cookie_metadata(self) -> str:
        jar = getattr(self.session.cookies, "jar", None)
        if jar is None:
            try:
                return ",".join(sorted(str(name) for name in self.session.cookies.get_dict())) or "none"
            except Exception:
                return "none"
        entries = sorted(
            {
                f"{cookie.name}@{str(cookie.domain or '').lstrip('.')}{str(cookie.path or '/')}"
                for cookie in jar
                if str(cookie.name or "").strip()
            }
        )
        return ",".join(entries) or "none"

    def session_cookies(self) -> dict[str, str]:
        """Return a transient cookie snapshot for the immediate PKCE handoff."""
        values: dict[str, str] = {}
        jar = getattr(self.session.cookies, "jar", None)
        if jar is not None:
            for cookie in jar:
                name = str(getattr(cookie, "name", "") or "").strip()
                value = str(getattr(cookie, "value", "") or "").strip()
                if name and value:
                    values[name] = value
        if values:
            return values
        try:
            raw = self.session.cookies.get_dict()
        except Exception:
            return {}
        return {
            str(name): str(value)
            for name, value in raw.items()
            if str(name).strip() and str(value).strip()
        }

    def _prewarm_grok_session(self) -> None:
        if self._grok_session_warmed and self._cookie_value("grok_device_id"):
            return
        last_error: GrokProtocolError | None = None
        last_cause: Exception | None = None
        for attempt in range(1, 4):
            try:
                response = self._request(
                    "GET",
                    "https://grok.com/",
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Referer": self.signup_url,
                        "Sec-Fetch-Dest": "document",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Site": "cross-site",
                        "Upgrade-Insecure-Requests": "1",
                    },
                    allow_redirects=True,
                )
                if not 200 <= response.status_code < 400:
                    last_error = GrokProtocolError(
                        f"Grok 会话预热失败: HTTP {response.status_code}",
                        stage="session_exchange",
                        retryable=response.status_code in {403, 429, 500, 502, 503, 504},
                        reason_code=f"prewarm_http_{response.status_code}",
                    )
                elif not self._cookie_value("grok_device_id"):
                    last_error = GrokProtocolError(
                        "Grok 会话预热未获得 grok_device_id",
                        stage="session_exchange",
                        retryable=True,
                        reason_code="prewarm_missing_device_cookie",
                    )
                else:
                    self._grok_session_warmed = True
                    self._emit(f"Grok 会话预热完成，Cookie={self._cookie_metadata()}")
                    return
            except Exception as exc:
                last_cause = exc
                last_error = GrokProtocolError(
                    f"Grok 会话预热失败: {type(exc).__name__}",
                    stage="session_exchange",
                    retryable=True,
                    reason_code="prewarm_transport_error",
                )
            if last_error is not None and not last_error.retryable:
                break
            if attempt < 3:
                self._emit(f"Grok 会话预热失败，准备重试（{attempt}/3）")
                time.sleep(0.5 * attempt)
        assert last_error is not None
        if last_cause is not None:
            raise last_error from last_cause
        raise last_error

    def _follow_signup_result(self, value: Any, *, base_url: str = "") -> SessionExchangeResult:
        redirect_url = str(value or "").strip() if isinstance(value, str) else ""
        if not redirect_url:
            return SessionExchangeResult("", "", 0, 0, "missing_redirect")
        redirect_url = urljoin(base_url or self.signup_url, redirect_url)
        if not _is_allowed_session_url(redirect_url):
            raise GrokProtocolError(
                "Grok 注册跳转目标不在允许范围",
                stage="session_exchange",
                reason_code="redirect_not_allowed",
            )

        current_url = redirect_url
        referer = self.signup_url
        last_status = 0
        for hop in range(1, 9):
            self._emit(f"Grok 会话交换请求[{hop}]: {summarize_sensitive_url(current_url)}")
            response = self._request(
                "GET",
                current_url,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": referer,
                    "Upgrade-Insecure-Requests": "1",
                },
                allow_redirects=False,
            )
            last_status = int(response.status_code)
            response_url = str(getattr(response, "url", "") or current_url)
            location = _normalized_headers(getattr(response, "headers", {})).get("location", "").strip()
            set_cookie_names = _set_cookie_names(getattr(response, "headers", {}))
            location_summary = summarize_sensitive_url(urljoin(current_url, location)) if location else "none"
            self._emit(
                "Grok 会话交换响应"
                f"[{hop}]: HTTP {last_status}, url={summarize_sensitive_url(response_url)}, "
                f"location={location_summary}, Set-Cookie={','.join(set_cookie_names) or 'none'}, "
                f"jar={self._cookie_metadata()}"
            )

            sso = self._cookie_value_for_domain("grok.com", "sso") or _response_cookie_value_for_domain(
                response, "grok.com", "sso"
            )
            sso_rw = self._cookie_value_for_domain("grok.com", "sso-rw") or _response_cookie_value_for_domain(
                response, "grok.com", "sso-rw"
            )

            if 300 <= last_status < 400:
                if not location:
                    if sso or sso_rw:
                        return SessionExchangeResult(
                            redirect_url,
                            response_url,
                            last_status,
                            hop,
                            "sso_cookie",
                            sso=sso or sso_rw,
                            sso_rw=sso_rw,
                        )
                    return SessionExchangeResult(
                        redirect_url,
                        response_url,
                        last_status,
                        hop,
                        "redirect_missing_location",
                    )
                next_url = urljoin(current_url, location)
                if not _is_allowed_session_url(next_url):
                    raise GrokProtocolError(
                        "Grok 会话交换重定向目标不在允许范围",
                        stage="session_exchange",
                        reason_code="redirect_not_allowed",
                        account_created=True,
                    )
                referer, current_url = _redirect_referer(current_url, next_url), next_url
                continue

            if sso or sso_rw:
                return SessionExchangeResult(
                    redirect_url,
                    response_url,
                    last_status,
                    hop,
                    "sso_cookie",
                    sso=sso or sso_rw,
                    sso_rw=sso_rw,
                )

            reason = f"callback_http_{last_status}" if last_status >= 400 else "callback_no_sso"
            return SessionExchangeResult(redirect_url, response_url, last_status, hop, reason)

        return SessionExchangeResult(redirect_url, current_url, last_status, 8, "redirect_limit")

    def create_user_and_session(
        self,
        *,
        email: str,
        code: str,
        given_name: str,
        family_name: str,
        password: str,
        turnstile_token: str,
    ) -> dict[str, Any]:
        if self._reference_client is not None:
            result = self._reference_client.create_account(
                email=email,
                given_name=given_name,
                family_name=family_name,
                password=password,
                email_validation_code=code,
                turnstile_token=turnstile_token,
                castle_request_token="",
                conversion_id=str(uuid.uuid4()),
            )
            if not bool(result.ok):
                fresh_turnstile = self.solve_turnstile()
                result = self._reference_client.create_account(
                    email=email,
                    given_name=given_name,
                    family_name=family_name,
                    password=password,
                    email_validation_code=code,
                    turnstile_token=fresh_turnstile,
                    castle_request_token="",
                    conversion_id=str(uuid.uuid4()),
                )
            if not bool(result.ok):
                raise GrokProtocolError(
                    f"公网版 Grok 创建账号失败: HTTP {result.http_status}",
                    stage="create_account",
                    retryable=int(result.http_status or 0) in {429, 500, 502, 503, 504},
                )
            sso = str(
                self._reference_client.fetch_sso_token(
                    email="",
                    password="",
                    save=False,
                    retries=3,
                )
                or ""
            ).strip()
            if not sso:
                raise GrokProtocolError(
                    "公网版 Grok 注册成功但未获得 SSO",
                    stage="session_exchange",
                    reason_code="reference_missing_sso",
                    account_created=True,
                    partial_result={
                        "email": email,
                        "password": password,
                        "sso": "",
                        "profile": {
                            "given_name": given_name,
                            "family_name": family_name,
                            "session_state": "missing",
                            "session_reason": "reference_missing_sso",
                        },
                        "source_type": "protocol",
                        "status": "pending_sso",
                    },
                )
            return {
                "email": email,
                "password": password,
                "sso": sso,
                "sso_rw": self._cookie_value("sso-rw"),
                "redirect_url": "",
                "session_reason": "reference_sso",
            }
        response = None
        value: Any = None
        redirect_url = ""
        exchange_result = SessionExchangeResult("", "", 0, 0, "not_started")
        if self.signup_flow != "xconsole":
            self._prewarm_grok_session()
        for attempt in range(2):
            castle_token = "" if self.signup_flow == "xconsole" else self.create_castle_token()
            tos_version: int | str = "$undefined" if self.signup_flow == "xconsole" else 1
            payload = {
                "emailValidationCode": code,
                "createUserAndSessionRequest": {
                    "email": email,
                    "givenName": given_name,
                    "familyName": family_name,
                    "clearTextPassword": password,
                    "tosAcceptedVersion": tos_version,
                },
                "turnstileToken": turnstile_token,
                "conversionId": str(uuid.uuid4()),
                "castleRequestToken": castle_token,
            }
            response = self._server_action_request(payload)
            response_headers = _normalized_headers(response.headers)
            action_redirect = extract_action_redirect(response.headers)
            content_type = response_headers.get("content-type", "").lower()
            value = (
                parse_flight_result(bytes(response.content or b""))
                if "text/x-component" in content_type
                else None
            )
            if self._invalid_action_response(response) and attempt == 0:
                self._emit("Server Action 已变化，刷新注册页元数据后重试")
                self.bootstrap(force=True)
                continue
            if not 200 <= response.status_code < 300:
                try:
                    self._raise_action_value(value)
                except GrokProtocolError as error:
                    error.retryable = error.retryable or response.status_code in {
                        429,
                        500,
                        502,
                        503,
                        504,
                    }
                    error.reason_code = error.reason_code or f"server_action_http_{response.status_code}"
                    raise
                detail = _summarize_server_action_response(response, self._metadata().action_id)
                raise GrokProtocolError(
                    f"Grok Server Action HTTP {response.status_code} ({detail})",
                    stage="create_account",
                    retryable=response.status_code in {429, 500, 502, 503, 504},
                    reason_code=f"server_action_http_{response.status_code}",
                )
            if "text/x-component" not in content_type and not action_redirect:
                raise GrokProtocolError(
                    f"Grok Server Action 返回类型异常: {content_type or 'missing content-type'}",
                    stage="create_account",
                    retryable="text/html" in content_type,
                )
            self._raise_action_value(value)
            if action_redirect:
                response_url = str(getattr(response, "url", "") or self.signup_url)
                redirect_url = urljoin(response_url, action_redirect)
                self._emit("检测到 Server Action 注册跳转，正在完成会话交换")
            elif isinstance(value, str):
                redirect_url = urljoin(self.signup_url, value)
                self._emit("检测到 React Flight 注册跳转，正在完成会话交换")
            break
        if response is None:
            raise GrokProtocolError("Grok Server Action 未返回响应", stage="create_account")

        if redirect_url:
            try:
                exchange_result = self._follow_signup_result(redirect_url, base_url=self.signup_url)
            except Exception as exc:
                error = exc if isinstance(exc, GrokProtocolError) else GrokProtocolError(
                    f"Grok 会话交换请求失败: {type(exc).__name__}",
                    stage="session_exchange",
                    reason_code="callback_transport_error",
                    account_created=True,
                )
                error.account_created = True
                error.partial_result = {
                    "email": email,
                    "password": password,
                    "sso": "",
                    "profile": {
                        "given_name": given_name,
                        "family_name": family_name,
                        "session_state": "missing",
                        "session_reason": error.reason_code or "callback_transport_error",
                        "redirect_url": summarize_sensitive_url(redirect_url),
                    },
                    "source_type": "protocol",
                    "status": "pending_sso",
                }
                if error is exc:
                    raise
                raise error from exc
        if redirect_url:
            sso = exchange_result.sso or self._cookie_value_for_domain("grok.com", "sso", "sso-rw")
            sso_rw = exchange_result.sso_rw or self._cookie_value_for_domain("grok.com", "sso-rw")
        else:
            sso = self._cookie_value("sso", "sso-rw")
            sso_rw = self._cookie_value("sso-rw")
        if not sso and not redirect_url:
            self._request(
                "GET",
                "https://grok.com/",
                headers={"Accept": "text/html,*/*", "Referer": self.signup_url},
                allow_redirects=True,
            )
            sso = self._cookie_value("sso", "sso-rw")
            sso_rw = self._cookie_value("sso-rw")
        if not sso:
            detail = "注册后跳转未建立会话" if redirect_url else "Server Action 未返回注册跳转"
            safe_redirect = summarize_sensitive_url(redirect_url)
            status = "pending_sso" if redirect_url else "submission_unconfirmed"
            session_state = "missing" if redirect_url else "unconfirmed"
            reason_code = exchange_result.reason_code if redirect_url else "missing_redirect"
            raise GrokProtocolError(
                f"Grok 提交结果无法确认且未获得 sso cookie: {detail}",
                stage="session_exchange",
                reason_code=reason_code,
                account_created=bool(redirect_url),
                partial_result={
                    "email": email,
                    "password": password,
                    "sso": "",
                    "profile": {
                        "given_name": given_name,
                        "family_name": family_name,
                        "session_state": session_state,
                        "session_reason": reason_code,
                        "redirect_url": safe_redirect,
                    },
                    "source_type": "protocol",
                    "status": status,
                },
            )
        return {
            "email": email,
            "password": password,
            "sso": sso,
            "sso_rw": sso_rw,
            "redirect_url": summarize_sensitive_url(redirect_url),
            "session_reason": exchange_result.reason_code,
        }

__all__ = [
    "DEFAULT_ACTION_ID",
    "DEFAULT_BASE_URL",
    "DEFAULT_CASTLE_PK",
    "DEFAULT_ROUTER_STATE_TREE",
    "DEFAULT_TURNSTILE_SITEKEY",
    "GrpcWebError",
    "GrpcWebResult",
    "GrokProtocolClient",
    "GrokProtocolError",
    "SessionExchangeResult",
    "SignupMetadata",
    "TurnstileSolver",
    "create_email_validation_request",
    "decode_grpc_web_response",
    "decode_next_f_payloads",
    "decode_varint",
    "encode_varint",
    "extract_action_id",
    "extract_action_redirect",
    "extract_castle_lazy_chunk",
    "extract_castle_pk",
    "extract_next_router_state_tree",
    "extract_script_urls",
    "extract_turnstile_sitekey",
    "grpc_web_envelope",
    "parse_flight_result",
    "parse_grpc_web_frames",
    "parse_protobuf_fields",
    "parse_verification_token",
    "protobuf_bool",
    "protobuf_string",
    "summarize_sensitive_url",
    "verify_email_validation_request",
]
