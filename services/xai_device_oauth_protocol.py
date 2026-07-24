"""Headless xAI Device Code authorization using a saved Grok session or login.

An existing SSO session is tried first.  If it cannot reach the device consent
form, the protocol falls back to password login with the existing Castle/jsdom
runtime and Turnstile provider, so neither path requires a browser page.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

from services.register.grok_protocol import (
    GrokProtocolClient,
    GrokProtocolError,
    TurnstileSolver,
    extract_turnstile_sitekey,
)
from services.xai_cli_oauth_protocol import (
    XAI_DEVICE_CODE_URL,
    XAI_OAUTH_CLIENT_ID,
    XAI_OAUTH_SCOPE,
    XAI_TOKEN_URL,
)


_DEVICE_VERIFY_URL = "https://auth.x.ai/oauth2/device/verify"
_DEVICE_APPROVE_HOST = "auth.x.ai"
_DEVICE_APPROVE_PATH = "/oauth2/device/approve"
_DEVICE_APPROVE_URL = f"https://{_DEVICE_APPROVE_HOST}{_DEVICE_APPROVE_PATH}"
_ACCOUNT_NAVIGATION_HOSTS = {"accounts.x.ai", "auth.x.ai"}
_ALLOWED_NAVIGATION_HOSTS = {
    "accounts.x.ai",
    "auth.x.ai",
    "auth.grok.com",
    "auth.grokusercontent.com",
    "auth.grokipedia.com",
    "grok.com",
}
_MAX_DEVICE_LIFETIME_SECONDS = 1_800

ProgressCallback = Callable[[str, str], None]


class XaiDeviceOAuthProtocolError(RuntimeError):
    def __init__(self, message: str, *, stage: str, retryable: bool = False):
        super().__init__(message)
        self.stage = str(stage or "protocol")
        self.retryable = bool(retryable)


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _normalize_sso(value: object) -> str:
    token = _clean_text(value)
    if token.lower().startswith("cookie:"):
        token = token.split(":", 1)[1].strip()
    if token.startswith("sso="):
        token = token[4:].split(";", 1)[0].strip()
    if not token or any(char in token for char in (";", "\r", "\n")):
        return ""
    return token


def _safe_json(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:
        raise XaiDeviceOAuthProtocolError(
            "xAI authorization returned an invalid JSON response",
            stage="response",
        ) from exc
    if not isinstance(payload, dict):
        raise XaiDeviceOAuthProtocolError(
            "xAI authorization returned an invalid response object",
            stage="response",
        )
    return payload


def _validated_url(value: object, *, hosts: set[str], stage: str) -> str:
    url = _clean_text(value)
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in hosts:
        host = parsed.hostname or "missing-host"
        path = parsed.path or "/"
        raise XaiDeviceOAuthProtocolError(
            f"xAI returned an unexpected navigation URL: {host}{path[:200]}",
            stage=stage,
            retryable=True,
        )
    return url


def _is_device_done_url(value: object) -> bool:
    path = urlparse(_clean_text(value)).path.lower()
    return "/oauth2/device/done" in path or path.endswith("/device/done")


def _is_sign_in_url(value: object) -> bool:
    url = _clean_text(value).lower()
    return any(marker in url for marker in ("/sign-in", "/login", "signin", "login_required"))


def _is_device_authorized_body(value: object) -> bool:
    body = _clean_text(value).lower()
    return any(
        marker in body
        for marker in ("device authorized", "设备已授权", "you have authorized", "device is authorized")
    )


def _approval_completed(client: GrokProtocolClient, response: Any, *, referer: str) -> bool:
    location = _clean_text(getattr(response, "headers", {}).get("location"))
    if _is_sign_in_url(location):
        raise XaiDeviceOAuthProtocolError("xAI device approval returned to sign-in", stage="approve")
    if _is_device_done_url(location) or _is_device_authorized_body(getattr(response, "text", "")):
        return True
    if not location or getattr(response, "status_code", 0) not in {301, 302, 303, 307, 308}:
        return False

    next_url = _validated_url(
        urljoin(str(getattr(response, "url", "") or _DEVICE_APPROVE_URL), location),
        hosts=_ACCOUNT_NAVIGATION_HOSTS,
        stage="approve",
    )
    if _is_sign_in_url(next_url):
        raise XaiDeviceOAuthProtocolError("xAI device approval redirected to sign-in", stage="approve")
    follow = client._request(
        "GET",
        next_url,
        headers={"Accept": "text/html,application/xhtml+xml", "Referer": referer},
        allow_redirects=True,
    )
    final_url = _clean_text(getattr(follow, "url", "")) or next_url
    if _is_sign_in_url(final_url):
        raise XaiDeviceOAuthProtocolError("xAI device approval follow-up returned to sign-in", stage="approve")
    return _is_device_done_url(final_url) or _is_device_authorized_body(getattr(follow, "text", ""))


class _ConsentFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict[str, Any]] = []
        self._form: dict[str, Any] | None = None
        self._button: dict[str, Any] | None = None
        self._select: dict[str, Any] | None = None
        self._option: dict[str, Any] | None = None

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {str(key).lower(): str(value or "") for key, value in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = self._attrs(attrs)
        if tag == "form":
            self._form = {
                "action": values.get("action", ""),
                "method": values.get("method", "get").lower(),
                "controls": [],
            }
            self.forms.append(self._form)
            return
        if self._form is None:
            return
        if tag == "input":
            self._form["controls"].append(
                {
                    "tag": "input",
                    "name": values.get("name", ""),
                    "value": values.get("value", ""),
                    "type": values.get("type", "text").lower(),
                    "checked": "checked" in values,
                }
            )
        elif tag == "button":
            self._button = {
                "tag": "button",
                "name": values.get("name", ""),
                "value": values.get("value", ""),
                "type": values.get("type", "submit").lower(),
                "text": "",
            }
        elif tag == "select":
            self._select = {
                "tag": "select",
                "name": values.get("name", ""),
                "options": [],
            }
        elif tag == "option" and self._select is not None:
            self._option = {
                "value": values.get("value", ""),
                "selected": "selected" in values,
                "text": "",
            }

    def handle_data(self, data: str) -> None:
        if self._button is not None:
            self._button["text"] += data
        if self._option is not None:
            self._option["text"] += data

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "option" and self._select is not None and self._option is not None:
            self._select["options"].append(self._option)
            self._option = None
        elif tag == "select" and self._form is not None and self._select is not None:
            self._form["controls"].append(self._select)
            self._select = None
        elif tag == "button" and self._form is not None and self._button is not None:
            self._button["text"] = _clean_text(self._button.get("text"))
            self._form["controls"].append(self._button)
            self._button = None
        elif tag == "form":
            self._form = None


class _NextFlightScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self._script_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "script":
            self._script_parts = []

    def handle_data(self, data: str) -> None:
        if self._script_parts is not None:
            self._script_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._script_parts is not None:
            self.scripts.append("".join(self._script_parts))
            self._script_parts = None


def _next_flight_records(html: str) -> list[Any]:
    parser = _NextFlightScriptParser()
    parser.feed(str(html or ""))
    decoder = json.JSONDecoder()
    chunks: list[str] = []
    marker = "self.__next_f.push("
    for script in parser.scripts:
        cursor = 0
        while True:
            start = script.find(marker, cursor)
            if start < 0:
                break
            start += len(marker)
            try:
                value, end = decoder.raw_decode(script, start)
            except (TypeError, ValueError):
                cursor = start
                continue
            cursor = end
            if isinstance(value, list) and len(value) >= 2 and value[0] == 1 and isinstance(value[1], str):
                chunks.append(value[1])

    records: list[Any] = []
    for line in "".join(chunks).splitlines():
        _, separator, encoded = line.partition(":")
        if not separator or not encoded:
            continue
        try:
            record, _ = decoder.raw_decode(encoded)
        except (TypeError, ValueError):
            continue
        records.append(record)
    return records


def _walk_json(value: Any):
    pending = [value]
    while pending:
        current = pending.pop()
        yield current
        if isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)


def _cookie_setter_url(payload: object) -> str:
    """Read createSession's cookie URL across old and nested RPC envelopes."""
    for node in _walk_json(payload):
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            if str(key).replace("_", "").lower() != "cookiesetterurl":
                continue
            url = _clean_text(value)
            if url:
                return url
    return ""


def _rpc_error_text(payload: object) -> str:
    for node in _walk_json(payload):
        if not isinstance(node, dict):
            continue
        error = node.get("error")
        if isinstance(error, str) and _clean_text(error):
            return _clean_text(error)[:300]
        if isinstance(error, dict):
            message = _clean_text(error.get("message") or error.get("detail") or error.get("description"))
            code = _clean_text(error.get("code") or error.get("type"))
            if message or code:
                return f"{code}: {message}".strip(": ")[:300]
        message = _clean_text(node.get("errorMessage") or node.get("error_message"))
        if message:
            return message[:300]
    return ""


def _flight_session_user_id(html: str) -> str:
    for record in _next_flight_records(html):
        for node in _walk_json(record):
            if not isinstance(node, dict):
                continue
            dehydrated = node.get("dehydratedState")
            if not isinstance(dehydrated, dict):
                continue
            queries = dehydrated.get("queries")
            if not isinstance(queries, list):
                continue
            for query in queries:
                if not isinstance(query, dict) or query.get("queryKey") != ["session"]:
                    continue
                state = query.get("state")
                data = state.get("data") if isinstance(state, dict) else None
                user = data.get("user") if isinstance(data, dict) else None
                user_id = _clean_text(user.get("userId")) if isinstance(user, dict) else ""
                if user_id:
                    return user_id
    return ""


def parse_device_consent_form(html: str, *, base_url: str, user_code: str) -> tuple[str, dict[str, str]]:
    parser = _ConsentFormParser()
    parser.feed(str(html or ""))
    for form in parser.forms:
        action = urljoin(base_url, _clean_text(form.get("action")))
        parsed = urlparse(action)
        if parsed.scheme != "https" or parsed.hostname != _DEVICE_APPROVE_HOST or parsed.path != _DEVICE_APPROVE_PATH:
            continue
        if _clean_text(form.get("method")).lower() != "post":
            continue

        payload: dict[str, str] = {}
        allow_control: dict[str, Any] | None = None
        for control in form.get("controls") or []:
            if not isinstance(control, dict):
                continue
            name = _clean_text(control.get("name"))
            tag = _clean_text(control.get("tag")).lower()
            if tag == "input" and name:
                input_type = _clean_text(control.get("type")).lower()
                if input_type in {"checkbox", "radio"} and not bool(control.get("checked")):
                    continue
                if input_type not in {"submit", "button", "reset"}:
                    value = _clean_text(control.get("value"))
                    if name not in payload or value:
                        payload[name] = value
            elif tag == "select" and name:
                options = [option for option in (control.get("options") or []) if isinstance(option, dict)]
                selected = next((option for option in options if option.get("selected")), None)
                selected = selected or next((option for option in options if _clean_text(option.get("value"))), None)
                if selected is not None:
                    payload[name] = _clean_text(selected.get("value") or selected.get("text"))
            elif tag == "button":
                value = _clean_text(control.get("value")).lower()
                text = _clean_text(control.get("text")).lower()
                if value in {"allow", "approve", "approved", "accept"} or text in {"allow", "approve", "accept"}:
                    allow_control = control

        if _clean_text(payload.get("user_code")) != _clean_text(user_code):
            raise XaiDeviceOAuthProtocolError("Device consent form returned the wrong user code", stage="consent")
        if allow_control is None:
            raise XaiDeviceOAuthProtocolError("Device consent form has no allow action", stage="consent")
        action_name = _clean_text(allow_control.get("name")) or "action"
        action_value = _clean_text(allow_control.get("value")) or "allow"
        payload[action_name] = action_value
        if _clean_text(payload.get("principal_type")).lower() == "user" and not _clean_text(payload.get("principal_id")):
            payload["principal_id"] = _flight_session_user_id(html)
        if not _clean_text(payload.get("principal_type")) or not _clean_text(payload.get("principal_id")):
            raise XaiDeviceOAuthProtocolError("Device consent form did not select an OAuth principal", stage="consent")
        return action, payload
    # The session exchange can briefly land on an intermediate accounts page
    # before the approval form is available.  Let the authorization queue run
    # its bounded consent-stage retry instead of treating that as permanent.
    raise XaiDeviceOAuthProtocolError(
        "xAI device consent form was not found",
        stage="consent",
        retryable=True,
    )


class XaiDeviceOAuthProtocol:
    def __init__(self, grok_config: dict[str, Any], *, proxy: str = "", progress: ProgressCallback | None = None):
        self.config = dict(grok_config or {})
        self.proxy = _clean_text(proxy) or "direct"
        self.progress = progress

    def _emit(self, stage: str, message: str) -> None:
        if self.progress is not None:
            self.progress(str(stage), str(message))

    def _turnstile_solver_config(self) -> dict[str, Any]:
        config = dict(self.config)
        if self.proxy and self.proxy.lower() != "direct":
            config["proxy"] = self.proxy
        return config

    @staticmethod
    def _set_sso_cookie(client: GrokProtocolClient, sso: str) -> None:
        client.session.cookies.set("sso", sso, domain=".x.ai", path="/", secure=True)

    @staticmethod
    def _clear_sso_cookie(client: GrokProtocolClient) -> None:
        client.session.cookies.delete("sso", domain=".x.ai", path="/")

    @staticmethod
    def _verify_device_code(client: GrokProtocolClient, user_code: str, *, stage: str) -> str:
        response = client._request(
            "POST",
            _DEVICE_VERIFY_URL,
            data={"user_code": user_code},
            headers={"Accept": "text/html,application/xhtml+xml", "Content-Type": "application/x-www-form-urlencoded"},
            allow_redirects=False,
        )
        location = _clean_text(response.headers.get("location"))
        if not location:
            raise XaiDeviceOAuthProtocolError(
                "xAI device verification did not return a navigation target",
                stage=stage,
                retryable=True,
            )
        return _validated_url(
            urljoin(str(response.url), location),
            hosts=_ACCOUNT_NAVIGATION_HOSTS,
            stage=stage,
        )

    def authorize(
        self,
        *,
        email: str,
        password: str,
        sso: str = "",
        sso_only: bool = False,
    ) -> dict[str, Any]:
        clean_email = _clean_text(email)
        clean_password = _clean_text(password)
        clean_sso = _normalize_sso(sso)
        if (sso_only or not clean_sso) and (not clean_email or not clean_password):
            raise XaiDeviceOAuthProtocolError("Saved Grok account is missing email or password", stage="account")

        client = GrokProtocolClient(self.config, proxy=self.proxy)
        solver: TurnstileSolver | None = None
        try:
            self._emit("device_code", "创建 xAI Device Code")
            start = client._request(
                "POST",
                XAI_DEVICE_CODE_URL,
                data={"client_id": XAI_OAUTH_CLIENT_ID, "scope": XAI_OAUTH_SCOPE},
                headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            )
            if start.status_code != 200:
                raise XaiDeviceOAuthProtocolError("Unable to start xAI device authorization", stage="device_code", retryable=True)
            device = _safe_json(start)
            device_code = _clean_text(device.get("device_code"))
            user_code = _clean_text(device.get("user_code"))
            if not device_code or not user_code:
                raise XaiDeviceOAuthProtocolError("xAI Device Code response is incomplete", stage="device_code")
            expires_in = max(30, min(int(device.get("expires_in") or _MAX_DEVICE_LIFETIME_SECONDS), _MAX_DEVICE_LIFETIME_SECONDS))
            interval = max(1, min(int(device.get("interval") or 5), 30))

            session_sso = clean_sso
            consent: Any | None = None
            approve_url = ""
            approve_form: dict[str, str] = {}
            device_done = False

            if clean_sso and not sso_only:
                self._emit("sso", "使用已有 Grok SSO 建立 Device Code 授权上下文")
                self._set_sso_cookie(client, clean_sso)
                try:
                    consent_url = self._verify_device_code(client, user_code, stage="sso")
                    device_done = urlparse(consent_url).path.startswith("/oauth2/device/done")
                    if not device_done:
                        direct_consent = client._request(
                            "GET",
                            consent_url,
                            headers={"Accept": "text/html,application/xhtml+xml"},
                            allow_redirects=True,
                        )
                        if direct_consent.status_code != 200:
                            raise XaiDeviceOAuthProtocolError(
                                "Unable to load xAI device consent with saved SSO",
                                stage="sso",
                                retryable=True,
                            )
                        final_url = _validated_url(
                            str(direct_consent.url),
                            hosts=_ACCOUNT_NAVIGATION_HOSTS,
                            stage="sso",
                        )
                        device_done = urlparse(final_url).path.startswith("/oauth2/device/done")
                        if not device_done:
                            approve_url, approve_form = parse_device_consent_form(
                                str(direct_consent.text or ""),
                                base_url=final_url,
                                user_code=user_code,
                            )
                            consent = direct_consent
                    self._emit("sso", "已有 Grok SSO 可用，跳过邮箱密码登录")
                except XaiDeviceOAuthProtocolError as sso_error:
                    self._clear_sso_cookie(client)
                    session_sso = ""
                    consent = None
                    approve_url = ""
                    approve_form = {}
                    device_done = False
                    if not clean_email or not clean_password:
                        raise XaiDeviceOAuthProtocolError(
                            "Saved Grok SSO is unavailable and no login password is available",
                            stage="sso",
                        ) from sso_error
                    self._emit("signin", "Grok SSO 已失效，回退邮箱密码登录")

            if consent is None and not device_done:
                self._emit("bootstrap", "发现当前 Castle SDK 和登录参数")
                metadata = client.bootstrap()
                self._emit("signin", "建立 xAI 账号登录上下文")
                sign_in_url = self._verify_device_code(client, user_code, stage="signin")
                sign_in = client._request(
                    "GET",
                    sign_in_url,
                    headers={"Accept": "text/html,application/xhtml+xml"},
                    allow_redirects=True,
                )
                if sign_in.status_code != 200:
                    raise XaiDeviceOAuthProtocolError("Unable to load xAI sign-in page", stage="signin", retryable=True)
                sign_in_url = _validated_url(str(sign_in.url), hosts=_ACCOUNT_NAVIGATION_HOSTS, stage="signin")
                sitekey = extract_turnstile_sitekey(str(sign_in.text or "")) or _clean_text(metadata.sitekey)
                if not sitekey:
                    raise XaiDeviceOAuthProtocolError("xAI sign-in page did not expose a Turnstile sitekey", stage="signin")

                self._emit("castle", "生成登录 Castle token")
                castle_token = client.create_castle_token(page_url=sign_in_url)
                self._emit("turnstile", "求解登录 Turnstile")
                solver = TurnstileSolver(self._turnstile_solver_config())
                turnstile_token = solver.solve(website_url=sign_in_url, sitekey=sitekey)

                self._emit("session", "提交 xAI 账号登录")
                rpc = client._request(
                    "POST",
                    urljoin(f"{client.base_url}/", "api/rpc"),
                    json={
                        "rpc": "createSession",
                        "req": {
                            "createSessionRequest": {
                                "credentials": {
                                    "case": "emailAndPassword",
                                    "value": {"email": clean_email, "clearTextPassword": clean_password},
                                }
                            },
                            "turnstileToken": turnstile_token,
                            "castleRequestToken": castle_token,
                        },
                    },
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "Origin": client.base_url,
                        "Referer": sign_in_url,
                    },
                    allow_redirects=False,
                )
                rpc_payload = _safe_json(rpc)
                setter_value = _cookie_setter_url(rpc_payload)
                if not setter_value:
                    rpc_error = _rpc_error_text(rpc_payload)
                    if rpc_error:
                        normalized_error = rpc_error.lower()
                        if "invalid-credentials" in normalized_error or "email or password" in normalized_error:
                            rpc_error = "保存的邮箱或密码不正确"
                        raise XaiDeviceOAuthProtocolError(
                            f"xAI 账号登录失败：{rpc_error}",
                            stage="session",
                        )
                if setter_value:
                    setter_url = _validated_url(
                        urljoin(f"{client.base_url}/", setter_value),
                        hosts=_ALLOWED_NAVIGATION_HOSTS,
                        stage="session",
                    )
                    session = client._request(
                        "GET",
                        setter_url,
                        headers={"Accept": "text/html,application/xhtml+xml", "Referer": sign_in_url},
                        allow_redirects=True,
                    )
                    if session.status_code >= 400:
                        raise XaiDeviceOAuthProtocolError(
                            "xAI session cookie exchange failed",
                            stage="session",
                            retryable=True,
                        )

                session_sso = client._cookie_value_for_domain("grok.com", "sso", "sso-rw")
                if sso_only:
                    if not session_sso:
                        raise XaiDeviceOAuthProtocolError(
                            "xAI password login completed without a Grok SSO session",
                            stage="session",
                            retryable=True,
                        )
                    self._emit("completed", "Grok SSO 登录态已恢复")
                    return {"sso": session_sso}

                self._emit("consent", "读取 Device Code 授权主体")
                consent_url = self._verify_device_code(client, user_code, stage="consent")
                consent = client._request(
                    "GET",
                    consent_url,
                    headers={"Accept": "text/html,application/xhtml+xml"},
                    allow_redirects=True,
                )
                if consent.status_code != 200:
                    raise XaiDeviceOAuthProtocolError("Unable to load xAI device consent", stage="consent", retryable=True)
                approve_url, approve_form = parse_device_consent_form(
                    str(consent.text or ""),
                    base_url=str(consent.url),
                    user_code=user_code,
                )

            if not device_done:
                self._emit("approve", "提交 Device Code Allow")
                minimal_form = {
                    "user_code": user_code,
                    "action": "allow",
                    "principal_type": _clean_text(approve_form.get("principal_type")) or "User",
                    "principal_id": _clean_text(approve_form.get("principal_id")),
                }
                approval_forms = [approve_form]
                if minimal_form != approve_form:
                    approval_forms.append(minimal_form)
                approved = False
                for approval_index, approval_form in enumerate(approval_forms):
                    approve = client._request(
                        "POST",
                        approve_url if approval_index == 0 else _DEVICE_APPROVE_URL,
                        data=approval_form,
                        headers={
                            "Accept": "text/html,application/xhtml+xml",
                            "Content-Type": "application/x-www-form-urlencoded",
                            "Origin": "https://accounts.x.ai",
                            "Referer": str(consent.url),
                        },
                        allow_redirects=False,
                    )
                    if approve.status_code < 200 or approve.status_code >= 400:
                        if approval_index + 1 < len(approval_forms):
                            continue
                        raise XaiDeviceOAuthProtocolError("xAI rejected the device approval", stage="approve")
                    if _approval_completed(client, approve, referer=str(consent.url)):
                        approved = True
                        break
                if not approved:
                    raise XaiDeviceOAuthProtocolError(
                        "xAI device approval did not reach the authorized state",
                        stage="approve",
                        retryable=True,
                    )

            self._emit("token", "轮询 xAI OAuth token")
            deadline = time.monotonic() + expires_in
            while time.monotonic() < deadline:
                token = client._request(
                    "POST",
                    XAI_TOKEN_URL,
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "device_code": device_code,
                        "client_id": XAI_OAUTH_CLIENT_ID,
                    },
                    headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
                    allow_redirects=False,
                )
                token_payload = _safe_json(token)
                if token.status_code == 200 and _clean_text(token_payload.get("access_token")):
                    session_sso = session_sso or client._cookie_value_for_domain("grok.com", "sso", "sso-rw")
                    if session_sso:
                        token_payload["sso"] = session_sso
                    self._emit("completed", "Device Code OAuth 已授权")
                    return token_payload
                error = _clean_text(token_payload.get("error"))
                if error == "slow_down":
                    interval = min(interval + 5, 30)
                elif error not in {"authorization_pending", "slow_down"}:
                    error_description = _clean_text(token_payload.get("error_description"))
                    detail = f" ({error_description})" if error_description else ""
                    raise XaiDeviceOAuthProtocolError(
                        f"xAI Device Code token exchange failed: {error or 'unexpected_response'}{detail}",
                        stage="token",
                        # Fresh accounts can transiently reject the first
                        # one-time device grant after consent. The protocol
                        # queue is bounded to one retry with a fresh code.
                        retryable=error == "invalid_grant",
                    )
                time.sleep(interval)
            raise XaiDeviceOAuthProtocolError("xAI Device Code authorization expired", stage="token", retryable=True)
        except XaiDeviceOAuthProtocolError:
            raise
        except GrokProtocolError as exc:
            raise XaiDeviceOAuthProtocolError(
                str(exc),
                stage=_clean_text(getattr(exc, "stage", "protocol")) or "protocol",
                retryable=bool(getattr(exc, "retryable", False)),
            ) from exc
        finally:
            if solver is not None:
                solver.close()
            client.close()


__all__ = [
    "XaiDeviceOAuthProtocol",
    "XaiDeviceOAuthProtocolError",
    "parse_device_consent_form",
]
