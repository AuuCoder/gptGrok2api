from __future__ import annotations

import copy
import random
import secrets
import string
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from services.register import mail_provider
from services.register.grok_protocol import GrokProtocolClient, GrokProtocolError
from services.proxy_service import proxy_settings
from utils.timezone import TIME_FORMAT, beijing_now_str


config: dict[str, Any] = {
    "mail": {
        "request_timeout": 30,
        "wait_timeout": 180,
        "wait_interval": 2,
        "api_use_register_proxy": True,
        "providers": [],
    },
    "proxy": "",
    "total": 1,
    "threads": 1,
    "target": "grok",
    "grok": {
        "max_mail_retries": 3,
        "provider": "yescaptcha",
        "api_key": "",
        "api_base": "",
        "action": "",
        "sitekey": "",
        "action_id": "",
        "base_url": "https://accounts.x.ai",
        "request_timeout": 30,
        "captcha_timeout": 180,
        "captcha_poll_interval": 3,
        "castle_timeout": 20,
        "castle_pk": "",
        "castle_sdk_url": "",
        "next_router_state_tree": "",
        "create_path": "/createTask",
        "result_path": "/getTaskResult",
        "custom_headers": {},
    },
}

register_log_sink = None
account_result_sink: Callable[[dict[str, Any]], None] | None = None
_print_lock = threading.Lock()


def log(text: str, color: str = "") -> None:
    if register_log_sink:
        try:
            register_log_sink(str(text), str(color or ""))
        except Exception:
            pass
    colors = {"red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m"}
    with _print_lock:
        prefix = colors.get(color, "")
        suffix = "\033[0m" if prefix else ""
        print(f"{prefix}{beijing_now_str(TIME_FORMAT)} {text}{suffix}")


def step(index: int, text: str, color: str = "") -> None:
    log(f"[任务{index}] {text}", color)


def _persist_account_snapshot(payload: dict[str, Any]) -> bool:
    if not account_result_sink:
        return False
    try:
        account_result_sink(copy.deepcopy(payload))
        return True
    except Exception as exc:
        log(f"Grok 账号凭据暂存失败: {type(exc).__name__}: {exc}", "red")
        return False


def _truthy(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    lowered = str(value or "").strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return default


def _mail_config(register_proxy: str) -> dict[str, Any]:
    mail = copy.deepcopy(config.get("mail") if isinstance(config.get("mail"), dict) else {})
    use_register_proxy = _truthy(mail.get("api_use_register_proxy"), True)
    mail["api_use_register_proxy"] = use_register_proxy
    mail["proxy"] = str(register_proxy or "").strip() if use_register_proxy else ""
    providers = mail.get("providers") if isinstance(mail.get("providers"), list) else []
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        if str(provider.get("type") or "") in {"icloud_api", "icloud_local"}:
            provider["project"] = "grok"
        provider["keyword"] = "xAI"
    return mail


def _resolve_register_proxy(raw_proxy: str) -> tuple[str, str]:
    profile = proxy_settings.get_profile(proxy=str(raw_proxy or "").strip(), upstream=True)
    return profile.proxy_url or "direct", profile.proxy_source


def _random_password(length: int = 18) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    chars = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%"),
        *[secrets.choice(alphabet) for _ in range(max(0, length - 4))],
    ]
    random.SystemRandom().shuffle(chars)
    return "".join(chars)


def _random_name() -> tuple[str, str]:
    first_names = ("James", "Robert", "John", "Michael", "David", "Emma", "Olivia", "Sophia")
    last_names = ("Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis")
    return secrets.choice(first_names), secrets.choice(last_names)


def _max_mail_retries(grok_config: dict[str, Any]) -> int:
    value = grok_config.get("max_mail_retries", grok_config.get("max_mail_retry", 3))
    try:
        return max(1, min(20, int(value or 3)))
    except (TypeError, ValueError):
        return 3


def _mail_retryable(error: Exception) -> bool:
    if isinstance(error, GrokProtocolError):
        return error.mail_retryable
    lowered = str(error or "").lower()
    return any(
        marker in lowered
        for marker in (
            "等待 grok 验证码超时",
            "邮箱域名",
            "邮箱已存在",
            "email domain",
            "disposable email",
            "email_in_use",
        )
    )


def _register_once(
    index: int,
    client: GrokProtocolClient,
    mail_config: dict[str, Any],
    attempt: int,
    max_attempts: int,
) -> dict[str, Any]:
    suffix = f"（邮箱尝试 {attempt}/{max_attempts}）" if max_attempts > 1 else ""
    step(index, f"开始创建邮箱{suffix}")
    mailbox = mail_provider.create_mailbox(mail_config)
    email = str(mailbox.get("address") or "").strip()
    if not email:
        mail_provider.release_mailbox(mailbox)
        raise GrokProtocolError("邮箱服务未返回 address", stage="mail", mail_retryable=True)
    label = str(mailbox.get("label") or mailbox.get("provider") or "")
    step(index, f"邮箱创建完成[{label}]: {email}")

    mailbox_finalized = False
    draft: dict[str, Any] | None = None
    try:
        mailbox["_received_after"] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        step(index, "正在初始化 Grok 注册协议")
        client.bootstrap()
        step(index, "正在发送 Grok 验证码")
        client.send_email_validation_code(email)
        step(index, "开始等待 Grok 验证码")
        code = mail_provider.wait_for_code(mail_config, mailbox)
        if not code:
            raise GrokProtocolError("等待 Grok 验证码超时", stage="mail", mail_retryable=True)
        code_text = str(code)
        step(index, f"已收到 Grok 验证码: {code_text}")
        normalized_code = code_text.strip()
        step(index, "正在校验 Grok 验证码")
        client.verify_email_validation_code(email, normalized_code)
        step(index, "Grok 验证码校验通过")

        given_name, family_name = _random_name()
        password = _random_password()
        step(index, "正在求解 Turnstile")
        turnstile_token = client.solve_turnstile()
        draft = {
            "email": email,
            "password": password,
            "sso": "",
            "profile": {
                "given_name": given_name,
                "family_name": family_name,
                "session_state": "submitting",
            },
            "source_type": "protocol",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "submitting",
        }
        _persist_account_snapshot(draft)
        step(index, "正在提交 Grok 注册")
        try:
            account = client.create_user_and_session(
                email=email,
                code=normalized_code,
                given_name=given_name,
                family_name=family_name,
                password=password,
                turnstile_token=turnstile_token,
            )
        except Exception as error:
            partial = getattr(error, "partial_result", None)
            if isinstance(partial, dict) and partial:
                persisted = _persist_account_snapshot(partial)
                try:
                    error.partial_persisted = persisted
                except Exception:
                    pass
            elif draft is not None:
                uncertain = copy.deepcopy(draft)
                reason = str(
                    getattr(error, "reason_code", "") or getattr(error, "stage", "") or type(error).__name__
                )
                pre_submit_failure = reason.startswith("prewarm_")
                uncertain["status"] = "submission_failed" if pre_submit_failure else "submission_unknown"
                uncertain["profile"]["session_state"] = "not_submitted" if pre_submit_failure else "unknown"
                uncertain["profile"]["session_reason"] = reason
                _persist_account_snapshot(uncertain)
            if bool(getattr(error, "account_created", False)):
                mail_provider.mark_mailbox_result(mailbox, success=True)
                mailbox_finalized = True
            raise
        sso = str(account.get("sso") or "").strip()
        if not sso:
            raise GrokProtocolError("Grok 注册结果缺少 sso", stage="create_account")
        mail_provider.mark_mailbox_result(mailbox, success=True)
        mailbox_finalized = True
        return {
            "email": email,
            "password": password,
            "sso": sso,
            "profile": {
                "given_name": given_name,
                "family_name": family_name,
                "redirect_url": str(account.get("redirect_url") or ""),
            },
            "source_type": "protocol",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "active",
        }
    except Exception as error:
        if not mailbox_finalized:
            mail_provider.mark_mailbox_result(mailbox, success=False, error=error)
        raise


def worker(index: int) -> dict[str, Any]:
    started = time.monotonic()
    proxy, proxy_source = _resolve_register_proxy(str(config.get("proxy") or "").strip())
    grok_config = copy.deepcopy(config.get("grok") if isinstance(config.get("grok"), dict) else {})
    mail_config = _mail_config(proxy)
    max_attempts = _max_mail_retries(grok_config)
    client = GrokProtocolClient(grok_config, proxy=proxy, log=lambda message: step(index, message))
    last_error: Exception | None = None
    try:
        step(index, "任务启动")
        step(index, f"注册网络出口: {proxy_source}")
        for attempt in range(1, max_attempts + 1):
            try:
                result = _register_once(index, client, mail_config, attempt, max_attempts)
                elapsed = time.monotonic() - started
                log(f"{result['email']} Grok 注册成功，本次耗时{elapsed:.1f}s", "green")
                return {"ok": True, "index": index, "result": result}
            except Exception as error:
                last_error = error
                if attempt >= max_attempts or not _mail_retryable(error):
                    raise
                step(index, f"当前邮箱不可用，准备更换邮箱: {error}", "yellow")
        raise last_error or RuntimeError("Grok 注册失败")
    except Exception as error:
        elapsed = time.monotonic() - started
        log(f"任务{index} Grok 注册失败，本次耗时{elapsed:.1f}s，原因: {error}", "red")
        result = {"ok": False, "index": index, "error": str(error)}
        partial = getattr(error, "partial_result", None)
        if isinstance(partial, dict) and partial:
            result["account"] = partial
            result["account_persisted"] = bool(getattr(error, "partial_persisted", False))
        return result
    finally:
        client.close()


__all__ = ["account_result_sink", "config", "log", "register_log_sink", "step", "worker"]
