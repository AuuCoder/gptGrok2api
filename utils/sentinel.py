"""OpenAI Sentinel Token (PoW) 生成与请求工具函数。

用于密码登录、注册等需要 sentinel token 的流程。
"""
from __future__ import annotations

import base64
import json
import os
import random
import re
import time
import uuid
from typing import TYPE_CHECKING

from utils.turnstile import solve_turnstile_token

if TYPE_CHECKING:
    from curl_cffi.requests import Session


class SentinelTokenGenerator:
    """Sentinel Token 生成器（PoW - Proof of Work）。"""
    MAX_ATTEMPTS = 500_000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(self, device_id: str, ua: str):
        self.device_id = device_id
        self.user_agent = ua
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        return format(h & 0xFFFFFFFF, "08x")

    def _get_config(self) -> list:
        perf_now = random.uniform(1000, 50000)
        return [
            "1920x1080",
            time.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime()),
            4294705152,
            random.random(),
            self.user_agent,
            f"{SENTINEL_ORIGIN}/sentinel/{SENTINEL_VERSION}/sdk.js",
            None,
            None,
            "en-US",
            random.random(),
            random.choice(["vendorSub-undefined", "plugins-undefined", "mimeTypes-undefined", "hardwareConcurrency-undefined"]),
            random.choice(["location", "implementation", "URL", "documentURI", "compatMode"]),
            random.choice(["Object", "Function", "Array", "Number", "parseFloat", "undefined"]),
            perf_now,
            self.sid,
            "",
            random.choice([4, 8, 12, 16]),
            time.time() * 1000 - perf_now,
        ]

    @staticmethod
    def _b64(data) -> str:
        return base64.b64encode(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).decode("ascii")

    def generate_requirements_token(self) -> str:
        data = self._get_config()
        data[3] = 1
        data[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._b64(data)

    def generate_token(self, seed: str, difficulty: str) -> str:
        start = time.time()
        data = self._get_config()
        difficulty = str(difficulty or "0")
        for i in range(self.MAX_ATTEMPTS):
            data[3] = i
            data[9] = round((time.time() - start) * 1000)
            payload = self._b64(data)
            if self._fnv1a_32(seed + payload)[: len(difficulty)] <= difficulty:
                return "gAAAAAB" + payload + "~S"
        return "gAAAAAB" + self.ERROR_PREFIX + self._b64(str(None))


# ── 默认 User-Agent 和 sec-ch-ua ──────────────────────────────
DEFAULT_SENTINEL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
DEFAULT_SENTINEL_SEC_CH_UA = '"Chromium";v="145", "Google Chrome";v="145", "Not/A)Brand";v="99"'
SENTINEL_ORIGIN = "https://chatgpt.com"
SENTINEL_VERSION = "20260423af3c"
SENTINEL_REQ_URL = f"{SENTINEL_ORIGIN}/backend-api/sentinel/req"


def _get_vm_sentinel_token(
    session: "Session",
    device_id: str,
    flow: str,
    *,
    user_agent: str,
    sec_ch_ua: str,
) -> tuple[str, str] | None:
    if os.getenv("OPENAI_SENTINEL_DISABLE_VM"):
        return None
    try:
        from utils.sentinel_vm import get_sentinel_token_via_vm

        result = get_sentinel_token_via_vm(
            session,
            device_id,
            flow,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
        )
        if isinstance(result, tuple) and len(result) == 2:
            return str(result[0]), str(result[1])
        return None
    except Exception:
        return None


def _response_cookie_value(response: object, session: object, name: str) -> str:
    raw_values: list[str] = []
    headers = getattr(response, "headers", None)
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
    try:
        return str(getattr(session, "cookies").get(name) or "")
    except Exception:
        return ""


def build_sentinel_token(
    session: "Session",
    device_id: str,
    flow: str,
    *,
    user_agent: str = "",
    sec_ch_ua: str = "",
) -> tuple[str, str]:
    """请求 sentinel token 并返回 (sentinel_header_value, oai_sc_cookie_value)。

    Args:
        session: curl_cffi Session 实例
        device_id: 设备 ID
        flow: 流程标识（如 "password_verify", "username_password_create" 等）
        user_agent: 可选的 User-Agent 覆盖
        sec_ch_ua: 可选的 sec-ch-ua 覆盖

    Returns:
        (openai-sentinel-token header value, oai-sc cookie value) 元组

    Raises:
        RuntimeError: sentinel 请求失败
    """
    ua = user_agent or DEFAULT_SENTINEL_USER_AGENT
    ch_ua = sec_ch_ua or DEFAULT_SENTINEL_SEC_CH_UA
    vm_bundle = _get_vm_sentinel_token(session, device_id, flow, user_agent=ua, sec_ch_ua=ch_ua)
    if vm_bundle:
        return vm_bundle
    generator = SentinelTokenGenerator(device_id, ua)
    requirements_token = generator.generate_requirements_token()
    resp = session.post(
        SENTINEL_REQ_URL,
        data=json.dumps({"p": requirements_token, "id": device_id, "flow": flow}),
        headers={
            "Content-Type": "text/plain;charset=UTF-8",
            "Referer": f"{SENTINEL_ORIGIN}/backend-api/sentinel/frame.html?sv={SENTINEL_VERSION}",
            "Origin": SENTINEL_ORIGIN,
            "User-Agent": ua,
            "sec-ch-ua": ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        },
        timeout=20,
        verify=False,
    )

    try:
        data = resp.json() if resp.text else {}
    except Exception:
        fallback = json.dumps(
            {"p": generator.generate_requirements_token(), "t": "", "c": "", "id": device_id, "flow": flow},
            separators=(",", ":"),
        )
        return fallback, ""

    token = str(data.get("token") or "").strip()
    if resp.status_code != 200 or not token:
        raise RuntimeError(f"sentinel_req_failed_{resp.status_code}")
    pow_data = data.get("proofofwork") or {}
    p_value = (
        generator.generate_token(str(pow_data.get("seed") or ""), str(pow_data.get("difficulty") or "0"))
        if pow_data.get("required") and pow_data.get("seed")
        else generator.generate_requirements_token()
    )
    turnstile_data = data.get("turnstile") or {}
    turnstile_token = ""
    if turnstile_data.get("required") and turnstile_data.get("dx"):
        # 对齐 sentinel sdk.js 的 token(flow) 行为：
        # t 字段由 req 阶段返回的 turnstile.dx 与本次 requirements_token 计算得到。
        turnstile_token = solve_turnstile_token(str(turnstile_data.get("dx") or ""), requirements_token) or ""
        if not turnstile_token:
            raise RuntimeError("sentinel_turnstile_token_failed")
    sentinel_value = json.dumps(
        {"p": p_value, "t": turnstile_token, "c": token, "id": device_id, "flow": flow},
        separators=(",", ":"),
    )
    oai_sc_value = _response_cookie_value(resp, session, "oai-sc")
    if not oai_sc_value:
        raise RuntimeError("sentinel_req_missing_oai_sc")
    return sentinel_value, oai_sc_value


def build_sentinel_with_so_token(
    session: "Session",
    device_id: str,
    flow: str,
    *,
    user_agent: str = "",
    sec_ch_ua: str = "",
) -> tuple[str, str, str]:
    """请求 Sentinel token，并返回主 token、Session Observer token 和 oai-sc。

    ``turnstile`` 和 Session Observer token 是两个独立字段：前者写入
    主 token 的 ``t`` 字段，后者才可作为 ``OpenAI-Sentinel-SO-Token``
    请求头。不能把 Turnstile 解题结果复用为 SO token。当前无浏览器 VM
    仅生成官方 SDK 的主 token；SO token 仍保守留空。

    Args:
        session: curl_cffi Session 实例
        device_id: 设备 ID
        flow: 流程标识（如 "oauth_create_account" 等）
        user_agent: 可选的 User-Agent 覆盖
        sec_ch_ua: 可选的 sec-ch-ua 覆盖

    Returns:
        (openai-sentinel-token, openai-sentinel-so-token, oai-sc cookie) 元组

    Raises:
        RuntimeError: sentinel 请求失败
    """
    ua = user_agent or DEFAULT_SENTINEL_USER_AGENT
    ch_ua = sec_ch_ua or DEFAULT_SENTINEL_SEC_CH_UA
    vm_bundle = _get_vm_sentinel_token(session, device_id, flow, user_agent=ua, sec_ch_ua=ch_ua)
    if vm_bundle:
        return vm_bundle[0], "", vm_bundle[1]
    generator = SentinelTokenGenerator(device_id, ua)
    requirements_token = generator.generate_requirements_token()
    resp = session.post(
        SENTINEL_REQ_URL,
        data=json.dumps({"p": requirements_token, "id": device_id, "flow": flow}),
        headers={
            "Content-Type": "text/plain;charset=UTF-8",
            "Referer": f"{SENTINEL_ORIGIN}/backend-api/sentinel/frame.html?sv={SENTINEL_VERSION}",
            "Origin": SENTINEL_ORIGIN,
            "User-Agent": ua,
            "sec-ch-ua": ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        },
        timeout=20,
        verify=False,
    )

    try:
        data = resp.json() if resp.text else {}
    except Exception:
        fallback = json.dumps(
            {"p": generator.generate_requirements_token(), "t": "", "c": "", "id": device_id, "flow": flow},
            separators=(",", ":"),
        )
        return fallback, "", ""

    token = str(data.get("token") or "").strip()
    if resp.status_code != 200 or not token:
        raise RuntimeError(f"sentinel_req_failed_{resp.status_code}")

    pow_data = data.get("proofofwork") or {}
    p_value = (
        generator.generate_token(str(pow_data.get("seed") or ""), str(pow_data.get("difficulty") or "0"))
        if pow_data.get("required") and pow_data.get("seed")
        else generator.generate_requirements_token()
    )

    turnstile_data = data.get("turnstile") or {}
    turnstile_token = ""
    if turnstile_data.get("required") and turnstile_data.get("dx"):
        turnstile_token = solve_turnstile_token(str(turnstile_data.get("dx") or ""), requirements_token) or ""
        if not turnstile_token:
            raise RuntimeError("sentinel_turnstile_token_failed")

    # Sentinel token 包含 PoW / Turnstile 字段。/req 的 so.snapshot_dx
    # 不是可直接发送的 SO token，未执行官方 Session Observer VM 时必须留空。
    sentinel_value = json.dumps(
        {"p": p_value, "t": turnstile_token, "c": token, "id": device_id, "flow": flow},
        separators=(",", ":"),
    )
    oai_sc_value = _response_cookie_value(resp, session, "oai-sc")
    if not oai_sc_value:
        raise RuntimeError("sentinel_req_missing_oai_sc")

    return sentinel_value, "", oai_sc_value
