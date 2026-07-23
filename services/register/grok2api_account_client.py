from __future__ import annotations

from typing import Any

from curl_cffi.requests import Session


class Grok2APIAccountError(RuntimeError):
    pass


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _bool_value(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    raw = _clean_text(value).lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def normalize_sso(value: object) -> str:
    token = _clean_text(value)
    if not token:
        raise ValueError("SSO token is required")
    lowered = token.lower()
    if ";" in token or lowered.startswith("cookie:") or "sso-rw=" in lowered:
        raise ValueError("只接受裸 SSO，不能传入完整 Cookie 串")
    if lowered.startswith("sso="):
        token = token[4:].strip()
    if not token or any(char.isspace() for char in token):
        raise ValueError("SSO token is invalid")
    return token


def _normalized_tokens(values: list[str]) -> list[str]:
    return list(dict.fromkeys(normalize_sso(value) for value in values))


class Grok2APIAccountClient:
    def __init__(self, grok_config: dict[str, Any] | None = None, *, session: Session | None = None):
        source = grok_config if isinstance(grok_config, dict) else {}
        nested = source.get("grok2api") if isinstance(source.get("grok2api"), dict) else {}

        def config_value(name: str, default: object = "") -> object:
            flat_key = f"grok2api_{name}"
            return source[flat_key] if flat_key in source else nested.get(name, default)

        self.enabled = _bool_value(config_value("enabled", False), False)
        configured_base = _clean_text(config_value("api_base")).rstrip("/")
        # Production calls do not inject a Session and always use the embedded
        # runtime. An explicit Session keeps the legacy HTTP path testable.
        self.embedded = session is None
        self.api_base = (
            configured_base
            if configured_base.lower().endswith("/admin/api")
            else f"{configured_base}/admin/api" if configured_base else ""
        )
        self.admin_key = _clean_text(config_value("admin_key"))
        requested_pool = _clean_text(config_value("pool", "auto")).lower() or "auto"
        self.pool = requested_pool if requested_pool in {"auto", "basic", "super", "heavy"} else "auto"
        self.auto_nsfw = _bool_value(config_value("auto_nsfw", False), False)
        self.verify_on_import = _bool_value(config_value("verify_on_import", True), True)
        try:
            self.timeout = max(1, min(300, int(config_value("timeout", 30) or 30)))
        except (TypeError, ValueError):
            self.timeout = 30
        self._session = session or Session(trust_env=False)

    def readiness(self) -> tuple[bool, str]:
        """Return configuration/runtime readiness without making a remote call."""
        if not self.enabled:
            return False, "Grok runtime is disabled"
        if self.embedded:
            from services.grok_runtime import grok_runtime

            if not grok_runtime.available:
                return False, "内置 Grok 运行时尚未启动"
            return True, ""
        if not self.api_base:
            return False, "Grok2API api_base is required"
        if not self.admin_key:
            return False, "Grok2API admin_key is required"
        return True, ""

    def _require_ready(self) -> None:
        ready, error = self.readiness()
        if not ready:
            raise Grok2APIAccountError(error)

    @staticmethod
    def _payload_secrets(payload: object) -> list[str]:
        if isinstance(payload, dict):
            values: list[str] = []
            for key, value in payload.items():
                if str(key).lower() in {"token", "tokens", "old_token"}:
                    if isinstance(value, list):
                        values.extend(_clean_text(item) for item in value if _clean_text(item))
                    elif _clean_text(value):
                        values.append(_clean_text(value))
            return values
        if isinstance(payload, list):
            return [_clean_text(item) for item in payload if _clean_text(item)]
        return []

    def _safe_error(self, error: object, payload: object = None) -> str:
        text = _clean_text(error) or type(error).__name__
        for secret in [self.admin_key, *self._payload_secrets(payload)]:
            if secret:
                text = text.replace(secret, "***")
        return text[:300]

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: object = None,
    ) -> dict[str, Any]:
        self._require_ready()
        url = f"{self.api_base}{path}"
        headers = {
            "Authorization": f"Bearer {self.admin_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            response = self._session.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=self.timeout,
            )
        except Exception as exc:
            raise Grok2APIAccountError(
                f"Grok2API request failed: {self._safe_error(exc, json_body)}"
            ) from exc

        status_code = int(getattr(response, "status_code", 0) or 0)
        try:
            payload = response.json()
        except Exception:
            payload = None
        if status_code < 200 or status_code >= 300:
            detail = payload if payload is not None else getattr(response, "text", "")
            raise Grok2APIAccountError(
                f"Grok2API HTTP {status_code}: {self._safe_error(detail, json_body)}"
            )
        if not isinstance(payload, dict):
            raise Grok2APIAccountError("Grok2API returned a non-object response")
        return payload

    def list(self, *, cancel_event: Any | None = None) -> dict[str, Any]:
        if cancel_event is not None and cancel_event.is_set():
            raise Grok2APIAccountError("Grok 账号列表读取已取消")
        if self.embedded:
            self._require_ready()
            from services.grok_runtime import grok_runtime

            return grok_runtime.run_sync(
                grok_runtime.list_accounts,
                timeout=self.timeout,
                cancel_event=cancel_event,
            )
        result = self._request("GET", "/tokens")
        if cancel_event is not None and cancel_event.is_set():
            raise Grok2APIAccountError("Grok 账号列表读取已取消")
        return result

    def add(
        self,
        tokens: list[str],
        *,
        pool: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized = _normalized_tokens(tokens)
        normalized_tags = list(
            dict.fromkeys(_clean_text(value) for value in (tags or []) if _clean_text(value))
        )
        selected_pool = _clean_text(pool or self.pool).lower() or "auto"
        if self.embedded:
            self._require_ready()
            from services.grok_runtime import grok_runtime

            return grok_runtime.run_sync(
                lambda: grok_runtime.add_accounts(
                    normalized,
                    pool=selected_pool,
                    tags=normalized_tags,
                    auto_nsfw=self.auto_nsfw,
                ),
                timeout=self.timeout,
            )
        return self._request(
            "POST",
            "/tokens/add",
            params={"auto_nsfw": "true" if self.auto_nsfw else "false"},
            json_body={
                "tokens": normalized,
                "pool": selected_pool,
                "tags": normalized_tags,
            },
        )

    def refresh(self, tokens: list[str]) -> dict[str, Any]:
        normalized = _normalized_tokens(tokens)
        if self.embedded:
            self._require_ready()
            from services.grok_runtime import grok_runtime

            batches = max(1, (len(normalized) + 14) // 15)
            timeout = min(300, max(self.timeout, 30 * batches))
            return grok_runtime.run_sync(
                lambda: grok_runtime.refresh_accounts(normalized),
                timeout=timeout,
            )
        return self._request(
            "POST",
            "/batch/refresh",
            json_body={"tokens": normalized},
        )

    def verify(
        self,
        tokens: list[str],
        *,
        cancel_event: Any | None = None,
    ) -> dict[str, Any]:
        """Probe each token once through the read-only fast quota endpoint."""
        normalized = _normalized_tokens(tokens)
        if cancel_event is not None and cancel_event.is_set():
            raise Grok2APIAccountError("Grok 账号验证已取消")
        if self.embedded:
            self._require_ready()
            from services.grok_runtime import grok_runtime

            # Each embedded probe has its own 25s upstream timeout and runs
            # in bounded batches of eight.  Keep a multi-account admin check
            # from timing out merely because a later batch has not started.
            batches = max(1, (len(normalized) + 7) // 8)
            timeout = min(300, max(self.timeout, 30 * batches))
            return grok_runtime.run_sync(
                lambda: grok_runtime.verify_accounts(normalized),
                timeout=timeout,
                cancel_event=cancel_event,
            )
        result = self._request(
            "POST",
            "/tokens/verify",
            json_body={"tokens": normalized},
        )
        if cancel_event is not None and cancel_event.is_set():
            raise Grok2APIAccountError("Grok 账号验证已取消")
        return result

    def chat_test(
        self,
        token: str,
        *,
        prompt: str,
        model: str,
        timeout_s: float = 45.0,
    ) -> dict[str, Any]:
        """Run one Console chat against exactly one supplied SSO token.

        Unlike the account-management endpoints this has no remote-admin API
        equivalent: it must execute inside the embedded runtime so the direct
        console protocol can be used without selecting a different account.
        """
        normalized = normalize_sso(token)
        self._require_ready()
        if not self.embedded:
            raise Grok2APIAccountError("账号对话测试仅支持内置 Grok 运行时")

        from services.grok_runtime import grok_runtime

        upstream_timeout = max(5.0, min(45.0, float(timeout_s)))
        return grok_runtime.run_sync(
            lambda: grok_runtime.chat_test(
                normalized,
                prompt=prompt,
                model=model,
                timeout_s=upstream_timeout,
            ),
            timeout=max(self.timeout, upstream_timeout + 5.0),
        )

    def set_disabled(self, tokens: list[str], disabled: bool) -> dict[str, Any]:
        normalized = _normalized_tokens(tokens)
        if self.embedded:
            self._require_ready()
            from services.grok_runtime import grok_runtime

            return grok_runtime.run_sync(
                lambda: grok_runtime.set_accounts_disabled(normalized, bool(disabled)),
                timeout=self.timeout,
            )
        return self._request(
            "POST",
            "/tokens/disabled/batch",
            json_body={"tokens": normalized, "disabled": bool(disabled)},
        )

    def delete(self, tokens: list[str]) -> dict[str, Any]:
        normalized = _normalized_tokens(tokens)
        if self.embedded:
            self._require_ready()
            from services.grok_runtime import grok_runtime

            return grok_runtime.run_sync(
                lambda: grok_runtime.delete_accounts(normalized),
                timeout=self.timeout,
            )
        return self._request(
            "DELETE",
            "/tokens",
            json_body=normalized,
        )


__all__ = ["Grok2APIAccountClient", "Grok2APIAccountError", "normalize_sso"]
